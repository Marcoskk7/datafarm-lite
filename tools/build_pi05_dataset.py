#!/usr/bin/env python3
"""Build a fresh inline-224 PI0.5 dataset from an admitted generated cohort.

The production scope requires technical PASS on every selected record, a zero-waiver
semantic allowlist, an accepted distribution receipt, and the frozen
terminal-home contract.  Media are split with an explicit carrier layout and
every episode passes a seam check and a first-frame round-trip gate.
Explicit exploratory subsets preserve their parent identities and missing
members; they require the same per-episode evidence and cannot claim production
eligibility or completion of the parent cohort.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datafarm.training.pi05_build import build  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--task", required=True, help="task instruction written into the dataset")
    parser.add_argument("--excise-recovery", action="store_true",
                        help="drop deliberate misaligned-approach recovery detours from the training rows")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--semantic-allowlist", type=Path, required=True)
    parser.add_argument("--distribution-acceptance", type=Path, required=True)
    parser.add_argument("--terminal-home-spec", type=Path, required=True)
    parser.add_argument("--template-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--target-repo-id", required=True)
    parser.add_argument("--piper-fk-source", type=Path, required=True)
    parser.add_argument("--trainer-root", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--batch-frames", type=int, default=16)
    parser.add_argument("--png-workers", type=int, default=24)
    parser.add_argument("--real-gripper-open-m", type=float, default=0.105)
    parser.add_argument("--expected-episodes", type=int, required=True,
                        help="Episode count of this build; source, candidate, allowlist and receipt must all match it.")
    parser.add_argument("--training-scope", choices=("production_policy", "exploratory_diagnostic"),
                        default="production_policy")
    parser.add_argument("--parent-execution-plan", type=Path,
                        help="Required for exploratory subsets; preserve all original source identities.")
    return parser.parse_args()



def main() -> None:
    build(parse_args())


if __name__ == "__main__":
    main()
