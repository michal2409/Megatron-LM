# lawrence mcafee

# ~~~~~~~~ import ~~~~~~~~

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# def test_knn():
def query_flat_nns():

    import faiss # conda install -c conda-forge -y faiss-gpu
    from scipy.spatial import KDTree
    import time
    from types import SimpleNamespace

    # ~~~~~~~~ arg ~~~~~~~~
    args = SimpleNamespace()
    args.dim = 1024
    args.ntrain = int(1e8)
    args.ntest = int(1e4)
    args.nnbr = 2000
    args.base_dir = "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retrieval"

    # pax({"args": args})

    time_map = {}

    # ~~~~~~~~ data ~~~~~~~~
    if 0:

        t = time.time()
        # train_data = np.random.rand(ntrain, dim)
        # train_data = np.zeros((ntrain, dim))
        train_data = np.empty((ntrain, dim), dtype = "f4")
        bs = 1000000
        for i0 in range(0, ntrain, bs):
            i1 = min(ntrain, i0 + bs)
            train_data[i0:i1, :] = np.random.rand(i1 - i0, dim)
            print("train data %d:%d / %d." % (i0, i1, ntrain), flush = True)
        time_map["train_data"] = time.time() - t
        print("time / train_data = %f." % time_map["train_data"], flush = True)

        t = time.time()
        test_data = np.random.rand(ntest, dim).astype("f4")
        time_map["test_data"] = time.time() - t
        print("time / test_data = %f." % time_map["test_data"], flush = True)

    else:

        from ..data.load_train_data import load_train_data

        t = time.time()
        train_data = load_train_data(args)
        time_map["train_data"] = time.time() - t
        print("time / train_data = %f." % time_map["train_data"], flush = True)

        t = time.time()
        test_data = train_data[:args.ntest]
        time_map["test_data"] = time.time() - t
        print("time / test_data = %f." % time_map["test_data"], flush = True)

        # pax({
        #     "train_data" : str(train_data.shape),
        #     "test_data" : str(test_data.shape),
        # })

    # ~~~~~~~~ index ~~~~~~~~
    if 0:

        t = time.time()
        kdtree = KDTree(train_data)
        time_map["init_kdtree"] = time.time() - t
        print("time / init_kdtree = %f." % time_map["init_kdtree"], flush = True)

        t = time.time()
        if 1:
            nns = kdtree.query(test_data, k = nnbr, workers = -1)
        else:
            nns = None
        time_map["query_kdtree"] = time.time() - t
        print("time / query_kdtree = %f." % time_map["query_kdtree"], flush = True)

    else:

        t = time.time()
        index = faiss.IndexFlatL2(args.dim)
        # index = faiss.index_cpu_to_all_gpus(index) # ... oom
        faiss.ParameterSpace().set_index_parameter(index, "verbose", 1)
        time_map["transfer_index"] = time.time() - t
        print("time / transfer_index = %f." % time_map["transfer_index"], flush = True)

        t = time.time()
        index.train(train_data)
        time_map["train_index"] = time.time() - t
        print("time / train_index = %f." % time_map["train_index"], flush = True)

        t = time.time()
        index.add(train_data)
        time_map["add_index"] = time.time() - t
        print("time / add_index = %f." % time_map["add_index"], flush = True)

        t = time.time()
        D, I = index.search(test_data, k = args.nnbr)
        time_map["query_index"] = time.time() - t
        print("time / query_index = %f." % time_map["query_index"], flush = True)

        # pax({"D": D, "I": I})

        t = time.time()
        path = "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retrieval/v2/n2000/Flat__t65191936__neighbors.hdf5"
        f = h5py.File(path, "w")
        f.create_dataset("neighbors", data = I)
        f.close()
        time_map["save_query"] = time.time() - t
        print("time / save_query = %f." % time_map["save_query"], flush = True)

    # ~~~~~~~~ status ~~~~~~~~
    print("%d, %d, %d ... %.1f [ %s ]." % (
        args.ntrain,
        args.ntest,
        args.dim,
        sum(time_map.values()),
        ", ".join([ "%s %.1f" % (k, v) for k, v in time_map.items() ]),
    ), flush = True)
    exit(0)
    pax({
        "train_data" : str(train_data.shape),
        "test_data" : str(test_data.shape),
        "kdtree" : kdtree,
        "nns" : nns,
        "time_map" : time_map,
    })

# eof
