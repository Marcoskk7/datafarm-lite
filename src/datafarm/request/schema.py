"""Request — `datafarm.request/1`（docs/ir/request.md）。

1a（一句话 → request.yaml）与 1b（request.yaml → scene-layout）之间的契约：只写意图，
不带坐标、资产 ID 或主机路径。结构由 pydantic 模型校验，跨字段约束（seed 区间不重叠、
object_id 唯一、support 引用存在且无环、相机属于机器人）在 `RequestDoc` 的 model validator 里显式检查。
同一份模型导出的 JSON Schema 用作 1a 的结构化输出 schema。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from datafarm.ir.asset_record import ROLES, SOURCE_KINDS
from datafarm.ir.common import IRError, portable
from datafarm.kernel.schema import IDENTIFIER_PATTERN

SCHEMA = "datafarm.request/1"
FLOOR = "floor"
OBJECT_ROLES = tuple(role for role in ROLES if role not in ("robot", "camera_rig"))
CAMERA_IDS = ("head", "left_wrist", "right_wrist")
# 机器人能力表：机器人 → 实际装了的相机。唯一出处；新机器人在这里加一行。
ROBOT_CAMERAS: dict[str, tuple[str, ...]] = {
    "airbot_single_g2": ("head", "left_wrist"),
}

Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN.pattern)]
Token = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
SeedRange = Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=2, max_length=2)]


class RequestError(IRError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ObjectConstraints(_Model):
    """物体的结构化约束；全部可选，空对象表示无约束。"""

    joint: Literal["prismatic", "revolute"] | None = Field(
        None, description="Articulation the object must have, e.g. a drawer slide is prismatic.")
    joint_state: Literal["open", "closed"] | None = Field(
        None, description="Initial state of that articulation; requires joint.")
    shape: Token | None = Field(None, description="Shape feature in snake_case, e.g. raised_rim.")
    support: Identifier | None = Field(
        None, description="object_id this object rests on, or 'floor'.")

    @model_validator(mode="after")
    def _joint_state_needs_joint(self) -> ObjectConstraints:
        if self.joint_state is not None and self.joint is None:
            raise ValueError("joint_state requires joint")
        return self


class SceneObject(_Model):
    object_id: Identifier = Field(description="Unique id, dotted lowercase, e.g. object.plate.")
    category: Token = Field(description="Object category noun in snake_case, e.g. drawer_cabinet.")
    role: Literal[OBJECT_ROLES] = Field(description="Part the object plays in the task.")
    constraints: ObjectConstraints = Field(default_factory=ObjectConstraints)
    count: int = Field(1, ge=1, description="Number of identical instances.")


class SceneRequest(_Model):
    setting: Token | None = Field(None, description="Scene setting, e.g. kitchen.")
    objects: list[SceneObject] = Field(min_length=1)
    asset_sources: list[Literal[SOURCE_KINDS]] | None = Field(
        None, min_length=1, description="Allowed asset sources in preference order; omitted means any.")


class SeedPartitions(_Model):
    """闭区间 `[first, last]`；dev 给作者调试，holdout 只给冻结判定器验收。"""

    dev: SeedRange
    holdout: SeedRange


class RenderRequest(_Model):
    engine: Literal["isaac_rtx"]
    views: list[Identifier] = Field(min_length=1, description="View ids: robot camera ids or virtual views.")
    materials: int = Field(ge=1, description="Material variants per view; 1 keeps the asset materials.")
    lights: int = Field(ge=1, description="Lighting variants per view; 1 keeps the scene lighting.")


class DatasetRequest(_Model):
    format: Literal["hdf5"]


class RequestDoc(_Model):
    schema_id: Literal[SCHEMA] = Field(alias="schema")
    request_id: Identifier
    robot: Literal[tuple(ROBOT_CAMERAS)]
    task: str = Field(min_length=1, description="The goal as one natural-language sentence.")
    scene: SceneRequest
    episodes: int = Field(ge=1, description="Accepted episodes to deliver.")
    seeds: SeedPartitions
    cameras: list[Literal[CAMERA_IDS]] = Field(min_length=1, description="Robot cameras to record.")
    render: RenderRequest
    dataset: DatasetRequest

    @model_validator(mode="after")
    def _cross_fields(self) -> RequestDoc:
        for name, (first, last) in (("dev", self.seeds.dev), ("holdout", self.seeds.holdout)):
            if first > last:
                raise ValueError(f"seeds.{name} must be [first, last] with first <= last, got [{first}, {last}]")
        (dev_first, dev_last), (hold_first, hold_last) = self.seeds.dev, self.seeds.holdout
        if dev_first <= hold_last and hold_first <= dev_last:
            raise ValueError(f"seeds.dev {self.seeds.dev} overlaps seeds.holdout {self.seeds.holdout}")

        ids = [obj.object_id for obj in self.scene.objects]
        duplicates = sorted({x for x in ids if ids.count(x) > 1})
        if duplicates:
            raise ValueError(f"scene.objects has duplicate object_id {duplicates}")
        if FLOOR in ids:
            raise ValueError(f"object_id {FLOOR!r} is reserved")
        if not any(obj.role == "task_object" for obj in self.scene.objects):
            raise ValueError("scene.objects needs at least one task_object")
        supports = {obj.object_id: obj.constraints.support for obj in self.scene.objects}
        for object_id, support in supports.items():
            if support is not None and support != FLOOR and support not in supports:
                raise ValueError(f"{object_id}: support {support!r} is not an object_id in scene.objects or 'floor'")
        for start in supports:
            seen, node = set(), start
            while node in supports:
                if node in seen:
                    raise ValueError(f"support cycle through {node!r}")
                seen.add(node)
                node = supports[node]

        available = ROBOT_CAMERAS[self.robot]
        if len(set(self.cameras)) != len(self.cameras):
            raise ValueError("cameras must be unique")
        missing = [camera for camera in self.cameras if camera not in available]
        if missing:
            raise ValueError(f"cameras {missing} are not available on {self.robot} (has {list(available)})")
        if len(set(self.render.views)) != len(self.render.views):
            raise ValueError("render.views must be unique")
        unrecorded = [view for view in self.render.views if view in CAMERA_IDS and view not in self.cameras]
        if unrecorded:
            raise ValueError(f"render.views {unrecorded} name robot cameras that are not in cameras")
        return self


def _location(loc: tuple[Any, ...]) -> str:
    text = "request"
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text


def validate_request(document: Any) -> RequestDoc:
    """校验一份 request 文档；返回不可变的 `RequestDoc`，失败抛 `RequestError`（每条错误一行）。"""
    if not isinstance(document, Mapping):
        raise RequestError("request must be an object")
    try:
        portable(document, "request")
    except IRError as exc:
        raise RequestError(str(exc)) from None
    try:
        return RequestDoc.model_validate(document)
    except ValidationError as exc:
        lines = [f"{_location(error['loc'])}: {error['msg']}" for error in exc.errors()]
        raise RequestError("invalid request:\n" + "\n".join(lines)) from None


def load_request(path: str | os.PathLike[str]) -> RequestDoc:
    with open(path, encoding="utf-8") as handle:
        try:
            document = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise RequestError(f"{os.fspath(path)}: not valid YAML: {exc}") from None
    return validate_request(document)


def request_json_schema() -> dict[str, Any]:
    """`RequestDoc` 的 JSON Schema（字段名用 `schema` 别名），作为 1a 的结构化输出 schema。跨字段约束不在其中。"""
    return RequestDoc.model_json_schema(by_alias=True)
