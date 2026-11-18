#!/usr/bin/env python3
"""临时 HDF5 导出/校验 CLI（tmp(B)，待 B 线同学确认）。

    # 导出：store 里读出 Episode 文档，写一个 HDF5（可选 Judge 结果与渲染清单）
    python tools/export_episode_hdf5.py export --store WORK/store \
        --episode-ref '{"episode_ref": {"kind": "episode-ir", "sha256": "…", "media_type": "application/json"}}' \
        --out WORK/episode.h5 [--judge verdict.json] [--render-manifest-ref '{"kind": "render-manifest", …}']

    # 校验：只看布局，或同时与 store 里的 Episode 逐数组比对
    python tools/export_episode_hdf5.py validate --file WORK/episode.h5 [--store WORK/store --episode-ref …]

JSON on stdout; exit 0 only when the requested operation succeeded (validate: no problems).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from datafarm.expert.hdf5_export import export_episode, validate_hdf5  # noqa: E402
from datafarm.kernel.artifacts import ArtifactRef  # noqa: E402
from datafarm.kernel.store import ArtifactStore  # noqa: E402

SOURCE_KEYS = ("request_ref", "original_request_ref", "task_ref", "task_contract_ref",
               "scene_ref", "runtime_ref", "expert_ref")


def _read_json(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(Path(value).read_text())


def _reference(raw, *keys):
    value = _read_json(raw)
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                value = value[key]
                break
    return ArtifactRef.from_dict(value)


def _document_payload(document):
    if "payload" in document and "state_trace" not in document:
        return document["payload"]
    return document


def _provenance(reference, payload):
    setup = payload["setup"]
    return {"episode_ref": reference.to_dict(),
            "profile": payload["profile"],
            "exporter": "tools/export_episode_hdf5.py",
            "real_ready": False,
            "source_sha256": setup.get("source_sha256", {}),
            "source_refs": {key: setup[key] for key in SOURCE_KEYS if setup.get(key) is not None},
            "asset_refs": setup.get("asset_refs", [])}


def command_export(args):
    store = ArtifactStore(args.store)
    reference = _reference(args.episode_ref, "episode_ref", "episode")
    document = store.load(reference)
    payload = _document_payload(document)
    manifest = None
    if args.render_manifest_ref:
        manifest = _document_payload(store.load(_reference(args.render_manifest_ref, "render_manifest_ref",
                                                           "render_manifest")))
    judge = _read_json(args.judge) if args.judge else None
    summary = export_episode(args.out, episode_document=document, judge_result=judge,
                             render_manifest=manifest, provenance=_provenance(reference, payload))
    print(json.dumps({"ok": True, "file": str(Path(args.out)), **summary}, sort_keys=True, indent=2))
    return 0


def command_validate(args):
    payload = None
    if bool(args.store) != bool(args.episode_ref):
        print(json.dumps({"ok": False, "error": "--store and --episode-ref must be given together"}))
        return 1
    if args.episode_ref:
        reference = _reference(args.episode_ref, "episode_ref", "episode")
        payload = _document_payload(ArtifactStore(args.store).load(reference))
    problems = validate_hdf5(args.file, payload)
    report = {"ok": not problems, "file": str(Path(args.file)), "problems": problems}
    if args.episode_ref:
        report["episode_ref"] = _reference(args.episode_ref, "episode_ref", "episode").to_dict()
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if not problems else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exporter = sub.add_parser("export", help="write one HDF5 file for one recorded Episode")
    exporter.add_argument("--store", required=True, help="ArtifactStore root")
    exporter.add_argument("--episode-ref", required=True, help="ArtifactRef JSON (或 {episode_ref: …})")
    exporter.add_argument("--out", required=True, help="output .h5 path")
    exporter.add_argument("--judge", help="Judge verdict JSON file (omitted: unverified)")
    exporter.add_argument("--render-manifest-ref", help="RenderManifest ArtifactRef JSON")
    exporter.set_defaults(handler=command_export)
    validator = sub.add_parser("validate", help="check layout; optionally compare with the Episode payload")
    validator.add_argument("--file", required=True)
    validator.add_argument("--store")
    validator.add_argument("--episode-ref")
    validator.set_defaults(handler=command_validate)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except Exception as exc:  # CLI 失败也要给 JSON，退出码 1
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
