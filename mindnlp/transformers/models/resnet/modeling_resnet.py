# coding=utf-8
# Copyright 2022 Microsoft Research, Inc. and The HuggingFace Inc. team. All rights reserved.
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
""" MindSpore ResNet model."""

from typing import Optional

import mindspore
from mindspore import nn, ops
from mindspore.common.initializer import HeNormal

from ...activations import ACT2FN
from ...modeling_outputs import (
    BackboneOutput,
    BaseModelOutputWithNoAttention,
    BaseModelOutputWithPoolingAndNoAttention,
    ImageClassifierOutputWithNoAttention,
)
from ...modeling_utils import PreTrainedModel
from ....utils import logging
from ...backbone_utils import BackboneMixin
from .configuration_resnet import ResNetConfig


logger = logging.get_logger(__name__)

# General docstring
_CONFIG_FOR_DOC = "ResNetConfig"

# Base docstring
_CHECKPOINT_FOR_DOC = "microsoft/resnet-50"
_EXPECTED_OUTPUT_SHAPE = [1, 2048, 7, 7]

# Image classification docstring
_IMAGE_CLASS_CHECKPOINT = "microsoft/resnet-50"
_IMAGE_CLASS_EXPECTED_OUTPUT = "tiger cat"


class ResNetConvLayer(nn.Cell):
    def __init__(
        self, in_channels: int, out_channels: int, kernel_size: int = 3, stride: int = 1, activation: str = "relu"
    ):
        super().__init__()
        self.convolution = nn.Conv2d(
            in_channels, out_channels,
            kernel_size=kernel_size, stride=stride, padding=kernel_size // 2, has_bias=False,
            pad_mode='pad'
        )
        self.normalization = nn.BatchNorm2d(out_channels)
        self.activation = ACT2FN[activation] if activation is not None else nn.Identity()

    def construct(self, input: mindspore.Tensor) -> mindspore.Tensor:
        hidden_state = self.convolution(input)
        hidden_state = self.normalization(hidden_state)
        hidden_state = self.activation(hidden_state)
        return hidden_state


class ResNetEmbeddings(nn.Cell):
    """
    ResNet Embeddings (stem) composed of a single aggressive convolution.
    """

    def __init__(self, config: ResNetConfig):
        super().__init__()
        self.embedder = ResNetConvLayer(
            config.num_channels, config.embedding_size, kernel_size=7, stride=2, activation=config.hidden_act
        )
        self.pooler = nn.MaxPool2d(kernel_size=3, stride=2, padding=1, pad_mode='pad')
        self.num_channels = config.num_channels

    def construct(self, pixel_values: mindspore.Tensor) -> mindspore.Tensor:
        num_channels = pixel_values.shape[1]
        if num_channels != self.num_channels:
            raise ValueError(
                "Make sure that the channel dimension of the pixel values match with the one set in the configuration."
            )
        embedding = self.embedder(pixel_values)
        embedding = self.pooler(embedding)
        return embedding


class ResNetShortCut(nn.Cell):
    """
    ResNet shortcut, used to project the residual features to the correct size. If needed, it is also used to
    downsample the input using `stride=2`.
    """

    def __init__(self, in_channels: int, out_channels: int, stride: int = 2):
        super().__init__()
        self.convolution = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride, has_bias=False, pad_mode='valid')
        self.normalization = nn.BatchNorm2d(out_channels)

    def construct(self, input: mindspore.Tensor) -> mindspore.Tensor:
        hidden_state = self.convolution(input)
        hidden_state = self.normalization(hidden_state)
        return hidden_state


class ResNetBasicLayer(nn.Cell):
    """
    A classic ResNet's residual layer composed by two `3x3` convolutions.
    """

    def __init__(self, in_channels: int, out_channels: int, stride: int = 1, activation: str = "relu"):
        super().__init__()
        should_apply_shortcut = in_channels != out_channels or stride != 1
        self.shortcut = (
            ResNetShortCut(in_channels, out_channels, stride=stride) if should_apply_shortcut else nn.Identity()
        )
        self.layer = nn.SequentialCell(
            ResNetConvLayer(in_channels, out_channels, stride=stride),
            ResNetConvLayer(out_channels, out_channels, activation=None),
        )
        self.activation = ACT2FN[activation]

    def construct(self, hidden_state):
        residual = hidden_state
        hidden_state = self.layer(hidden_state)
        residual = self.shortcut(residual)
        hidden_state += residual
        hidden_state = self.activation(hidden_state)
        return hidden_state


