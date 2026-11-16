#!/usr/bin/env python3
"""Agent-authored scene layout gated by request conformance and plan; see docs/reference/scene-assembly.md.

    python tools/scene_author.py --request request.yaml --work WORK --cache /data/simready-cache \
        --robot-package /data/robot_pkg --agent-config scene-author.json --max-rounds 5 \
        --prepare-python /path/to/isaac/python

The host runs prepare, conformance and plan after every agent session; `check` (Isaac Lab, GPU)
is not run here. Exit 0 = accepted, 1 = not accepted, 2 = usage or configuration error.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datafarm.entry.scene_author_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
