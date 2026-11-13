#!/usr/bin/env python3
"""为 EVAL-v1 独立评审创建盲化、匿名的评审任务。

对每个 subject（一个 H3 package 目录，或任何带有 pack/ + score.json + subject.json 描述符的目录），把
lite pack 复制到 <run>/reviews/<rid>/pack，链接 generated.mp4 / carrier.mp4，并写入 <run>/reviews/<rid>/task.json
（仅 mode + OOD 描述）。subject 身份保存在 <run>/reviews/.mapping.json，评审者
禁止读取。对同一 subject 重复运行是幂等的。

  python h3_review_prep.py [--run-dir <run>] --subjects <dir> [<dir> ...]   # 为每个 subject 打印 rid
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from pathlib import Path

import h3_paths


def subject_info(d: Path) -> dict:
    if (d / "package.json").exists():
        pkg = json.loads((d / "package.json").read_text())
        asm = json.loads((d / "assembled.json").read_text())
        bundle = json.loads(Path(pkg["bundle"]).read_text())
        var = next((v for v in bundle.get("ood_variants", []) if v["name"] == pkg.get("variant")), None)
        return {"kind": "h3", "recipe": pkg["recipe"]["name"], "task": pkg["task"], "episode": pkg["episode"],
                "mode": pkg["mode"], "variant": var, "generated": asm["final"], "carrier": asm["carrier_clip"]}
    rec = json.loads((d / "subject.json").read_text())  # 非 package 的 subject 描述符
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", nargs="+", required=True)
    h3_paths.add_run_dir_arg(ap)
    args = ap.parse_args()
    RUN = h3_paths.run_dir(args.run_dir)
    MAP = RUN / "reviews" / ".mapping.json"
    MAP.parent.mkdir(parents=True, exist_ok=True)
    mapping = json.loads(MAP.read_text()) if MAP.exists() else {}
    by_subject = {v["subject"]: k for k, v in mapping.items()}
    for s in args.subjects:
        d = Path(s).resolve()
        if str(d) in by_subject:
            print(by_subject[str(d)], d)
            continue
        info = subject_info(d)
        rid = "r" + secrets.token_hex(4)
        rd = RUN / "reviews" / rid
        shutil.copytree(d / "pack", rd / "pack")
        for name, src in (("generated.mp4", info["generated"]), ("carrier.mp4", info["carrier"])):
            try:  # 硬链接/复制，绝不使用符号链接：`ls -l` 不得泄露 subject 的身份
                os.link(src, rd / name)
            except OSError:
                shutil.copy(src, rd / name)
        task = {"review_id": rid, "mode": info["mode"], "task": info["task"]}
        if info["mode"] == "ood" and info.get("variant"):
            task["ood_change"] = {"tabletop": info["variant"]["tabletop"], "lighting": info["variant"]["lighting"]}
        (rd / "task.json").write_text(json.dumps(task, indent=2))
        (rd / "review").mkdir()
        mapping[rid] = {"subject": str(d), **{k: v for k, v in info.items() if k not in ("generated", "carrier")}}
        print(rid, d)
    MAP.write_text(json.dumps(mapping, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
