# lawrence mcafee

# ~~~~~~~~ import ~~~~~~~~
import subprocess

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# subprocess.run(["pwd"])
subprocess.run([
    "python",
    "generation/generate_embeddings_bert_hdf5.py",
    "--device",
    "0",
    "--input",
    # "/gpfs/fs1/projects/gpu_adlr/datasets/boxinw/processed_data/chunks/Wikipedia_en_ftfy_id_shuf_text_document.chunks.hdf5",
    "/gpfs/fs1/projects/gpu_adlr/datasets/boxinw/processed_data/chunks/sampled_pretraining/sampled_pretraining_corpus.chunks.hdf5.0000.feat.hdf5",
    "--output",
    # "/gpfs/fs1/projects/gpu_adlr/datasets/boxinw/processed_data/chunks/Wikipedia_en_ftfy_id_shuf_text_document.chunks.hdf5.0000.feat.hdf5",
    "/gpfs/fs1/projects/gpu_adlr/datasets/lmcafee/retrieval/bert/0000.feat.hdf5",
    "--bs",
    "128",
    "--split",
    "16",
    "--pointer",
    "0",
])

# eof
