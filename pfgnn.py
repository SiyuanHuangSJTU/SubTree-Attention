import math
import torch
from torch.nn import Parameter, Linear
import torch.nn.functional as F
import numpy as np
from torch_geometric.utils import degree

from pfprop import MessageProp, KeyProp, MessageProp_random_walk_with_teleportation, KeyProp_random_walk_with_teleportation


class PFGT(torch.nn.Module):
    def __init__(self, num_features, num_classes, hidden_channels, dropout, K, aggr):
        super(PFGT, self).__init__()
        self.input_trans = Linear(num_features, hidden_channels)
        self.linQ = Linear(hidden_channels, hidden_channels)
        self.linK = Linear(hidden_channels, hidden_channels)
        self.linV = Linear(hidden_channels, num_classes)

        if (aggr=='random_walk_with_teleportation'):
            self.propM = MessageProp_random_walk_with_teleportation(node_dim=-3)
            self.propK = KeyProp_random_walk_with_teleportation(node_dim=-2)        
        else:
            self.propM = MessageProp(aggr=aggr, node_dim=-3)
            self.propK = KeyProp(aggr=aggr, node_dim=-2)

        self.c = hidden_channels
        self.dropout = dropout
        self.K = K
        self.aggr = aggr

        self.cst = 10e-6

        self.hopwise = Parameter(torch.ones(K+1, dtype=torch.float))
        self.alpha = Parameter(torch.zeros(K, dtype=torch.float))

    def reset_parameters(self):
        torch.nn.init.ones_(self.hopwise)
        torch.nn.init.zeros_(self.alpha)

    def forward(self, data):
        x = data.graph['node_feat']
        edge_index = data.graph['edge_index']

        if (self.aggr=='random_walk_with_teleportation'):

            row, col = edge_index
            deg = degree(col, x.size(0), dtype=x.dtype)
            deg_inv_sqrt = deg.pow(-1)
            deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
            norm = deg_inv_sqrt[row]

        x = F.dropout(x, p=self.dropout, training=self.training)
        x = F.relu(self.input_trans(x))
        x = F.dropout(x, p=self.dropout, training=self.training)
        Q = self.linQ(x)
        K = self.linK(x)
        V = self.linV(x)

        Q = 1 + F.elu(Q)
        K = 1 + F.elu(K)

        # M = K.repeat(1, V.size(1)).view(-1, V.size(1), K.size(1)).transpose(-1, -2) * V.repeat(1, K.size(1)).view(-1, K.size(1), V.size(1))
        M = torch.einsum('ni,nj->nij',[K,V])

        hidden = V*(self.hopwise[0])
        for hop in range(self.K):
            if (self.aggr=='random_walk_with_teleportation'):
                num_nodes = x.size(0)
                alpha = self.alpha.clamp(min=0, max=1)
                teleportM = alpha[hop] * torch.sum(M, dim=0, keepdim=True) / num_nodes
                teleportK = alpha[hop] * torch.sum(K, dim=0, keepdim=True) / num_nodes
                M = self.propM(M, edge_index, norm.view(-1,1,1))
                K = self.propK(K, edge_index, norm.view(-1,1))
                M = (1 - alpha[hop]) * M + teleportM
                K = (1 - alpha[hop]) * K + teleportK
            else:
                M = self.propM(M, edge_index)
                K = self.propK(K, edge_index)         
            # H = (Q.repeat(1, M.size(-1)).view(-1, M.size(-1),
                #  Q.size(-1)).transpose(-1, -2) * M).sum(dim=-2)
            H = torch.einsum('ni,nij->nj',[Q,M])
            # C = (Q * K).sum(dim=-1, keepdim=True) + self.cst
            C = torch.einsum('ni,ni->n',[Q,K]).unsqueeze(-1) + self.cst
            H = H / C
            gamma = self.hopwise[hop+1]
            hidden = hidden + gamma*H

        return hidden


