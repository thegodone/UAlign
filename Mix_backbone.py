import torch
from typing import Any, Dict, List, Tuple, Optional, Union
from collections.abc import Iterable
import torch.nn.functional as F
from GATconv import MyGATConv
from GINConv import MyGINConv
from sparse_backBone import (
    SparseAtomEncoder, SparseBondEncoder, SparseEdgeUpdateLayer
)


class MhAttnBlock(torch.nn.Module):
    def __init__(
        self, Qdim: int, Kdim: int, Vdim: int, Odim: int, heads: int = 1,
        negative_slope: float = 0.2, dropout: float = 0
    ):
        super(MhAttnBlock, self).__init__()
        self.Qdim, self.Kdim, self.Vdim = Qdim, Kdim, Vdim
        self.heads, self.Odim = heads, Odim
        self.negative_slope = negative_slope
        self.LinearK = torch.nn.Linear(Kdim, heads * Odim, bias=False)
        self.LinearQ = torch.nn.Linear(Qdim, heads * Odim, bias=False)
        self.alphaQ = torch.nn.Parameter(torch.zeros(1, 1, heads, Odim))
        self.alphaK = torch.nn.Parameter(torch.zeros(1, 1, heads, Odim))
        self.bias = torch.nn.Parameter(torch.zeros(heads, Odim))
        self.LinearV = torch.nn.Linear(Vdim, heads * Odim, bias=False)
        self.dropout_fun = torch.nn.Dropout(dropout)

        torch.nn.init.xavier_uniform_(self.alphaK)
        torch.nn.init.xavier_uniform_(self.alphaQ)
        torch.nn.init.xavier_uniform_(self.bias)

    def forward(
        self, Q: torch.Tensor, K: torch.Tensor, V: torch.Tensor,
        attn_mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        (batch_size, Qsize), Ksize = Q.shape[:2], K.shape[1]
        Qproj = self.LinearQ(Q).reshape(batch_size, -1, self.heads, self.Odim)
        Kproj = self.LinearK(K).reshape(batch_size, -1, self.heads, self.Odim)
        Vproj = self.LinearV(V).reshape(batch_size, -1, self.heads, self.Odim)

        attn_Q = (self.alphaQ * Qproj).sum(dim=-1)
        attn_K = (self.alphaK * Kproj).sum(dim=-1)

        attn_K = attn_K.unsqueeze(dim=1).repeat(1, Qsize, 1, 1)
        attn_Q = attn_Q.unsqueeze(dim=2).repeat(1, 1, Ksize, 1)
        attn_w = F.leaky_relu(attn_K + attn_Q, self.negative_slope)

        if attn_mask is not None:
            attn_mask = torch.logical_not(attn_mask.unsqueeze(dim=-1))
            INF = (1 << 32) - 1
            attn_w = torch.masked_fill(attn_w, attn_mask, -INF)
        attn_w = self.dropout_fun(torch.softmax(attn_w, dim=2).unsqueeze(-1))
        x_out = (attn_w * Vproj.unsqueeze(dim=1)).sum(dim=2) + self.bias
        return x_out.reshape(batch_size, Qsize, -1)


class SelfAttnBlock(torch.nn.Module):
    def __init__(
        self, input_dim: int, output_dim: int, heads: int = 1,
        negative_slope: float = 0.2, dropout: float = 0
    ):
        super(SelfAttnBlock, self).__init__()
        self.model = MhAttnBlock(
            Qdim=input_dim, Kdim=input_dim, Vdim=input_dim, heads=heads,
            Odim=output_dim, negative_slope=negative_slope, dropout=dropout
        )

    def forward(
        self, X: torch.Tensor, attn_mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        return self.model(Q=X, K=X, V=X, attn_mask=attn_mask)


class MixConv(torch.nn.Module):
    def __init__(
        self, emb_dim: int, gnn_args: Dict[str, Any],
        heads: int = 1, negative_slope: float = 0.2,
        dropout: float = 0, gnn_type: str = 'gin',
        update_gate: str = 'add'
    ):
        super(MixConv, self).__init__()
        assert emb_dim % heads == 0, 'The dim of input' +\
            ' should be evenly divided by num of heads'
        self.attn_conv = SelfAttnBlock(
            input_dim=emb_dim, output_dim=emb_dim // heads, heads=heads,
            negative_slope=negative_slope, dropout=dropout
        )

        assert update_gate in ['add', 'cat', 'gate'], \
            f'Invalid update method {update_gate}'

        if gnn_type == 'gin':
            self.gnn_conv = MyGINConv(**gnn_args)
        elif gnn_type == 'gat':
            self.gnn_conv = MyGATConv(**gnn_args)
        else:
            raise NotImplementedError(f'Invalid gnn type {gcn}')
        self.update_method = update_gate
        if self.update_method == 'gate':
            self.update_gate = torch.nn.GRUCell(emb_dim, emb_dim)
        elif self.update_method == 'cat':
            self.update_gate = torch.nn.Linear(emb_dim << 1, emb_dim)

    def forward(
        self, node_feat: torch.Tensor, attn_mask: torch.Tensor,
        edge_index: torch.Tensor, edge_feat: torch.Tensor,
        ptr: torch.Tensor,
    ) -> torch.Tensor:
        conv_res = self.gnn_conv(
            x=node_feat, edge_attr=edge_feat, edge_index=edge_index
        )
        batch_size, max_node = attn_mask.shape[:2]
        batch_mask = self.batch_mask(ptr, max_node, batch_size)
        attn_input = self.graph2batch(
            node_feat=node_feat, batch_mask=batch_mask,
            batch_size=batch_size, max_node=max_node
        )
        attn_res = self.attn_conv(attn_input, attn_mask=attn_mask)

        if self.update_method == 'gate':
            return self.update_gate(attn_res[batch_mask], conv_res)
        elif self.update_method == 'cat':
            return self.update_gate(torch.cat(
                [attn_res[batch_mask], conv_res], dim=-1
            ))
        else:
            return attn_res[batch_mask] + conv_res

    def batch_mask(
        self, ptr: torch.Tensor, max_node: int, batch_size: int
    ) -> torch.Tensor:
        num_nodes = ptr[1:] - ptr[:-1]
        mask = torch.arange(max_node).repeat(batch_size, 1)
        mask = mask.to(num_nodes.device)
        return mask < num_nodes.reshape(-1, 1)

    def graph2batch(
        self, node_feat: torch.Tensor, batch_mask: torch.Tensor,
        batch_size: int, max_node: int
    ) -> torch.Tensor:
        answer = torch.zeros(batch_size, max_node, node_feat.shape[-1])
        answer = answer.to(node_feat.device)
        answer[batch_mask] = node_feat
        return answer


class MixFormer(torch.nn.Module):
    def __init__(
        self, emb_dim: int, n_layers: int, gnn_args: Union[Dict, List[Dict]],
        dropout: float = 0, heads: int = 1, negative_slope: float = 0.2,
        pos_enc: str = 'none', pos_args: Optional[Dict] = None,
        n_class: Optional[int] = None, gnn_type: str = 'gin',
        update_gate: str = 'add', residual: bool = True,
        edge_last: bool = True
    ):
        super(MixFormer, self).__init__()

        self.pos_enc = pos_enc
        self.residual = residual
        self.atom_encoder = SparseAtomEncoder(emb_dim, n_class)
        self.bond_encoder = SparseBondEncoder(emb_dim, n_class)

        self.num_layers = n_layers
        self.lns = torch.nn.ModuleList()
        self.convs = torch.nn.ModuleList()
        self.edge_update = torch.nn.ModuleList()
        self.edge_last = edge_last

        self.dropout_fun = torch.nn.Dropout(dropout)

        for i in range(self.num_layers):
            self.lns.append(torch.nn.LayerNorm(emb_dim))
            gnn_layer = gnn_args[i] if isinstance(gnn_args, list) else gnn_args
            self.convs.append(MixConv(
                emb_dim=emb_dim, gnn_args=gnn_layer, heads=heads,
                dropout=dropout, gnn_type=gnn_type, update_gate=update_gate
            ))
            if i < self.num_layers - 1 or self.edge_last:
                self.edge_update.append(SparseEdgeUpdateLayer(
                    emb_dim, emb_dim, residual=residual
                ))

        if pos_enc == 'Lap':
            assert pos_args is not None, 'require parameters for pos emb'
            self.merger = torch.nn.Linear(pos_args['dim'] + emb_dim, emb_dim)
        elif pos_enc != 'none':
            raise NotImplementedError(f'Invalid pos enc {pos_enc}')

    def transform_pos(self, x, graph):
        if self.pos_enc == 'Lap':
            return self.merger(torch.cat(x, graph.lap_pos_enc)) + x
        elif self.pos_enc != 'none':
            raise NotImplementedError(f'Invalid pos enc {self.pos_enc}')

    def forward(self, graph):
        node_feats = self.atom_encoder(graph.x, graph.get('node_rxn', None))
        edge_feats = self.bond_encoder(
            graph.edge_attr, graph.org_mask,
            graph.self_mask, graph.get('edge_rxn', None)
        )

        for i in range(self.num_layers):
            conv_res = self.convs[i](
                node_feat=node_feats, edge_feat=edge_feats, ptr=graph.ptr,
                attn_mask=graph.attn_mask, edge_index=graph.edge_index
            ) + (node_feats if self.residual else 0)

            node_feats = self.dropout_fun(torch.relu(self.lns[i](conv_res)))

            if i < self.num_layers - 1 or self.edge_last:
                edge_feats = self.edge_update[i](
                    edge_attr=edge_feats, x=node_feats,
                    edge_index=graph.edge_index
                )

        return node_feats, edge_feats
