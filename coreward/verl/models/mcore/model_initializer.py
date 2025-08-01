# Copyright 2025 Bytedance Ltd. and/or its affiliates
# Copyright (c) 2025, NVIDIA CORPORATION. All rights reserved.
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
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

# use mcore transformer config to initialize the model
from abc import ABC, abstractmethod

from megatron.core.models.gpt.gpt_layer_specs import get_gpt_decoder_block_spec
from megatron.core.models.gpt.gpt_model import GPTModel

from .config_converter import PretrainedConfig, TransformerConfig


class BaseModelInitializer(ABC):
    """Base class for model initializers."""

    def __init__(self, tfconfig: TransformerConfig, hf_config: PretrainedConfig):
        self.tfconfig = tfconfig
        self.hf_config = hf_config

    @abstractmethod
    def get_transformer_layer_spec(self):
        """Get the transformer layer specification.
        https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/core/models/gpt/gpt_layer_specs.py"""
        pass

    def get_rope_scaling_args(self) -> dict:
        """Get rope scaling args."""
        rope_scaling_args = {}
        if "rope_scaling" in self.hf_config:
            if self.hf_config.rope_scaling is not None:
                assert self.hf_config.rope_scaling["type"] == "linear", "only linear scaling is supported for now"
                rope_scaling_args["seq_len_interpolation_factor"] = self.hf_config.rope_scaling["factor"]
        return rope_scaling_args

    def initialize(
        self,
        pre_process: bool = True,
        post_process: bool = True,
        share_embeddings_and_output_weights: bool = False,
        value: bool = False,
        **extra_kwargs,
    ) -> GPTModel:
        """Initialize a GPT model with the given configuration.
        https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/core/models/gpt/gpt_model.py

        Args:
            pre_process (bool): include embedding layer.
            post_process (bool): including an output layer.
            share_embeddings_and_output_weights (bool): input embeddings and output logit weights are shared.
            value (bool): add an extra linear layer for classification or regression.

        Returns:
            GPTModel: An initialized GPT model instance
        """
        transformer_layer_spec = self.get_transformer_layer_spec()
        rope_scaling_args = self.get_rope_scaling_args()

        model = GPTModel(
            config=self.tfconfig,
            transformer_layer_spec=transformer_layer_spec,
            vocab_size=self.hf_config.vocab_size,
            max_sequence_length=self.hf_config.max_position_embeddings,
            pre_process=pre_process,
            post_process=post_process,
            share_embeddings_and_output_weights=share_embeddings_and_output_weights,
            position_embedding_type="rope",
            rotary_base=self.hf_config.rope_theta,
            **rope_scaling_args,
        )

        if post_process and value:
            from verl.models.llama.megatron.layers.parallel_linear import LinearForLastLayer

            model.output_layer = LinearForLastLayer(input_size=self.tfconfig.hidden_size, output_size=1, config=self.tfconfig)

        return model


class DenseModel(BaseModelInitializer):
    """Initializer for dense models like Llama and Qwen2."""

    def get_transformer_layer_spec(self):
        assert self.tfconfig.normalization == "RMSNorm", "only RMSNorm is supported for now"
        return get_gpt_decoder_block_spec(self.tfconfig, use_transformer_engine=True)


class Qwen2MoEModel(BaseModelInitializer):
    """Initializer for Qwen2 MoE models."""

    def get_transformer_layer_spec(self):
        assert self.tfconfig.normalization == "RMSNorm", "only RMSNorm is supported for now"
        transformer_layer_spec = get_gpt_decoder_block_spec(self.tfconfig, use_transformer_engine=True)

        # Patch layer spec for shared experts
        for i in range(len(transformer_layer_spec.layer_specs)):
            transformer_layer_spec.layer_specs[i].submodules.mlp.submodules.shared_experts.params["gate"] = True

        return transformer_layer_spec

    def initialize(self, freeze_moe_router: bool = True, **kwargs):
        # Qwen default freeze_moe_router: true
        model = super().initialize(**kwargs)
        if freeze_moe_router:
            for layer in model.decoder.layers:
                layer.mlp.router.weight.requires_grad = False
                layer.mlp.shared_experts.gate_weight.requires_grad = False
        return model


class MixtralModel(BaseModelInitializer):
    """Initializer for Mixtral models."""

    def get_transformer_layer_spec(self):
        assert self.tfconfig.normalization == "RMSNorm", "only RMSNorm is supported for now"
        transformer_layer_spec = get_gpt_decoder_block_spec(self.tfconfig, use_transformer_engine=True)
        return transformer_layer_spec

    def initialize(self, freeze_moe_router: bool = False, **kwargs):
        model = super().initialize(**kwargs)
        if freeze_moe_router:
            for layer in model.decoder.layers:
                layer.mlp.router.weight.requires_grad = False
        return model


class Qwen3MoEModel(BaseModelInitializer):
    """Initializer for Qwen3 MoE models."""

    def get_transformer_layer_spec(self):
        assert self.tfconfig.normalization == "RMSNorm", "only RMSNorm is supported for now"
        transformer_layer_spec = get_gpt_decoder_block_spec(self.tfconfig, use_transformer_engine=True)
        return transformer_layer_spec

    def initialize(self, freeze_moe_router: bool = True, **kwargs):
        # Qwen default freeze_moe_router: true
        model = super().initialize(**kwargs)
        if freeze_moe_router:
            for layer in model.decoder.layers:
                layer.mlp.router.weight.requires_grad = False
        return model


class Qwen25VLModel(BaseModelInitializer):
    """Initializer for Qwen2.5 VL models."""

    def get_transformer_layer_spec(self):
        raise NotImplementedError("VLM is not supported yet")
