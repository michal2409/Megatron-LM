The Llama-2 [family of models](https://ai.meta.com/llama/) are an open-source set of pretrained & fine-tuned (for chat) models that have achieved strong results across a wide set of benchmarks. At the time of release, Llama-2 models achieved among the best results for open-source models, and were competitive with the closed-source GPT-3.5 model (see https://arxiv.org/pdf/2307.09288.pdf).

Llama-2 checkpoints can be loaded into Megatron for inference and for fine-tuning. Loading these checkpoints consists of three steps:

1. Get access to download the checkpoints.
2. Convert the checkpoints from Llama-2 format to Megatron format.
3. Setup arguments for launching the model.

The following sections detail these steps.

# Contents
  * [Llama-2 Inference](#llama-2-inference)
    * [Checkpoint Conversion](#llama-2-checkpoint-conversion)
    * [Inference](#llama-2-launch-inference)

# [Download checkpoints](#llama-2-download-checkpoints)

Users must first apply for access to download the Llama-2 checkpoints either directly from [Meta](https://ai.meta.com/resources/models-and-libraries/llama-downloads/) or through [Huggingface](https://huggingface.co/docs/transformers/main/model_doc/llama2) (HF). The checkpoints are available in two formats, native Llama-2 format (available from both the Meta and HF links), and HF format (available only from HF). Either format can be used for converting to Megatron, as detailed next.

# [Checkpoint Conversion](#llama-2-checkpoint-conversion)

Depending on which checkpoint format is downloaded (native Llama-2 or HF), one or two steps must be taken to convert to Megatron format.

1. Native Llama-2 format: The native Llama-2 checkpoints must first be converted to HF format before converting to Megatron format. The `transformers` package is required for the first step, and must have version >=4.31.0 (e.g., `pip install transformers>=4.31.0`). Assuming the downloaded checkpoints are in `$CHECKPOINT_DIR` (with separate sub-directories for 7B, 13B, 70B, etc.), the following example command can be used to convert from Llama-2 format to HF format:

`$> python $LIB_DIR/transformers/models/llama/convert_llama_weights_to_hf.py --input_dir $LLAMA_FORMAT_DIR --output_dir $HF_FORMAT_DIR --model_size 7B`

Valid values for `--model_size` include `7B`, `13B`, and `70B` (for pretrained-only models), and `7Bf`, `13Bf`, and `70Bf` (for chat-finetuned models). Use `python convert_llama_weights_to_hf.py --help` for additional argument details. Once the checkpoints have been converted to HF format, proceed to step 2.

2. HF format: The HF checkpoints can be converted to Megatron format by using Megatron's own Llama-2 checkpoint converter for HF format (see script `tools/checkpoint/loader_llama2_hf.py`).

# [Inference](#llama-2-launch-inference)

in ...
