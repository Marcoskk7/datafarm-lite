"""评估器篡改清单（参照 HELIX 的 ``evaluator_manifest``）。

每轮开始时，工具链会哈希所有用于决定或输入 verdict 的内容：judge 与
verifier 代码、verifier 的 contract 包、rollout runner 与 physics/material
文件、初始状态生成器、holdout seed 列表、adapter 与 brief。它会在每次会话
结束时以及 holdout 评估之前重新哈希。任何差异都会使该轮无效，除非运维人员
事先记录了修订（``tools/expert_harness.py amend``）——这样，诸如 verifier
改版之类的轮中规则变更就会成为可审计的事件，而不是静默变更。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from datafarm.expert.layout import RoundPaths
from datafarm.expert.state import EventLog, RoundState
from datafarm.expert.util import HarnessError, atomic_write_json, read_json, sha256_file, sha256_json, tree_digest, utc_now

MANIFEST_SCHEMA = "datafarm.expert-harness.manifest/1"


def describe_path(path: str) -> dict[str, Any]:
    if os.path.islink(path) and not os.path.exists(path):
        return {"kind": "dangling_link", "target": os.readlink(path)}
    if os.path.isdir(path):
        return tree_digest(path)
    if os.path.isfile(path):
        return {"kind": "file", "sha256": sha256_file(path), "bytes": os.path.getsize(path)}
    return {"kind": "missing"}


def build_manifest(paths: Iterable[str], *, extra: Mapping[str, str] | None = None,
                   created_at: str | None = None) -> dict[str, Any]:
    entries = {str(p): describe_path(str(p)) for p in sorted(set(str(p) for p in paths))}
    extra = dict(extra or {})
    return {"schema": MANIFEST_SCHEMA, "created_at": created_at, "entries": entries, "extra": extra,
            "digest": sha256_json({"entries": entries, "extra": extra})}


def diff_manifests(base: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes = []
    b, c = base.get("entries", {}), current.get("entries", {})
    for path in sorted(set(b) | set(c)):
        if b.get(path) != c.get(path):
            changes.append({"path": path, "before": (b.get(path) or {}).get("sha256") or (b.get(path) or {}).get("kind"),
                            "after": (c.get(path) or {}).get("sha256") or (c.get(path) or {}).get("kind")})
    be, ce = base.get("extra", {}), current.get("extra", {})
    for key in sorted(set(be) | set(ce)):
        if be.get(key) != ce.get(key):
            changes.append({"path": f"extra:{key}", "before": be.get(key), "after": ce.get(key)})
    return changes


class ManifestGuard:
    """轮次级篡改检查，带已存储的基准和已记录的修订。"""

    def __init__(self, paths: RoundPaths, entries: Iterable[str], state: RoundState, events: EventLog,
                 clock_now, extra: Mapping[str, str] | None = None) -> None:
        self.paths = paths
        self.entries = sorted(set(str(e) for e in entries))
        self.state = state
        self.events = events
        self.now = clock_now
        self.extra = dict(extra or {})

    @property
    def baseline_path(self) -> Path:
        return self.paths.manifests / "baseline.json"

    def build(self) -> dict[str, Any]:
        return build_manifest(self.entries, extra=self.extra, created_at=utc_now(self.now()))

    def record_start(self) -> dict[str, Any]:
        manifest = self.build()
        atomic_write_json(self.paths.manifests / "start.json", manifest, mode=0o600)
        atomic_write_json(self.baseline_path, manifest, mode=0o600)
        self.state.data["manifest"]["start_digest"] = manifest["digest"]
        self.state.save()
        self.events.emit("manifest_baseline", private=True, digest=manifest["digest"],
                         entries=len(manifest["entries"]))
        self.events.emit("manifest_check", label="start", ok=True)
        return manifest

    def check(self, label: str) -> tuple[bool, list[dict[str, Any]]]:
        baseline = read_json(self.baseline_path)
        current = self.build()
        changes = diff_manifests(baseline, current)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        atomic_write_json(self.paths.manifests / f"{safe_label}.json", current, mode=0o600)
        info = self.state.data["manifest"]
        info["checks"] = int(info.get("checks", 0)) + 1
        info["last_label"] = label
        if changes:
            info["ok"] = False
            info.setdefault("changes", []).append({"label": label, "changes": changes[:50]})
            self.state.violation("tamper", f"{len(changes)} protected entries changed at {label}",
                                 utc_now(self.now()))
        self.state.save()
        # 公开事件不携带路径或摘要（holdout seed 文件就是其中之一）
        self.events.emit("manifest_check", label=label, ok=not changes, changed=len(changes))
        self.events.emit("manifest_check_detail", private=True, label=label, changes=changes)
        return not changes, changes

    def rebaseline(self, *, reason: str, operator: str) -> dict[str, Any]:
        if not self.baseline_path.exists():
            raise HarnessError("the round has not started, so there is no baseline to amend")
        if not reason.strip():
            raise HarnessError("an amendment needs a reason")
        before = read_json(self.baseline_path)
        current = self.build()
        changes = diff_manifests(before, current)
        atomic_write_json(self.baseline_path, current, mode=0o600)
        number = int(self.state.data["manifest"].get("amendments", 0)) + 1
        self.state.data["manifest"]["amendments"] = number
        self.state.data.setdefault("amendments", []).append(
            {"number": number, "utc": utc_now(self.now()), "operator": operator, "reason": reason,
             "changed": [c["path"] for c in changes if not c["path"].startswith("extra:")]})
        self.state.save()
        self.events.emit("amendment", number=number, reason=reason, operator=operator,
                         changed_entries=len(changes))
        self.events.emit("amendment_detail", private=True, number=number, changes=changes,
                         digest=current["digest"])
        return {"amendment": number, "changes": changes, "digest": current["digest"]}
