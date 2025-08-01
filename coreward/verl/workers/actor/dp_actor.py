# Copyright 2024 Bytedance Ltd. and/or its affiliates
# Copyright 2023-2024 SGLang Team
# Copyright 2025 ModelBest Inc. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Single Process Actor
"""

import itertools
import logging
import os
from typing import Tuple

import torch
from flash_attn.bert_padding import index_first_axis, pad_input, rearrange, unpad_input
from torch import nn
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP

import verl.utils.torch_functional as verl_F
from verl import DataProto
from verl.trainer.ppo.core_algos import agg_loss, compute_policy_loss, kl_penalty
from verl.utils.debug import GPUMemoryLogger
from verl.utils.fsdp_utils import FSDPModule, fsdp2_clip_grad_norm_
from verl.utils.py_functional import append_to_dict
from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches
from verl.utils.torch_functional import logprobs_from_logits
from verl.utils.ulysses import gather_outpus_and_unpad, ulysses_pad_and_slice_inputs
from verl.workers.actor import BasePPOActor

__all__ = ["DataParallelPPOActor"]

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class DataParallelPPOActor(BasePPOActor):
    def __init__(self, config, actor_module: nn.Module, actor_optimizer: torch.optim.Optimizer = None):
        """When optimizer is None, it is Reference Policy"""
        super().__init__(config)
        self.actor_module = actor_module
        self.actor_optimizer = actor_optimizer

        self.use_remove_padding = self.config.get("use_remove_padding", False)
        print(f"Actor use_remove_padding={self.use_remove_padding}")
        self.use_fused_kernels = self.config.get("use_fused_kernels", False)
        print(f"Actor use_fused_kernels={self.use_fused_kernels}")

        self.ulysses_sequence_parallel_size = self.config.ulysses_sequence_parallel_size
        self.use_ulysses_sp = self.ulysses_sequence_parallel_size > 1

        self.compute_entropy_from_logits = (
            torch.compile(verl_F.entropy_from_logits, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.entropy_from_logits
        )

        self.compute_self_certainty_from_logits = (
            torch.compile(verl_F.self_certainty_from_logits, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.self_certainty_from_logits
        )
        
        self.compute_sentence_level_certainty_from_logits = (
            torch.compile(verl_F.sentence_level_certainty_from_logits, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.sentence_level_certainty_from_logits
        )
        # self.compute_sentence_level_certainty_from_logits = verl_F.sentence_level_certainty_from_logits

        self.compute_sentence_entropy_from_logits = (
            torch.compile(verl_F.sentence_entropy_from_logits, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.sentence_entropy_from_logits
        )
        
        self.compute_avg_sentence_probs = (
            torch.compile(verl_F.sentence_avg_probs, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.sentence_avg_probs            
        )
        
        if self.use_fused_kernels:
            from verl.utils.experimental.torch_functional import FusedLinearForPPO

            self.fused_linear_for_ppo = FusedLinearForPPO()

            # FusedLinearForPPO has an error when compiled, disable for now
            # if self.config.get("use_torch_compile", True):
            #     self.fused_linear_for_ppo.compile(dynamic=True)

    def _forward_micro_batch(
        self, micro_batch, temperature, 
        calculate_entropy=False, 
        calculate_self_certainty=False, 
        calculate_sentence_certainty=False,
        calculate_sentence_entropy=False,
        calculate_sentence_avg_prob=False) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            entropy: # (bs, response_len)
            log_probs: # (bs, response_len)
        """
        response_length = micro_batch["responses"].size(-1)
        multi_modal_inputs = {}
        if "multi_modal_inputs" in micro_batch:
            for key in micro_batch["multi_modal_inputs"][0].keys():
                multi_modal_inputs[key] = torch.cat([inputs[key] for inputs in micro_batch["multi_modal_inputs"]], dim=0)

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            input_ids = micro_batch["input_ids"]
            batch_size, seqlen = input_ids.shape
            attention_mask = micro_batch["attention_mask"]
            position_ids = micro_batch["position_ids"]
            entropy = None
            self_certainty = None
            sentence_certainty = None
            sentence_entropy = None
            sentence_avg_prob = None
            if position_ids.dim() == 3:  # qwen2vl mrope
                position_ids = position_ids.transpose(0, 1)  # (bsz, 3, seqlen) -> (3, bsz, seqlen)
 
            if self.use_remove_padding:
                input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1), attention_mask)  # input_ids_rmpad (total_nnz, ...)
                input_ids_rmpad = input_ids_rmpad.transpose(0, 1)  # (1, total_nnz)

                # unpad the position_ids to align the rotary
                if position_ids.dim() == 3:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids, "c b s ... -> (b s) c ..."), indices).transpose(0, 1).unsqueeze(1)  # (3, bsz, seqlen) -> (3, 1, bsz * seqlen)
                else:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids.unsqueeze(-1), "b s ... -> (b s) ..."), indices).transpose(0, 1)

                # for compute the log_prob
                input_ids_rmpad_rolled = torch.roll(input_ids_rmpad, shifts=-1, dims=1)  # (1, total_nnz)

                # pad and slice the inputs if sp > 1
                if self.use_ulysses_sp:
                    input_ids_rmpad, position_ids_rmpad, pad_size = ulysses_pad_and_slice_inputs(
                        input_ids_rmpad,
                        position_ids_rmpad=position_ids_rmpad,
                        sp_size=self.ulysses_sequence_parallel_size,
                    )
                    input_ids_rmpad_rolled, _, _ = ulysses_pad_and_slice_inputs(
                        input_ids_rmpad_rolled,
                        position_ids_rmpad=None,
                        sp_size=self.ulysses_sequence_parallel_size,
                    )

                input_ids_rmpad_rolled = input_ids_rmpad_rolled.squeeze(0)  # ((total_nnz / sp) + pad)

                # only pass input_ids and position_ids to enable flash_attn_varlen
                output = self.actor_module(
                    input_ids=input_ids_rmpad,
                    attention_mask=None,
                    position_ids=position_ids_rmpad,
                    **multi_modal_inputs,
                    use_cache=False,
                )  # prevent model thinks we are generating

                if self.use_fused_kernels:
                    hidden_states = output.last_hidden_state
                    vocab_weights = self.actor_module.lm_head.weight

                    log_probs, entropy_rmpad = self.fused_linear_for_ppo(
                        hidden_states=hidden_states.squeeze(0),
                        vocab_weights=vocab_weights,
                        input_ids=input_ids_rmpad_rolled,
                        temperature=temperature,
                    )
                    self_certainty_rmpad = None  # TODO: add self_certainty_rmpad
                    sentence_certainty = None     # TODO: add sentence_certainty_rmpad
                    sentence_entropy = None

                else:
                    logits_rmpad = output.logits.squeeze(0)  # (total_nnz, vocab_size)

                    # logits_rmpad = output.logits.squeeze(0)  # (total_nnz, vocab_size)
                    logits_rmpad.div_(temperature)

                    # if use_sp: ((total_nnz / sp) + pad) ; if not use_sp: (batch, seqlen)
                    inplace_backward = True
                    if calculate_entropy:
                        inplace_backward = False
                    log_probs = logprobs_from_logits(
                        logits=logits_rmpad,
                        labels=input_ids_rmpad_rolled,
                        inplace_backward=inplace_backward,
                    )

                    # compute entropy
                    if calculate_entropy:
                        entropy_rmpad = self.compute_entropy_from_logits(logits_rmpad)  # ((total_nnz / sp) + pad)
                    if calculate_self_certainty:
                        self_certainty_rmpad = self.compute_self_certainty_from_logits(logits_rmpad)  # ((total_nnz / sp) + pad)
                    if calculate_sentence_certainty:
                        sentence_certainty = self.compute_sentence_level_certainty_from_logits(logits_rmpad, attention_mask=attention_mask, indices=indices, batch_size=batch_size)
                    if calculate_sentence_entropy:
                        sentence_entropy = self.compute_sentence_entropy_from_logits(logits_rmpad, attention_mask=attention_mask, indices=indices, batch_size=batch_size)
                    if calculate_sentence_avg_prob:
                        sentence_avg_prob = self.compute_avg_sentence_probs(logits_rmpad, attention_mask=attention_mask, indices=indices, batch_size=batch_size)
                    
                # gather log_prob if sp > 1
                if self.use_ulysses_sp:
                    # gather and unpad for the ulysses sp
                    log_probs = gather_outpus_and_unpad(
                        log_probs,
                        gather_dim=0,
                        unpad_dim=0,
                        padding_size=pad_size,
                    )
                    if calculate_entropy:
                        entropy_rmpad = gather_outpus_and_unpad(
                            entropy_rmpad,
                            gather_dim=0,
                            unpad_dim=0,
                            padding_size=pad_size,
                        )
                    if calculate_self_certainty:
                        self_certainty_rmpad = gather_outpus_and_unpad(
                            self_certainty_rmpad,
                            gather_dim=0,
                            unpad_dim=0,
                            padding_size=pad_size,
                        )

                # pad back to (bsz, seqlen)
                if calculate_entropy:
                    full_entropy = pad_input(
                        hidden_states=entropy_rmpad.unsqueeze(-1),
                        indices=indices,
                        batch=batch_size,
                        seqlen=seqlen,
                    )
                if calculate_self_certainty:
                    full_self_certainty = pad_input(
                        hidden_states=self_certainty_rmpad.unsqueeze(-1),
                        indices=indices,
                        batch=batch_size,
                        seqlen=seqlen,
                    )
 
                full_log_probs = pad_input(
                    hidden_states=log_probs.unsqueeze(-1),
                    indices=indices,
                    batch=batch_size,
                    seqlen=seqlen,
                )

                # only return response part:
                if calculate_entropy:
                    entropy = full_entropy.squeeze(-1)[:, -response_length - 1 : -1]  # (bsz, response_length)
                if calculate_self_certainty:
                    self_certainty = full_self_certainty.squeeze(-1)[:, -response_length - 1 : -1]  # (bsz, response_length)
                if calculate_sentence_certainty:
                    sentence_certainty = sentence_certainty          # (bsz,)
                if calculate_sentence_entropy:
                    sentence_entropy = sentence_entropy
                if calculate_sentence_avg_prob:
                    sentence_avg_prob = sentence_avg_prob
                log_probs = full_log_probs.squeeze(-1)[:, -response_length - 1 : -1]  # (bsz, response_length)

            else:  # not using rmpad and no ulysses sp
                output = self.actor_module(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    **multi_modal_inputs,
                    use_cache=False,
                )  # prevent model thinks we are generating

                if self.use_fused_kernels:
                    hidden_states = output.last_hidden_state
                    vocab_weights = self.actor_module.lm_head.weight

                    log_probs, entropy = self.fused_linear_for_ppo(
                        hidden_states=hidden_states[:, -response_length - 1 : -1, :],
                        vocab_weights=vocab_weights,
                        input_ids=micro_batch["responses"],
                        temperature=temperature,
                    )
                    self_certainty = None  # TODO: add self_certainty
                    sentence_certainty = None     # TODO: add sentence_certainty
                    sentence_entropy = None
                    sentence_avg_prob = None
                else:
                    logits = output.logits

                    logits.div_(temperature)
                    logits = logits[:, -response_length - 1 : -1, :]  # (bsz, response_length, vocab_size)
                    log_probs = logprobs_from_logits(logits, micro_batch["responses"])
                    response_mask = attention_mask[:, -response_length:]
                    if calculate_entropy:
                        entropy = verl_F.entropy_from_logits(logits)  # (bsz, response_length)
                    if calculate_self_certainty:
                        self_certainty = verl_F.self_certainty_from_logits(logits)  # (bsz, response_length)
                    if calculate_sentence_certainty:
                        sentence_certainty = verl_F.sentence_level_certainty_from_logits(logits, response_mask=response_mask, batch_size=batch_size)
                    if calculate_sentence_entropy:
                        sentence_entropy = verl_F.sentence_entropy_from_logits(logits, response_mask=response_mask, batch_size=batch_size)
                    if calculate_sentence_avg_prob:
                        sentence_avg_prob = verl_F.sentence_avg_probs(logits, response_mask=response_mask, batch_size=batch_size)
            return entropy, log_probs, self_certainty, sentence_certainty, sentence_entropy, sentence_avg_prob

    def _optimizer_step(self):
        assert self.config.grad_clip is not None

        if isinstance(self.actor_module, FSDP):
            grad_norm = self.actor_module.clip_grad_norm_(max_norm=self.config.grad_clip)
        elif isinstance(self.actor_module, FSDPModule):
            grad_norm = fsdp2_clip_grad_norm_(self.actor_module.parameters(), max_norm=self.config.grad_clip)
        else:
            grad_norm = torch.nn.utils.clip_grad_norm_(self.actor_module.parameters(), max_norm=self.config.grad_clip)

        # if grad_norm is not finite, skip the update
        if not torch.isfinite(grad_norm):
            print(f"WARN: rank {torch.distributed.get_rank()} grad_norm is not finite: {grad_norm}")
            self.actor_optimizer.zero_grad()
        else:
            self.actor_optimizer.step()
        return grad_norm

    @GPUMemoryLogger(role="dp actor", logger=logger)
    def compute_log_prob(self, data: DataProto, calculate_entropy=False, calculate_self_certainty=False, calculate_sentence_certainty=False, calculate_sentence_entropy=False, calculate_sentence_avg_prob=False) -> torch.Tensor:
        """Compute the log probability of the responses given input_ids, attention_mask and position_ids

        Args:
            data (DataProto): a DataProto containing keys

                ``input_ids``: tensor of shape [batch_size, sequence_length]. torch.int64. Note that input_ids is the
                concatenation of prompt and response. Note that ``sequence_length = prompt_length + response_length``.

                ``attention_mask``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``position_ids``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``responses``:  tensor of shape [batch_size, response_length]. torch.int64.

        Returns:
            torch.Tensor: the log_prob tensor
        """
        # set to eval
        self.actor_module.eval()

        micro_batch_size = data.meta_info["micro_batch_size"]
        temperature = data.meta_info["temperature"]  # temperature must be in the data.meta_info to avoid silent error
        use_dynamic_bsz = data.meta_info["use_dynamic_bsz"]

        select_keys = ["responses", "input_ids", "attention_mask", "position_ids"]
        batch = data.select(batch_keys=select_keys).batch
        has_multi_modal_inputs = "multi_modal_inputs" in data.non_tensor_batch.keys()

        if has_multi_modal_inputs:
            num_micro_batches = data.batch.batch_size[0] // micro_batch_size
            non_tensor_select_keys = ["multi_modal_inputs"]
            micro_batches = data.select(select_keys, non_tensor_select_keys).chunk(num_micro_batches)
        elif use_dynamic_bsz:
            # split using dynamic bsz
            max_token_len = data.meta_info["max_token_len"] * self.ulysses_sequence_parallel_size
            micro_batches, indices = rearrange_micro_batches(batch=batch, max_token_len=max_token_len)
        else:
            micro_batches = batch.split(micro_batch_size)

        log_probs_lst = []
        entropy_lst = []
        self_certainty_lst = []
        sentence_certainty_lst = []
        sentence_entropy_lst = []
        sentence_avg_prob_lst = []
        for micro_batch in micro_batches:
            if isinstance(micro_batch, DataProto):
                micro_batch = {**micro_batch.batch, **micro_batch.non_tensor_batch}
            with torch.no_grad():
                entropy, log_probs, self_certainty, sentence_certainty, sentence_entropy, sentence_avg_prob = self._forward_micro_batch(micro_batch, temperature=temperature, 
                                                                                                                     calculate_entropy=calculate_entropy, 
                                                                                                                     calculate_self_certainty=calculate_self_certainty, 
                                                                                                                     calculate_sentence_certainty=calculate_sentence_certainty,
                                                                                                                     calculate_sentence_entropy=calculate_sentence_entropy,
                                                                                                                     calculate_sentence_avg_prob=calculate_sentence_avg_prob)
            log_probs_lst.append(log_probs)
            if calculate_entropy:
                entropy_lst.append(entropy)
            if calculate_self_certainty:
                self_certainty_lst.append(self_certainty)
            if calculate_sentence_certainty:
                sentence_certainty_lst.append(sentence_certainty)
            if calculate_sentence_entropy:
                sentence_entropy_lst.append(sentence_entropy)
            if calculate_sentence_avg_prob:
                sentence_avg_prob_lst.append(sentence_avg_prob)

        log_probs = torch.concat(log_probs_lst, dim=0)
        entropys = None
        self_certaintys = None
        sentence_certaintys = None
        sentence_entropys = None
        sentence_avg_probs = None
        if calculate_entropy:
            entropys = torch.concat(entropy_lst, dim=0)
        if calculate_self_certainty:
            self_certaintys = torch.concat(self_certainty_lst, dim=0)
        if calculate_sentence_certainty:
            sentence_certaintys = torch.concat(sentence_certainty_lst, dim=0)
        if calculate_sentence_entropy:
            sentence_entropys = torch.concat(sentence_entropy_lst, dim=0)
        if calculate_sentence_avg_prob:
            sentence_avg_probs = torch.concat(sentence_avg_prob_lst, dim=0)
        
        if use_dynamic_bsz:
            indices = list(itertools.chain.from_iterable(indices))
            assert len(indices) == log_probs.size(0), f"{len(indices)} vs. {log_probs.size()}"
            revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
            log_probs = log_probs[revert_indices]

        return log_probs, entropys, self_certaintys, sentence_certaintys, sentence_entropys, sentence_avg_probs

    @GPUMemoryLogger(role="dp actor", logger=logger)
    def update_policy(self, data_ori: DataProto, data_aug: DataProto):
        # make sure we are in training mode
        self.actor_module.train()

        temperature = data_ori.meta_info["temperature"]  # temperature must be in the data.meta_info to avoid silent error
        multi_turn = data_ori.meta_info.get("multi_turn", False)

        select_keys = ["responses", "input_ids", "attention_mask", "position_ids", "old_log_probs", "advantages"]
        if multi_turn:
            select_keys.append("loss_mask")
        if self.config.use_kl_loss:
            select_keys.append("ref_log_prob")
        batch_ori = data_ori.select(batch_keys=select_keys).batch
        batch_aug = data_aug.select(batch_keys=select_keys).batch
        has_multi_modal_inputs = "multi_modal_inputs" in data_ori.non_tensor_batch.keys()

        # Split to make minibatch iterator for updating the actor
        # See PPO paper for details. https://arxiv.org/abs/1707.06347
        # if has_multi_modal_inputs:
        #     num_mini_batches = data_ori.batch.batch_size[0] // self.config.ppo_mini_batch_size
        #     non_tensor_select_keys = ["multi_modal_inputs"]
        #     dataloader_ori = data_ori.select(select_keys, non_tensor_select_keys).chunk(num_mini_batches)
        # else:
        dataloader_ori = batch_ori.split(self.config.ppo_mini_batch_size)
        dataloader_aug = batch_aug.split(self.config.ppo_mini_batch_size)

        metrics = {}
        for epoch in range(self.config.ppo_epochs):
            for batch_idx, (data_o, data_a) in enumerate(zip(dataloader_ori, dataloader_aug)):
                # split batch into micro_batches
                mini_batch_ori = data_o
                mini_batch_aug = data_a
                # if has_multi_modal_inputs:
                #     self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size_per_gpu
                #     num_micro_batches = mini_batch.batch.batch_size[0] // self.config.ppo_micro_batch_size_per_gpu
                #     micro_batches = data.select(select_keys, non_tensor_select_keys).chunk(num_micro_batches)
                if self.config.use_dynamic_bsz:
                    max_token_len = self.config.ppo_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
                    micro_batches_ori, _ = rearrange_micro_batches(batch=mini_batch_ori, max_token_len=max_token_len)
                    micro_batches_aug, _ = rearrange_micro_batches(batch=mini_batch_aug, max_token_len=max_token_len)
                else:
                    self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size_per_gpu
                    # split batch into micro_batches
                    micro_batches_ori = mini_batch_ori.split(self.config.ppo_micro_batch_size_per_gpu)
                    micro_batches_aug = mini_batch_aug.split(self.config.ppo_micro_batch_size_per_gpu)

                self.actor_optimizer.zero_grad()

                for data_o, data_a in zip(micro_batches_ori, micro_batches_aug):
                    # Support all hardwares
                    if isinstance(data_o, DataProto):
                        data_o = {**data_o.batch.to(torch.cuda.current_device()), **data_o.non_tensor_batch}
                    else:
                        data_o = data_o.to(torch.cuda.current_device())  # actor device is cpu when using offload
                    if isinstance(data_a, DataProto):
                        data_a = {**data_a.batch.to(torch.cuda.current_device()), **data_a.non_tensor_batch}
                    else:
                        data_a = data_a.to(torch.cuda.current_device())  # actor device is cpu when using offload
                    responses_ori = data_o["responses"]
                    response_length_ori = responses_ori.size(1)
                    attention_mask_ori = data_o["attention_mask"]
                    if multi_turn:
                        response_mask_ori = data_o["loss_mask"][:, -response_length_ori:]
                    else:
                        response_mask_ori = attention_mask_ori[:, -response_length_ori:]
                    responses_aug = data_a["responses"]
                    response_length_aug = responses_aug.size(1)
                    attention_mask_aug = data_a["attention_mask"]
                    if multi_turn:
                        response_mask_aug = data_a["loss_mask"][:, -response_length_aug:]
                    else:
                        response_mask_aug = attention_mask_ori[:, -response_length_aug:]

                    old_log_prob_ori = data_o["old_log_probs"]
                    advantages_ori = data_o["advantages"]
                    old_log_prob_aug = data_a["old_log_probs"]
                    advantages_aug = data_a["advantages"]

                    clip_ratio = self.config.clip_ratio
                    clip_ratio_low = self.config.clip_ratio_low if self.config.clip_ratio_low is not None else clip_ratio
                    clip_ratio_high = self.config.clip_ratio_high if self.config.clip_ratio_high is not None else clip_ratio
                    clip_ratio_c = self.config.get("clip_ratio_c", 3.0)
                    entropy_coeff = self.config.entropy_coeff
                    loss_agg_mode = self.config.loss_agg_mode

                    # all return: (bsz, response_length)
                    calculate_entropy = False
                    if entropy_coeff != 0:
                        calculate_entropy = True
                    entropy_ori, log_prob_ori, *_ = self._forward_micro_batch(micro_batch=data_o, temperature=temperature, calculate_entropy=calculate_entropy, calculate_self_certainty=False, calculate_sentence_certainty=False)
                    entropy_aug, log_prob_aug, *_ = self._forward_micro_batch(micro_batch=data_a, temperature=temperature, calculate_entropy=calculate_entropy, calculate_self_certainty=False, calculate_sentence_certainty=False)
                    # breakpoint()
                    pg_loss_ori, pg_clipfrac_ori, ppo_kl_ori, pg_clipfrac_lower_ori = compute_policy_loss(
                        old_log_prob=old_log_prob_ori,
                        log_prob=log_prob_ori,
                        advantages=advantages_ori,
                        response_mask=response_mask_ori,
                        cliprange=clip_ratio,
                        cliprange_low=clip_ratio_low,
                        cliprange_high=clip_ratio_high,
                        clip_ratio_c=clip_ratio_c,
                        loss_agg_mode=loss_agg_mode,
                    )
                    pg_loss_aug, pg_clipfrac_aug, ppo_kl_aug, pg_clipfrac_lower_aug = compute_policy_loss(
                        old_log_prob=old_log_prob_aug,
                        log_prob=log_prob_aug,
                        advantages=advantages_aug,
                        response_mask=response_mask_aug,
                        cliprange=clip_ratio,
                        cliprange_low=clip_ratio_low,
                        cliprange_high=clip_ratio_high,
                        clip_ratio_c=clip_ratio_c,
                        loss_agg_mode=loss_agg_mode,
                    )

                    if entropy_coeff != 0:
                        entropy_loss_ori = agg_loss(loss_mat=entropy_ori, loss_mask=response_mask_ori, loss_agg_mode=loss_agg_mode)

                        # compute policy loss
                        policy_loss_ori = pg_loss_ori - entropy_loss_ori * entropy_coeff
                        
                        entropy_loss_aug = agg_loss(loss_mat=entropy_aug, loss_mask=response_mask_aug, loss_agg_mode=loss_agg_mode)

                        # compute policy loss
                        policy_loss_aug = pg_loss_aug - entropy_loss_aug * entropy_coeff
                    else:
                        policy_loss_ori = pg_loss_ori
                        policy_loss_aug = pg_loss_aug

                    if self.config.use_kl_loss:
                        ref_log_prob_ori = data_o["ref_log_prob"]
                        # compute kl loss
                        kld = kl_penalty(logprob=log_prob_ori, ref_logprob=ref_log_prob_ori, kl_penalty=self.config.kl_loss_type)
                        kl_loss_ori = agg_loss(loss_mat=kld, loss_mask=response_mask_ori, loss_agg_mode=self.config.loss_agg_mode)

                        policy_loss_ori = policy_loss_ori + kl_loss_ori * self.config.kl_loss_coef
                        metrics["actor/kl_loss_ori"] = kl_loss_ori.detach().item()
                        metrics["actor/kl_coef"] = self.config.kl_loss_coef

                        ref_log_prob_aug = data_a["ref_log_prob"]
                        # compute kl loss
                        kld = kl_penalty(logprob=log_prob_aug, ref_logprob=ref_log_prob_aug, kl_penalty=self.config.kl_loss_type)
                        kl_loss_aug = agg_loss(loss_mat=kld, loss_mask=response_mask_aug, loss_agg_mode=self.config.loss_agg_mode)

                        policy_loss_aug = policy_loss_aug + kl_loss_aug * self.config.kl_loss_coef
                        metrics["actor/kl_loss_aug"] = kl_loss_aug.detach().item()
                    if self.config.use_dynamic_bsz:
                        # relative to the dynamic bsz
                        loss = (policy_loss_ori + policy_loss_aug) * (len(data_o) / self.config.ppo_mini_batch_size)
                    else:
                        loss = (policy_loss_ori + policy_loss_aug) / self.gradient_accumulation
                    loss.backward()

                    data = {
                        "actor/pg_loss": (pg_loss_ori + pg_loss_aug).detach().item(),
                        "actor/pg_loss_ori": pg_loss_ori.detach().item(),
                        "actor/pg_loss_aug": pg_loss_aug.detach().item(),
                        "actor/pg_clipfrac_ori": pg_clipfrac_ori.detach().item(),
                        "actor/pg_clipfrac_aug": pg_clipfrac_aug.detach().item(),
                        "actor/ppo_kl_ori": ppo_kl_ori.detach().item(),
                        "actor/ppo_kl_aug": ppo_kl_aug.detach().item(),
                        "actor/pg_clipfrac_lower_ori": pg_clipfrac_lower_ori.detach().item(),
                        "actor/pg_clipfrac_lower_aug": pg_clipfrac_lower_aug.detach().item(),
                    }
                    append_to_dict(metrics, data)

                grad_norm = self._optimizer_step()
                data = {"actor/grad_norm": grad_norm.detach().item()}
            append_to_dict(metrics, data)
        self.actor_optimizer.zero_grad()
        return metrics
