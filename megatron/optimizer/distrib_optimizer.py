# >>>
# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
# <<<

"""Megatron distributed optimizer."""


# >>>
from collections import defaultdict
# <<<
import math
import torch

from megatron import get_args
from megatron import get_timers
from megatron import print_rank_0
from megatron.core import mpu, tensor_parallel
from megatron.model.module import param_is_not_shared

from .optimizer import MixedPrecisionOptimizer, _zero_grad_group_helper

# >>>
from lutil import pax, print_seq, tp
# <<<


class Range:
    """
    A range represents a start and end points for indexing a shard
    from a full tensor.
    """
    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.size = end - start
    def normalize(self, start = 0):
        return Range(start, start + self.size)
    def __str__(self):
        return "%d,%d [%d]" % (self.start, self.end, self.size)


class DistributedOptimizer(MixedPrecisionOptimizer):
    """Distributed optimizer, for all data types (fp16, bf16, and fp32).

    Arguments:
        optimizer: base optimizer such as Adam or SGD
        clip_grad: clip gradeints with this global L2 norm. Note
            that clipping is ignored if clip_grad == 0
        log_num_zeros_in_grad: return number of zeros in the gradients.
        params_have_main_grad: flag indicating if parameters have
            a `main_grad` field. If this is set, we are assuming
            that the model parameters are store in the `main_grad`
            field instead of the typical `grad` field. This happens
            for the DDP cases where there is a continuous buffer
            holding the gradients. For example for bfloat16, we want
            to do gradient accumulation and all-reduces in float32
            and as a result we store those gradients in the main_grad.
            Note that main grad is not necessarily in float32.
        use_contiguous_buffers_in_local_ddp: if true, the local DDP model
            is using a contiguous buffer to hold the model grads.
        fp16: if true, the model is running in fp16.
        bf16: if true, the model is running in bfloat16.
        grad_scaler: used for scaling gradients. Note that this can be
            None. This case happens when `bf16 = True` and we don't
            use any loss scale. Note that for `bf16 = True`, we can have
            a constnat gradient scaler. Also for `bf16 = False`, we
            always require a grad scaler.
        models: list of models (i.e., the virtual pipelining models). This
            is used by the distributed optimizer for mapping parameters.
    """

    @classmethod
    def build_model_gbuf_param_range_map(cls, model, dtype, gbuf_world_range):
        """
        Build mapping from param reference to grad buffer shard ranges.

        This method builds a mapping from parameter references to grad
        buffer shard ranges, specific to each data-parallel (DP) rank's
        set of 'owned' parameters. Each grad buffer (padded to be an even
        multiple of DP-world-size) is conceptually divided into DP-world-size
        contiguous regions, where each DP rank 'owns' a contiguous regions.
        Ownership in this sense means DP rank is responsible for reducing
        the relevant subset of grads, and updating the relevant subset of
        params.

        This conceptual partitioning of the grad buffer does NOT respect
        parameter boundaries, and as such it is assumed that each created
        range references a shard (or subset) of the full parameter. It is
        easiest to think of each DP rank as operating (i.e., reducing,
        gathering) purely on views into the grad buffer, for all model-to-
        main & main-to-model operations.

        This method creates three ranges:
        - The param's range within the entire grad buffer (i.e., world index).
        - The param's range within the DP rank's local view of the grad buffer.
        - The param's range within itself (i.e., its shard).
        """

        # Param range map.
        param_world_index_map = model._grad_buffer_param_index_map[dtype]
        param_range_map = {}
        for param, param_world_indexes in param_world_index_map.items():

            # Param range.
            # >>>
            param_world_order, param_world_start, param_world_end = \
                param_world_indexes
            # <<<
            param_local_start = max(
                0,
                param_world_start - gbuf_world_range.start)
            param_local_end = min(
                gbuf_world_range.size,
                param_world_end - gbuf_world_range.start)

            # Add param, if within local gbuf range.
            if param_local_end > param_local_start:
                param_local_range = Range(param_local_start, param_local_end)
                param_world_range = param_local_range.normalize(
                    param_local_start + gbuf_world_range.start)
                sub_param_start = max(0, gbuf_world_range.start-param_world_start)
                sub_param_range = param_local_range.normalize(sub_param_start)
                # >>>
                param_range_map[param] = {
                    "gbuf_world_order" : param_world_order,
                    "gbuf_world" : param_world_range,
                    "gbuf_local" : param_local_range,
                    "param" : sub_param_range,
                }
                # <<<

        return param_range_map


    @classmethod
    def build_model_gbuf_range(cls, model, dtype):
        """
        Build mapping between params and their grad buffers.

        This method does the initial setup for the method above. This setup
        includes determining the shard ranges into the DDP's grad buffer for
        each data-parallel (DP) rank. Each DP rank keeps range info for
        all other DP ranks, for the purpose of creating args for
        reduce-scatter and all-gather.
        """

        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_world_size = mpu.get_data_parallel_world_size()

        # Grad buffer range.
        grad_buffer = model._grad_buffers[dtype]
        gbuf_size = grad_buffer.numel
        max_gbuf_range_size = int(math.ceil(gbuf_size / data_parallel_world_size))

        # All world ranges. (i.e., across all data parallel ranks)
        gbuf_world_all_ranges = []
        for r in range(data_parallel_world_size):
            gbuf_world_start = r * max_gbuf_range_size
            gbuf_world_end = min(gbuf_size, gbuf_world_start+max_gbuf_range_size)
            gbuf_world_range = Range(gbuf_world_start, gbuf_world_end)
            gbuf_world_all_ranges.append(gbuf_world_range)

        # Local DP's ranges.
        gbuf_world_range = gbuf_world_all_ranges[data_parallel_rank]
        gbuf_local_range = gbuf_world_range.normalize()

        # Get each param's ranges.
        param_range_map = cls.build_model_gbuf_param_range_map(model,
                                                               dtype,
                                                               gbuf_world_range)

        # Group into dict.
        data = {
            "local" : gbuf_local_range,
            "world" : gbuf_world_range,
            "world_all" : gbuf_world_all_ranges,
            "param_map" : param_range_map,
            "max_range_size" : max_gbuf_range_size,
        }

        return data


    @classmethod
    def build_model_gbuf_range_map(cls, model):
        """
        Create param-to-grad-buffer mappings, for grad buffer data types
        within a specific virtual model.
        """
        return {
            dtype : cls.build_model_gbuf_range(model, dtype)
            for dtype in model._grad_buffers
        }


    @classmethod
    def build_model_param_gbuf_map(cls, model_gbuf_ranges):
        """
        Create a reverse of the model_gbuf_ranges, for referencing in
        opposite direction.
        """
        param_gbuf_map = {}
        for model_index, model_gbuf_range_map in enumerate(model_gbuf_ranges):
            for dtype, gbuf_range_map in model_gbuf_range_map.items():
                for param, param_range_map in gbuf_range_map["param_map"].items():
                    param_gbuf_map[param] = (model_index, dtype)
        return param_gbuf_map


    @classmethod
    def build_optimizer_group_ranges(cls, param_groups, model_gbuf_ranges):
        """
        Create optimizer groups.

        Given the set of parameter shard ranges that are owned by the current
        data-parallel (DP) rank, gather the set of parameters that will be
        used (in the method below) to create the current DP's optimizer
        groups.
        """

        num_groups = len(param_groups)

        # Param group map.
        world_param_group_map = {}
        for group_index, group in enumerate(param_groups):
            for param in group["params"]:
                assert param.requires_grad
                world_param_group_map[param] = group_index

        # Optimizer group ranges.
        # >>>
        local_param_group_map = {}
        # <<<
        group_ranges = [ {"params": []} for _ in param_groups ]
        for model_gbuf_range_map in model_gbuf_ranges:
            for dtype, gbuf_range_map in model_gbuf_range_map.items():
                for param in gbuf_range_map["param_map"]:
                    group_index = world_param_group_map[param]
                    group_range = group_ranges[group_index]
                    group_range["params"].append(param)
                    # >>>
                    local_param_group_map[param] = \
                        (group_index, len(group_range["params"]) - 1)
                    # <<<

        # Squeeze zero-size group ranges.
        for group_index, group_range in enumerate(group_ranges):
            group_range["orig_group"] = param_groups[group_index]
        group_ranges = [ g for g in group_ranges if len(g["params"]) > 0 ]

        # >>>
        # pax(0, {
        #     "param_groups" : param_groups,
        #     **{"group_ranges / %d" % i : r for i, r in enumerate(group_ranges)},
        # })
        # pax(0, {
        #     "local_param_group_map / values" :
        #     list(local_param_group_map.values()),
        # )})
        # <<<

        # >>>
        # return group_ranges
        # return param_group_map, group_ranges
        return local_param_group_map, group_ranges
        # <<<


    @classmethod
    def build_model_and_main_param_groups(cls,
                                          model_gbuf_ranges,
                                          param_gbuf_map,
                                          opt_group_ranges):
        """
        Create main parameter groups needed for the optimizer step.

        These groups encompass both: 1) groups used by this class, for
        reducing/gather, and 2) groups used by the inner optimizer for the
        parameter update. Given that the conceptual grad buffer partitioning
        (created in earlier method) doesn't respect parameter boundaries,
        the optimizer operates on shards of the model parameters, rather than
        the full parameters.
        """

        # Parameter groups:
        #   model_float16_groups: original float16 parameters
        #   model_fp32_groups: original fp32 parameters
        #   shard_float16_groups: shards of original float16 parameters
        #   shard_fp32_groups: shards of original fp32 parameters
        #   shard_fp32_from_float16_groups: fp32 copy of float16 parameters
        model_float16_groups = []
        model_fp32_groups = []
        shard_float16_groups = []
        shard_fp32_groups = []
        shard_fp32_from_float16_groups = []

        # Allocate (or slice) each group's param shard.
        for group_index, group_range in enumerate(opt_group_ranges):

            # Params of this group.
            model_float16_params_this_group = []
            model_fp32_params_this_group = []
            shard_float16_params_this_group = []
            shard_fp32_params_this_group = []
            shard_fp32_from_float16_params_this_group = []
            model_float16_groups.append(model_float16_params_this_group)
            model_fp32_groups.append(model_fp32_params_this_group)
            shard_float16_groups.append(shard_float16_params_this_group)
            shard_fp32_groups.append(shard_fp32_params_this_group)
            shard_fp32_from_float16_groups.append(
                shard_fp32_from_float16_params_this_group)

            for model_param in group_range["params"]:

                assert model_param.requires_grad

                model_index, dtype = param_gbuf_map[model_param]
                gbuf_range = model_gbuf_ranges[model_index][dtype]
                param_range = gbuf_range["param_map"][model_param]["param"]

                # fp16, bf16 params.
                if model_param.type() in ['torch.cuda.HalfTensor',
                                          'torch.cuda.BFloat16Tensor']:

                    # Clone model -> main.
                    shard_model_param = model_param.detach().view(-1) \
                        [param_range.start:param_range.end]
                    shard_main_param = shard_model_param.clone().float()
                    tensor_parallel.copy_tensor_model_parallel_attributes(
                        shard_model_param, model_param)
                    tensor_parallel.copy_tensor_model_parallel_attributes(
                        shard_main_param, model_param)
                    if hasattr(model_param, 'shared'):
                        shard_model_param.shared = model_param.shared
                        shard_main_param.shared = model_param.shared

                    # Add to group.
                    model_float16_params_this_group.append(model_param)
                    shard_float16_params_this_group.append(shard_model_param)
                    shard_fp32_from_float16_params_this_group.append(shard_main_param)

                # fp32 params.
                elif model_param.type() == 'torch.cuda.FloatTensor':
                    shard_model_param = model_param.view(-1) \
                        [param_range.start:param_range.end]
                    model_fp32_params_this_group.append(model_param)
                    shard_fp32_params_this_group.append(shard_model_param)
                    tensor_parallel.copy_tensor_model_parallel_attributes(
                        shard_model_param, model_param)
                    if hasattr(model_param, 'shared'):
                        shard_model_param.shared = model_param.shared

                else:
                    raise TypeError('Wrapped parameters must be one of '
                                    'torch.cuda.FloatTensor,  '
                                    'torch.cuda.HalfTensor, or '
                                    'torch.cuda.BFloat16Tensor. '
                                    'Received {}'.format(param.type()))

            # Update optimizer's params.
            group_range["orig_group"]["params"] = [
                *shard_fp32_params_this_group,
                *shard_fp32_from_float16_params_this_group,
            ]

        return (
            model_float16_groups,
            model_fp32_groups,
            shard_float16_groups,
            shard_fp32_groups,
            shard_fp32_from_float16_groups,
        )


    def __init__(self, optimizer, clip_grad, log_num_zeros_in_grad,
                 params_have_main_grad, use_contiguous_buffers_in_local_ddp,
                 fp16, bf16, params_dtype, grad_scaler, models):
        """
        See top of class definition for argument descriptions.

        The steps in this method create the core mapping between DDP grad
        buffers, parameters, and parameter shard ranges, that is needed for
        converting between model param indexes and main parameter shard
        indexes. This method also updates the optimizer parameter groups
        with the newly created shards.
        """

        super().__init__(
            optimizer, clip_grad, log_num_zeros_in_grad,
            params_have_main_grad, use_contiguous_buffers_in_local_ddp,
            fp16, bf16, params_dtype, grad_scaler, models)

        # Verify that contiguous buffers are being used.
        # - Note: this should already be checked in arguments.py.
        assert use_contiguous_buffers_in_local_ddp

        # Model grad buffer ranges.
        self.model_gbuf_ranges = []
        for model_index, model in enumerate(self.models):
            self.model_gbuf_ranges.append(self.build_model_gbuf_range_map(model))
        self.model_param_gbuf_map = \
            self.build_model_param_gbuf_map(self.model_gbuf_ranges)

        # Optimizer ranges.
        # self.opt_group_ranges = self.build_optimizer_group_ranges(
        self.model_param_group_index_map, self.opt_group_ranges = \
            self.build_optimizer_group_ranges(self.optimizer.param_groups,
                                              self.model_gbuf_ranges)
        
        # Allocate main param shards.
        (
            self.model_float16_groups,
            self.model_fp32_groups,
            self.shard_float16_groups,
            self.shard_fp32_groups,
            self.shard_fp32_from_float16_groups,
        ) = self.build_model_and_main_param_groups(self.model_gbuf_ranges,
                                                   self.model_param_gbuf_map,
                                                   self.opt_group_ranges)

        # Initialize param buffers.
        # - These are views on the DDP model's grad buffers, that share
        #   storage & have their own dtype. This is safe because the param
        #   dtype size is always <= grad dtype size.
        self.param_buffers = []
        for model_index, model in enumerate(self.models):
            current_param_buffers = {}
            for dtype, grad_buffer in model._grad_buffers.items():
                # >>>
                # param_buffer=torch.tensor(grad_buffer.data.storage()._untyped(),
                param_buffer = torch.tensor(grad_buffer.data.storage().untyped(),
                                            dtype = params_dtype,
                                            device = grad_buffer.data.device)
                # <<<
                param_buffer = param_buffer[:grad_buffer.numel_padded]
                current_param_buffers[dtype] = param_buffer
            self.param_buffers.append(current_param_buffers)

        # Update optimizer groups.
        # - Also, leverage state_dict() and load_state_dict() to
        #   recast preexisting per-param state tensors.
        self.optimizer.param_groups = \
            [ g["orig_group"] for g in self.opt_group_ranges ]
        self.optimizer.load_state_dict(self.optimizer.state_dict())


    def get_model_param_range_map(self, param):
        """
        Given a model param, get the index sub-range of the param that this
        data-parallel rank owns.
        """
        model_index, dtype = self.model_param_gbuf_map[param]
        gbuf_range_map = self.model_gbuf_ranges[model_index][dtype]
        param_range_map = gbuf_range_map["param_map"][param]
        return param_range_map


    def get_model_parallel_group(self):
        """
        With the distributed optimizer, the model parallel group is the
        entire world.
        """
        return None


    # def state_dict(self):
    #     """
    #     The state dict must contain the fp32-from-float16 shards.
    #     """
    #     state_dict = {}
    #     state_dict['optimizer'] = self.optimizer.state_dict()
    #     if self.grad_scaler:
    #         state_dict['grad_scaler'] = self.grad_scaler.state_dict()
    #     state_dict['shard_fp32_from_float16_groups'] = \
    #         self.shard_fp32_from_float16_groups
    #     return state_dict
    # def state_dict(self):
    #     """
    #     The state dict must contain the fp32-from-float16 shards.
    #     """

    #     # >>>
    #     # state_dict = self.optimizer.state_dict()
    #     # pax(0, {
    #     #     **{"state_dict / %s" % k : v for k, v in state_dict.items()},
    #     #     ".. / state / 0" : state_dict["state"][0],
    #     #     ".. / param_groups / 0" : state_dict["param_groups"][0],
    #     # })
    #     # <<<

    #     default_optimizer_state_dict = self.optimizer.state_dict()

    #     # Common state dict (across DP ranks).
    #     common_state_dict = {
    #         "optimizer" : {
    #             "param_groups" : default_optimizer_state_dict["param_groups"],
    #         },
    #     }
    #     if self.grad_scaler:
    #         common_state_dict["grad_scaler"] = self.grad_scaler.state_dict()
    #         pax(1, {"grad_scaler": self.grad_scaler})

    #     # Data parallel state dict.
    #     dp_state_dict = {}
    #     for model_idx, gbuf_range_maps in enumerate(self.model_gbuf_ranges):
    #         dtype_state = {}
    #         for dtype, gbuf_range_map in gbuf_range_maps.items():
    #             param_state = {}
    #             for param_idx, (model_param, param_range_map) in \
    #                 enumerate(gbuf_range_map["param_map"].items()):

    #                 group_index, group_order = \
    #                     self.model_param_group_index_map[model_param]
    #                 world_order = param_range_map["gbuf_world_order"]

    #                 optim_param = self.optimizer.param_groups[group_index]["params"][group_order]
    #                 optim_state = self.optimizer.state[optim_param]

    #                 param_state[world_order] = {
    #                     "group_index" : group_index,
    #                     "group_order" : group_order,
    #                     "param_range_map" : param_range_map,
    #                     # "param" : self.shard_fp32_from_float16_groups[group_index][group_order],
    #                     "param" : optim_param,
    #                     "optim" : optim_state,
    #                 }

    #                 # pax(0, {
    #                 #     "optimizer" : self.optimizer,
    #                 #     "param_state / wo" : param_state[world_order],
    #                 # })

    #             dtype_state[dtype] = param_state
    #         dp_state_dict[model_idx] = dtype_state

    #     state_dict = {
    #         "common" : common_state_dict,
    #         "dp" : dp_state_dict,
    #     }

    #     # >>>
    #     # print_seq("opt/groups %d, state/groups %d." % (
    #     #     len(self.optimizer.param_groups),
    #     #     len(common_state_dict["optimizer"]["param_groups"]),
    #     # ))
    #     # pax(0, {"state_dict": state_dict})
    #     # <<<

    #     return state_dict
    # def save_state(self):
    #     # _grad_buffer_param_index_map
    #     # for model_idx, gbuf_range_maps in enumerate(self.model_gbuf_ranges):
    #     #     for dtype, gbuf_range_map in gbuf_range_maps.items():
    #     #         for param_idx, (model_param, param_range_map) in \
    #     #             enumerate(gbuf_range_map["param_map"].items()):
    #     #     pax(0, {
    #     #         "model_idx" : model_idx,
    #     #         "gbuf_range_map" : gbuf_range_map,
    #     #     })

    #     data_parallel_world_size = mpu.get_data_parallel_world_size()
    #     data_parallel_rank = mpu.get_data_parallel_rank()
    #     data_parallel_group = mpu.get_data_parallel_group()
    #     data_parallel_global_ranks = list(mpu._DATA_PARALLEL_GLOBAL_RANKS)

    #     # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    #     # sent_tensor = torch.zeros((data_parallel_rank + 1,),
    #     sent_tensor = torch.zeros((torch.distributed.get_rank() + 1,),
    #                               dtype=torch.float32,
    #                               device="cuda")
    #     if data_parallel_rank == 0:
    #         recv_tensors = [torch.zeros((r+1,),
    #                                     dtype=torch.float32,
    #                                     device="cuda") for r in range(8)]
    #     else:
    #         recv_tensors = None

    #     torch.distributed.gather(
    #         sent_tensor,
    #         recv_tensors,
    #         0,
    #         data_parallel_group)

    #     raise Exception("test done.")
    #     # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

    #     for model_idx, model in enumerate(self.models):
    #         for dtype, param_index_map in \
    #             model._grad_buffer_param_index_map.items():

    #             gbuf_world_numel = model._grad_buffers[dtype].numel_padded
    #             gbuf_local_numel = int(gbuf_world_numel/data_parallel_world_size)

    #             for model_param, (
    #                     param_world_order,
    #                     param_world_start,
    #                     param_world_end,
    #             ) in param_index_map.items():

    #                 dp_rank_start = int(param_world_start // gbuf_local_numel)
    #                 dp_rank_end = int((param_world_end-1) // gbuf_local_numel) + 1

    #                 dp_rank_ranges = []
    #                 for dp_rank in range(dp_rank_start, dp_rank_end):

    #                     if dp_rank == dp_rank_start:
    #                         local_idx_start = \
    #                             param_world_start % gbuf_local_numel
    #                     else:
    #                         local_idx_start = 0

    #                     if dp_rank == dp_rank_end - 1:
    #                         local_idx_end = \
    #                             (param_world_end - 1) % gbuf_local_numel + 1
    #                     else:
    #                         local_idx_end = gbuf_local_numel

    #                     dp_rank_ranges.append((
    #                         dp_rank,
    #                         local_idx_start,
    #                         local_idx_end,
    #                     ))

    #                 # pax(0, {"dp_rank_ranges": dp_rank_ranges})

    #                 # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    #                 # try:
    #                 #     group_index, group_order = \
    #                 #         self.model_param_group_index_map[model_param]
    #                 #     main_param = self.optimizer.param_groups \
    #                 #         [group_index]["params"][group_order].detach().cpu()
    #                 # except:
    #                 #     # main_param = None
    #                 #     main_param = torch.zeros(
    #                 #         (1,), dtype=torch.float32, device="cpu")

    #                 # # if data_parallel_rank == 0:
    #                 # if data_parallel_rank == dp_rank_ranges[0][0]:
    #                 #     gather_list = [torch.zeros((i1 - i0,),
    #                 #                                dtype=torch.float32,
    #                 #                                device="cpu")
    #                 #                    for _, i0, i1 in dp_rank_ranges]
    #                 # else:
    #                 #     gather_list = None

    #                 # torch.distributed.gather(
    #                 #     main_param,
    #                 #     gather_list,
    #                 #     data_parallel_global_ranks[0],
    #                 #     torch.distributed.new_group(
    #                 #         data_parallel_global_ranks[dp_rank_start:dp_rank_end]),
    #                 # )
    #                 # +++++++++++++++++++++++++++++++++++++++++++++++++++++
    #                 try:
    #                     group_index, group_order = \
    #                         self.model_param_group_index_map[model_param]
    #                     main_param = self.optimizer.param_groups \
    #                         [group_index]["params"][group_order].detach() # .cpu()
    #                 except KeyError:
    #                     main_param = torch.zeros(
    #                         (1,), dtype=torch.float32, device="cuda") # "cpu")

    #                 if data_parallel_rank == 0:
    #                     gather_list = []
    #                     for dp_rank in range(data_parallel_world_size):
    #                         if dp_rank >= dp_rank_start and dp_rank < dp_rank_end:
    #                             _, local_start, local_end = \
    #                                 dp_rank_ranges[dp_rank - dp_rank_start]
    #                             size = (local_end - local_start,)
    #                         else:
    #                             size = (1,)
    #                         gather_list.append(torch.zeros(size,
    #                                                        dtype=torch.float32,
    #                                                        device="cuda")) # "cpu"
    #                 else:
    #                     gather_list = None

    #                 # print_seq("main_param = %s." % str(main_param.shape))
                    
    #                 # try:
    #                 torch.distributed.gather(
    #                     main_param,
    #                     gather_list,
    #                     data_parallel_global_ranks[0],
    #                     # 0,
    #                     data_parallel_group,
    #                 )

    #                 # torch.distributed.all_gather(
    #                 #     gather_list,
    #                 #     main_param,
    #                 #     data_parallel_group,
    #                 # )
    #                 # except:
    #                 #     print_seq("main_param = %s." % str(main_param.shape))
    #                 #     print_seq("gather_list = %s." % (
    #                 #         ", ".join([str(tuple(t.shape)) for t in gather_list])
    #                 #         if gather_list else "--"))

    #                 pax(0, {"gather_list": gather_list})
    #                 # +++++++++++++++++++++++++++++++++++++++++++++++++++++
    #                 # if torch.distributed.get_rank() in data_parallel_global_ranks:
    #                 # if data_parallel_rank >= dp_rank_start and \
    #                 #    data_parallel_rank < dp_rank_end:

    #                 #     group_index, group_order = \
    #                 #         self.model_param_group_index_map[model_param]
    #                 #     main_param = self.optimizer.param_groups \
    #                 #         [group_index]["params"][group_order] # .detach() # .cpu()

    #                 #     if data_parallel_rank == dp_rank_start:
    #                 #         gather_list = []
    #                 #         for dp_rank in range(dp_rank_start, dp_rank_end):
    #                 #             _, local_start, local_end = \
    #                 #                 dp_rank_ranges[dp_rank - dp_rank_start]
    #                 #             size = (local_end - local_start,)
    #                 #             gather_list.append(torch.zeros(
    #                 #                 size,
    #                 #                 dtype=torch.float32,
    #                 #                 device="cuda")) # "cpu"
    #                 #     else:
    #                 #         gather_list = None

    #                 #     # pax(7, {
    #                 #     #     "global dp start" :
    #                 #     #     data_parallel_global_ranks[dp_rank_start],
    #                 #     #     "main_param" : tp(main_param),
    #                 #     #     "gather_list" : gather_list,
    #                 #     # })
                        
    #                 #     print("start >>>>>>>>>")
    #                 #     torch.distributed.gather(
    #                 #         main_param,
    #                 #         gather_list,
    #                 #         # data_parallel_global_ranks[dp_rank_start],
    #                 #         0,
    #                 #         torch.distributed.new_group([
    #                 #             data_parallel_global_ranks[r]
    #                 #             for r in range(dp_rank_start, dp_rank_end)
    #                 #         ]),
    #                 #     )
    #                 #     print("end >>>>>>>>>>>>>")

    #                 #     pax(4, {
    #                 #         "gather_list": gather_list,
    #                 #     })
    #                 # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    #                 torch.distributed.barrier()
    #                 exit()

    #                 print_rank_0("world %d ... dp [%d, %d)." % (
    #                     param_world_order,
    #                     dp_rank_start,
    #                     dp_rank_end,
    #                 ))
    #                 # pax(0, {
    #                 #     "data_parallel_world_size" : data_parallel_world_size,
    #                 #     "gbuf_world_numel" : gbuf_world_numel,
    #                 #     "gbuf_local_numel" : gbuf_local_numel,
    #                 #     # "model_param" : tp(model_param),
    #                 #     "dp_rank_start" : dp_rank_start,
    #                 #     "dp_rank_end" : dp_rank_end,
    #                 # })
    #             pax(0, {
    #                 "param_index_map" : {
    #                     f"{id(k)} / {k.shape}" : v
    #                     for k, v in param_index_map.items()
    #                 },
    #             })

    #     raise Exception("hi.")
    def save_state(self):

        pax(0, {"self": self})

        data_parallel_world_size = mpu.get_data_parallel_world_size()
        # data_parallel_rank = mpu.get_data_parallel_rank()
        # data_parallel_group = mpu.get_data_parallel_group()
        # data_parallel_global_ranks = list(mpu._DATA_PARALLEL_GLOBAL_RANKS)

        for model_idx, gbuf_range_maps in enumerate(self.model_gbuf_ranges):
            for dtype, gbuf_range_map in gbuf_range_maps.items():

                model = self.models[model_idx]
                gbuf_world_numel = model._grad_buffers[dtype].numel_padded
                gbuf_local_numel = int(gbuf_world_numel/data_parallel_world_size)
                gbuf_local = torch.zeros((gbuf_local_numel,),
                                         dtype=dtype,
                                         device="cpu")

                for param_idx, (model_param, param_range_map) in \
                    enumerate(gbuf_range_map["param_map"].items()):

                    gbuf_local_start = param_range_map["gbuf_local"].start
                    gbuf_local_end = param_range_map["gbuf_local"].end

                    gbuf_local[gbuf_local_start:gbuf_local_end].data.copy_()

                    pax(0, {
                        "gbuf_local" : tp(gbuf_local),
                        "model_idx" : model_idx,
                        "gbuf_range_map" : gbuf_range_map,
                        "param_range_map" : param_range_map,
                    })


        raise Exception("hi.")


        # pax(0, {
        #     "model_gbuf_range_map" : self.model_gbuf_range_map,
        # })

        for model_idx, model in enumerate(self.models):
            for dtype, param_index_map in \
                model._grad_buffer_param_index_map.items():

                gbuf_world_numel = model._grad_buffers[dtype].numel_padded
                gbuf_local_numel = int(gbuf_world_numel/data_parallel_world_size)

                gbuf_local = torch.zeros((gbuf_local_numel,),
                                         dtype=dtype,
                                         device="cpu")

                for model_param, (
                        param_world_order,
                        param_world_start,
                        param_world_end,
                ) in param_index_map.items():

                    dp_rank_start = int(param_world_start // gbuf_local_numel)
                    dp_rank_end = int((param_world_end-1) // gbuf_local_numel) + 1

                    dp_rank_ranges = []
                    for dp_rank in range(dp_rank_start, dp_rank_end):

                        if dp_rank == dp_rank_start:
                            local_idx_start = \
                                param_world_start % gbuf_local_numel
                        else:
                            local_idx_start = 0

                        if dp_rank == dp_rank_end - 1:
                            local_idx_end = \
                                (param_world_end - 1) % gbuf_local_numel + 1
                        else:
                            local_idx_end = gbuf_local_numel

                        dp_rank_ranges.append((
                            dp_rank,
                            local_idx_start,
                            local_idx_end,
                        ))

                    # pax(0, {"dp_rank_ranges": dp_rank_ranges})

                    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
                    # try:
                    #     group_index, group_order = \
                    #         self.model_param_group_index_map[model_param]
                    #     main_param = self.optimizer.param_groups \
                    #         [group_index]["params"][group_order].detach().cpu()
                    # except:
                    #     # main_param = None
                    #     main_param = torch.zeros(
                    #         (1,), dtype=torch.float32, device="cpu")

                    # # if data_parallel_rank == 0:
                    # if data_parallel_rank == dp_rank_ranges[0][0]:
                    #     gather_list = [torch.zeros((i1 - i0,),
                    #                                dtype=torch.float32,
                    #                                device="cpu")
                    #                    for _, i0, i1 in dp_rank_ranges]
                    # else:
                    #     gather_list = None

                    # torch.distributed.gather(
                    #     main_param,
                    #     gather_list,
                    #     data_parallel_global_ranks[0],
                    #     torch.distributed.new_group(
                    #         data_parallel_global_ranks[dp_rank_start:dp_rank_end]),
                    # )
                    # +++++++++++++++++++++++++++++++++++++++++++++++++++++
                    try:
                        group_index, group_order = \
                            self.model_param_group_index_map[model_param]
                        main_param = self.optimizer.param_groups \
                            [group_index]["params"][group_order].detach() # .cpu()
                    except KeyError:
                        main_param = torch.zeros(
                            (1,), dtype=torch.float32, device="cuda") # "cpu")

                    if data_parallel_rank == 0:
                        gather_list = []
                        for dp_rank in range(data_parallel_world_size):
                            if dp_rank >= dp_rank_start and dp_rank < dp_rank_end:
                                _, local_start, local_end = \
                                    dp_rank_ranges[dp_rank - dp_rank_start]
                                size = (local_end - local_start,)
                            else:
                                size = (1,)
                            gather_list.append(torch.zeros(size,
                                                           dtype=torch.float32,
                                                           device="cuda")) # "cpu"
                    else:
                        gather_list = None

                    # print_seq("main_param = %s." % str(main_param.shape))
                    
                    # try:
                    torch.distributed.gather(
                        main_param,
                        gather_list,
                        data_parallel_global_ranks[0],
                        # 0,
                        data_parallel_group,
                    )

                    # torch.distributed.all_gather(
                    #     gather_list,
                    #     main_param,
                    #     data_parallel_group,
                    # )
                    # except:
                    #     print_seq("main_param = %s." % str(main_param.shape))
                    #     print_seq("gather_list = %s." % (
                    #         ", ".join([str(tuple(t.shape)) for t in gather_list])
                    #         if gather_list else "--"))

                    pax(0, {"gather_list": gather_list})
                    # +++++++++++++++++++++++++++++++++++++++++++++++++++++
                    # if torch.distributed.get_rank() in data_parallel_global_ranks:
                    # if data_parallel_rank >= dp_rank_start and \
                    #    data_parallel_rank < dp_rank_end:

                    #     group_index, group_order = \
                    #         self.model_param_group_index_map[model_param]
                    #     main_param = self.optimizer.param_groups \
                    #         [group_index]["params"][group_order] # .detach() # .cpu()

                    #     if data_parallel_rank == dp_rank_start:
                    #         gather_list = []
                    #         for dp_rank in range(dp_rank_start, dp_rank_end):
                    #             _, local_start, local_end = \
                    #                 dp_rank_ranges[dp_rank - dp_rank_start]
                    #             size = (local_end - local_start,)
                    #             gather_list.append(torch.zeros(
                    #                 size,
                    #                 dtype=torch.float32,
                    #                 device="cuda")) # "cpu"
                    #     else:
                    #         gather_list = None

                    #     # pax(7, {
                    #     #     "global dp start" :
                    #     #     data_parallel_global_ranks[dp_rank_start],
                    #     #     "main_param" : tp(main_param),
                    #     #     "gather_list" : gather_list,
                    #     # })
                        
                    #     print("start >>>>>>>>>")
                    #     torch.distributed.gather(
                    #         main_param,
                    #         gather_list,
                    #         # data_parallel_global_ranks[dp_rank_start],
                    #         0,
                    #         torch.distributed.new_group([
                    #             data_parallel_global_ranks[r]
                    #             for r in range(dp_rank_start, dp_rank_end)
                    #         ]),
                    #     )
                    #     print("end >>>>>>>>>>>>>")

                    #     pax(4, {
                    #         "gather_list": gather_list,
                    #     })
                    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
                    torch.distributed.barrier()
                    exit()

                    print_rank_0("world %d ... dp [%d, %d)." % (
                        param_world_order,
                        dp_rank_start,
                        dp_rank_end,
                    ))
                    # pax(0, {
                    #     "data_parallel_world_size" : data_parallel_world_size,
                    #     "gbuf_world_numel" : gbuf_world_numel,
                    #     "gbuf_local_numel" : gbuf_local_numel,
                    #     # "model_param" : tp(model_param),
                    #     "dp_rank_start" : dp_rank_start,
                    #     "dp_rank_end" : dp_rank_end,
                    # })
                pax(0, {
                    "param_index_map" : {
                        f"{id(k)} / {k.shape}" : v
                        for k, v in param_index_map.items()
                    },
                })

        raise Exception("hi.")

    def state_dict(self):
        """
        The state dict must contain the fp32-from-float16 shards.
        """

        # pax(0, {
        #     "optimizer" : self.optimizer,
        #     "optimizer / state" :
        #     [tp(d["exp_avg"]) for d in self.optimizer.state.values()],
        #     **{"optimizer / params / %d" % i : [tp(p) for p in g["params"]]
        #        for i, g in enumerate(self.optimizer.param_groups)},
        #     **{"shard_fp32_from_float16_groups / %d" % i : [tp(p) for p in g]
        #        for i, g in enumerate(self.shard_fp32_from_float16_groups)},
        # })

        # state_order_map = {p:i for 
        default_state_dict = self.optimizer.state_dict()

        # Shard state dicts.
        shard_state_dicts = []
        for model_idx, gbuf_range_maps in enumerate(self.model_gbuf_ranges):
            for dtype, gbuf_range_map in gbuf_range_maps.items():
                for param_idx, (model_param, param_range_map) in \
                    enumerate(gbuf_range_map["param_map"].items()):

                    group_index, group_order = \
                        self.model_param_group_index_map[model_param]
                    world_order = param_range_map["gbuf_world_order"]

                    main_param = self.optimizer.param_groups[group_index]["params"][group_order]
                    state_order = default_state_dict["param_groups"][group_index]["params"][group_order]
                    optim_state = self.optimizer.state[main_param]

                    shard_state_dicts.append({
                        "world_order" : world_order,
                        "state_order" : state_order,
                        "group_index" : group_index,
                        "group_order" : group_order,
                        "param_range_map" : param_range_map,
                        "param" : main_param,
                        "optim" : optim_state,
                    })

        state_dict = {}
        state_dict['optimizer'] = default_state_dict
        state_dict['optimizer']['state'] = shard_state_dicts
        if self.grad_scaler:
            state_dict['grad_scaler'] = self.grad_scaler.state_dict()

        # >>>
        # pax(0, {
        #     "state_dict" : state_dict,
        #     "state_dict / optimizer" : state_dict["optimizer"],
        # })
        # <<<

        return state_dict


    # def load_state_dict(self, state_dict):
    #     """
    #     Load the state dict.
    #     """

    #     # Optimizer.
    #     optimizer_key = 'optimizer'
    #     if optimizer_key not in state_dict:
    #         optimizer_key = 'optimizer_state_dict'
    #         print_rank_0('***WARNING*** loading optimizer from '
    #                      'an old checkpoint ...')
    #     self.optimizer.load_state_dict(state_dict[optimizer_key])

    #     # Grad scaler.
    #     if 'grad_scaler' not in state_dict:
    #         if self.fp16:
    #             print_rank_0('***WARNING*** found an old checkpoint, will not '
    #                          'load grad scaler ...')
    #     else:
    #         if self.grad_scaler:
    #             self.grad_scaler.load_state_dict(state_dict['grad_scaler'])
    #         else:
    #             print_rank_0('***WARNING*** fould the grad scaler in the '
    #                          'checkpoint but it is None in the class. '
    #                          'Skipping loading grad scaler ...')

    #     # Copy data for the main params.
    #     for current_group, saved_group in zip(
    #             self.shard_fp32_from_float16_groups,
    #             state_dict["shard_fp32_from_float16_groups"]):
    #         for current_param, saved_param in zip(current_group, saved_group):
    #             current_param.data.copy_(saved_param.data)
    def load_state_dict(self, state_dict):
        """
        Load the state dict.
        """

        # Collect shards & set main params.
        optim_state_dicts = []
        for shard_state_dict in state_dict["optimizer"]["state"]:

            # Collect optimizer state.
            optim_state_dicts.append((
                shard_state_dict["state_order"],
                shard_state_dict["optim"],
            ))

            # Set main params.
            group_index = shard_state_dict["group_index"]
            group_order = shard_state_dict["group_order"]
            self.shard_fp32_from_float16_groups[group_index][group_order] \
                .data.copy_(shard_state_dict["param"])

        # Sort by pre-save state order.
        optim_state_dicts.sort(key = lambda d : d[0])
        optim_state_dicts = [ d[1] for d in optim_state_dicts ]

        # Optimizer.
        self.optimizer.load_state_dict({
            **state_dict["optimizer"],
            "state" : {i:d for i,d in enumerate(optim_state_dicts)},
        })

        # pax(0, {
        #     "optimizer" : self.optimizer,
        #     "optimizer / state" :
        #     [tp(d["exp_avg"]) for d in self.optimizer.state.values()],
        #     **{"optimizer / params / %d" % i : [tp(p) for p in g["params"]]
        #        for i, g in enumerate(self.optimizer.param_groups)},
        #     **{"shard_fp32_from_float16_groups / %d" % i : [tp(p) for p in g]
        #        for i, g in enumerate(self.shard_fp32_from_float16_groups)},
        # })

        # Grad scaler.
        if self.grad_scaler:
            self.grad_scaler.load_state_dict(state_dict['grad_scaler'])
        else:
            print_rank_0('***WARNING*** found the grad scaler in the '
                         'checkpoint but it is None in the class. '
                         'Skipping loading grad scaler ...')


    def zero_grad(self, set_to_none=True):
        """
        Zero grads.

        We only need to zero the model related parameters, i.e.,
        model_float16_groups & model_fp32_groups. We additionally zero
        the remaining groups as a memory optimization to reduce
        fragmentation; in the case of set_to_none==True, the space
        used by this field can be safely deallocated at this point.
        """
        for groups in (
                self.model_float16_groups,
                self.model_fp32_groups,
                self.shard_float16_groups, # grad empty/unused here?
                self.shard_fp32_groups, # throws grad-access warning
                self.shard_fp32_from_float16_groups):
            for group in groups:
                _zero_grad_group_helper(group, set_to_none)


    @staticmethod
    def get_model_buffer_dp_views(model_buffers):
        """
        Get shard views of each of the DDP's param/grad buffers.

        In this nested list, the top level is grouped by the virtual model
        index and the buffer's data type. The sub-level is a list of
        shards of that buffer, where each shard in the list represents
        a contiguous view of the buffer, that is owned by a data-parallel
        rank. The shard boundary does not respect parameter boundaries, and
        so the elements of some parameters are split across data parallel
        ranks.

        Additionally, return references to the entire buffers, for use
        in _reduce_scatter_base and _all_gather_base.
        """

        data_parallel_world_size = mpu.get_data_parallel_world_size()

        # Buffer views.
        view_items = []
        for model_index, buffers in enumerate(model_buffers):
            for dtype, buf in buffers.items():

                assert buf.numel() % data_parallel_world_size == 0
                shard_size = int(buf.numel() / data_parallel_world_size)
                buf_views = [buf[(r*shard_size):((r+1)*shard_size)]
                             for r in range(data_parallel_world_size)]
                view_items.append((model_index, dtype, buf, buf_views))

        return view_items


    def get_model_grad_buffer_dp_views(self):
        return self.get_model_buffer_dp_views([
            {dtype : mem_buffer.data}
            for model in self.models
            for dtype, mem_buffer in model._grad_buffers.items()])


    def get_model_param_buffer_dp_views(self):
        return self.get_model_buffer_dp_views(self.param_buffers)


    def reduce_model_grads(self, args, timers):
        """
        Reduce-scatter model grads.

        The DDP's grad buffer is used for the reduce-scatter, and thus no
        tensors are dynamically allocated.

        Note: this is a different order of reduction, versus the non-
        distributed optimizer, which reduces: 1) layernorm grads, 2) all
        grads, 3) embedding grads.
        """

        # All-reduce layer-norm grads (for sequence parallelism).
        timers('layernorm-grads-all-reduce', log_level=1).start(
            barrier=args.barrier_with_L1_time)
        self.allreduce_layernorm_grads(args)
        timers('layernorm-grads-all-reduce').stop()

        # All-reduce embedding grads.
        timers('embedding-grads-all-reduce', log_level=1).start(
            barrier=args.barrier_with_L1_time)
        self.allreduce_embedding_grads(args)
        timers('embedding-grads-all-reduce').stop()

        # Reduce-scatter setup.
        timers('grads-reduce-scatter', log_level=1).start(
            barrier=args.barrier_with_L1_time)
        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_world_size = mpu.get_data_parallel_world_size()
        data_parallel_group = mpu.get_data_parallel_group()

        # Scale grad buffers by '1 / data_parallel_world_size'.
        for model in self.models:
            for dtype, gbuf in model._grad_buffers.items():
                gbuf.data /= data_parallel_world_size

        # Reduce-scatter all grads.
        gbuf_view_items = self.get_model_grad_buffer_dp_views()
        for index, (model_index, dtype, gbuf, gbuf_views) \
            in enumerate(gbuf_view_items):

            torch.distributed._reduce_scatter_base(
                gbuf_views[data_parallel_rank],
                gbuf,
                group = data_parallel_group,
            )

        timers('grads-reduce-scatter').stop()


    def gather_model_params(self, args, timers):
        """
        All-gather updated model params.

        The DDP's param buffer is used for the all-gather, and thus no
        tensors are dynamically allocated. After the all-gather, the params
        can be copied from the param buffer to the param.
        """

        timers('params-all-gather', log_level=1).start(
            barrier=args.barrier_with_L1_time)

        data_parallel_rank = mpu.get_data_parallel_rank()
        data_parallel_group = mpu.get_data_parallel_group()

        # All-gather updated main params.
        # - All param buffer views are guaranteed to have the same num elements
        #   across all data parallel ranks, due to grad buffer padding that is
        #   done in distributed.py, and extended to the param buffers. Thus,
        #   all sub-views will have consistent start/end indexes across data
        #   parallel ranks.
        pbuf_view_items = self.get_model_param_buffer_dp_views()
        for index, (model_index, dtype, pbuf, pbuf_views) \
            in enumerate(pbuf_view_items):

            torch.distributed._all_gather_base(
                pbuf,
                pbuf_views[data_parallel_rank],
                group = data_parallel_group,
            )

        # Copy from param buffer to each param.
        for model_id, model in enumerate(self.models):
            for dtype, param_map in model._grad_buffer_param_index_map.items():
                # >>>
                # raise Exception("update _gbpim usage.")
                # for param, buf_range in param_map.items():
                #     param_buf = self.param_buffers[model_id][dtype]
                #     param_buf_shard = param_buf[buf_range[0]:buf_range[1]]
                #     param.view(-1).detach().copy_(param_buf_shard)
                for param, (buf_order, buf_start, buf_end) in param_map.items():
                    param_buf = self.param_buffers[model_id][dtype]
                    param_buf_shard = param_buf[buf_start:buf_end]
                    param.view(-1).detach().copy_(param_buf_shard)
                # <<<

        timers('params-all-gather').stop()


    def _collect_main_grad_data_for_unscaling(self):
        """
        Note: this should be equivalent to the float-16 optimizer's method,
        but writtent differently, so the two should be combined.
        """
        return [
            param.grad.data
            for group in self.optimizer.param_groups
            for param in group["params"]
        ]


    def _get_model_and_main_params_data_float16(self):
        """
        Get aligned list of model and main params.
        """
        model_data = []
        main_data = []
        for model_group, main_group in zip(self.shard_float16_groups,
                                           self.shard_fp32_from_float16_groups):
            for model_param, main_param in zip(model_group, main_group):
                model_data.append(model_param.data)
                main_data.append(main_param.data)
        return model_data, main_data


    def _copy_model_grads_to_main_grads(self):
        """
        Copy model grads to main grads.

        Since this step follows a reduce-scatter through the DDP's grad
        buffer, this method is responsible for copying the updated grads
        from the grad buffer to the main shard's grad field.
        """

        # Utility method for copying group grads.
        def copy_group_grads(model_groups, shard_main_groups):
            for model_group, shard_main_group in zip(model_groups,
                                                     shard_main_groups):
                for model_param, shard_main_param in zip(model_group,
                                                         shard_main_group):

                    param_range_map = self.get_model_param_range_map(model_param)
                    param_range = param_range_map["param"]
                    assert param_range.size == shard_main_param.nelement()

                    model_grad = model_param.main_grad
                    shard_model_grad = model_grad.view(-1) \
                        [param_range.start:param_range.end]
                    shard_main_param.grad = shard_model_grad.float()

        # Copy model groups to shard groups.
        copy_group_grads(self.model_float16_groups,
                         self.shard_fp32_from_float16_groups)
        copy_group_grads(self.model_fp32_groups,
                         self.shard_fp32_groups)


    def _copy_main_params_to_model_params(self):
        """
        Copy main params to model params.

        Since this step is followed by an all-gather through the DDP's grad
        buffer, this method is responsible for copying the updated params
        from the main shards into the correct position in the grad buffer.
        """

        # Utility method for copying group params.
        def copy_group_params(shard_main_groups, model_groups):
            for shard_main_group, model_group in zip(shard_main_groups,
                                                     model_groups):
                for shard_main_param, model_param in zip(shard_main_group,
                                                         model_group):

                    param_range_map = self.get_model_param_range_map(model_param)
                    world_range = param_range_map["gbuf_world"]

                    assert world_range.size == shard_main_param.nelement()

                    model_id, dtype = self.model_param_gbuf_map[model_param]
                    model_param_buffer = self.param_buffers[model_id][dtype]

                    shard_model_param = model_param_buffer.view(-1) \
                        [world_range.start:world_range.end]

                    shard_model_param.data.copy_(shard_main_param)

        # Copy shard groups to model groups.
        copy_group_params(self.shard_fp32_from_float16_groups,
                          self.model_float16_groups)
        copy_group_params(self.shard_fp32_groups,
                          self.model_fp32_groups)
