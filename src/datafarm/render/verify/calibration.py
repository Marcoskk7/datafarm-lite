"""Render verifier 的反事实标定：正对照 + ``counterfactual.SUITE`` 中的每个缺陷（``tools/calibrate_render_verifier.py`` 的实现）。"""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
import time

import cv2

from datafarm.render.verify import counterfactual as CF
from datafarm.render.verify.media import load_clip, load_reference_clip
from datafarm.render.verify.verifier import run_checks

G: dict = {}


def _run(name: str):
    spec, base, carrier, real, refs, state = G["spec"], G["gen"], G["car"], G["real"], G["refs"], G["state"]
    start = time.time()
    if name == "positive_control":
        clip = base
        expected = ()
    else:
        cf = next(c for c in CF.SUITE if c.name == name)
        entity = spec.entities[0]
        robot = next((e for e in spec.entities if e.role == "robot"), None)
        frames = cf.make(G["gen_full"], G["car_full"], spec, entity, robot)
        if frames is None:
            return {"name": name, "status": "not_constructible"}
        clip = CF.clip_from_full(base, frames)
        expected = cf.expected
    receipt = run_checks(spec, carrier, clip, real, refs, state=state)
    verdicts = {c["name"]: c["verdict"] for c in receipt["checks"]}
    failed = [n for n, v in verdicts.items() if v == "fail"]
    caught = all(any(f.startswith(e) for f in failed) for e in expected) if expected else None
    return {"name": name, "verdict": receipt["verdict"], "failed": failed, "expected": list(expected),
            "caught": caught, "verdicts": verdicts, "seconds": round(time.time() - start, 1),
            "reasons": {c["name"]: c["reason"] for c in receipt["checks"] if c["verdict"] != "pass"}}


def summarize(results: list, *, suite: list, partial: bool, waivers: dict) -> dict:
    """Calibration verdict; nothing is dropped from the denominator silently."""
    unknown = set(waivers) - set(suite)
    if unknown:
        raise ValueError(f"waivers name unknown counterfactuals: {sorted(unknown)}")
    positive = next((r for r in results if r["name"] == "positive_control"), None)
    negatives = [r for r in results if r["name"] != "positive_control"]
    unconstructed = [r["name"] for r in negatives if r.get("status") == "not_constructible"]
    unwaived = [name for name in unconstructed if name not in waivers]
    missed = [r["name"] for r in negatives
              if r.get("status") != "not_constructible" and not r.get("caught")]
    passes = positive["verdict"] == "pass" if positive else None
    return {
        "positive_control_passes": passes,
        "caught": sum(1 for r in negatives if r.get("caught")),
        "total": len(suite),
        "constructed": len(negatives) - len(unconstructed),
        "missed": missed,
        "not_constructible": unconstructed,
        "unwaived_not_constructible": unwaived,
        "waivers": waivers,
        "partial": partial,
        "calibrated": bool(passes) and not partial and not missed and not unwaived,
        "results": results,
    }


def run_suite(spec, state, *, carrier: Path, generated: Path, real: Path | None, references: list[Path],
              names: list[str], workers: int) -> list:
    """在 fork 出的 worker 中对 ``names`` 中的正对照与反事实逐一运行 verifier。"""
    kw = dict(layout_size=spec.layout_size, work_scale=spec.work_scale, stride=spec.stride)
    G["spec"] = spec
    G["state"] = state
    G["car"] = load_clip(carrier, **kw)
    G["gen"] = load_clip(generated, **kw)
    G["real"] = load_reference_clip(real, layout_size=spec.layout_size,
                                    work_scale=spec.work_scale) if real else None
    G["refs"] = [cv2.resize(cv2.imread(str(p)), spec.layout_size) for p in references]
    G["car_full"] = CF.full_rate_frames(str(carrier), spec)
    G["gen_full"] = CF.full_rate_frames(str(generated), spec)
    with mp.get_context("fork").Pool(min(workers, len(names))) as pool:
        return pool.map(_run, names)
