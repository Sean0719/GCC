#!/usr/bin/env python
# encoding: utf-8
# File Name: graph_dataset.py
# Author: Jiezhong Qiu
# Create Time: 2019/12/11 12:17
# TODO:

import numpy as np
import operator
import dgl
import networkx as nx
import matplotlib.pyplot as plt
import torch
from dgl.data import AmazonCoBuy, Coauthor
import dgl.data

from cogdl.datasets import build_dataset
import data_util
import horovod.torch as hvd


def worker_init_fn(worker_id):
    worker_info = torch.utils.data.get_worker_info()
    dataset = worker_info.dataset
    dataset.graphs, _ = dgl.data.utils.load_graphs(
            dataset.dgl_graphs_file,
            dataset.jobs[worker_id]
            )
    dataset.length = sum([g.number_of_nodes() for g in dataset.graphs])
    np.random.seed(worker_info.seed % (2 ** 32))

def hvd_worker_init_fn(worker_id):
    worker_info = torch.utils.data.get_worker_info()
    dataset = worker_info.dataset
    dataset.graphs, _ = dgl.data.utils.load_graphs(
            dataset.dgl_graphs_file,
            dataset.jobs[worker_id]
            )
    dataset.length = sum([g.number_of_nodes() for g in dataset.graphs])
    np.random.seed((worker_info.seed % (2 ** 32) + hvd.rank()) % (2 ** 32))
    #  print("hvd.rank=%d, hvd.local_rank=%d, worker_id=%d, dataset.length=%d" % (hvd.rank(), hvd.local_rank(), worker_id, dataset.length))

class LoadBalanceGraphDataset(torch.utils.data.IterableDataset):
    def __init__(self, rw_hops=64, restart_prob=0.8,
            positional_embedding_size=32,
            step_dist=[1.0, 0.0, 0.0],
            num_workers=1,
            dgl_graphs_file="data_bin/dgl/yuxiao_lscc_wo_fb_and_friendster_plus_dgl_built_in_graphs2.bin",
            num_samples=10000,
            num_copies=1,
            graph_transform=None):
        super(LoadBalanceGraphDataset).__init__()
        self.rw_hops = rw_hops
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        self.num_samples = num_samples
        assert sum(step_dist) == 1.0
        assert(positional_embedding_size > 1)
        self.dgl_graphs_file = dgl_graphs_file
        graph_sizes = dgl.data.utils.load_labels(dgl_graphs_file)['graph_sizes'].tolist()
        print("load graph done")

        # a simple greedy algorithm for load balance
        # sorted graphs w.r.t its size in decreasing order
        # for each graph, assign it to the worker with least workload
        assert num_workers % num_copies == 0
        jobs = [list() for i in range(num_workers // num_copies)]
        workloads = [0] * (num_workers // num_copies)
        graph_sizes = sorted(enumerate(graph_sizes), key=operator.itemgetter(1), reverse=True)
        # Drop top 2 largest graphs
        # graph_sizes = graph_sizes[2:]
        for idx, size in graph_sizes:
            argmin = workloads.index(min(workloads))
            workloads[argmin] += size
            jobs[argmin].append(idx)
        self.jobs = jobs * num_copies
        self.total = self.num_samples * num_workers
        self.graph_transform = graph_transform

    def __len__(self):
        return self.num_samples * num_workers

    def __iter__(self):
        samples = np.random.randint(low=0, high=self.length, size=self.num_samples)
        for idx in samples:
            yield self.__getitem__(idx)

    def __getitem__(self, idx):
        #  worker_info = torch.utils.data.get_worker_info()
        #  print("hvd.rank=%d, hvd.local_rank=%d, worker_id=%d, seed=%d, idx=%d" % (hvd.rank(), hvd.local_rank(), worker_info.id, worker_info.seed, idx))
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if  node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                    g=self.graphs[graph_idx],
                    seeds=[node_idx],
                    num_traces=1,
                    num_hops=step
                    )[0][0][-1].item()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx, other_node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops)

        graph_q = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=node_idx,
                trace=traces[0],
                positional_embedding_size=self.positional_embedding_size,
                )
        graph_k = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=other_node_idx,
                trace=traces[1],
                positional_embedding_size=self.positional_embedding_size,
                )
        if self.graph_transform:
            graph_q = self.graph_transform(graph_q)
            graph_k = self.graph_transform(graph_k)
        return graph_q, graph_k

class GraphDataset(torch.utils.data.Dataset):
    def __init__(self, rw_hops=64, subgraph_size=64, restart_prob=0.8, positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0]):
        super(GraphDataset).__init__()
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        assert sum(step_dist) == 1.0
        assert(positional_embedding_size  > 1)
        #  graphs = []
        graphs, _ = dgl.data.utils.load_graphs("data_bin/dgl/lscc_graphs.bin", [0, 1, 2])
        for name in ["cs", "physics"]:
            g = Coauthor(name)[0]
            g.remove_nodes((g.in_degrees() == 0).nonzero().squeeze())
            g.readonly()
            graphs.append(g)
        for name in ["computers", "photo"]:
            g = AmazonCoBuy(name)[0]
            g.remove_nodes((g.in_degrees() == 0).nonzero().squeeze())
            g.readonly()
            graphs.append(g)
        # more graphs are comming ...
        print("load graph done")
        self.graphs = graphs
        self.length = sum([g.number_of_nodes() for g in self.graphs])

    def __len__(self):
        return self.length

    def getplot(self, idx):
        graph_q, graph_k = self.__getitem__(idx)
        graph_q = graph_q.to_networkx()
        graph_k = graph_k.to_networkx()
        figure_q = plt.figure(figsize=(10, 10))
        nx.draw(graph_q)
        plt.draw()
        image_q = data_util.plot_to_image(figure_q)
        figure_k = plt.figure(figsize=(10, 10))
        nx.draw(graph_k)
        plt.draw()
        image_k = data_util.plot_to_image(figure_k)
        return image_q, image_k

    def _convert_idx(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if  node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()
        return graph_idx, node_idx

    def __getitem__(self, idx):
        graph_idx, node_idx = self._convert_idx(idx)

        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                    g=self.graphs[graph_idx],
                    seeds=[node_idx],
                    num_traces=1,
                    num_hops=step
                    )[0][0][-1].item()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx, other_node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops)

        graph_q = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=node_idx,
                trace=traces[0],
                positional_embedding_size=self.positional_embedding_size)
        graph_k = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=other_node_idx,
                trace=traces[1],
                positional_embedding_size=self.positional_embedding_size)
        return graph_q, graph_k


