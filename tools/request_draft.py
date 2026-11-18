#!/usr/bin/env python3
"""Draft request.yaml (`datafarm.request/1`) from one sentence; see docs/reference/request-drafting.md.

    OPENAI_API_KEY=... python tools/request_draft.py "put the plate into the open drawer" \
        --config configs/llm.json --out WORK/request.yaml [--call-log WORK/agent-calls.jsonl]

Writes request.yaml and request.draft.json (sentence, hashes, model, response id, per-field provenance).
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datafarm.entry.request_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
