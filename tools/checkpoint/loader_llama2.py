# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.

import json
import os
import sys
import torch
import types

# >>>
from lutil import pax, tp
# <<<

def add_arguments(parser):
    group = parser.add_argument_group(title='Llama-2 loader')

    group.add_argument('--true-vocab-size', type=int, default=None,
                       help='original size of vocab, if specified will trim padding from embedding table.')
    group.add_argument('--vocab-file', type=str, default=None,
                       help='Path to the vocab file. If specified will use this to get vocab size and '
                       'trim padding from the embedding table.')
    group.add_argument('--tokenizer-model', required=True,
                       help='Sentencepiece tokenizer model.')
    group.add_argument('--megatron-path', type=str, default=None,
                       help='Base directory of deepspeed repository')

def load_args_from_checkpoint(args):

    # Read Llama args.
    llama_args_path = os.path.join(args.load, "params.json")
    with open(llama_args_path) as f:
        llama_args = json.load(f)

    # pt_path = os.path.join(args.load, "

    # Update Megatron args.
    args.seq_length = 4096
    args.max_position_embeddings = 4096
    args.hidden_size = llama_args["dim"]
    # args.make_vocab_size_divisible_by = llama_args["multiple_of"]
    args.num_attention_heads = llama_args["n_heads"]
    args.num_layers = llama_args["n_layers"]
    args.global_batch_size = 1024
    args.layernorm_epsilon = llama_args["norm_eps"]
    args.iteration = 0
    args.add_position_embedding = False
    args.use_rotary_position_embeddings = True
    # args.rotary_percent = 0.5
    args.swiglu = True
    args.tokenizer_type = "Llama2"
    # args.tokenizer_type = "SentencePieceTokenizer"

    args.vocab_size = -1 # 32000 # ... set from tokenizer
    args.padded_vocab_size = -1 # 32000 # ... set from tokenizer
    # args.llama_num_kv_heads = llama_args["n_kv_heads"]
    # args.llama_ffn_dim_multiplier = llama_args["ffm_dim_multiplier"]
    # args.llama_multiple_of = llama_args["multiple_of"]
    args.llama = llama_args

    ffn_dim_multiplier = llama_args["ffn_dim_multiplier"]
    ffn_multiple_of = llama_args["multiple_of"]
    ffn_hidden_size = 4 * int(2 * args.hidden_size / 3)
    if ffn_dim_multiplier is not None:
        ffn_hidden_size = int(ffn_dim_multiplier * ffn_hidden_size)
    ffn_hidden_size = ffn_multiple_of * ((ffn_hidden_size + ffn_multiple_of - 1) // ffn_multiple_of)
    args.ffn_hidden_size = ffn_hidden_size

    # >>>
    # pax({
    #     "ffn_dim_multiplier" : ffn_dim_multiplier,
    #     "ffn_multiple_of" : ffn_multiple_of,
    #     "ffn_hidden_size" : ffn_hidden_size,
    # })
    # <<<

    model_name = os.path.basename(args.load).split("-")[-1]
    args.tensor_model_parallel_size = {
        "7b" : 1,
        "13b" : 2,
        "70b" : 8,
    }[model_name]
    args.pipeline_model_parallel_size = 1

    # >>>
    # pax({"args": args})
    # <<<

def load_vocab_size(args):
    from megatron.tokenizer import build_tokenizer
    tokenizer = build_tokenizer(args)
    args.vocab_size = tokenizer.vocab_size

def set_preprocess_state(model, state_dict):

    param = state_dict["tok_embeddings.weight"]
    model = model.language_model.embedding.word_embeddings

    pax({
        # "model" : model,
        # "state_dict" : list(state_dict.keys()),
        "model" : model,
        "model / weight" : tp(model.weight.clone()),
        "params" : tp(param),
    })

def set_attn_state(layer, layer_state_dict):

    attn = layer.self_attention
    attn_state_dict = {k:v for k,v in layer_state_dict.items() if k.startswith("attention.")}

    pax({
        "attn" : attn,
        "query_key_value" : str(attn.query_key_value.weight.shape),
        "dense" : str(attn.dense.weight.shape),
        "attn_state_dict": {k:str(v.shape) for k,v in attn_state_dict.items()},
    })

def set_layer_state(model, model_state_dict, layer_idx):

    layer = model.language_model.encoder.layers[layer_idx]

    layer_state_dict = {".".join(k.split(".")[2:]):v
                        for k,v in model_state_dict.items()
                        if k.startswith(f"layers.{layer_idx}")}
    layer_state_dict["rope.freqs"] = model_state_dict["rope.freqs"]

    set_attn_state(layer, layer_state_dict)
    set_mlp_state(layer, layer_state_dict)
    set_rmsnorm_state(layer.input_layernormm)
    set_rmsnorm_state(layer.post_attention_layernormm)

    pax({
        "layer" : layer,
        "layer / mlp" : layer.mlp,
        "layer / mlp / h->4h" : tp(layer.mlp.dense_h_to_4h.weight.clone()),
        "layer / mlp / 4h->h" : tp(layer.mlp.dense_4h_to_h.weight.clone()),
        "layer_state_dict" : {k:str(v.shape) for k,v in layer_state_dict.items()},
    })

def load_checkpoint_to_model(args, rank, model):

    # Load state dict.
    filename = os.path.join(args.load, f"consolidated.0{rank}.pth")
    assert os.path.isfile(filename), f"missing checkpoint file '{filename}'."
    state_dict = torch.load(filename)

    # Set model state.
    # ... set_preprocess_state(model, state_dict)
    # ... set_postprocess_state(model, state_dict)
    for layer_idx in range(args.num_layers):
        set_layer_state(model, state_dict, layer_idx)

    pax({
        "args" : args,
        "rank" : rank,
        "model" : model,
        "filename" : filename,
        "state_dict" : list(state_dict.keys()),
    })

def _load_checkpoint(queue, args):

    # Search in directory above this
    sys.path.append(os.path.abspath(
        os.path.join(os.path.dirname(__file__),
                     os.path.pardir,
                     os.path.pardir)))
    if args.megatron_path is not None:
        sys.path.insert(0, args.megatron_path)

    try:
        from megatron.arguments import parse_args, validate_args
        from megatron.global_vars import set_args, set_global_variables
        # from megatron.checkpointing import load_args_from_checkpoint, load_checkpoint
        from megatron.model import module
        from megatron.core import mpu
        from megatron.core.enums import ModelType
        from megatron import fused_kernels
    except ModuleNotFoundError:
        print("Unable to import Megatron, please specify the path to Megatron using --megatron-path. Exiting.")
        queue.put("exit")
        exit(1)

    # We want all arguments to come from us
    sys.argv = ['script.py',
                '--no-masked-softmax-fusion',
                '--no-bias-gelu-fusion',
                '--no-bias-dropout-fusion',
                '--no-async-tensor-model-parallel-allreduce',
                '--use-cpu-initialization',
                '--micro-batch-size', '1',
                '--no-load-optim',
                '--no-load-rng',
                '--no-save-optim',
                '--no-save-rng',
                '--no-initialization',
                '--load', args.load_dir
                ]

    margs = parse_args()
    margs.tokenizer_model = args.tokenizer_model
    load_args_from_checkpoint(margs)
    load_vocab_size(margs)

    # Arguments do sanity checks on the world size, but we don't care,
    # so trick it into thinking we are plenty of processes
    margs.world_size = margs.tensor_model_parallel_size * margs.pipeline_model_parallel_size

    margs = validate_args(margs)

    def check_for_arg(arg_name, default=None):
        if getattr(margs, arg_name, None) is None:
            if default is not None:
                setattr(margs, arg_name, default)
            else:
                print(f"Checkpoint does not specify the argument {arg_name}. Exiting.")
                print(f"Arguments: {margs}")
                queue.put("exit")
                exit(1)

    check_for_arg('tensor_model_parallel_size')
    check_for_arg('pipeline_model_parallel_size')
    check_for_arg('num_layers')
    check_for_arg('hidden_size')
    check_for_arg('seq_length')
    check_for_arg('num_attention_heads')
    check_for_arg('max_position_embeddings')
    check_for_arg('position_embedding_type')
    check_for_arg('tokenizer_type')
    check_for_arg('iteration')
    check_for_arg('bert_binary_head')
    check_for_arg('disable_bias_linear', False)
    check_for_arg('params_dtype')
    check_for_arg('swiglu', False)

    # Determine how to make our models
    assert args.model_type == 'GPT', 'Llama-2 is a GPT model.'
    from pretrain_gpt import model_provider
    margs.model_type = ModelType.encoder_or_decoder

    # suppress warning about torch.distributed not being initialized
    module.MegatronModule.embedding_warning_printed = True

    def get_models(count, dtype):
        models = []
        for rank in range(count):
            mpu.set_tensor_model_parallel_rank(rank)
            model = model_provider(True, True).to(dtype)
            load_checkpoint_to_model(margs, rank, model)
            models.append(model)
            pax({"model": model})
        return models

    set_global_variables(margs, build_tokenizer=False)
    mpu.set_tensor_model_parallel_world_size(margs.tensor_model_parallel_size)
    mpu.set_pipeline_model_parallel_world_size(margs.pipeline_model_parallel_size)
    mpu.set_virtual_pipeline_model_parallel_world_size(margs.virtual_pipeline_model_parallel_size)
    fused_kernels.load(margs)

    # >>>
    # # Get true (non-padded) vocab size
    # if args.true_vocab_size is not None:
    #     true_vocab_size = args.true_vocab_size
    # elif args.vocab_file is not None:
    #     vocab = json.load(open(args.vocab_file))
    #     true_vocab_size = len(vocab)
    #     if args.true_vocab_size is not None and true_vocab_size != args.true_vocab_size:
    #         print("Both --true-vocab-size and --vocab-file specified and the vocab size does not match, aborting.")
    #         queue.put("exit")
    #         exit(1)
    # else:
    #     true_vocab_size = None
    # <<<

    # short aliases
    tp_size = margs.tensor_model_parallel_size
    pp_size = margs.pipeline_model_parallel_size
    vp_size = margs.virtual_pipeline_model_parallel_size
    if vp_size is None:
        vp_size = 1

    # metadata
    md = types.SimpleNamespace()
    md.model_type = args.model_type
    md.num_layers = margs.num_layers
    md.hidden_size = margs.hidden_size
    md.seq_length = margs.seq_length
    md.num_attention_heads = margs.num_attention_heads
    md.max_position_embeddings = margs.max_position_embeddings
    md.tokenizer_type = margs.tokenizer_type
    md.iteration = margs.iteration
    md.params_dtype = margs.params_dtype
    md.bert_binary_head = margs.bert_binary_head
    md.output_layer = margs.untie_embeddings_and_output_weights
    md.position_embedding_type = margs.position_embedding_type
    md.linear_bias = margs.add_bias_linear
    md.swiglu = margs.swiglu
    md.previous_tensor_parallel_size = margs.tensor_model_parallel_size
    md.previous_pipeline_parallel_size = margs.pipeline_model_parallel_size
    # md.true_vocab_size = true_vocab_size
    # md.make_vocab_size_divisible_by = margs.make_vocab_size_divisible_by
    # md.checkpoint_args = checkpoint_args

    # pax({"margs": margs, "md": md, "tp_size": tp_size})

    # Get first pipe stage
    mpu.set_pipeline_model_parallel_rank(0)
    all_models = [get_models(tp_size, md.params_dtype)]
    models = all_models[0][0]

    pax({"models": models})

    md.consumed_train_samples = 0 # consumed_train_samples
    md.consumed_valid_samples = 0 # consumed_valid_samples
    queue.put(md)

    def queue_put(name, msg):
        print(f"sending {name}")
        msg["name"] = name
        queue.put(msg)

    # Send embeddings
    message = {
        "word embeddings": torch.cat(
            [models[tp_rank].language_model.embedding.word_embeddings.weight.data for tp_rank in range(tp_size)],
            dim = 0)
    }
    if md.position_embedding_type == 'learned_absolute':
        message["position embeddings"] = models[0].language_model.embedding.position_embeddings.weight.data
    else:
        assert not hasattr(models[0].language_model.embedding, 'position_embeddings')

    queue_put("embeddings", message)

    total_layer_num = 0
    for vp_rank in range(vp_size):
        mpu.set_virtual_pipeline_model_parallel_rank(vp_rank)
        for pp_rank in range(pp_size):
            if pp_rank > 0:
                mpu.set_pipeline_model_parallel_rank(pp_rank)
                if vp_rank == 0:
                    all_models.append(get_models(tp_size, md.params_dtype))
            models = all_models[pp_rank][vp_rank]
            for layer_num in range(len(models[0].language_model.encoder.layers)):
                message = {}

                # Get non-parallel tensors from tp_rank 0
                layer = models[0].language_model.encoder.layers[layer_num]
                message["input layernorm weight"] = layer.input_layernorm.weight.data
                message["input layernorm bias"] = layer.input_layernorm.bias.data
                message["post layernorm weight"] = layer.post_attention_layernorm.weight.data
                message["post layernorm bias"] = layer.post_attention_layernorm.bias.data
                if md.linear_bias:
                    message["dense bias"] = layer.self_attention.dense.bias.data
                    message["mlp l1 bias"] = layer.mlp.dense_4h_to_h.bias.data

                # Grab all parallel tensors for this layer
                qkv_weight = []
                qkv_bias = []
                dense_weight = []
                mlp_l0_weight = []
                mlp_l0_bias = []
                mlp_l1_weight = []
                for tp_rank, model in enumerate(models):
                    layer = model.language_model.encoder.layers[layer_num]
                    qkv_weight.append(layer.self_attention.query_key_value.weight.data)
                    dense_weight.append(layer.self_attention.dense.weight.data)
                    mlp_l0_weight.append(layer.mlp.dense_h_to_4h.weight.data)
                    mlp_l1_weight.append(layer.mlp.dense_4h_to_h.weight.data)
                    if md.linear_bias:
                        qkv_bias.append(layer.self_attention.query_key_value.bias.data)
                        mlp_l0_bias.append(layer.mlp.dense_h_to_4h.bias.data)

                # Handle gated linear units
                if md.swiglu:
                    # concat all the first halves ('W's) and all the second halves ('V's)
                    for tp_rank in range(tp_size):
                        mlp_l0_weight[tp_rank] = torch.chunk(mlp_l0_weight[tp_rank], 2, dim=0)
                    message["mlp l0 weight W"] = torch.cat([w[0] for w in mlp_l0_weight], dim=0)
                    message["mlp l0 weight V"] = torch.cat([w[1] for w in mlp_l0_weight], dim=0)
                else:
                    message["mlp l0 weight"] = torch.cat(mlp_l0_weight, dim=0)

                # simple concat of the rest
                message["qkv weight"] = torch.cat(qkv_weight, dim=0)
                message["dense weight"] = torch.cat(dense_weight, dim=1)
                message["mlp l1 weight"] = torch.cat(mlp_l1_weight, dim=1)
                if md.linear_bias:
                    message["qkv bias"] = torch.cat(qkv_bias, dim=0)
                    if md.swiglu:
                        for tp_rank in range(tp_size):
                            mlp_l0_bias[tp_rank] = torch.chunk(mlp_l0_bias[tp_rank], 2, dim=0)
                        message["mlp l0 bias W"] = torch.cat([b[0] for b in mlp_l0_bias],dim=0)
                        message["mlp l0 bias V"] = torch.cat([b[1] for b in mlp_l0_bias],dim=0)
                    else:
                        message["mlp l0 bias"] = torch.cat(mlp_l0_bias, dim=0)

                queue_put(f"transformer layer {total_layer_num}", message)

                total_layer_num = total_layer_num + 1

    # Send final layernorm from tp_rank 0
    message = {
        "weight": models[0].language_model.encoder.final_layernorm.weight.data,
        "bias": models[0].language_model.encoder.final_layernorm.bias.data
    }
    queue_put("final layernorm", message)

    if md.output_layer:
        message = {
            "weight": torch.cat(
                [models[tp_rank].language_model.output_layer.weight.data for tp_rank in range(tp_size)],
                dim = 0)
        }
        queue_put("output layer", message)


    # Send BERT lm head and binary head if it exists
    if md.model_type == 'BERT':
        message = {
            "weight": models[0].language_model.pooler.dense.weight.data,
            "bias": models[0].language_model.pooler.dense.bias.data
        }
        queue_put("pooler", message)

        message = {
            "dense weight": models[0].lm_head.dense.weight.data,
            "dense bias": models[0].lm_head.dense.bias.data,
            "layernorm weight": models[0].lm_head.layernorm.weight.data,
            "layernorm bias": models[0].lm_head.layernorm.bias.data
        }
        queue_put("lm head", message)

        if md.bert_binary_head:
            message = {
                "weight": models[0].binary_head.weight.data,
                "bias": models[0].binary_head.bias.data
            }
            queue_put("binary head", message)
    queue.put("done")

def load_checkpoint(queue, args):
    try:
        _load_checkpoint(queue, args)
    except:
        queue.put("exit")
        raise