class CogDLGraphDataset(GraphDataset):
    def __init__(self, dataset, rw_hops=64, subgraph_size=64, restart_prob=0.8, positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0]):
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        assert(positional_embedding_size > 1)

        class tmp():
            # HACK
            pass
        args = tmp()
        args.dataset = dataset
        self.data = build_dataset(args)[0]
        self.graphs = [self._create_dgl_graph(self.data)]
        self.length = sum([g.number_of_nodes() for g in self.graphs])
        self.total = self.length

    def _create_dgl_graph(self, data):
        graph = dgl.DGLGraph()
        src, dst = data.edge_index.tolist()
        num_nodes = data.edge_index.max() + 1
        graph.add_nodes(num_nodes)
        graph.add_edges(src, dst)
        graph.add_edges(dst, src)
        # assert all(graph.out_degrees() != 0)
        graph.readonly()
        return graph


class CogDLGraphClassificationDataset(CogDLGraphDataset):
    def __init__(self, dataset, rw_hops=64, subgraph_size=64, restart_prob=0.8, positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0]):
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        assert(positional_embedding_size > 1)

        class tmp():
            # HACK
            pass
        args = tmp()
        args.dataset = dataset
        self.dataset = build_dataset(args)
        self.graphs = [self._create_dgl_graph(data) for data in self.dataset]

        self.length = len(self.graphs)
        self.total = self.length

    def _convert_idx(self, idx):
        graph_idx = idx
        node_idx = self.graphs[idx].out_degrees().argmax().item()
        return graph_idx, node_idx

class CogDLGraphClassificationDatasetLabeled(CogDLGraphClassificationDataset):
    def __init__(self, dataset, rw_hops=64, subgraph_size=64, restart_prob=0.8, positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0]):
        super(CogDLGraphClassificationDatasetLabeled, self).__init__(dataset, rw_hops, subgraph_size, restart_prob, positional_embedding_size, step_dist)
        self.num_classes = self.dataset.data.y.max().item() + 1

    def __getitem__(self, idx):
        graph_idx = idx
        node_idx = self.graphs[idx].out_degrees().argmax().item()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops)

        graph_q = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=node_idx,
                trace=traces[0],
                positional_embedding_size=self.positional_embedding_size)
        return graph_q, self.dataset.data.y[graph_idx].item()

class CogDLGraphDatasetLabeled(CogDLGraphDataset):
    def __init__(self, dataset, rw_hops=64, subgraph_size=64, restart_prob=0.8, positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0], cat_prone=False):
        super(CogDLGraphDatasetLabeled, self).__init__(dataset, rw_hops, subgraph_size, restart_prob, positional_embedding_size, step_dist)
        assert(len(self.graphs) == 1)
        self.num_classes = self.data.y.shape[1]

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if  node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops)

        graph_q = data_util._rwr_trace_to_dgl_graph(
                g=self.graphs[graph_idx],
                seed=node_idx,
                trace=traces[0],
                positional_embedding_size=self.positional_embedding_size)
        return graph_q, self.data.y[idx].argmax().item()

if __name__ == '__main__':
    import horovod.torch as hvd
    hvd.init()
    num_workers=1
    import psutil
    mem = psutil.virtual_memory()
    print(mem.used/1024**3)
    graph_dataset = LoadBalanceGraphDataset(num_workers=num_workers)
    mem = psutil.virtual_memory()
    print(mem.used/1024**3)
    graph_loader = torch.utils.data.DataLoader(
            graph_dataset,
            batch_size=1,
            collate_fn=data_util.batcher(),
            num_workers=num_workers,
            worker_init_fn=worker_init_fn
            )
    mem = psutil.virtual_memory()
    print(mem.used/1024**3)
    for step, batch in enumerate(graph_loader):
        print(batch.graph_q.batch_size)
        mem = psutil.virtual_memory()
        print(mem.used/1024**3)
        #  print(batch.graph_q)
        #  print(batch.graph_q.ndata['pos_directed'])
        #  print(batch.graph_q.ndata['pos_undirected'])
    exit(0)
    graph_dataset = CogDLGraphDataset(dataset="wikipedia")
    pq, pk = graph_dataset.getplot(0)
    graph_loader = torch.utils.data.DataLoader(
            dataset=graph_dataset,
            batch_size=20,
            collate_fn=data_util.batcher(),
            shuffle=True,
            num_workers=4)
    for step, batch in enumerate(graph_loader):
        print(batch.graph_q)
        print(batch.graph_q.ndata['x'].shape)
        print(batch.graph_q.batch_size)
        print("max", batch.graph_q.edata['efeat'].max())
        break