class ResNetBottleNeckLayer(nn.Cell):
    """
    A classic ResNet's bottleneck layer composed by three `3x3` convolutions.

    The first `1x1` convolution reduces the input by a factor of `reduction` in order to make the second `3x3`
    convolution faster. The last `1x1` convolution remaps the reduced features to `out_channels`. If
    `downsample_in_bottleneck` is true, downsample will be in the first layer instead of the second layer.
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        stride: int = 1,
        activation: str = "relu",
        reduction: int = 4,
        downsample_in_bottleneck: bool = False,
    ):
        super().__init__()
        should_apply_shortcut = in_channels != out_channels or stride != 1
        reduces_channels = out_channels // reduction
        self.shortcut = (
            ResNetShortCut(in_channels, out_channels, stride=stride) if should_apply_shortcut else nn.Identity()
        )
        self.layer = nn.SequentialCell(
            ResNetConvLayer(
                in_channels, reduces_channels, kernel_size=1, stride=stride if downsample_in_bottleneck else 1
            ),
            ResNetConvLayer(reduces_channels, reduces_channels, stride=stride if not downsample_in_bottleneck else 1),
            ResNetConvLayer(reduces_channels, out_channels, kernel_size=1, activation=None),
        )
        self.activation = ACT2FN[activation]

    def construct(self, hidden_state):
        residual = hidden_state
        hidden_state = self.layer(hidden_state)
        residual = self.shortcut(residual)
        hidden_state += residual
        hidden_state = self.activation(hidden_state)
        return hidden_state


class ResNetStage(nn.Cell):
    """
    A ResNet stage composed by stacked layers.
    """

    def __init__(
        self,
        config: ResNetConfig,
        in_channels: int,
        out_channels: int,
        stride: int = 2,
        depth: int = 2,
    ):
        super().__init__()

        layer = ResNetBottleNeckLayer if config.layer_type == "bottleneck" else ResNetBasicLayer

        if config.layer_type == "bottleneck":
            first_layer = layer(
                in_channels,
                out_channels,
                stride=stride,
                activation=config.hidden_act,
                downsample_in_bottleneck=config.downsample_in_bottleneck,
            )
        else:
            first_layer = layer(in_channels, out_channels, stride=stride, activation=config.hidden_act)
        self.layers = nn.SequentialCell(
            first_layer, *[layer(out_channels, out_channels, activation=config.hidden_act) for _ in range(depth - 1)]
        )

    def construct(self, input: mindspore.Tensor) -> mindspore.Tensor:
        hidden_state = input
        for layer in self.layers:
            hidden_state = layer(hidden_state)
        return hidden_state


class ResNetEncoder(nn.Cell):
    def __init__(self, config: ResNetConfig):
        super().__init__()
        self.stages = nn.CellList([])
        # based on `downsample_in_first_stage` the first layer of the first stage may or may not downsample the input
        self.stages.append(
            ResNetStage(
                config,
                config.embedding_size,
                config.hidden_sizes[0],
                stride=2 if config.downsample_in_first_stage else 1,
                depth=config.depths[0],
            )
        )
        in_out_channels = zip(config.hidden_sizes, config.hidden_sizes[1:])
        for (in_channels, out_channels), depth in zip(in_out_channels, config.depths[1:]):
            self.stages.append(ResNetStage(config, in_channels, out_channels, depth=depth))

    def construct(
        self, hidden_state: mindspore.Tensor, output_hidden_states: bool = False, return_dict: bool = True
    ) -> BaseModelOutputWithNoAttention:
        hidden_states = () if output_hidden_states else None

        for stage_module in self.stages:
            if output_hidden_states:
                hidden_states = hidden_states + (hidden_state,)

            hidden_state = stage_module(hidden_state)

        if output_hidden_states:
            hidden_states = hidden_states + (hidden_state,)

        if not return_dict:
            return tuple(v for v in [hidden_state, hidden_states] if v is not None)

        return BaseModelOutputWithNoAttention(
            last_hidden_state=hidden_state,
            hidden_states=hidden_states,
        )


class ResNetPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = ResNetConfig
    base_model_prefix = "resnet"
    main_input_name = "pixel_values"
    _no_split_modules = ["ResNetConvLayer", "ResNetShortCut"]
    _keys_to_ignore_on_load_unexpected = [r'num_batches_tracked']

    def _init_weights(self, module):
        if isinstance(module, nn.Conv2d):
            module.weight.initialize(HeNormal(mode='fan_out', nonlinearity='relu'))
        elif isinstance(module, (nn.BatchNorm2d, nn.GroupNorm)):
            module.weight.initialize('ones')
            module.bias.initialize('zeros')


class ResNetModel(ResNetPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.embedder = ResNetEmbeddings(config)
        self.encoder = ResNetEncoder(config)
        self.pooler = nn.AdaptiveAvgPool2d((1, 1))
        # Initialize weights and apply final processing
        self.post_init()

    def construct(
        self, pixel_values: mindspore.Tensor, output_hidden_states: Optional[bool] = None, return_dict: Optional[bool] = None
    ) -> BaseModelOutputWithPoolingAndNoAttention:
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        embedding_output = self.embedder(pixel_values)

        encoder_outputs = self.encoder(
            embedding_output, output_hidden_states=output_hidden_states, return_dict=return_dict
        )

        last_hidden_state = encoder_outputs[0]

        pooled_output = self.pooler(last_hidden_state)

        if not return_dict:
            return (last_hidden_state, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPoolingAndNoAttention(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=encoder_outputs.hidden_states,
        )


class ResNetForImageClassification(ResNetPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.resnet = ResNetModel(config)
        # classification head
        self.classifier = nn.SequentialCell(
            nn.Flatten(),
            nn.Dense(config.hidden_sizes[-1], config.num_labels) if config.num_labels > 0 else nn.Identity(),
        )
        # initialize weights and apply final processing
        self.post_init()

    def construct(
        self,
        pixel_values: Optional[mindspore.Tensor] = None,
        labels: Optional[mindspore.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> ImageClassifierOutputWithNoAttention:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the image classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.resnet(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(pooled_output)

        loss = None

        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and labels.dtype in (mindspore.int64, mindspore.int32):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"
            if self.config.problem_type == "regression":
                if self.num_labels == 1:
                    loss = ops.mse_loss(logits.squeeze(), labels.squeeze())
                else:
                    loss = ops.mse_loss(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss = ops.cross_entropy(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss = ops.binary_cross_entropy_with_logits(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return (loss,) + output if loss is not None else output

        return ImageClassifierOutputWithNoAttention(loss=loss, logits=logits, hidden_states=outputs.hidden_states)


class ResNetBackbone(ResNetPreTrainedModel, BackboneMixin):
    def __init__(self, config):
        super().__init__(config)
        super()._init_backbone(config)

        self.num_features = [config.embedding_size] + config.hidden_sizes
        self.embedder = ResNetEmbeddings(config)
        self.encoder = ResNetEncoder(config)

        # initialize weights and apply final processing
        self.post_init()

    def construct(
        self, pixel_values: mindspore.Tensor, output_hidden_states: Optional[bool] = None, return_dict: Optional[bool] = None
    ) -> BackboneOutput:
        """
        Returns:

        Examples:

        ```python
        >>> from transformers import AutoImageProcessor, AutoBackbone
        >>> import torch
        >>> from PIL import Image
        >>> import requests

        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> processor = AutoImageProcessor.from_pretrained("microsoft/resnet-50")
        >>> model = AutoBackbone.from_pretrained(
        ...     "microsoft/resnet-50", out_features=["stage1", "stage2", "stage3", "stage4"]
        ... )

        >>> inputs = processor(image, return_tensors="pt")

        >>> outputs = model(**inputs)
        >>> feature_maps = outputs.feature_maps
        >>> list(feature_maps[-1].shape)
        [1, 2048, 7, 7]
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )

        embedding_output = self.embedder(pixel_values)

        outputs = self.encoder(embedding_output, output_hidden_states=True, return_dict=True)

        hidden_states = outputs.hidden_states

        feature_maps = ()
        for idx, stage in enumerate(self.stage_names):
            if stage in self.out_features:
                feature_maps += (hidden_states[idx],)

        if not return_dict:
            output = (feature_maps,)
            if output_hidden_states:
                output += (outputs.hidden_states,)
            return output

        return BackboneOutput(
            feature_maps=feature_maps,
            hidden_states=outputs.hidden_states if output_hidden_states else None,
            attentions=None,
        )

__all__ = [
    "ResNetForImageClassification",
    "ResNetModel",
    "ResNetPreTrainedModel",
    "ResNetBackbone",
]
