"""Author the tiny committed L4 test model once (Story 7.3).

Run from the repo root (``onnx`` is a tool-only dependency, never a project one)::

    uv run --extra l4 --with onnx python tools/make_l4_fixture.py

Writes, under ``tests/fixtures/``:

* ``l4_tiny/model.onnx`` + ``tokenizer.json`` -- two logits; the "unsafe" class is the
  one at ``positive_index`` in ``commishdesk/narrate/l4.toml`` (softmax contract);
* ``l4_tiny_sigmoid/model.onnx`` + ``tokenizer.json`` -- one logit (sigmoid contract).

Each model is a bag of words: ``logits = sum(embedding[input_ids])``. The vocabulary is
``[UNK]``, ``toxic``, ``nice``, ``idiot``; "toxic" and "idiot" push the unsafe logit up
hard, "nice" nudges it down slightly, and a constant bias keeps text with no known word low
(so a toxic word lost to truncation is visible). It is a plumbing fixture, not a classifier:
it exists so the real ``onnxruntime`` + ``tokenizers`` code path runs in CI with no download.
Re-run after changing ``positive_index`` in ``l4.toml``; the output is deterministic.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from tokenizers import Tokenizer, models, normalizers, pre_tokenizers

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
VOCAB = {"[UNK]": 0, "toxic": 1, "nice": 2, "idiot": 3}
#: Per-token push on the unsafe-minus-safe logit gap.
PUSH = {"toxic": 14.0, "idiot": 14.0, "nice": -0.02}
#: Constant push on the gap, so text with no known word scores low (about 0.0025).
BIAS = -6.0


def _tokenizer() -> Tokenizer:
    tokenizer = Tokenizer(models.WordLevel(VOCAB, unk_token="[UNK]"))
    tokenizer.normalizer = normalizers.Lowercase()
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    return tokenizer


def _model(table: np.ndarray, bias: np.ndarray) -> onnx.ModelProto:
    ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, [1, "seq"])
    mask = helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [1, "seq"])
    logits = helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, table.shape[1]])
    nodes = [
        helper.make_node("Gather", ["table", "input_ids"], ["rows"], axis=0),
        helper.make_node("ReduceSum", ["rows", "axes"], ["summed"], keepdims=0),
        # attention_mask is consumed (x0) so the graph really declares both inputs.
        helper.make_node("Cast", ["attention_mask"], ["mask_f"], to=TensorProto.FLOAT),
        helper.make_node("ReduceSum", ["mask_f", "axes"], ["mask_sum"], keepdims=1),
        helper.make_node("Mul", ["mask_sum", "zero"], ["mask_zero"]),
        helper.make_node("Add", ["summed", "mask_zero"], ["biased_in"]),
        helper.make_node("Add", ["biased_in", "bias"], ["logits"]),
    ]
    graph = helper.make_graph(
        nodes,
        "l4_tiny",
        [ids, mask],
        [logits],
        initializer=[
            numpy_helper.from_array(table, "table"),
            numpy_helper.from_array(np.array([1], dtype=np.int64), "axes"),
            numpy_helper.from_array(np.array(0.0, dtype=np.float32), "zero"),
            numpy_helper.from_array(bias, "bias"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def _write(directory: Path, table: np.ndarray, bias: np.ndarray, tokenizer: Tokenizer) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    onnx.save(_model(table, bias), str(directory / "model.onnx"))
    tokenizer.save(str(directory / "tokenizer.json"))


def main() -> None:
    positive_index = tomllib.loads((ROOT / "commishdesk" / "narrate" / "l4.toml").read_text(encoding="utf-8"))[
        "positive_index"
    ]
    tokenizer = _tokenizer()

    softmax = np.zeros((len(VOCAB), 2), dtype=np.float32)
    for word, push in PUSH.items():
        softmax[VOCAB[word], positive_index] = push / 2
        softmax[VOCAB[word], 1 - positive_index] = -push / 2
    softmax_bias = np.zeros(2, dtype=np.float32)
    softmax_bias[positive_index] = BIAS / 2
    softmax_bias[1 - positive_index] = -BIAS / 2
    _write(FIXTURES / "l4_tiny", softmax, softmax_bias, tokenizer)

    sigmoid = np.zeros((len(VOCAB), 1), dtype=np.float32)
    for word, push in PUSH.items():
        sigmoid[VOCAB[word], 0] = push
    _write(FIXTURES / "l4_tiny_sigmoid", sigmoid, np.array([BIAS], dtype=np.float32), tokenizer)


if __name__ == "__main__":
    main()
