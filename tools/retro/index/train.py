# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.

import h5py
import numpy as np
import os
import shutil
import torch
from tqdm import tqdm

from megatron import get_retro_args, print_rank_0
from tools.bert_embedding import DiskDataParallelBertEmbedder
from tools.retro.db.utils import (
    get_indexed_dataset_infos,
    get_merged_sampled_dataset,
)
from tools.retro.index.factory import IndexFactory
from tools.retro.utils import GPTToTextDataset

from .utils import (
    get_training_data_dir,
    get_training_data_merged,
)


def get_empty_index_path():
    '''Path of empty index.'''
    args = get_retro_args()
    index = IndexFactory.get_index(args.retro_index_type)
    empty_index_path = index.get_empty_index_path()
    return empty_index_path


def embed_db():
    '''Embed DB chunks.

    Store chunks in blocks on disk. These blocks will later be merged into
    a single dataset for training the index.
    '''

    # Embed only if index not already trained.
    empty_index_path = get_empty_index_path()
    if os.path.isfile(empty_index_path):
        return

    args = get_retro_args()

    # Get db dataset.
    gpt_dataset = get_merged_sampled_dataset()
    text_dataset = GPTToTextDataset(gpt_dataset)

    # Embed dataset.
    embedder = DiskDataParallelBertEmbedder(args.retro_bert_batch_size,
                                            args.retro_bert_max_chunk_length,
                                            args.retro_block_size,
                                            args.bert_embedder_type)
    embedder.embed_text_dataset("index", get_training_data_dir(), text_dataset)


def train_on_embeddings():
    '''Train index on embedded DB chunks.'''
    args = get_retro_args()
    index = IndexFactory.get_index(args.retro_index_type)
    index.train(get_training_data_merged)


def remove_embeddings():
    '''Remove embeddings after training.'''
    torch.distributed.barrier()
    if torch.distributed.get_rank() != 0:
        return
    empty_index_path = get_empty_index_path()
    assert os.path.isfile(empty_index_path)
    shutil.rmtree(get_training_data_dir())


def train_index():
    '''Train index on DB chunks.'''
    embed_db()
    train_on_embeddings()
    remove_embeddings()
