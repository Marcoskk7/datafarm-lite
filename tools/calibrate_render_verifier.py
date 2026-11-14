#!/usr/bin/env python3
"""Counterfactual calibration of the spec-driven Render verifier.

Takes one render a reviewer has accepted, verifies it (positive control), then
injects every defect in ``counterfactual.SUITE`` and verifies again.  A spec is
calibrated for a task only when the full suite ran, the positive control passes,
every counterfactual fails in its expected check, and a counterfactual that
cannot be constructed for this spec is waived explicitly with a reason
(``--waive name=reason``).  ``--only`` runs are diagnostics and never calibrate.
Writes ``calibration.json`` with the detection matrix, the waivers and the
sha256 of the spec and the accepted render it was calibrated on; exit 0 only
when ``calibrated`` is true.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import replace  # noqa: E402

from datafarm.kernel.io import sha256_file as sha256  # noqa: E402
from datafarm.render.verify import counterfactual as CF  # noqa: E402
from datafarm.render.verify.calibration import run_suite, summarize  # noqa: E402
from datafarm.render.verify.episode import frame_events, gripper_events, load_state  # noqa: E402
from datafarm.render.verify.spec import load_spec  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--carrier", required=True, type=Path)
    parser.add_argument("--generated", required=True, type=Path, help="a reviewer-accepted render")
    parser.add_argument("--real", type=Path)
    parser.add_argument("--reference", action="append", default=[], type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--seams", default="")
    parser.add_argument("--event-frames", default="")
    parser.add_argument("--mode", choices=("sim2real", "ood"))
    parser.add_argument("--perturbed-axes", default="")
    parser.add_argument("--only", default="", help="diagnostic subset; never yields a calibration")
    parser.add_argument("--waive", action="append", default=[], metavar="NAME=REASON",
                        help="accept that one counterfactual cannot be built for this spec")
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    state = load_state(args.state) if args.state else None
    roles = [e.role for e in spec.entities]
    events = gripper_events(state, roles=roles) if state else []
    if args.event_frames:
        events += frame_events([int(f) for f in args.event_frames.split(",")], roles=roles)
    spec = spec.with_episode(events=events or None, seams=[int(s) for s in args.seams.split(",") if s])
    if args.mode:
        spec = replace(spec, realism=replace(spec.realism, mode=args.mode,
                                             perturbed_axes=tuple(a for a in args.perturbed_axes.split(",") if a)))
    waivers = {}
    for item in args.waive:
        name, _, reason = item.partition("=")
        if not reason.strip():
            parser.error(f"--waive {item!r} needs NAME=REASON")
        waivers[name] = reason.strip()
    suite = [c.name for c in CF.SUITE]
    names = ["positive_control"] + suite
    if args.only:
        names = [n for n in names if n in args.only.split(",")]
    results = run_suite(spec, state, carrier=args.carrier, generated=args.generated, real=args.real,
                        references=args.reference, names=names, workers=args.workers)
    positive = next((r for r in results if r["name"] == "positive_control"), None)
    negatives = [r for r in results if r["name"] != "positive_control"]
    summary = {
        "schema": "datafarm.render-verifier-calibration/2",
        "spec_id": spec.spec_id,
        "spec_sha256": sha256(args.spec),
        "generated": str(args.generated),
        "generated_sha256": sha256(args.generated),
        **summarize(results, suite=suite, partial=bool(args.only), waivers=waivers),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "calibration.json").write_text(json.dumps(summary, indent=1))
    print(f"positive control: {positive['verdict'] if positive else 'n/a'} {positive.get('failed') if positive else ''}")
    for r in negatives:
        if r.get("status") == "not_constructible":
            print(f"  {r['name']:18s} not constructible")
            continue
        mark = "CAUGHT" if r["caught"] else "MISSED"
        print(f"  {r['name']:18s} {mark:6s} failed={r['failed']}")
    print(f"caught {summary['caught']}/{summary['constructed']} constructed of {summary['total']}; "
          f"unwaived not constructible: {summary['unwaived_not_constructible']}")
    if summary["partial"]:
        print("partial --only run: diagnostic only, not a calibration")
    return 0 if summary["calibrated"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
