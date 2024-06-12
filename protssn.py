import os
import torch
import math
import yaml
import warnings
import argparse
import numpy as np
import pandas as pd
import torch.nn.functional as F
import scipy.spatial as spa
from tqdm import tqdm
from torch_geometric.data import Data
from scipy.special import softmax
from Bio.PDB import PDBParser, ShrakeRupley
from Bio.PDB.PDBExceptions import PDBConstructionWarning
from typing import Callable, List, Optional
from src.utils.dataset_utils import safe_index, one_hot_res, log, dihedral
from src.models import PLM_model, GNN_model
from src.utils.dataset_utils import NormalizeProtein
warnings.filterwarnings("ignore")

class ProtSSN:
    def __init__(self, 
                 num_residue_type: int = 20, micro_radius: int = 20, cutoff: int = 30, 
                 c_alpha_max_neighbors: int = 10, seq_dist_cut: int = 64, 
                 pre_transform: Optional[Callable] = None, 
                 plm_model: Optional[Callable] = None, gnn_model: Optional[Callable] = None):
        self.num_residue_type = num_residue_type
        self.micro_radius = micro_radius
        self.cutoff = cutoff
        self.c_alpha_max_neighbors = c_alpha_max_neighbors
        self.seq_dist_cut = seq_dist_cut
        self.pre_transform = pre_transform
        
        self.sr = ShrakeRupley(probe_radius=1.4, n_points=100)  
        self.biopython_parser = PDBParser()
        self.loss_fn = torch.nn.CrossEntropyLoss()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.allowable_features = {
            'possible_amino_acids': ['ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 'ILE', 'LEU', 'LYS', 'MET',
                                    'PHE', 'PRO', 'SER', 'THR', 'TRP', 'TYR', 'VAL', 'HIP', 'HIE', 'TPO', 'HID', 'LEV', 'MEU',
                                    'PTR', 'GLV', 'CYT', 'SEP', 'HIZ', 'CYM', 'GLM', 'ASQ', 'TYS', 'CYX', 'GLZ', 'misc'],
        }
        self.plm_model = plm_model.to(self.device)
        self.gnn_model = gnn_model.to(self.device)

    @torch.no_grad()
    def compute_logits(self, pdb_file, *args, **kwargs) -> torch.Tensor:
        graph = self.generate_protein_graph(pdb_file)
        batch_graph = self.plm_model([graph])
        logits, embeds = self.gnn_model(batch_graph)
        return logits
    
    @torch.no_grad()
    def compute_embedding(self, pdb_file, reduction=None, *args, **kwargs) -> torch.Tensor:
        graph = self.generate_protein_graph(pdb_file)
        batch_graph = self.plm_model([graph])
        logits, embeds = self.gnn_model(batch_graph)
        if reduction is None:
            return embeds
        elif reduction == 'mean':
            return embeds.mean(dim=0)
        elif reduction == 'sum':
            return embeds.sum(dim=0)
        elif reduction == 'max':
            return embeds.max(dim=0)
        return embeds
    
    @torch.no_grad()
    def compute_perplexity(self, pdb_file, *args, **kwargs) -> float:
        graph = self.generate_protein_graph(pdb_file)
        batch_graph = self.plm_model([graph])
        logits, embeds = self.gnn_model(batch_graph)
        loss = self.loss_fn(logits[:, :20], graph.x[:,:20])
        return torch.exp(loss).item()
    
    def generate_protein_graph(self, pdb_file):
        rec, rec_coords, c_alpha_coords, n_coords, c_coords,seq = self.get_receptor_inference(pdb_file)
        graph = self.get_calpha_graph(rec, c_alpha_coords, n_coords, c_coords,seq)
        if not graph:
            return None
        
        if self.pre_transform is not None:
            graph = self.pre_transform(graph)
        
        del graph['distances']
        del graph['edge_dist']
        del graph['mu_r_norm']
        
        return graph
        
    def rec_residue_featurizer(self, rec, one_hot=True, add_feature=None):
        num_res = len([_ for _ in rec.get_residues()])
        num_feature = 2
        if add_feature.any():
            num_feature += add_feature.shape[1]
        res_feature = torch.zeros(num_res, self.num_residue_type + num_feature)
        count = 0
        self.sr.compute(rec, level="R")
        for residue in rec.get_residues():
            sasa = residue.sasa
            for atom in residue:
                if atom.name == 'CA':
                    bfactor = atom.bfactor
            assert not np.isinf(bfactor)
            assert not np.isnan(bfactor)
            assert not np.isinf(sasa)
            assert not np.isnan(sasa)

            residx = safe_index(self.allowable_features['possible_amino_acids'], residue.get_resname())
            res_feat_1 = one_hot_res(residx, num_residue_type=self.num_residue_type) if one_hot else [residx]
            if not res_feat_1:
                return False
            res_feat_1.append(sasa)
            res_feat_1.append(bfactor)
            if num_feature > 2:
                res_feat_1.extend(list(add_feature[count, :]))
            res_feature[count, :] = torch.tensor(
                res_feat_1, dtype=torch.float32)
            count += 1

        for k in range(self.num_residue_type, self.num_residue_type + 2):
            mean = res_feature[:, k].mean()
            std = res_feature[:, k].std()
            res_feature[:, k] = (res_feature[:, k] - mean) / (std + 0.000000001)
        return res_feature

    def get_node_features(self, n_coords, c_coords, c_alpha_coords, coord_mask, with_coord_mask=True):
        num_res = n_coords.shape[0]

        num_angle_type = 2
        angles = np.zeros((num_res, num_angle_type))
        for i in range(num_res-1):
            # These angles are called φ (phi) which involves the backbone atoms C-N-Cα-C
            angles[i, 0] = dihedral(c_coords[i], n_coords[i], c_alpha_coords[i], n_coords[i+1])
            # psi involves the backbone atoms N-Cα-C-N.
            angles[i, 1] = dihedral(n_coords[i], c_alpha_coords[i], c_coords[i], n_coords[i+1])
    
        node_scalar_features = np.zeros((num_res, num_angle_type*2))
        for i in range(num_angle_type):
            node_scalar_features[:, 2*i] = np.sin(angles[:, i])
            node_scalar_features[:, 2*i + 1] = np.cos(angles[:, i])

        if with_coord_mask:
            node_scalar_features = torch.cat([
                node_scalar_features,
                coord_mask.float().unsqueeze(-1)
            ], dim=-1)
        node_vector_features = None
        return node_scalar_features, node_vector_features

    def get_calpha_graph(self, rec, c_alpha_coords, n_coords, c_coords,seq):
        scalar_feature, vec_feature = self.get_node_features(
            n_coords, c_coords, c_alpha_coords, coord_mask=None, 
            with_coord_mask=False
            )
        # Extract 3D coordinates and n_i,u_i,v_i
        # vectors of representative residues ################
        residue_representatives_loc_list = []
        n_i_list = []
        u_i_list = []
        v_i_list = []
        for i, residue in enumerate(rec.get_residues()):
            n_coord = n_coords[i]
            c_alpha_coord = c_alpha_coords[i]
            c_coord = c_coords[i]
            u_i = (n_coord - c_alpha_coord) / np.linalg.norm(n_coord - c_alpha_coord)
            t_i = (c_coord - c_alpha_coord) / np.linalg.norm(c_coord - c_alpha_coord)
            n_i = np.cross(u_i, t_i) / np.linalg.norm(np.cross(u_i, t_i))   # main chain
            v_i = np.cross(n_i, u_i)
            assert (math.fabs(np.linalg.norm(v_i) - 1.) < 1e-5), "protein utils protein_to_graph_dips, v_i norm larger than 1"
            n_i_list.append(n_i)
            u_i_list.append(u_i)
            v_i_list.append(v_i)
            residue_representatives_loc_list.append(c_alpha_coord)
        
        # (N_res, 3)
        residue_representatives_loc_feat = np.stack(residue_representatives_loc_list, axis=0)
        
        n_i_feat = np.stack(n_i_list, axis=0)
        u_i_feat = np.stack(u_i_list, axis=0)
        v_i_feat = np.stack(v_i_list, axis=0)
        num_residues = len(c_alpha_coords)
        if num_residues <= 1:
            raise ValueError(f"rec contains only 1 residue!")

        ################### Build the k-NN graph ##############################
        assert num_residues == residue_representatives_loc_feat.shape[0]
        assert residue_representatives_loc_feat.shape[1] == 3
        distances = spa.distance.cdist(c_alpha_coords, c_alpha_coords)

        src_list = []
        dst_list = []
        dist_list = []
        mean_norm_list = []
        for i in range(num_residues):
            dst = list(np.where(distances[i, :] < self.cutoff)[0])
            dst.remove(i)
            if self.c_alpha_max_neighbors != None and len(dst) > self.c_alpha_max_neighbors:
                dst = list(np.argsort(distances[i, :]))[1: self.c_alpha_max_neighbors + 1]
            if len(dst) == 0:
                # choose second because first is i itself
                dst = list(np.argsort(distances[i, :]))[1:2]
                log(f'The c_alpha_cutoff {self.cutoff} was too small for one c_alpha such that it had no neighbors. So we connected it to the closest other c_alpha')
            assert i not in dst
            
            src = [i] * len(dst)
            src_list.extend(src)
            dst_list.extend(dst)
            valid_dist = list(distances[i, dst])
            dist_list.extend(valid_dist)
            valid_dist_np = distances[i, dst]
            
            sigma = np.array([1., 2., 5., 10., 30.]).reshape((-1, 1))
            # (sigma_num, neigh_num)
            weights = softmax(-valid_dist_np.reshape((1, -1)) ** 2 / sigma, axis=1)
            # print(weights)
            assert weights[0].sum() > 1 - 1e-2 and weights[0].sum() < 1.01
            # (neigh_num, 3)
            diff_vecs = residue_representatives_loc_feat[src, :] - residue_representatives_loc_feat[dst, :]
            # (sigma_num, 3)
            mean_vec = weights.dot(diff_vecs)
            # (sigma_num,)
            denominator = weights.dot(np.linalg.norm(diff_vecs, axis=1))
            # (sigma_num,)
            mean_vec_ratio_norm = np.linalg.norm(mean_vec, axis=1) / denominator
            mean_norm_list.append(mean_vec_ratio_norm)
            
        assert len(src_list) == len(dst_list)
        assert len(dist_list) == len(dst_list)
        
        residue_representatives_loc_feat = torch.from_numpy(residue_representatives_loc_feat.astype(np.float32))
        x = self.rec_residue_featurizer(rec, one_hot=True, add_feature=scalar_feature)
        
        if isinstance(x, bool) and (not x):
            return False

        graph = Data(
            x=x,
            pos=residue_representatives_loc_feat,
            edge_attr=self.get_edge_features(src_list, dst_list, dist_list, divisor=4),
            edge_index=torch.tensor([src_list, dst_list]),
            edge_dist=torch.tensor(dist_list),
            distances=torch.tensor(distances),
            mu_r_norm=torch.from_numpy(np.array(mean_norm_list).astype(np.float32)),
            seq=seq
            )

        # Loop over all edges of the graph and build the various p_ij, q_ij, k_ij, t_ij pairs
        edge_feat_ori_list = []
        for i in range(len(dist_list)):
            src = src_list[i]
            dst = dst_list[i]
            # place n_i, u_i, v_i as lines in a 3x3 basis matrix
            basis_matrix = np.stack((n_i_feat[dst, :], u_i_feat[dst, :], v_i_feat[dst, :]), axis=0)
            p_ij = np.matmul(
                basis_matrix,
                residue_representatives_loc_feat[src, :] - residue_representatives_loc_feat[dst, :]
                )
            q_ij = np.matmul(basis_matrix, n_i_feat[src, :])  # shape (3,)
            k_ij = np.matmul(basis_matrix, u_i_feat[src, :])
            t_ij = np.matmul(basis_matrix, v_i_feat[src, :])
            s_ij = np.concatenate((p_ij, q_ij, k_ij, t_ij), axis=0)  # shape (12,)
            edge_feat_ori_list.append(s_ij)

        edge_feat_ori_feat = np.stack(edge_feat_ori_list, axis=0)  # shape (num_edges, 4, 3)
        edge_feat_ori_feat = torch.from_numpy(edge_feat_ori_feat.astype(np.float32))

        graph.edge_attr = torch.cat([graph.edge_attr, edge_feat_ori_feat], axis=1)  # (num_edges, 17)
        return graph

    def get_edge_features(self, src_list, dst_list, dist_list, divisor=4):
        seq_edge = torch.absolute(torch.tensor(src_list) - torch.tensor(dst_list)).reshape(-1, 1)
        seq_edge = torch.where(seq_edge > self.seq_dist_cut, self.seq_dist_cut, seq_edge)
        seq_edge = F.one_hot(seq_edge, num_classes=self.seq_dist_cut + 1).reshape((-1, self.seq_dist_cut + 1))
        
        contact_sig = torch.where(torch.tensor(dist_list) <= 8, 1, 0).reshape(-1, 1)
        # avg distance = 7. So divisor = (4/7)*7 = 4
        dist_fea = self.distance_featurizer(dist_list, divisor=divisor)
        
        return torch.concat([seq_edge, dist_fea, contact_sig], dim=-1)

    def get_receptor_inference(self, rec_path):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=PDBConstructionWarning)
            structure = self.biopython_parser.get_structure('random_id', rec_path)
            rec = structure[0]
        coords = []
        c_alpha_coords = []
        n_coords = []
        c_coords = []
        valid_chain_ids = []
        lengths = []
        seq = []
        for i, chain in enumerate(rec):
            chain_coords = []  # num_residues, num_atoms, 3
            chain_c_alpha_coords = []
            chain_n_coords = []
            chain_c_coords = []
            count = 0
            invalid_res_ids = []
            for res_idx, residue in enumerate(chain):
                if residue.get_resname() == 'HOH':
                    invalid_res_ids.append(residue.get_id())
                    continue
                residue_coords = []
                c_alpha, n, c = None, None, None
                for atom in residue:
                    if atom.name == 'CA':
                        c_alpha = list(atom.get_vector())
                        seq.append(str(residue).split(" ")[1])
                    if atom.name == 'N':
                        n = list(atom.get_vector())
                    if atom.name == 'C':
                        c = list(atom.get_vector())
                    residue_coords.append(list(atom.get_vector()))
                # only append residue if it is an amino acid and not some weired molecule that is part of the complex
                if c_alpha != None and n != None and c != None:
                    chain_c_alpha_coords.append(c_alpha)
                    chain_n_coords.append(n)
                    chain_c_coords.append(c)
                    chain_coords.append(np.array(residue_coords))
                    count += 1
                else:
                    invalid_res_ids.append(residue.get_id())
            for res_id in invalid_res_ids:
                chain.detach_child(res_id)
            lengths.append(count)
            coords.append(chain_coords)
            c_alpha_coords.append(np.array(chain_c_alpha_coords))
            n_coords.append(np.array(chain_n_coords))
            c_coords.append(np.array(chain_c_coords))
            if len(chain_coords) > 0:
                valid_chain_ids.append(chain.get_id())
        valid_coords = []
        valid_c_alpha_coords = []
        valid_n_coords = []
        valid_c_coords = []
        valid_lengths = []
        invalid_chain_ids = []
        for i, chain in enumerate(rec):
            if chain.get_id() in valid_chain_ids:
                valid_coords.append(coords[i])
                valid_c_alpha_coords.append(c_alpha_coords[i])
                valid_n_coords.append(n_coords[i])
                valid_c_coords.append(c_coords[i])
                valid_lengths.append(lengths[i])
            else:
                invalid_chain_ids.append(chain.get_id())
        # list with n_residues arrays: [n_atoms, 3]
        coords = [item for sublist in valid_coords for item in sublist]

        c_alpha_coords = np.concatenate(valid_c_alpha_coords, axis=0)  # [n_residues, 3]
        n_coords = np.concatenate(valid_n_coords, axis=0)  # [n_residues, 3]
        c_coords = np.concatenate(valid_c_coords, axis=0)  # [n_residues, 3]

        for invalid_id in invalid_chain_ids:
            rec.detach_child(invalid_id)

        assert len(c_alpha_coords) == len(n_coords)
        assert len(c_alpha_coords) == len(c_coords)
        assert sum(valid_lengths) == len(c_alpha_coords)
        return rec, coords, c_alpha_coords, n_coords, c_coords,seq


    def distance_featurizer(self, dist_list, divisor) -> torch.Tensor:
        # you want to use a divisor that is close to 4/7 times the average distance that you want to encode
        length_scale_list = [1.5 ** x for x in range(15)]
        center_list = [0. for _ in range(15)]

        num_edge = len(dist_list)
        dist_list = np.array(dist_list)

        transformed_dist = [np.exp(- ((dist_list / divisor) ** 2) / float(length_scale))
                            for length_scale, center in zip(length_scale_list, center_list)]

        transformed_dist = np.array(transformed_dist).T
        transformed_dist = transformed_dist.reshape((num_edge, -1))
        return torch.from_numpy(transformed_dist.astype(np.float32))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    
    # model config
    parser.add_argument("--gnn", type=str, default="egnn", help="gat, gcn or egnn")
    parser.add_argument("--gnn_config", type=str, default="src/config/egnn.yaml", help="gnn config")
    parser.add_argument("--gnn_hidden_dim", type=int, default=512, help="hidden size of gnn")
    parser.add_argument("--gnn_model_path", type=str, default="", help="gnn model path")
    parser.add_argument("--plm", type=str, default="facebook/esm2_t33_650M_UR50D", help="esm param number")
    parser.add_argument("--plm_hidden_size", type=int, default=1280, help="hidden size of plm")
    parser.add_argument("--c_alpha_max_neighbors", type=int, default=10, help="graph dataset K")
    
    # dataset config
    parser.add_argument("--pdb_file", type=str, default=None, help="pdb file path")
    parser.add_argument("--pdb_dir", type=str, default=None, help="pdb file directory")
    parser.add_argument("--out_file", type=str, default=None, help="output file path")
    
    args = parser.parse_args()
    
    args.gnn_config = yaml.load(open(args.gnn_config), Loader=yaml.FullLoader)[args.gnn]
    args.gnn_config["hidden_channels"] = args.gnn_hidden_dim
    # load model
    plm_model = PLM_model(args)
    gnn_model = GNN_model(args)
    gnn_model.load_state_dict(torch.load(args.gnn_model_path))
    
    protssn = ProtSSN(
        c_alpha_max_neighbors=args.c_alpha_max_neighbors,
        pre_transform=NormalizeProtein(
            filename=f'norm/cath_k{args.c_alpha_max_neighbors}_mean_attr.pt'
        ),
        plm_model=plm_model, gnn_model=gnn_model
    )
    
    if args.pdb_file:
        logits = protssn.compute_logits(args.pdb_file)
        print(logits, logits.shape)
        ppl = protssn.compute_perplexity(args.pdb_file)
        print(ppl)
        embeds = protssn.compute_embedding(args.pdb_file)
        print(embeds.shape)
    
    if args.pdb_dir:
        pdb_files = sorted(os.listdir(args.pdb_dir))
        ppl_info = {"name": [], "ppl": []}
        for pdb_file in tqdm(pdb_files):
            pdb_file = os.path.join(args.pdb_dir, pdb_file)
            ppl = protssn.compute_perplexity(pdb_file)
            ppl_info["name"].append(pdb_file.split("/")[-1])
            ppl_info["ppl"].append(ppl)
        pd.DataFrame(ppl_info).to_csv(args.out_file, index=False)