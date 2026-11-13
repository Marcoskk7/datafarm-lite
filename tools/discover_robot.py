#!/usr/bin/env python3
"""Discover source structure and bind optional semantics; no GPU/model calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datafarm.simulation.robot_binding import BindingError, bind_robot, discover_urdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('urdf', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        descriptor = discover_urdf(args.urdf.read_bytes())
        profile = json.loads(args.profile.read_text(encoding='utf-8-sig')) if args.profile else None
        report = bind_robot(descriptor, profile)
        content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding='utf-8')
        else:
            print(content, end='')
        return 0
    except (BindingError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
