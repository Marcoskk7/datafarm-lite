from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from datafarm.grounding.quantities import validate_grounding_artifact
from datafarm.kernel.artifacts import ArtifactDocument, GateReceipt, IRKind
from datafarm.kernel.schema import canonical_json_bytes, validate_artifact, validate_gate_receipt
from datafarm.entry.doctor import add_parsers as add_doctor_parser, handle as handle_doctor
from datafarm.kernel.io import sha256_bytes


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _validate(path: Path) -> int:
    value = _load(path)
    schema = value.get("schema")
    if schema == "datafarm.gate-receipt":
        validate_gate_receipt(GateReceipt.from_dict(value))
    elif isinstance(schema, str) and schema.startswith("datafarm."):
        document = ArtifactDocument.from_dict(value)
        if document.kind is IRKind.GROUNDING:
            validate_grounding_artifact(document)
        else:
            validate_artifact(document)
    else:
        raise ValueError(f"unsupported schema {schema!r}")
    digest = sha256_bytes(canonical_json_bytes(value))
    print(json.dumps({"valid": True, "sha256": digest, "schema": schema}, indent=2))
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datafarm")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate a current artifact or receipt")
    validate.add_argument("path", type=Path)
    add_doctor_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.path)
    if args.command == "doctor":
        return handle_doctor(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
