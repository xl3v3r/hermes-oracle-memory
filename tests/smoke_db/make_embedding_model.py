#!/usr/bin/env python3
"""Build a tiny text-embedding model in ONNX format for database smoke tests.

The plugin embeds text inside the database (VECTOR_EMBEDDING with an ONNX
model), so an empty test database needs a model before anything can be
stored. The production model is several hundred megabytes. This one is about
11 KB: it lower-cases the text, splits it on spaces, counts the words of a
fixed 768-word vocabulary, and normalises the counts. It produces 768
dimensions like the production model and uses standard ONNX operators only.

It is good enough to exercise storage and search. It says nothing about
retrieval quality.

    python tests/smoke_db/make_embedding_model.py --out /tmp/smoke_embedding.onnx

Needs the `onnx` package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DIMENSIONS = 768
INPUT_NAME = "input"
OUTPUT_NAME = "embedding"
# The metadata DBMS_VECTOR.LOAD_ONNX_MODEL needs for this model.
LOAD_METADATA = '{"function":"embedding","embeddingOutput":"embedding","input":{"input":["DATA"]}}'

_WORDS = (
    "verification memory provider stores recalls sentence batch first second named row updated "
    "oracle database vector search index text hybrid recall embedding model fact session tag "
    "skill handoff project release note user agent hermes"
).split()


def vocabulary() -> list[str]:
    words: list[str] = list(dict.fromkeys(_WORDS))
    n = 0
    while len(words) < DIMENSIONS:
        words.append(f"w{n:04d}")
        n += 1
    return words[:DIMENSIONS]


def build_model() -> bytes:
    from onnx import TensorProto, checker, helper

    nodes = [
        helper.make_node("StringNormalizer", [INPUT_NAME], ["lower"], case_change_action="LOWER"),
        helper.make_node("StringSplit", ["lower"], ["tokens", "token_counts"], delimiter=" "),
        helper.make_node(
            "TfIdfVectorizer", ["tokens"], ["counts"], mode="TF",
            min_gram_length=1, max_gram_length=1, max_skip_count=0,
            ngram_counts=[0], ngram_indexes=list(range(DIMENSIONS)), pool_strings=vocabulary(),
        ),
        # The bias keeps a text with no vocabulary word away from the zero vector.
        helper.make_node("Add", ["counts", "bias"], ["biased"]),
        helper.make_node("LpNormalization", ["biased"], [OUTPUT_NAME], axis=1, p=2),
    ]
    graph = helper.make_graph(
        nodes,
        "hermes_oracle_memory_smoke_embedding",
        [helper.make_tensor_value_info(INPUT_NAME, TensorProto.STRING, ["batch"])],
        [helper.make_tensor_value_info(OUTPUT_NAME, TensorProto.FLOAT, ["batch", DIMENSIONS])],
        initializer=[helper.make_tensor("bias", TensorProto.FLOAT, [DIMENSIONS], [0.01] * DIMENSIONS)],
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 20)], producer_name="hermes-oracle-memory-tests"
    )
    model.ir_version = 9
    checker.check_model(model)
    return model.SerializeToString()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", required=True, help="file to write the model to")
    args = parser.parse_args(argv)
    try:
        data = build_model()
    except ImportError:
        print("the `onnx` package is required: python -m pip install onnx", file=sys.stderr)
        return 2
    Path(args.out).write_bytes(data)
    print(f"wrote {args.out} ({len(data)} bytes, {DIMENSIONS} dimensions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
