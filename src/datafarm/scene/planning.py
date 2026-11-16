"""布局求解的组装步骤：节点位姿 + 机器人基座 + 占地检查 + 可达性。

纯计算，不做文件 I/O，也不加载 AssetRecord 或机器人描述符：`tools/scene_assemble.py`
负责 `--spec` / `--work`、ArtifactStore 和 URDF 解析，再把 `spec`、`records`、`descriptor`
交给 `run_plan`。这样 `datafarm.scene` 不用 import `datafarm.adapters.sim`（同层不可互导），
也能被其他调用方（如布局作者 agent 的预检）复用。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datafarm.scene import layout


def run_plan(spec: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]],
             descriptor: Mapping[str, Any], *, spec_sha256: str) -> dict[str, Any]:
    """解出 `plan.json` 的内容：节点、机器人基座、占地与可达性，`ok` 是后两者的合取。"""
    nodes = layout.resolve_nodes(spec, records)
    reach = layout.reachability(spec, nodes, records, descriptor)
    footprint = layout.footprint_check(spec, nodes, records)
    return {
        "spec_sha256": spec_sha256,
        "nodes": nodes,
        "robot_base_pose": layout.robot_base_pose(spec, nodes, records),
        "footprint": footprint,
        "reachability": reach,
        "ok": bool(reach["ok"] and footprint["ok"]),
    }


def plan_diagnostics(plan: Mapping[str, Any]) -> list[str]:
    """把 `plan` 的失败原因写成一行一条，可直接回灌给布局作者。

    只有失败项：不可达目标（名字 + 位置误差）与占地问题（悬空、重叠）；全部通过时返回空列表。
    """
    lines = [
        f"reach target {row['name']} unreachable: position error {row['position_error_m']} m"
        for row in plan.get("reachability", {}).get("targets", []) if not row.get("ok")
    ]
    lines.extend(plan.get("footprint", {}).get("problems", []))
    return lines
