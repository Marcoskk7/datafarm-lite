"""``tools/request_draft.py``：一句话 → request.yaml（1a），旁边写 request.draft.json 记录来源。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaml

from datafarm.adapters.llm.client import OpenAIStructuredLLM
from datafarm.config import source_root
from datafarm.kernel.io import atomic_write_bytes, atomic_write_json, sha256_bytes, sha256_text
from datafarm.request.drafting import DraftResult, draft_request
from datafarm.request.ports import StructuredLLMError
from datafarm.request.schema import RequestError

SIDECAR_SCHEMA = "datafarm.request-draft/1"
DEFAULT_PROMPT = Path("prompts/request/draft.md")


def sidecar_path(out: Path) -> Path:
    """`request.yaml` → `request.draft.json`。"""
    return out.with_name(out.stem + ".draft.json")


def request_yaml(result: DraftResult) -> bytes:
    document = result.request.model_dump(by_alias=True, exclude_none=True)
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")


def sidecar(result: DraftResult, *, prompt_file_sha256: str, request_sha256: str) -> dict:
    return {"schema": SIDECAR_SCHEMA, "sentence": result.sentence, "sentence_sha256": sha256_text(result.sentence),
            "prompt_file_sha256": prompt_file_sha256, "prompt_sha256": result.prompt_sha256,
            "request_sha256": request_sha256, "model": result.llm.model, "response_id": result.llm.response_id,
            "input_tokens": result.llm.input_tokens, "output_tokens": result.llm.output_tokens,
            "provenance": dict(sorted(result.provenance.items()))}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tools/request_draft.py", description=__doc__)
    parser.add_argument("sentence", help="the data request as one sentence")
    parser.add_argument("--config", type=Path, required=True, help="LLM config JSON (configs/llm.template.json)")
    parser.add_argument("--out", type=Path, required=True, help="request.yaml to write; must not exist")
    parser.add_argument("--call-log", type=Path, help="agent-call ledger (default: agent-calls.jsonl next to --out)")
    parser.add_argument("--prompt", type=Path, help=f"extraction prompt (default: {DEFAULT_PROMPT} of this checkout)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    out: Path = args.out
    side = sidecar_path(out)
    for target in (out, side):
        if target.exists():
            print(f"{target} exists; drafts are kept, choose a new --out", file=sys.stderr)
            return 2
    prompt_bytes = (args.prompt or source_root() / DEFAULT_PROMPT).read_bytes()
    call_log = args.call_log or out.parent / "agent-calls.jsonl"
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        llm = OpenAIStructuredLLM(config, call_log=call_log)
        result = draft_request(args.sentence, llm, instructions=prompt_bytes.decode("utf-8"))
    except (ValueError, RequestError, StructuredLLMError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    data = request_yaml(result)
    atomic_write_bytes(out, data)
    atomic_write_json(side, sidecar(result, prompt_file_sha256=sha256_bytes(prompt_bytes),
                                    request_sha256=sha256_bytes(data)), sort_keys=False)
    print(json.dumps({"request": str(out), "draft": str(side), "request_id": result.request.request_id}, indent=2))
    return 0