class MHPFGT(torch.nn.Module):
    def __init__(self, num_features, num_classes, hidden_channels, dropout, K, num_heads, ind_gamma, gamma_softmax, multi_concat, aggr):
        super(MHPFGT, self).__init__()
        self.headc = headc = hidden_channels // num_heads
        self.input_trans = Linear(num_features, hidden_channels)
        self.linQ = Linear(hidden_channels, headc * num_heads)
        self.linK = Linear(hidden_channels, headc * num_heads)
        self.linV = Linear(hidden_channels, num_classes * num_heads)
        if (multi_concat):
            self.output = Linear(num_classes * num_heads, num_classes)

        if (aggr=='random_walk_with_teleportation'):
            self.propM = MessageProp_random_walk_with_teleportation(node_dim=-4)
            self.propK = KeyProp_random_walk_with_teleportation(node_dim=-3)        
        else:
            self.propM = MessageProp(aggr=aggr, node_dim=-4)
            self.propK = KeyProp(aggr=aggr, node_dim=-3)

        self.dropout = dropout
        self.K = K
        self.num_heads = num_heads
        self.num_classes = num_classes
        self.multi_concat = multi_concat
        self.ind_gamma = ind_gamma
        self.gamma_softmax = gamma_softmax
        self.aggr = aggr

        self.cst = 10e-6

        if (ind_gamma):
            if (gamma_softmax):
                self.hopwise = Parameter(torch.ones(K+1))
                self.headwise = Parameter(torch.zeros(size=(self.num_heads,K)))
            else:
                self.hopwise = Parameter(torch.ones(size=(self.num_heads,K+1)))
        else:
            self.hopwise = Parameter(torch.ones(K+1))
        
        self.alpha = Parameter(torch.zeros(K, dtype=torch.float))

    def reset_parameters(self):
        if (self.ind_gamma and self.gamma_softmax):
            torch.nn.init.ones_(self.hopwise)
            torch.nn.init.zeros_(self.headwise)
        else:
            torch.nn.init.ones_(self.hopwise)
        self.input_trans.reset_parameters()
        self.linQ.reset_parameters()
        self.linK.reset_parameters()
        self.linV.reset_parameters()
        if (self.multi_concat):
            self.output.reset_parameters()
        torch.nn.init.zeros_(self.alpha)

    def forward(self, data):
        x = data.graph['node_feat']
        edge_index = data.graph['edge_index']

        if (self.aggr=='random_walk_with_teleportation'):

            row, col = edge_index
            deg = degree(col, x.size(0), dtype=x.dtype)
            deg_inv_sqrt = deg.pow(-1)
            deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
            norm = deg_inv_sqrt[row]

        x = F.dropout(x, p=self.dropout, training=self.training)
        x = F.relu(self.input_trans(x))
        x = F.dropout(x, p=self.dropout, training=self.training)

        Q = self.linQ(x)
        K = self.linK(x)
        V = self.linV(x)

        Q = 1 + F.elu(Q)
        K = 1 + F.elu(K)

        Q = Q.view(-1, self.num_heads, self.headc)
        K = K.view(-1, self.num_heads, self.headc)
        V = V.view(-1, self.num_heads, self.num_classes)

        M = torch.einsum('nhi,nhj->nhij', [K, V])

        if (self.ind_gamma):
            if (self.gamma_softmax):
                hidden = V * (self.hopwise[0])
            else:
                hidden = V * (self.hopwise[:, 0].unsqueeze(-1))
        else:
            hidden = V * (self.hopwise[0])

        if ((self.ind_gamma) and (self.gamma_softmax)):
            layerwise = F.softmax(self.headwise, dim=-2)

        for hop in range(self.K):
            if (self.aggr=='random_walk_with_teleportation'):
                num_nodes = x.size(0)
                alpha = self.alpha.clamp(min=0, max=1)
                teleportM = alpha[hop] * torch.sum(M, dim=0, keepdim=True) / num_nodes
                teleportK = alpha[hop] * torch.sum(K, dim=0, keepdim=True) / num_nodes
                M = self.propM(M, edge_index, norm.view(-1,1,1,1))
                K = self.propK(K, edge_index, norm.view(-1,1,1))
                M = (1 - alpha[hop]) * M + teleportM
                K = (1 - alpha[hop]) * K + teleportK
            else:
                M = self.propM(M, edge_index)
                K = self.propK(K, edge_index) 
            H = torch.einsum('nhi,nhij->nhj', [Q, M])
            C = torch.einsum('nhi,nhi->nh', [Q, K]).unsqueeze(-1) + self.cst
            H = H / C
            if (self.ind_gamma):
                if (self.gamma_softmax):
                    gamma = self.hopwise[hop+1] * layerwise[:, hop].unsqueeze(-1)
                else:
                    gamma = self.hopwise[:, hop+1].unsqueeze(-1)
            else:
                gamma = self.hopwise[hop+1]
            hidden = hidden + gamma * H

        if (self.multi_concat):
            hidden = hidden.view(-1, self.num_classes * self.num_heads)
            hidden = F.dropout(hidden, p=self.dropout, training=self.training)
            hidden = self.output(hidden)
        else:
            hidden = hidden.sum(dim=-2)

        return hidden