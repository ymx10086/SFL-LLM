import logging
import math
from typing import Optional, Union, Tuple

import torch
from regex import regex
from torch.nn import CrossEntropyLoss
from transformers import GPT2LMHeadModel, GPT2Model
from transformers.modeling_outputs import BaseModelOutputWithPastAndCrossAttentions, CausalLMOutputWithCrossAttentions

from sfl.model.noise import DxPrivacy
from sfl.model.split_model import SplitModel
from sfl.simulator.param_keeper import ParameterKeeper
from sfl.config import FLConfig

logger = logging.getLogger(__name__)


class GPT2SplitLMHeadModel(GPT2LMHeadModel, SplitModel):
    """
    GPT2带head的模型，最后一层用于文本生成
    """

    def __init__(self, config):
        super(GPT2SplitLMHeadModel, self).__init__(config)
        self.transformer = GPT2SplitModel(config)

    def get_top_to_trunk_grad(self, detach=True):
        return self.transformer.get_top_to_trunk_grad(detach)

    def get_trunk_to_bottom_grad(self, detach=True):
        return self.transformer.get_trunk_to_bottom_grad(detach)

    def get_adapter_module_regex(self):
        # Trunk部分(h.start~h.end)的proj/fc/_attn模块
        if self.fl_config is not None:
            blocks = []
            if self.fl_config.use_lora_at_bottom:
                blocks += [str(i) for i in range(self.fl_config.split_point_1)]
            if self.fl_config.use_lora_at_trunk:
                blocks += [str(i) for i in range(self.fl_config.split_point_1, self.fl_config.split_point_2)]
            if self.fl_config.use_lora_at_top:
                blocks += [str(i) for i in range(self.fl_config.split_point_2, self.config.n_layer)]
            reg = rf".*\.h\.({'|'.join(blocks)})\..*(.+attn|proj|fc)$"
            return reg
        return ""

    @staticmethod
    def _get_block_num(param_name: str):
        # 获得该参数所属的block的块号，不属于block则返回-1
        r = regex.findall('\.h\.[0-9]+', param_name)
        return int(r[0].split('.')[-1]) if len(r) > 0 else -1

    def get_bottom_params(self, trainable_only=True):
        for nm, p in self.named_parameters():
            if trainable_only and not p.requires_grad:
                continue
            if self._get_block_num(nm) >= self.fl_config.split_point_1:
                break
            else:
                yield nm, p

    def get_top_params(self, trainable_only=True):
        trunk = False
        for nm, p in self.named_parameters():
            if trainable_only and not p.requires_grad:
                continue
            if self._get_block_num(nm) >= self.fl_config.split_point_2:
                trunk = True
            if trunk:
                yield nm, p

    def get_trunk_params(self, trainable_only=True):
        for nm, p in self.named_parameters():
            if trainable_only and not p.requires_grad:
                continue
            if self.fl_config.split_point_1 <= self._get_block_num(nm) < self.fl_config.split_point_2:
                yield nm, p

    def reset_params(self, named_params, reset_mode: str):
        for name, p in named_params:
            if reset_mode == 'Embedding':
                if 'wte' in name or 'wpe' in name:
                    continue
            if 'ln_' in name:
                if 'bias' in name:
                    p.data.zero_()
                elif 'ln_' in name and 'weight' in name:
                    p.data.fill_(1.0)
            elif 'mlp' in name or 'wte' in name or 'wpe' in name:
                if 'weight' in name:
                    p.data.normal_(mean=0.0, std=self.config.initializer_range)
                elif 'bias' in name:
                    p.data.zero_()
            if name == "c_proj.weight":
                # Special Scaled Initialization --> There are 2 Layer Norms per Transformer Block
                p.data.normal_(mean=0.0,
                               std=(self.config.initializer_range / math.sqrt(2 * self.config.n_layer)))

    def config_sfl(self, config: FLConfig, param_keeper: ParameterKeeper | None):
        super(GPT2SplitLMHeadModel, self).config_sfl(config, param_keeper)
        self.transformer.config_sfl(config, param_keeper)

    def forward(
            self,
            input_ids: Optional[torch.LongTensor] = None,
            past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
            attention_mask: Optional[torch.FloatTensor] = None,
            token_type_ids: Optional[torch.LongTensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            head_mask: Optional[torch.FloatTensor] = None,
            inputs_embeds: Optional[torch.FloatTensor] = None,
            encoder_hidden_states: Optional[torch.Tensor] = None,
            encoder_attention_mask: Optional[torch.FloatTensor] = None,
            labels: Optional[torch.LongTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithCrossAttentions]:
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.transformer(
            input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        if self.fl_config and self.fl_config.attack_mode:
            return transformer_outputs
        hidden_states = transformer_outputs[0]

        # Set device for model parallelism
        if self.model_parallel:
            torch.cuda.set_device(self.transformer.first_device)
            hidden_states = hidden_states.to(self.lm_head.weight.device)

        lm_logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            # move labels to correct device to enable model parallelism
            labels = labels.to(lm_logits.device)
            # Shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=lm_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
        )

    def cut_forward(self, hidden_states):
        hidden_states = self.transformer.cut_forward(hidden_states)[0]
        lm_logits = self.lm_head(hidden_states)
        return CausalLMOutputWithCrossAttentions(
            logits=lm_logits
        )


class GPT2SplitModel(GPT2Model, SplitModel):
    """
    GPT2主模型，主要在FP过程中收集中间输出和梯度
    """

    def get_adapter_module_regex(self):
        pass

    def get_bottom_params(self, trainable_only=True):
        pass

    def get_top_params(self, trainable_only=True):
        pass

    def get_trunk_params(self, trainable_only=True):
        pass

    def get_top_to_trunk_grad(self, detach=True):
        if 'trunk_to_top' in self.intermediate_fx:
            if detach:
                return self.intermediate_fx['trunk_to_top'].detach().cpu(), self.intermediate_fx[
                    'trunk_to_top'].grad.clone().detach().cpu()
            else:
                return self.intermediate_fx['trunk_to_top'], self.intermediate_fx['trunk_to_top'].grad
        return []

    def get_trunk_to_bottom_grad(self, detach=True):
        if 'bottom_to_trunk' in self.intermediate_fx:
            if detach:
                return self.intermediate_fx['bottom_to_trunk'].detach().cpu(), self.intermediate_fx[
                    'bottom_to_trunk'].grad.clone().detach().cpu()
            else:
                return self.intermediate_fx['bottom_to_trunk'], self.intermediate_fx[
                    'bottom_to_trunk'].grad
        return []

    def _store_bottom_to_trunk_fx(self, fx):
        self.intermediate_fx['bottom_to_trunk'] = fx

    def _store_trunk_to_top_fx(self, fx):
        self.intermediate_fx['trunk_to_top'] = fx

    def __init__(self, config):
        super().__init__(config)
        self.perturber = None
        self.intermediate_fx = {}

    def config_sfl(self, config: FLConfig, param_keeper: ParameterKeeper | None):
        super().config_sfl(config, param_keeper)
        self.perturber = DxPrivacy(self.wte, self.config.vocab_size, self.fl_config.noise_scale)

    def forward(
            self,
            input_ids: Optional[torch.LongTensor] = None,
            past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
            attention_mask: Optional[torch.FloatTensor] = None,
            token_type_ids: Optional[torch.LongTensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            head_mask: Optional[torch.FloatTensor] = None,
            inputs_embeds: Optional[torch.FloatTensor] = None,
            encoder_hidden_states: Optional[torch.Tensor] = None,
            encoder_attention_mask: Optional[torch.FloatTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPastAndCrossAttentions]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            self.warn_if_padding_and_no_attention_mask(input_ids, attention_mask)
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
            batch_size = input_ids.shape[0]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
            batch_size = inputs_embeds.shape[0]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if token_type_ids is not None:
            token_type_ids = token_type_ids.view(-1, input_shape[-1])
        if position_ids is not None:
            position_ids = position_ids.view(-1, input_shape[-1])

        if past_key_values is None:
            past_length = 0
            past_key_values = tuple([None] * len(self.h))
        else:
            past_length = past_key_values[0][0].size(-2)
        if position_ids is None:
            position_ids = torch.arange(past_length, input_shape[-1] + past_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).view(-1, input_shape[-1])

        # GPT2Attention mask.
        if attention_mask is not None:
            if batch_size <= 0:
                raise ValueError("batch_size has to be defined and > 0")
            attention_mask = attention_mask.view(batch_size, -1)
            # We create a 3D attention mask from a 2D tensor mask.
            # Sizes are [batch_size, 1, 1, to_seq_length]
            # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
            # this attention mask is more simple than the triangular masking of causal attention
            # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
            attention_mask = attention_mask[:, None, None, :]

            # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
            # masked positions, this operation will create a tensor which is 0.0 for
            # positions we want to attend and the dtype's smallest value for masked positions.
            # Since we are adding it to the raw scores before the softmax, this is
            # effectively the same as removing these entirely.
            attention_mask = attention_mask.to(dtype=self.dtype)  # fp16 compatibility
            attention_mask = (1.0 - attention_mask) * torch.finfo(self.dtype).min

        # If a 2D or 3D attention mask is provided for the cross-attention
        # we need to make broadcastable to [batch_size, num_heads, seq_length, seq_length]
        if self.config.add_cross_attention and encoder_hidden_states is not None:
            encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()
            encoder_hidden_shape = (encoder_batch_size, encoder_sequence_length)
            if encoder_attention_mask is None:
                encoder_attention_mask = torch.ones(encoder_hidden_shape, device=device)
            encoder_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # head_mask has shape n_layer x batch x n_heads x N x N
        head_mask = self.get_head_mask(head_mask, self.config.n_layer)

        if inputs_embeds is None:
            inputs_embeds = self.wte(input_ids)
            if self.fl_config and self.fl_config.noise_mode == 'dxp':
                inputs_embeds = self.perturber(inputs_embeds)
        position_embeds = self.wpe(position_ids)
        hidden_states = inputs_embeds + position_embeds

        if token_type_ids is not None:
            token_type_embeds = self.wte(token_type_ids)
            hidden_states = hidden_states + token_type_embeds

        hidden_states = self.drop(hidden_states)

        output_shape = (-1,) + input_shape[1:] + (hidden_states.size(-1),)

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        presents = () if use_cache else None
        all_self_attentions = () if output_attentions else None
        all_cross_attentions = () if output_attentions and self.config.add_cross_attention else None
        all_hidden_states = () if output_hidden_states else None
        for i, (block, layer_past) in enumerate(zip(self.h, past_key_values)):
            # Model parallel
            if self.model_parallel:
                torch.cuda.set_device(hidden_states.device)
                # Ensure layer_past is on same device as hidden_states (might not be correct)
                if layer_past is not None:
                    layer_past = tuple(past_state.to(hidden_states.device) for past_state in layer_past)
                # Ensure that attention_mask is always on the same device as hidden_states
                if attention_mask is not None:
                    attention_mask = attention_mask.to(hidden_states.device)
                if isinstance(head_mask, torch.Tensor):
                    head_mask = head_mask.to(hidden_states.device)
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, use_cache, output_attentions)

                    return custom_forward

                outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    None,
                    attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                outputs = block(
                    hidden_states,
                    layer_past=layer_past,
                    attention_mask=attention_mask,
                    head_mask=head_mask[i],
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_attention_mask,
                    use_cache=use_cache,
                    output_attentions=output_attentions,
                )

            hidden_states = outputs[0]
            if use_cache is True:
                presents = presents + (outputs[1],)

            if output_attentions:
                all_self_attentions = all_self_attentions + (outputs[2 if use_cache else 1],)
                if self.config.add_cross_attention:
                    all_cross_attentions = all_cross_attentions + (outputs[3 if use_cache else 2],)

            # Model Parallel: If it's the last layer for that device, put things on the next device
            if self.model_parallel:
                for k, v in self.device_map.items():
                    if i == v[-1] and "cuda:" + str(k) != self.last_device:
                        hidden_states = hidden_states.to("cuda:" + str(k + 1))

            # SFL: store intermediate hidden states
            if self.fl_config and self.fl_config.attack_mode:
                if i == self.fl_config.split_point_1 - 1 and self.fl_config.attack_mode == 'b2tr':
                    return hidden_states
                elif i == self.fl_config.split_point_2 and self.fl_config.attack_mode == 'tr2t':
                    return hidden_states

            if self.training and self.fl_config is not None and self.fl_config.collect_intermediates:
                if i == self.fl_config.split_point_1 - 1:  # bottom-trunk
                    if self.fl_config.noise_mode == 'gaussian':
                        min = hidden_states.min()
                        max = hidden_states.max()
                        noise = torch.randn_like(hidden_states)
                        noise = noise * (max - min) * self.fl_config.noise_scale
                        hidden_states = hidden_states + noise
                    hidden_states.retain_grad()
                    self._store_bottom_to_trunk_fx(hidden_states)
                elif i == self.fl_config.split_point_2:  # trunk-top
                    hidden_states.retain_grad()
                    self._store_trunk_to_top_fx(hidden_states)

        hidden_states = self.ln_f(hidden_states)

        hidden_states = hidden_states.view(output_shape)
        # Add last hidden state
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, presents, all_hidden_states, all_self_attentions, all_cross_attentions]
                if v is not None
            )

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )

    def cut_forward(self, hidden_states):
        for i, block in enumerate(self.h):
            if i < self.fl_config.split_point_2:
                continue
            outputs = block(
                hidden_states)
            hidden_states = outputs[0]
        hidden_states = self.ln_f(hidden_states)
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states
        )
