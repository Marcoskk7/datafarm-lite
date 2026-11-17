"""请求 → 场景布局的符合性检查（1b 闸门）。

确定性、纯 Python：不启动仿真，也不做文件 I/O（`load_role_rules` 除外）。调用方传入选好的
`RequestDoc`、解析后的 scene-layout 文档，以及可选的 AssetRecord 表（按 asset_id 索引）。

`ok` 只表示没有 problems；`complete=False` 表示有关检查因缺少 AssetRecord 被跳过
（`skipped` 里逐条写明），此时的 `ok` 不能证明关节约束成立。是否接受不完整报告由调用方决定。

规则的取值全部在 `configs/scene-roles.json`（`datafarm.scene-roles/1`），本模块只解释规则。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from datafarm.request.schema import RequestDoc

RULES_SCHEMA = "datafarm.scene-roles/1"
DEFAULT_SUPPORT = "floor"
RANDOMIZATION_KEYS = ("position_jitter_m", "yaw_range_deg")
REQUIRED_ROLES = ("task_object", "receptacle", "background")
ROLE_RULE_KEYS = frozenset({"reach_targets_min", "require_probe", "probe_asset_role", "require_randomization"})


@dataclass(frozen=True)
class ConformanceReport:
    """`ok` = 没有 problems；`complete` = 所有检查都真正跑过（关节检查未因缺记录跳过）。"""

    ok: bool
    problems: list[str]
    skipped: list[str] = field(default_factory=list)
    complete: bool = True


def load_role_rules(path: str | Path) -> dict[str, Any]:
    """读取并轻校验角色规则表；未知键、缺失角色和类型错误都直接报错。"""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("scene role rules must be an object")
    if document.get("schema") != RULES_SCHEMA:
        raise ValueError(f"scene role rules schema must be {RULES_SCHEMA}")

    roles = document.get("roles")
    if not isinstance(roles, Mapping):
        raise ValueError("scene role rules needs a 'roles' object")
    missing = [role for role in REQUIRED_ROLES if role not in roles]
    if missing:
        raise ValueError(f"scene role rules missing roles {missing}")
    for role, rule in roles.items():
        if not isinstance(rule, Mapping):
            raise ValueError(f"roles.{role} must be an object")
        unknown = set(rule) - ROLE_RULE_KEYS
        if unknown:
            raise ValueError(f"roles.{role} has unknown keys {sorted(unknown)}")
        if "reach_targets_min" in rule and (not isinstance(rule["reach_targets_min"], int)
                                            or rule["reach_targets_min"] < 0):
            raise ValueError(f"roles.{role}.reach_targets_min must be a non-negative integer")

    robots = document.get("robots")
    if not isinstance(robots, Mapping):
        raise ValueError("scene role rules needs a 'robots' object")
    for name, rule in robots.items():
        if not isinstance(rule, Mapping) or not isinstance(rule.get("package_asset"), str):
            raise ValueError(f"robots.{name} must declare a package_asset string")
        if "asset_id" in rule and not isinstance(rule["asset_id"], str):
            raise ValueError(f"robots.{name}.asset_id must be a string")
        cameras = rule.get("cameras", [])
        if not isinstance(cameras, list) or not all(isinstance(c, Mapping) and isinstance(c.get("camera_id"), str)
                                                    for c in cameras):
            raise ValueError(f"robots.{name}.cameras must be a list of camera objects with camera_id")

    thresholds = document.get("joint_state")
    if not isinstance(thresholds, Mapping):
        raise ValueError("scene role rules needs a 'joint_state' object")
    for key in ("open_min_fraction", "closed_max_fraction"):
        if not isinstance(thresholds.get(key), (int, float)):
            raise ValueError(f"joint_state.{key} must be a number")
    return document


def _fraction(value: float, joint: Mapping[str, Any]) -> float:
    """关节位置在 [lower, upper] 里的归一化位置；区间退化时按是否高于下限取 0/1。"""
    limits = joint["limits"]
    lower, upper = float(limits["lower"]), float(limits["upper"])
    span = upper - lower
    if span <= 0:
        return 1.0 if value > lower else 0.0
    return (value - lower) / span


def check_conformance(request: RequestDoc, layout: Mapping[str, Any],
                      records: Mapping[str, Mapping[str, Any]] | None,
                      rules: Mapping[str, Any]) -> ConformanceReport:
    """检查 scene-layout 是否忠实实现 request；问题按请求物体与布局顺序排列。

    `count>1` 的请求物体按 `<object_id>.<i>`（i 从 0 起）命名各自的布局节点；`count=1` 时
    node_id 必须等于 object_id。`records` 为 None 时关节类型/初值检查记为 skipped。
    """
    problems: list[str] = []
    skipped: list[str] = []
    nodes = list(layout.get("nodes") or [])
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for node in nodes:
        by_id.setdefault(node.get("node_id"), []).append(node)
    assets = {entry.get("asset_id"): entry for entry in (layout.get("assets") or [])}
    nodes_by_asset: dict[str, list[str]] = {}
    for node in nodes:
        nodes_by_asset.setdefault(node.get("asset_id"), []).append(node.get("node_id"))
    robot = layout.get("robot") or {}
    robot_node_id = robot.get("node_id") if isinstance(robot, Mapping) else None

    expected_ids: dict[str, list[str]] = {}
    request_role_by_node: dict[str, str] = {}
    for obj in request.scene.objects:
        ids = ([obj.object_id] if obj.count == 1
               else [f"{obj.object_id}.{index}" for index in range(obj.count)])
        expected_ids[obj.object_id] = ids
        for node_id in ids:
            request_role_by_node[node_id] = obj.role

    thresholds = rules.get("joint_state") or {}
    open_min = float(thresholds.get("open_min_fraction", 0.5))
    closed_max = float(thresholds.get("closed_max_fraction", 0.1))
    sources = request.scene.asset_sources
    simready_checked: set[str] = set()

    def asset_plays(asset_value: Any, wanted: str) -> bool:
        """布局里用这个资产的节点中，是否有请求角色为 `wanted` 的。"""
        return any(request_role_by_node.get(using) == wanted
                   for using in nodes_by_asset.get(asset_value, ()))

    for obj in request.scene.objects:
        role_rule = (rules.get("roles") or {}).get(obj.role) or {}
        for node_id in expected_ids[obj.object_id]:
            matches = by_id.get(node_id, [])
            if not matches:
                problems.append(f"missing layout node for request object '{obj.object_id}' "
                                f"(expected node_id '{node_id}')")
                continue
            if len(matches) > 1:
                problems.append(f"duplicate layout node '{node_id}': request object "
                                f"'{obj.object_id}' must map to exactly one node")
                continue
            node = matches[0]
            asset_id = node.get("asset_id")
            asset = assets.get(asset_id)
            if asset is None:
                problems.append(f"node '{node_id}': asset '{asset_id}' is not declared in layout assets")
            elif asset.get("role") != obj.role:
                problems.append(f"node '{node_id}': asset '{asset_id}' has role "
                                f"'{asset.get('role')}', request says '{obj.role}'")
            if (asset is not None and sources is not None and "simready" in asset
                    and "simready" not in sources and asset_id not in simready_checked):
                simready_checked.add(asset_id)
                problems.append(f"asset '{asset_id}': simready is not allowed by request "
                                f"asset_sources {list(sources)}")

            support = obj.constraints.support or DEFAULT_SUPPORT
            allowed = ({DEFAULT_SUPPORT} if support == DEFAULT_SUPPORT
                       else set(expected_ids.get(support, [support])))
            actual_support = node.get("on", DEFAULT_SUPPORT)
            if actual_support not in allowed:
                problems.append(f"node '{node_id}': support '{actual_support}' does not match "
                                f"request support '{support}'")

            joint_type = obj.constraints.joint
            if joint_type is not None:
                state = obj.constraints.joint_state
                if records is None:
                    skipped.append(f"node '{node_id}': joint type '{joint_type}' not checked (no asset records)")
                    if state is not None:
                        skipped.append(f"node '{node_id}': joint_state '{state}' not checked (no asset records)")
                else:
                    record = records.get(asset_id)
                    joints = ([j for j in (record.get("joints") or []) if j.get("type") == joint_type]
                              if isinstance(record, Mapping) else [])
                    if not isinstance(record, Mapping):
                        problems.append(f"node '{node_id}': asset '{asset_id}' has no AssetRecord")
                    elif not joints:
                        problems.append(f"node '{node_id}': asset '{asset_id}' has no {joint_type} joint")
                    elif state is not None:
                        values = node.get("joint_state") or {}
                        fractions = [_fraction(float(values.get(joint.get("name"), 0.0)), joint)
                                     for joint in joints]
                        if state == "open" and not any(f >= open_min for f in fractions):
                            problems.append(f"node '{node_id}': joint_state 'open' but no {joint_type} "
                                            f"joint reaches {round(open_min * 100)}% of its range")
                        elif state == "closed" and not any(f <= closed_max for f in fractions):
                            problems.append(f"node '{node_id}': joint_state 'closed' but no {joint_type} "
                                            f"joint stays within {round(closed_max * 100)}% of its lower limit")

            min_targets = int(role_rule.get("reach_targets_min", 0))
            targets = [t for t in (layout.get("reach_targets") or []) if t.get("node") == node_id]
            if len(targets) < min_targets:
                problems.append(f"node '{node_id}' ({obj.role}): needs at least {min_targets} reach "
                                f"targets on this node, found {len(targets)}")

            probes = [p for p in (layout.get("probes") or []) if p.get("node") == node_id]
            if role_rule.get("require_probe") and not probes:
                problems.append(f"node '{node_id}' ({obj.role}): requires at least one probe "
                                f"with node '{node_id}'")
            probe_role = role_rule.get("probe_asset_role")
            if probe_role is not None:
                if not any(asset_plays(probe.get("asset_id"), probe_role) for probe in probes):
                    problems.append(f"node '{node_id}' ({obj.role}): requires a probe with node "
                                    f"'{node_id}' whose asset is a {probe_role} asset")

            if role_rule.get("require_randomization"):
                randomization = node.get("randomization")
                if (not isinstance(randomization, Mapping)
                        or not any(key in randomization for key in RANDOMIZATION_KEYS)):
                    problems.append(f"node '{node_id}' ({obj.role}): missing randomization "
                                    f"(position_jitter_m / yaw_range_deg)")

    expected = {node_id for ids in expected_ids.values() for node_id in ids}
    for node_id in by_id:
        if node_id not in expected and node_id != robot_node_id:
            problems.append(f"layout node '{node_id}' is not in the request")

    if not isinstance(robot, Mapping) or not robot.get("asset_id"):
        problems.append("layout has no robot block with asset_id")
    else:
        robot_rule = (rules.get("robots") or {}).get(request.robot)
        if robot_rule is None:
            problems.append(f"no robot rule for request robot '{request.robot}'")
        else:
            expected_asset = robot_rule.get("asset_id")
            if expected_asset is not None and robot.get("asset_id") != expected_asset:
                problems.append(f"layout robot asset_id '{robot.get('asset_id')}' does not match "
                                f"request robot '{request.robot}' (expected '{expected_asset}')")
            expected_package = robot_rule.get("package_asset")
            if robot.get("package_asset") != expected_package:
                problems.append(f"layout robot package_asset '{robot.get('package_asset')}' does not "
                                f"match request robot '{request.robot}' (expected '{expected_package}')")

    # request.cameras 是要记录的物理相机（`head` → SceneGraph `camera.head`）；布局不声明就进不了 SceneGraph，
    # 渲染在 RenderSpec 阶段才报 "neither a scene graph camera nor a configured virtual view"（lab30 实跑踩到）。
    # 相机内外参是机器人的标定事实（来源见 docs/ir/scene-graph.md），由角色规则 robots.<robot>.cameras 给出，
    # 布局必须原样声明（`${robot_node}` 换成布局机器人 node_id），不允许 agent 自拟标定。
    declared = {c.get("camera_id"): c for c in (layout.get("cameras") or []) if isinstance(c, Mapping)}
    robot_rule = (rules.get("robots") or {}).get(request.robot) or {}
    facts = {}
    for camera in robot_rule.get("cameras") or []:
        text = json.dumps(camera).replace("${robot_node}", str(robot_node_id))
        facts[camera["camera_id"]] = json.loads(text)
    for camera in request.cameras:
        camera_id = f"camera.{camera}"
        expected = facts.get(camera_id)
        if camera_id not in declared:
            hint = f": declare exactly {json.dumps(expected, separators=(',', ':'))}" if expected else ""
            problems.append(f"request camera '{camera}' is not declared in layout cameras as '{camera_id}'{hint}")
        elif expected is not None and declared[camera_id] != expected:
            problems.append(f"layout camera '{camera_id}' differs from the robot's calibration facts: declare "
                            f"exactly {json.dumps(expected, separators=(',', ':'))}")

    return ConformanceReport(ok=not problems, problems=problems, skipped=skipped, complete=not skipped)
