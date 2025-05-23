# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
import tempfile
import unittest

import torch
from executorch.exir import EdgeCompileConfig, to_edge

from executorch.exir.capture._config import ExecutorchBackendConfig
from executorch.exir.passes.init_mutable_pass import InitializedMutableBufferPass
from executorch.extension.llm.modules.attention import (
    MultiHeadAttention as ETMultiHeadAttention,
)
from executorch.runtime import Runtime
from torch._inductor.package import load_package, package_aoti
from torch.testing import assert_close
from torchtune.models.llama3_1._position_embeddings import Llama3ScaledRoPE
from torchtune.modules.attention import MultiHeadAttention as TTMultiHeadAttention


class AttentionTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        torch.manual_seed(0)
        # Constants
        self.embed_dim = 2048
        self.num_heads = 8
        self.num_kv_heads = 8
        self.head_dim = 64
        self.max_seq_len = 128
        self.encoder_max_seq_len = 128
        self.rope_base = 500_000
        self.scale_factor = 32

        # Module dependency injections.
        self.q_proj = torch.nn.Linear(
            self.embed_dim, self.num_heads * self.head_dim, bias=False
        )
        self.k_proj = torch.nn.Linear(
            self.embed_dim, self.num_kv_heads * self.head_dim, bias=False
        )
        self.k_proj.weight.requires_grad = False
        self.v_proj = torch.nn.Linear(
            self.embed_dim, self.num_kv_heads * self.head_dim, bias=False
        )
        self.v_proj.weight.requires_grad = False
        self.output_proj = torch.nn.Linear(
            self.num_heads * self.head_dim, self.embed_dim, bias=False
        )
        self.pos_embeddings = Llama3ScaledRoPE(
            dim=self.head_dim,
            max_seq_len=self.max_seq_len,
            base=self.rope_base,
            scale_factor=self.scale_factor,
        )

        # Original TorchTune reference module to test accuracy against.
        self.tt_mha = TTMultiHeadAttention(
            embed_dim=self.embed_dim,
            num_heads=self.num_heads,
            num_kv_heads=self.num_kv_heads,
            head_dim=self.head_dim,
            q_proj=self.q_proj,
            k_proj=self.k_proj,
            v_proj=self.v_proj,
            output_proj=self.output_proj,
            pos_embeddings=self.pos_embeddings,
            max_seq_len=self.max_seq_len,
        )

        # Source transformed module that we are testing.
        self.et_mha = ETMultiHeadAttention(
            embed_dim=self.embed_dim,
            num_heads=self.num_heads,
            num_kv_heads=self.num_kv_heads,
            head_dim=self.head_dim,
            q_proj=self.q_proj,
            k_proj=self.k_proj,
            v_proj=self.v_proj,
            output_proj=self.output_proj,
            pos_embeddings=self.pos_embeddings,
            max_seq_len=self.max_seq_len,
        )
        self.et_mha.load_state_dict(self.tt_mha.state_dict())

        # Common inputs.
        seq_len = 10
        self.x = torch.randn(1, seq_len, self.embed_dim)
        self.y = torch.randn(1, seq_len, self.embed_dim)
        self.input_pos = torch.arange(seq_len).unsqueeze(0)  # shape [1, seq_len]
        self.seq_len_dim = torch.export.Dim("seq_len", min=1, max=self.max_seq_len)
        self.dynamic_shapes = {
            "x": {
                0: torch.export.Dim.STATIC,
                1: self.seq_len_dim,
                2: torch.export.Dim.STATIC,
            },
            "y": {
                0: torch.export.Dim.STATIC,
                1: self.seq_len_dim,
                2: torch.export.Dim.STATIC,
            },
            "input_pos": {0: torch.export.Dim.STATIC, 1: self.seq_len_dim},
        }
        self.causal_mask = torch.tril(
            torch.ones(
                size=(self.max_seq_len, self.max_seq_len),
                dtype=torch.bool,
            )
        )

    def test_attention_eager(self):
        et_res = self.et_mha(self.x, self.x)  # Self attention.
        tt_res = self.tt_mha(self.x, self.x)  # Self attention.

        assert_close(et_res, tt_res)

        # test with kv cache
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)

        et_res = self.et_mha(self.x, self.x)  # Self attention.
        tt_res = self.tt_mha(self.x, self.x)  # Self attention.

        assert_close(et_res, tt_res)
        self.et_mha.reset_cache()
        self.tt_mha.reset_cache()

        et_res = self.et_mha(
            self.x, self.x, input_pos=self.input_pos
        )  # Self attention with input pos.
        tt_res = self.tt_mha(
            self.x, self.x, input_pos=self.input_pos
        )  # Self attention with input pos.

        assert_close(et_res, tt_res)

        # test kv cache read. Input pos can be [10, 11, ..., 19]
        next_input_pos = torch.arange(10, 20).unsqueeze(0)
        et_res = self.et_mha(
            self.x, self.x, input_pos=next_input_pos
        )  # Self attention with input pos.
        tt_res = self.tt_mha(
            self.x, self.x, input_pos=next_input_pos
        )  # Self attention with input pos.

        assert_close(et_res, tt_res)

    def test_attention_export(self):
        # Self attention.

        # test with kv cache
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        with torch.no_grad():
            et_mha_ep = torch.export.export(
                self.et_mha,
                (self.x, self.y),
                kwargs={"input_pos": self.input_pos},
                dynamic_shapes=self.dynamic_shapes,
                strict=True,
            )
        et_res = et_mha_ep.module()(self.x, self.x, input_pos=self.input_pos)
        tt_res = self.tt_mha(self.x, self.x, input_pos=self.input_pos)

        assert_close(et_res, tt_res)

    @unittest.skipIf(
        int(os.getenv("RUN_SKIPPED", 0)) < 1, reason="TODO(T207740932): test is flaky"
    )
    def test_attention_aoti(self):
        # Self attention.

        # test with kv cache
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        with torch.no_grad():
            so = torch._export.aot_compile(
                self.et_mha,
                args=(self.x, self.x),
                kwargs={"input_pos": self.input_pos},
                options={
                    "aot_inductor.package": True,
                    "reorder_for_peak_memory": False,
                },
                dynamic_shapes=self.dynamic_shapes,
            )
        with tempfile.TemporaryDirectory() as tempdir:
            path = package_aoti(os.path.join(tempdir, "mha.pt2"), so)
            mha_aoti = load_package(path)

            aoti_res = mha_aoti(self.x, self.x, input_pos=self.input_pos)
            tt_res = self.tt_mha(self.x, self.x, input_pos=self.input_pos)
            assert_close(aoti_res, tt_res)

    def test_attention_executorch(self):
        # Self attention.
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)

        with torch.no_grad():
            et_mha_ep = torch.export.export(
                self.et_mha,
                (self.x, self.y),
                kwargs={"input_pos": self.input_pos},
                dynamic_shapes=self.dynamic_shapes,
                strict=True,
            )
        et_program = to_edge(
            et_mha_ep,
            compile_config=EdgeCompileConfig(
                _core_aten_ops_exception_list=[torch.ops.aten._assert_async.msg],
                _check_ir_validity=False,
            ),
        ).to_executorch(
            config=ExecutorchBackendConfig(
                passes=[InitializedMutableBufferPass(["kv_cache_pos"])],
            )
        )

        runtime = Runtime.get()
        program = runtime.load_program(et_program.buffer)
        method = program.load_method("forward")
        et_res = method.execute((self.x, self.x, self.input_pos))
        tt_res = self.tt_mha(self.x, self.x, input_pos=self.input_pos)

        assert_close(et_res[0], tt_res)

    def test_attention_torch_cond_eager(self):
        # Different from vanilla torchtune MHA, we rewrite the if condition with torch.cond. We need to make sure they are giving the same results regarding the if condition.
        # For the first run of MHA we provide `y` but for the second run it will be a tensor full of nan.
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)

        mask = self.causal_mask[self.input_pos, :]
        # First run.
        et_res = self.et_mha(self.x, self.y, mask=mask, input_pos=self.input_pos)
        tt_res = self.tt_mha(self.x, self.y, mask=mask, input_pos=self.input_pos)

        assert_close(et_res, tt_res)

        # Second run tests kv cache read. Input pos is [10, 11, ..., 19]
        next_input_pos = torch.arange(10, 20).unsqueeze(0)

        empty_y = torch.full_like(self.x, torch.nan)
        mask = self.causal_mask[next_input_pos, :]
        et_res = self.et_mha(self.x, empty_y, mask=mask, input_pos=next_input_pos)
        tt_res = self.tt_mha(self.x, None, mask=mask, input_pos=next_input_pos)

        assert_close(et_res, tt_res)

    def test_attention_torch_cond_export(self):
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        mask = self.causal_mask[self.input_pos, :]
        dynamic_shapes = {
            **self.dynamic_shapes,
            **{
                "mask": {
                    0: torch.export.Dim.STATIC,
                    1: self.seq_len_dim,
                    2: torch.export.Dim.STATIC,
                }
            },
        }
        with torch.no_grad():
            et_mha_ep = torch.export.export(
                self.et_mha,
                (self.x, self.y),
                kwargs={
                    "mask": mask,
                    "input_pos": self.input_pos,
                },
                dynamic_shapes=dynamic_shapes,
                strict=True,
            )

        # First run.
        et_res = et_mha_ep.module()(self.x, self.y, mask=mask, input_pos=self.input_pos)
        tt_res = self.tt_mha(self.x, self.y, mask=mask, input_pos=self.input_pos)

        assert_close(et_res, tt_res)

        # Second run tests kv cache read. Input pos is [10, 11, ..., 19]
        next_input_pos = torch.arange(10, 20).unsqueeze(0)
        empty_y = torch.full_like(self.y, torch.nan)
        mask = self.causal_mask[next_input_pos, :]
        et_res = et_mha_ep.module()(
            self.x, empty_y, mask=mask, input_pos=next_input_pos
        )
        tt_res = self.tt_mha(self.x, None, mask=mask, input_pos=next_input_pos)

        assert_close(et_res, tt_res)

    def test_attention_torch_cond_executorch(self):
        self.et_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        self.tt_mha.setup_cache(1, dtype=torch.float32, max_seq_len=self.max_seq_len)
        mask = self.causal_mask[self.input_pos, :]
        dynamic_shapes = {
            **self.dynamic_shapes,
            **{
                "mask": {
                    0: torch.export.Dim.STATIC,
                    1: self.seq_len_dim,
                    2: torch.export.Dim.STATIC,
                }
            },
        }
        with torch.no_grad():
            et_mha_ep = torch.export.export(
                self.et_mha,
                (self.x, self.y),
                kwargs={
                    "mask": mask,
                    "input_pos": self.input_pos,
                },
                dynamic_shapes=dynamic_shapes,
                strict=True,
            )
        et_program = to_edge(
            et_mha_ep,
            compile_config=EdgeCompileConfig(
                _core_aten_ops_exception_list=[torch.ops.aten._assert_async.msg],
                _check_ir_validity=False,
            ),
        ).to_executorch(
            config=ExecutorchBackendConfig(
                passes=[InitializedMutableBufferPass(["kv_cache_pos"])],
            )
        )

        # First run.
        runtime = Runtime.get()
        program = runtime.load_program(et_program.buffer)
        method = program.load_method("forward")
        et_res = method.execute((self.x, self.y, mask, self.input_pos))
        tt_res = self.tt_mha(self.x, self.y, mask=mask, input_pos=self.input_pos)

        assert_close(et_res[0], tt_res)

        # Second run tests kv cache read. Input pos is [10, 11, ..., 19]
        next_input_pos = torch.arange(10, 20).unsqueeze(0)
        empty_y = torch.full_like(self.y, torch.nan)
        mask = self.causal_mask[next_input_pos, :]
        et_res = method.execute((self.x, empty_y, mask, next_input_pos))
        tt_res = self.tt_mha(self.x, None, mask=mask, input_pos=next_input_pos)

        assert_close(et_res[0], tt_res)
