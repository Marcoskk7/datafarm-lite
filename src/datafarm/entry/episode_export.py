"""Trusted export entry: sealed Episode IR in the ArtifactStore -> one fresh HDF5 file.

Usage::

    python -m datafarm.entry.episode_export --store STORE --episode-ref episode-ref.json \
        --output out/episode.h5 [--verdict verdict.json] [--manifest out/episode.h5.json]

``--episode-ref`` accepts the bare ref or the ``episode-ref.json`` written by
``datafarm.entry.scene_job``. The Judge verdict is copied into ``/meta`` only when given;
export never certifies success or replay.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from datafarm.adapters.export.hdf5_episode import write_episode_hdf5
from datafarm.kernel.artifacts import ArtifactRef


def export_episode(store, episode_ref, output, *, judge_result=None):
    from datafarm.kernel.schema import validate_ref
    validate_ref(episode_ref, "episode_ref")
    reference = ArtifactRef.from_dict(episode_ref)
    if reference.kind != "episode-ir":
        raise ValueError("Episode IR reference required")
    document = store.document(reference)
    return write_episode_hdf5(document.payload, output, episode_ref=reference.to_dict(), judge_result=judge_result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m datafarm.entry.episode_export", description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--episode-ref", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verdict", type=Path, help="optional judge-result/1 JSON to copy into /meta")
    parser.add_argument("--manifest", type=Path, help="optional path for the export manifest JSON")
    args = parser.parse_args(argv)
    from datafarm.kernel.store import ArtifactStore
    try:
        value = json.loads(args.episode_ref.read_text(encoding="utf-8"))
        if isinstance(value, dict) and "episode_ref" in value:
            value = value["episode_ref"]
        if value is None:
            raise ValueError("initialization failed; no Episode to export")
        verdict = json.loads(args.verdict.read_text(encoding="utf-8")) if args.verdict else None
        if verdict is not None and (not isinstance(verdict, dict) or verdict.get("schema_version") != "judge-result/1"):
            raise ValueError("verdict must be a judge-result/1 object")
        manifest = export_episode(ArtifactStore(args.store), value, args.output, judge_result=verdict)
        if args.manifest is not None:
            if args.manifest.exists():
                raise ValueError("manifest path exists")
            args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
