# lawrence mcafee

? ? ?

# ~~~~~~~~ import ~~~~~~~~
import faiss

from lutil import pax

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def move_index_to_gpu(index):

    # ~~~~~~~~ move ~~~~~~~~
    index_ivf = faiss.extract_index_ivf(index)
    clustering_index = faiss.index_cpu_to_all_gpus(faiss.IndexFlatL2(index_ivf.d))
    index_ivf.clustering_index = clustering_index

    # ~~~~~~~~ verbose ~~~~~~~~
    faiss.ParameterSpace().set_index_parameter(index, "verbose", 1)
    # index.verbose = True # ... maybe?

    # ~~~~~~~~ debug ~~~~~~~~
    # pax({"index": index})

    # ~~~~~~~~ return ~~~~~~~~
    return index

# eof
