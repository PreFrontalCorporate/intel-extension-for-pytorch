import torch
import torch.nn as nn
from common_utils import TestCase
import unittest
from typing import Tuple
import intel_extension_for_pytorch as ipex
import itertools


class MaskedMHA(torch.nn.Module):
    def __init__(self, hidden_size=4096, n_head=16, n_head_kv=16, head_dim=256):
        super().__init__()
        self.num_heads = n_head
        self.num_kv = n_head_kv
        self.head_dim = head_dim
        self.query_key_value = nn.Linear(
            hidden_size, (n_head_kv * 2 + n_head) * head_dim
        )

    def _split_heads(
        self, fused_qkv: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Split the last dimension into (num_heads, head_dim), results share same memory
        storage as `fused_qkv`

        Args:
            fused_qkv (`torch.tensor`, *required*): [batch_size, seq_length, (num_heads + kv_num * 2) * head_dim]

        Returns:
            query: [batch_size, seq_length, num_heads, head_dim]
            key: [batch_size, seq_length, kv_num, head_dim]
            value: [batch_size, seq_length, kv_num, head_dim]
        """
        bs = fused_qkv.shape[0]
        query_layer = fused_qkv[:, :, : self.num_heads * self.head_dim]
        query_layer = query_layer.view(bs, -1, self.num_heads, self.head_dim)
        key_layer = fused_qkv[
            :,
            :,
            self.num_heads
            * self.head_dim : (self.num_heads + self.num_kv)
            * self.head_dim,
        ]
        key_layer = key_layer.view(bs, -1, self.num_kv, self.head_dim)
        value_layer = fused_qkv[:, :, (self.num_heads + self.num_kv) * self.head_dim :]
        value_layer = value_layer.view(bs, -1, self.num_kv, self.head_dim)
        return query_layer, key_layer, value_layer

    def _repeat_kv(self, x: torch.Tensor, n_rep: int) -> torch.Tensor:
        "torch.repeat_interleave(x, dim=2, repeats=n_rep)"
        bs, slen, n_kv_heads, head_dim = x.shape
        if n_rep == 1:
            return x
        return (
            x[:, :, :, None, :]
            .expand(bs, slen, n_kv_heads, n_rep, head_dim)
            .reshape(bs, slen, n_kv_heads * n_rep, head_dim)
        )

    def forward(
        self,
        input_t,
        key_cache,
        value_cache,
        max_position,
        attention_mask,
        beam_idx,
        indirect_access_kv_cache=False,
        offset=0,
        enable_linear=True,
        fp8=False,
        bf16=False,
    ):
        qtype = torch.float8_e5m2
        head_size = self.head_dim
        origin_type = input_t.dtype
        with torch.autocast(
            device_type="cpu",
            enabled=bf16,
            dtype=torch.bfloat16,
        ):
            if enable_linear:
                query, key, value = self._split_heads(self.query_key_value(input_t))
            else:
                query, key, value = self._split_heads(input_t)
        key = key.to(qtype).to(origin_type)
        value = value.to(qtype).to(origin_type)
        if key_cache is not None:
            key_cache = key_cache.to(qtype).to(origin_type)
            if fp8:
                key_cache = key_cache.to(qtype)
        if value_cache is not None:
            value_cache = value_cache.to(qtype).to(origin_type)
            if fp8:
                value_cache = value_cache.to(qtype)
        if indirect_access_kv_cache:
            query = query.contiguous()
            key = key.contiguous()
            value = value.contiguous()
            return torch.ops.torch_ipex.masked_multihead_self_attention(
                query,
                key,
                value,
                key_cache,
                value_cache,
                beam_idx,
                offset,
                head_size**0.5,
                max_position,
                None,
                attention_mask,
            )
        else:
            # Get the concatenated key and value
            if key_cache is not None:
                key = torch.cat([key_cache, key], dim=1)
                value = torch.cat([value_cache, value], dim=1)
            key_cache = key
            value_cache = value
            n_rep = self.num_heads // self.num_kv
            key = self._repeat_kv(key, n_rep)
            value = self._repeat_kv(value, n_rep)

            key = key.transpose(1, 2)
            query = query.transpose(1, 2)
            value = value.transpose(1, 2)
            if origin_type == torch.half:
                key = key.to(torch.float32)
                query = query.to(torch.float32)
                value = value.to(torch.float32)
            # matmul new_key and new_value to get the attention score
            attention_scores = torch.matmul(query, key.transpose(-1, -2))
            # scale the attention score
            attention_scores = attention_scores / (head_size**0.5)
            if attention_mask is not None:
                attention_scores = attention_scores + attention_mask
            # softmax the attention score
            attention_probs = attention_scores.softmax(dim=-1)
            # matmul the attention score and value to get the context
            attention_output = torch.matmul(attention_probs, value)
            if origin_type == torch.half:
                attention_output = attention_output.to(origin_type)
            return attention_output, None, key_cache, value_cache, None


class MaskedMHATest(TestCase):
    def _test_mha(self, torchcompile=False):
        beam_size_list = [1, 4]
        batch_size_list = [1, 2, 4]
        head_size_list = [256, 120]
        head_num = 16
        head_num_kv_list = [1, 4, 16]
        max_seq_len = 64
        first_seq_len = 32
        for batch_size, beam_size, head_num_kv, head_size in itertools.product(
            batch_size_list, beam_size_list, head_num_kv_list, head_size_list
        ):
            key_cache = None
            value_cache = None
            offset = 0
            hidden_size = head_num * head_size
            mha = MaskedMHA(
                hidden_size=hidden_size,
                n_head=head_num,
                n_head_kv=head_num_kv,
                head_dim=head_size,
            )

            if torchcompile:
                torch._dynamo.reset()
                torch._dynamo.config.capture_dynamic_output_shape_ops = True
                ipex._set_compiler_backend("inductor")
                mha = torch.compile(mha, backend="ipex", dynamic=True)

            # first token decode
            input_t = torch.randn(
                batch_size,
                first_seq_len,
                head_num * head_size,
                dtype=torch.float32,
            )
            key_cache_iakv = torch.randn(
                max_seq_len,
                beam_size * batch_size,
                head_num,
                head_size,
                dtype=torch.float32,
            )
            value_cache_iakv = torch.randn(
                max_seq_len,
                beam_size * batch_size,
                head_num,
                head_size,
                dtype=torch.float32,
            )
            beam_idx = torch.zeros(
                max_seq_len, beam_size * batch_size, dtype=torch.int64
            )
            # create attention mask and causal mask
            attention_mask = torch.zeros(
                batch_size, 1, first_seq_len, first_seq_len, dtype=torch.float32
            )
            casual_mask = torch.full(
                (first_seq_len, first_seq_len), -1e6, dtype=input_t.dtype
            )
            casual_mask = casual_mask.triu(1)
            casual_mask = casual_mask.unsqueeze(0).unsqueeze(0)
            attention_mask = (
                attention_mask + casual_mask
            )  # combine the attention mask and causal mask
            # UT for first token with fp32
            with torch.inference_mode(), torch.no_grad():
                naive_output, _, key_cache, value_cache, _ = mha(
                    input_t, None, None, max_seq_len, attention_mask, None, None
                )
                (
                    indirect_access_kv_cache_output,
                    _,
                    key_cache_iakv,
                    value_cache_iakv,
                    beam_idx,
                ) = mha(
                    input_t,
                    key_cache_iakv,
                    value_cache_iakv,
                    max_seq_len,
                    attention_mask,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                )
                # self.assertEqual(naive_output, indirect_access_kv_cache_output)
                key_cache = key_cache.repeat_interleave(beam_size, dim=0)
                value_cache = value_cache.repeat_interleave(beam_size, dim=0)
                for i in range(batch_size):
                    self.assertEqual(
                        key_cache.transpose(0, 1)[:, i * beam_size, :, :],
                        key_cache_iakv[0:first_seq_len, i * beam_size, :, :],
                    )
                    self.assertEqual(
                        value_cache.transpose(0, 1)[:, i * beam_size, :, :],
                        value_cache_iakv[0:first_seq_len, i * beam_size, :, :],
                    )
                if beam_size == 4:
                    beam_idx_t = torch.zeros(beam_size * batch_size, dtype=torch.int64)
                    for i in range(1, batch_size):
                        beam_idx_t[i * beam_size : i * beam_size + beam_size] = (
                            beam_idx_t[i * beam_size : i * beam_size + beam_size]
                            + i * beam_size
                        )
                elif beam_size == 1:
                    beam_idx_t = torch.arange(batch_size)
                beam_idx[offset] = beam_idx_t
                # reorder cache for naive impelementation
                key_cache = torch.index_select(key_cache, 0, beam_idx_t)
                value_cache = torch.index_select(value_cache, 0, beam_idx_t)

            # # #UT for first token with bf16
            input_t_bf16 = input_t.bfloat16()
            key_cache_iakv_bf16 = key_cache_iakv.bfloat16()
            value_cache_iakv_bf16 = value_cache_iakv.bfloat16()
            attention_mask_bf16 = attention_mask.bfloat16()

            input_t_fp8 = input_t_bf16.clone()
            key_cache_iakv_fp8 = key_cache_iakv_bf16.clone()
            value_cache_iakv_fp8 = value_cache_iakv_bf16.clone()
            attention_mask_fp8 = attention_mask_bf16.clone()
            with torch.inference_mode(), torch.no_grad(), torch.autocast(
                device_type="cpu",
                enabled=True,
                dtype=torch.bfloat16,
            ):
                naive_output_bf16, _, key_cache_bf16, value_cache_bf16, _ = mha(
                    input_t_bf16,
                    None,
                    None,
                    max_seq_len,
                    attention_mask_bf16,
                    None,
                    None,
                    bf16=True,
                )
                (
                    indirect_access_kv_cache_output_bf16,
                    _,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    beam_idx,
                ) = mha(
                    input_t_bf16,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    max_seq_len,
                    attention_mask_bf16,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                    bf16=True,
                )
            (
                indirect_access_kv_cache_output_fp8,
                _,
                key_cache_iakv_fp8,
                value_cache_iakv_fp8,
                beam_idx,
            ) = mha(
                input_t_fp8,
                key_cache_iakv_fp8,
                value_cache_iakv_fp8,
                max_seq_len,
                attention_mask_fp8,
                beam_idx,
                True,
                torch.tensor(offset),
                fp8=True,
                bf16=True,
            )
            assert key_cache_iakv_fp8.dtype == torch.float8_e5m2
            assert value_cache_iakv_fp8.dtype == torch.float8_e5m2
            self.assertEqual(
                naive_output_bf16,
                indirect_access_kv_cache_output_bf16,
                prec=2e-2,
            )
            self.assertEqual(
                indirect_access_kv_cache_output_fp8,
                indirect_access_kv_cache_output_bf16,
            )
            key_cache_bf16 = key_cache_bf16.repeat_interleave(beam_size, dim=0)
            value_cache_bf16 = value_cache_bf16.repeat_interleave(beam_size, dim=0)
            for i in range(batch_size):
                self.assertEqual(
                    key_cache_bf16.transpose(0, 1)[:, i * beam_size, :, :],
                    key_cache_iakv_bf16[0:first_seq_len, i * beam_size, :, :],
                )
                self.assertEqual(
                    key_cache_iakv_bf16[0:first_seq_len, i * beam_size, :, :],
                    key_cache_iakv_fp8[0:first_seq_len, i * beam_size, :, :],
                )
                self.assertEqual(
                    value_cache_bf16.transpose(0, 1)[:, i * beam_size, :, :],
                    value_cache_iakv_bf16[0:first_seq_len, i * beam_size, :, :],
                )
                self.assertEqual(
                    value_cache_iakv_bf16[0:first_seq_len, i * beam_size, :, :],
                    value_cache_iakv_fp8[0:first_seq_len, i * beam_size, :, :],
                )
                key_cache_bf16 = torch.index_select(key_cache_bf16, 0, beam_idx_t)
                value_cache_bf16 = torch.index_select(value_cache_bf16, 0, beam_idx_t)
            offset = offset + first_seq_len
            # UT for next token with fp32
            input_t = torch.randn(
                beam_size * batch_size,
                1,
                head_num * head_size,
                dtype=torch.float32,
            )
            attention_mask = torch.zeros(
                beam_size * batch_size, 1, 1, offset + 1, dtype=torch.float32
            )
            with torch.inference_mode(), torch.no_grad():
                naive_output, _, key_cache, value_cache, _ = mha(
                    input_t,
                    key_cache,
                    value_cache,
                    max_seq_len,
                    attention_mask,
                    None,
                    None,
                )
                (
                    indirect_access_kv_cache_output,
                    _,
                    key_cache_iakv,
                    value_cache_iakv,
                    beam_idx,
                ) = mha(
                    input_t,
                    key_cache_iakv,
                    value_cache_iakv,
                    max_seq_len,
                    attention_mask,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                )
                self.assertEqual(naive_output, indirect_access_kv_cache_output)
                self.assertEqual(
                    key_cache.transpose(0, 1)[offset],
                    key_cache_iakv[offset, :, :, :],
                )
                self.assertEqual(
                    value_cache.transpose(0, 1)[offset],
                    value_cache_iakv[offset, :, :, :],
                )
            # #UT for next token with bf16
            input_t_bf16 = input_t.bfloat16()
            attention_mask_bf16 = attention_mask.bfloat16()
            input_t_fp8 = input_t_bf16.clone()
            key_cache_iakv_fp8 = key_cache_iakv_bf16.clone()
            value_cache_iakv_fp8 = value_cache_iakv_bf16.clone()
            attention_mask_fp8 = attention_mask_bf16.clone()
            with torch.inference_mode(), torch.no_grad(), torch.autocast(
                device_type="cpu",
                enabled=True,
                dtype=torch.bfloat16,
            ):
                naive_output_bf16, _, key_cache_bf16, value_cache_bf16, _ = mha(
                    input_t_bf16,
                    key_cache_bf16,
                    value_cache_bf16,
                    max_seq_len,
                    attention_mask_bf16,
                    None,
                    None,
                    bf16=True,
                )
                (
                    indirect_access_kv_cache_output_bf16,
                    _,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    beam_idx,
                ) = mha(
                    input_t_bf16,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    max_seq_len,
                    attention_mask_bf16,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                    bf16=True,
                )
                self.assertEqual(
                    naive_output_bf16,
                    indirect_access_kv_cache_output_bf16,
                    prec=0.05,
                )
                self.assertEqual(
                    key_cache_bf16.transpose(0, 1)[offset],
                    key_cache_iakv_bf16[offset, :, :, :],
                )
                self.assertEqual(
                    value_cache_bf16.transpose(0, 1)[offset],
                    value_cache_iakv_bf16[offset, :, :, :],
                )
                if beam_size == 4:
                    beam_idx_t = torch.tensor([1, 3, 0, 0]).repeat(batch_size)
                    for i in range(1, batch_size):
                        beam_idx_t[i * beam_size : i * beam_size + beam_size] = (
                            beam_idx_t[i * beam_size : i * beam_size + beam_size]
                            + i * beam_size
                        )
                elif beam_size == 1:
                    beam_idx_t = torch.arange(batch_size)
                beam_idx[offset] = beam_idx_t
                offset = offset + 1
                # reorder cache for naive impelementation
                key_cache = torch.index_select(key_cache, 0, beam_idx_t)
                value_cache = torch.index_select(value_cache, 0, beam_idx_t)
                key_cache_bf16 = torch.index_select(key_cache_bf16, 0, beam_idx_t)
                value_cache_bf16 = torch.index_select(value_cache_bf16, 0, beam_idx_t)
            # UT for next token with fp32
            input_t = torch.randn(
                beam_size * batch_size,
                1,
                head_num * head_size,
                dtype=torch.float32,
            )
            attention_mask = torch.zeros(
                beam_size * batch_size, 1, 1, offset + 1, dtype=torch.float32
            )
            with torch.inference_mode(), torch.no_grad():
                naive_output, _, key_cache, value_cache, _ = mha(
                    input_t,
                    key_cache,
                    value_cache,
                    max_seq_len,
                    attention_mask,
                    None,
                    None,
                )
                (
                    indirect_access_kv_cache_output,
                    _,
                    key_cache_iakv,
                    value_cache_iakv,
                    beam_idx,
                ) = mha(
                    input_t,
                    key_cache_iakv,
                    value_cache_iakv,
                    max_seq_len,
                    attention_mask,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                )
                self.assertEqual(naive_output, indirect_access_kv_cache_output)
                self.assertEqual(
                    key_cache.transpose(0, 1)[offset],
                    key_cache_iakv[offset, :, :, :],
                )
                self.assertEqual(
                    value_cache.transpose(0, 1)[offset],
                    value_cache_iakv[offset, :, :, :],
                )
            # #UT for next token with bf16
            input_t_bf16 = input_t.bfloat16()
            attention_mask_bf16 = attention_mask.bfloat16()
            input_t_fp8 = input_t_bf16.clone()
            key_cache_iakv_fp8 = key_cache_iakv_bf16.clone()
            value_cache_iakv_fp8 = value_cache_iakv_bf16.clone()
            attention_mask_fp8 = attention_mask_bf16.clone()
            with torch.inference_mode(), torch.no_grad(), torch.autocast(
                device_type="cpu",
                enabled=True,
                dtype=torch.bfloat16,
            ):
                naive_output_bf16, _, key_cache_bf16, value_cache_bf16, _ = mha(
                    input_t_bf16,
                    key_cache_bf16,
                    value_cache_bf16,
                    max_seq_len,
                    attention_mask_bf16,
                    None,
                    None,
                    bf16=True,
                )
                (
                    indirect_access_kv_cache_output_bf16,
                    _,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    beam_idx,
                ) = mha(
                    input_t_bf16,
                    key_cache_iakv_bf16,
                    value_cache_iakv_bf16,
                    max_seq_len,
                    attention_mask_bf16,
                    beam_idx,
                    True,
                    torch.tensor(offset),
                    bf16=True,
                )
                self.assertEqual(
                    naive_output_bf16,
                    indirect_access_kv_cache_output_bf16,
                    prec=0.05,
                )
                self.assertEqual(
                    key_cache_bf16.transpose(0, 1)[offset],
                    key_cache_iakv_bf16[offset, :, :, :],
                )
                self.assertEqual(
                    value_cache_bf16.transpose(0, 1)[offset],
                    value_cache_iakv_bf16[offset, :, :, :],
                )
            (
                indirect_access_kv_cache_output_fp8,
                _,
                key_cache_iakv_fp8,
                value_cache_iakv_fp8,
                beam_idx,
            ) = mha(
                input_t_fp8,
                key_cache_iakv_fp8,
                value_cache_iakv_fp8,
                max_seq_len,
                attention_mask_fp8,
                beam_idx,
                True,
                torch.tensor(offset),
                bf16=True,
                fp8=True,
            )
            self.assertEqual(
                indirect_access_kv_cache_output_fp8,
                indirect_access_kv_cache_output_bf16,
                prec=5e-3,
            )
            self.assertEqual(
                key_cache_iakv_fp8[offset, :, :, :].bfloat16(),
                key_cache_iakv_bf16[offset, :, :, :],
            )
            self.assertEqual(
                value_cache_iakv_fp8[offset, :, :, :].bfloat16(),
                value_cache_iakv_bf16[offset, :, :, :],
            )

    def _test_masked_multihead_self_attention(self):
        head_size = 128
        max_position = 64
        seq_len = 16
        head_num = 32
        dtype = torch.float32
        offset = torch.tensor(16)
        query = torch.rand((1, seq_len, head_num, head_size), dtype=dtype)
        key = torch.rand((1, seq_len, head_num, head_size), dtype=dtype)
        value = torch.rand((1, seq_len, head_num, head_size), dtype=dtype)
        key_cache = torch.rand((max_position, 1, head_num, head_size), dtype=dtype)
        value_cache = torch.rand((max_position, 1, head_num, head_size), dtype=dtype)
        beam_idx = torch.zeros((max_position, 1), dtype=torch.int64)
        attention_mask = torch.zeros((1, 1, seq_len, 32), dtype=dtype)
        qtype = torch.float8_e5m2
        for dtype in [
            torch.bfloat16,
        ]:
            with torch.inference_mode(), torch.no_grad(), torch.autocast(
                device_type="cpu",
                enabled=True,
                dtype=dtype,
            ):
                query = query.to(dtype)
                key = key.to(dtype).to(qtype).to(dtype)
                value = value.to(dtype).to(qtype).to(dtype)
                query_fp8 = query.clone()
                key_fp8 = key.clone()
                value_fp8 = value.clone()
                key_cache_fp8 = key_cache.to(dtype).to(qtype)
                value_cache_fp8 = value_cache.to(dtype).to(qtype)
                key_cache = key_cache_fp8.to(dtype)
                value_cache = value_cache_fp8.to(dtype)
                attention_mask = attention_mask.to(dtype)
                attention_mask_fp8 = attention_mask.clone()
                refe_outputs = torch.ops.torch_ipex.masked_multihead_self_attention(
                    query,
                    key,
                    value,
                    key_cache,
                    value_cache,
                    beam_idx,
                    offset,
                    head_size**0.5,
                    max_position,
                    None,
                    attention_mask,
                )
                test_outputs = torch.ops.torch_ipex.masked_multihead_self_attention(
                    query_fp8,
                    key_fp8,
                    value_fp8,
                    key_cache_fp8,
                    value_cache_fp8,
                    beam_idx,
                    offset,
                    head_size**0.5,
                    max_position,
                    None,
                    attention_mask_fp8,
                )
                self.assertEqual(test_outputs[0], refe_outputs[0], prec=5e-3)

    def test_mha(self):
        self._test_mha(torchcompile=False)
        self._test_masked_multihead_self_attention()


if __name__ == "__main__":
    test = unittest.main()
