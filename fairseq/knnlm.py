import torch
import faiss
import math
import numpy as np
from fairseq import utils
import time
from fairseq.data import Dictionary


class KNN_Dstore(object):
    def __init__(self, args):
        self.half = args.fp16
        self.dimension = args.decoder_embed_dim
        self.k = args.k
        self.dstore_size = args.dstore_size
        self.metric_type = args.faiss_metric_type
        self.sim_func = args.knn_sim_func
        self.dstore_fp16 = args.dstore_fp16
        self.index = self.setup_faiss(args)

    def setup_faiss(self, args):
        if not args.dstore_filename:
            raise ValueError('Cannot build a datastore without the data.')

        start = time.time()
        index = faiss.read_index(args.indexfile, faiss.IO_FLAG_ONDISK_SAME_DIR)
        print('Reading datastore took {} s'.format(time.time() - start))
        index.nprobe = args.probe

        if args.dstore_fp16:
            print('Keys are fp16 and vals are int16')
            if not args.no_load_keys:
                self.keys = np.memmap(args.dstore_filename + '_keys.npy', dtype=np.float16, mode='r',
                                      shape=(self.dstore_size, self.dimension))
            self.vals = np.memmap(args.dstore_filename + '_vals.npy', dtype=np.int16, mode='r',
                                  shape=(self.dstore_size, 1))
        else:
            print('Keys are fp32 and vals are int64')
            if not args.no_load_keys:
                self.keys = np.memmap(args.dstore_filename + '_keys.npy', dtype=np.float32, mode='r',
                                      shape=(self.dstore_size, self.dimension))
            self.vals = np.memmap(args.dstore_filename + '_vals.npy', dtype=np.int, mode='r',
                                  shape=(self.dstore_size, 1))

        # also read in the token-sample mapping file
        self.token_sample_map = torch.load(args.dstore_filename + '_map.pt')
        self.inv_token_sample_map = np.zeros(self.dstore_size, dtype='i')
        for k, v in self.token_sample_map.items():
            self.inv_token_sample_map[v[0]:v[1]] = k

        # read in the locality feature from npy file
        if 'test' in args.dstore_filename:
            self.locality_features = np.load('examples/language_model/java/java_test_pre.original_path.npy')
        else:
            self.locality_features = np.load('examples/language_model/java/java_validation_pre.original_path.npy')

        # If you wish to load all the keys into memory
        # CAUTION: Only do this if your RAM can handle it!
        if args.move_dstore_to_mem:
            print('Loading to memory...')
            start = time.time()

            if not args.no_load_keys:
                del self.keys
                self.keys_from_memmap = np.memmap(args.dstore_filename + '_keys.npy', dtype=np.float32, mode='r',
                                                  shape=(self.dstore_size, self.dimension))
                self.keys = np.zeros((self.dstore_size, self.dimension),
                                     dtype=np.float16 if args.dstore_fp16 else np.float32)
                self.keys = self.keys_from_memmap[:]
                self.keys = self.keys.astype(np.float16 if args.dstore_fp16 else np.float32)

            del self.vals
            self.vals_from_memmap = np.memmap(args.dstore_filename + '_vals.npy', dtype=np.int, mode='r',
                                              shape=(self.dstore_size, 1))
            self.vals = np.zeros((self.dstore_size, 1), dtype=np.int16 if args.dstore_fp16 else np.int)
            self.vals = self.vals_from_memmap[:]
            self.vals = self.vals.astype(np.int16 if args.dstore_fp16 else np.int)
            print('Loading to memory took {} s'.format(time.time() - start))

        return index

    def get_knns(self, queries, sample_ids=None):
        start = time.time()
        redundancy = 2048
        new_knns = []
        new_dists = []
        total_block_count = 0

        dists, knns = self.index.search(queries.detach().cpu().float().numpy(), self.k + redundancy)
        # print(dists.shape)
        # print(knns.shape)

        for x, y, i in zip(knns, dists, sample_ids):
            blocked_range = self.token_sample_map[i.item()]
            mask = (x < blocked_range[0]) | (x >= blocked_range[1])
            new_x = x[mask]
            new_y = y[mask]
            total_block_count += self.k + redundancy - len(new_x)

            new_x = new_x[:self.k]
            new_y = new_y[:self.k]

            if len(new_x) < 1024:
                print(len(new_x))
            new_knns.append(new_x)
            new_dists.append(new_y)
        dists = np.array(new_dists)
        knns = np.array(new_knns)
        # print(dists.shape)
        # print(knns.shape)
        # print(total_block_count)
        return dists, knns

    def get_knn_log_prob(self, queries, tgt, pad_idx, sample_ids=None):
        def dist_func(d, k, q, function=None):
            if not function:
                # Default behavior for L2 metric is to recompute distances.
                # Default behavior for IP metric is to return faiss distances.
                qsize = q.shape
                if self.metric_type == 'l2':
                    start = time.time()
                    knns_vecs = torch.from_numpy(self.keys[k]).cuda().view(qsize[0], self.k, -1)
                    if self.half:
                        knns_vecs = knns_vecs.half()
                    query_vecs = q.view(qsize[0], 1, qsize[1]).repeat(1, self.k, 1)
                    l2 = torch.sum((query_vecs - knns_vecs.detach()) ** 2, dim=2)
                    return -1 * l2
                return d

            if function == 'dot':
                qsize = q.shape
                return (torch.from_numpy(self.keys[k]).cuda() * q.view(qsize[0], 1, qsize[1])).sum(dim=-1)

            if function == 'do_not_recomp_l2':
                return -1 * d

            raise ValueError("Invalid knn similarity function!")

        # queries  are TxBxC
        # reshape: (TxB)xC
        qshape = queries.shape
        queries = queries.view(-1, qshape[-1])
        tgt = tgt.contiguous().view(-1)
        token_sample_ids = sample_ids.repeat_interleave(qshape[0])
        dists, knns = self.get_knns(queries[tgt != pad_idx], sample_ids=token_sample_ids[tgt != pad_idx])
        reduced_token_sample_ids = token_sample_ids[tgt != pad_idx]

        locality = self.locality_features[
            np.tile(self.inv_token_sample_map[reduced_token_sample_ids.cpu()], (knns.shape[1], 1)).T,
            self.inv_token_sample_map[knns]]
        # exit()
        # for i in range(knns.shape[0]):
        #     for j in range(knns.shape[1]):
        #         locality[i, j] = self.locality_features[self.inv_token_sample_map[reduced_token_sample_ids[i]],
        #                                                 self.inv_token_sample_map[knns[i, j]]]

        locality = torch.from_numpy(locality).cuda()
        # (T_reducedxB)xK
        dists = torch.from_numpy(dists).cuda()
        start = time.time()
        dists = dist_func(dists, knns, queries[tgt != pad_idx, :], function=self.sim_func)
        probs = utils.log_softmax(dists + 5000 * locality, dim=-1)

        index_mask = torch.eq(torch.from_numpy(self.vals[knns]).long().cuda().squeeze(-1),
                              tgt[tgt != pad_idx].unsqueeze(-1)).float()
        index_mask[index_mask == 0] = -10000  # for stability
        index_mask[index_mask == 1] = 0

        # (T_reducedxB)
        yhat_knn_prob = torch.logsumexp(probs + index_mask, dim=-1).clone()
        full_yhat_knn_prob = torch.full([qshape[0] * qshape[1]], -10000).cuda()
        full_yhat_knn_prob[tgt != pad_idx] = yhat_knn_prob

        # TxBx1
        return full_yhat_knn_prob.view(qshape[0], qshape[1], 1)