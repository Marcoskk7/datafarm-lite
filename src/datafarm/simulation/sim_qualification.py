"""模拟器资格认证：让模拟器支持 task 的合理动作。

模拟器 adapter 是 Episode IR 的签署权威，但它的物理特性可能让一个合理的
动作无法完成：闭合的平行夹爪可能抓不住悬挂的布料，于是真实机器人的
动作对每个 expert 都失败。因此，在为 task 生成 episode 之前，
pipeline 会针对候选 *模拟器 variant*（参数 patch 和可选的接触模型）运行脚本化的 *skill probe*
（task 各 skill 的规范化、类真实机器人动作），按声明的标准评判每个 probe，
并选出能让每个必需 probe 通过的最小改动。该选择以 certificate 形式签发，绑定确切的
variant 文件和 probe 证据；``verify_certificate`` 会独立重算它。

本模块只包含决策逻辑。运行 probe 以及从模拟器状态提取指标是
task 代码的职责。
"""

from __future__ import annotations

from datafarm.common import is_finite_number, sha256_json
import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "datafarm.sim-qualification.certificate/1.0.0"


@dataclass(frozen=True)
class SimulatorVariant:
    """相对于 task 基础配置的一个候选模拟器配置。"""

    name: str
    material_patch: Mapping[str, Any] = field(default_factory=dict)
    scene_patch: Mapping[str, Any] = field(default_factory=dict)
    runtime_flags: tuple[str, ...] = ()
    note: str = ""

    @property
    def change_magnitude(self) -> int:
        """独立改动的数量；选择器偏好最小者。"""

        return len(self.material_patch) + len(self.scene_patch) + len(self.runtime_flags)


def apply_patch(document: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """返回 ``document`` 的副本，其中点号键的值被替换。

    字符串值 ``"*k"`` 会把现有数字乘以 ``k``。尚不存在的路径是
    错误：资格认证绝不能静默添加一个未被消费的字段。
    """

    out = copy.deepcopy(dict(document))
    for dotted, value in patch.items():
        keys = dotted.split(".")
        node: Any = out
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(f"patch path does not exist: {dotted}")
            node = node[key]
        last = keys[-1]
        if not isinstance(node, dict) or last not in node:
            raise ValueError(f"patch path does not exist: {dotted}")
        if isinstance(value, str) and value.startswith("*"):
            base = node[last]
            if not isinstance(base, (int, float)) or isinstance(base, bool):
                raise ValueError(f"multiplier needs a numeric field: {dotted}")
            value = base * float(value[1:])
        node[last] = value
    return out


@dataclass(frozen=True)
class ProbeOutcome:
    probe: str
    kind: str
    passed: bool
    failures: tuple[str, ...]
    advisories: tuple[str, ...]
    metrics: Mapping[str, Any]


def evaluate_hold_probe(
    probe: str,
    metrics: Mapping[str, Any],
    *,
    max_slip_m: float = 0.03,
    min_lift_m: float = 0.10,
    max_strain_p95: float = 1.15,
    max_strain_p999: float | None = None,
    min_flat_area: float = 0.85,
    max_flat_height_m: float = 0.02,
) -> ProbeOutcome:
    """抓取并悬挂 probe：闭合的夹爪必须能承载悬挂的布料。

    必需：probe 到达悬挂状态（存在 ``hang_z_max_m``），布料至少被抬起
    ``min_lift_m``，每个被夹住的 patch 都保持在其 pad 的 ``max_slip_m`` 范围内，且悬挂的布料
    拉伸不超过 ``max_strain_p95``。当设置 ``max_strain_p999`` 时，弹簧应变的
    第 99.9 百分位（被夹住 patch 旁边的少数弹簧，接触模型会把载荷集中在那里）也
    要求低于该值。最终的平整度取决于脚本化的放置动作，仅作提示。
    """

    failures: list[str] = []
    advisories: list[str] = []
    z_max = metrics.get("hang_z_max_m")
    if not is_finite_number(z_max):
        failures.append("hang_not_reached")
    elif z_max < min_lift_m:
        failures.append("cloth_not_lifted")
    slips = metrics.get("held_dz_m") or {}
    if not slips:
        failures.append("grip_slip_unmeasured")
    elif any(not is_finite_number(v) or v < -max_slip_m for v in slips.values()):
        failures.append("grip_slipped")
    strain = metrics.get("hang_strain_p95")
    if not is_finite_number(strain):
        failures.append("hang_strain_unmeasured")
    elif strain > max_strain_p95:
        failures.append("cloth_overstretched")
    if max_strain_p999 is not None:
        local = metrics.get("hang_strain_p999")
        if not is_finite_number(local):
            failures.append("local_strain_unmeasured")
        elif local > max_strain_p999:
            failures.append("cloth_locally_overstretched")
    area, height = metrics.get("final_area_frac"), metrics.get("final_height_mm")
    if not (is_finite_number(area) and area >= min_flat_area and is_finite_number(height) and height / 1000.0 < max_flat_height_m):
        advisories.append("laid_down_not_flat")
    return ProbeOutcome(probe, "hold", not failures, tuple(failures), tuple(advisories), dict(metrics))


def evaluate_fold_probe(
    probe: str,
    metrics: Mapping[str, Any],
    *,
    min_bottom_layer_capture: float = 0.5,
    require_settled: bool = False,
    max_creep_m: float | None = None,
    min_rest_window_s: float = 8.0,
) -> ProbeOutcome:
    """由 task 验证器评判的脚本化折叠 probe。

    必需：最后一次释放时两处折叠都在位（``m1_at_release``）。可选要求，只在
    spec 设置时使用：稳定后的最终状态中折叠仍在位（``settled_passed``），并且
    单独放置的已折叠布料保持不动：释放后 2 秒到结束之间最大的 landmark 漂移
    （``creep_mm``，窗口至少 ``min_rest_window_s``）至多为 ``max_creep_m``。很少被抬起的底层
    以及机器人 gate 失败仅作提示。
    """

    failures: list[str] = []
    advisories: list[str] = []
    if metrics.get("m1_at_release") is not True:
        failures.append("folds_not_in_place_at_release")
    if require_settled:
        settled = metrics.get("settled_passed")
        if settled is None:
            failures.append("settled_unmeasured")
        elif settled is not True:
            failures.append("folds_not_in_place_when_settled")
    if max_creep_m is not None:
        creep, window = metrics.get("creep_mm"), metrics.get("rest_window_s")
        if not (is_finite_number(creep) and is_finite_number(window)):
            failures.append("creep_unmeasured")
        elif window < min_rest_window_s:
            failures.append("rest_window_short")
        elif creep / 1000.0 > max_creep_m:
            failures.append("fold_creeps")
    capture = metrics.get("bottom_layer_capture_frac")
    if not is_finite_number(capture) or capture < min_bottom_layer_capture:
        advisories.append("bottom_layer_rarely_captured")
    if metrics.get("robot_failures"):
        advisories.append("robot_gate_failed")
    return ProbeOutcome(probe, "fold", not failures, tuple(failures), tuple(advisories), dict(metrics))


@dataclass(frozen=True)
class VariantResult:
    variant: SimulatorVariant
    outcomes: tuple[ProbeOutcome, ...]

    def outcome(self, probe: str) -> ProbeOutcome | None:
        return next((o for o in self.outcomes if o.probe == probe), None)

    def passes(self, required: Iterable[str]) -> bool:
        return all((o := self.outcome(p)) is not None and o.passed for p in required)

    @property
    def score(self) -> float:
        """通过的 probe 计 2 分，每个提示扣 0.25；只用于打破平局。"""

        return sum(2.0 * o.passed - 0.25 * len(o.advisories) for o in self.outcomes)


@dataclass(frozen=True)
class QualificationDecision:
    selected: str | None
    ranking: tuple[tuple[str, bool, int, float], ...]   # (variant, 是否通过必需 probe, 改动量, score)
    reason: str


def select_variant(results: Sequence[VariantResult], required_probes: Sequence[str]) -> QualificationDecision:
    """选出能让每个必需 probe 通过的最小改动（平局：更高 score、更小名称）。"""

    if not required_probes:
        raise ValueError("at least one required probe is needed")
    ranking = sorted(
        ((r.variant.name, r.passes(required_probes), r.variant.change_magnitude, r.score) for r in results),
        key=lambda row: (not row[1], row[2], -row[3], row[0]),
    )
    passing = [row for row in ranking if row[1]]
    if not passing:
        return QualificationDecision(None, tuple(ranking), "no variant passes every required probe")
    best = passing[0]
    return QualificationDecision(best[0], tuple(ranking),
                                 f"smallest change ({best[2]}) passing {len(required_probes)} required probes")


def build_certificate(
    *,
    task_family: str,
    decision: QualificationDecision,
    results: Sequence[VariantResult],
    required_probes: Sequence[str],
    file_digests: Mapping[str, Mapping[str, str]],
    criteria: Mapping[str, Any],
    canonical_digests: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """把决策与证据以及所选 variant 的确切文件绑定。

    ``file_digests`` 是字节的 SHA-256 摘要；``canonical_digests``（用于 JSON 文件）是
    :func:`canonical_json_sha256` 摘要，因此一次 run 可以证明它使用了相同内容，即使文件
    被重新序列化。证据保留每个 probe 的指标。
    """

    if decision.selected is None:
        raise ValueError("cannot certify: " + decision.reason)
    chosen = next(r for r in results if r.variant.name == decision.selected)
    if decision.selected not in file_digests:
        raise ValueError("file digests of the selected variant are required")
    return {
        "schema": SCHEMA,
        "task_family": task_family,
        "selected_variant": {
            "name": chosen.variant.name, "note": chosen.variant.note,
            "material_patch": dict(chosen.variant.material_patch), "scene_patch": dict(chosen.variant.scene_patch),
            "runtime_flags": list(chosen.variant.runtime_flags), "files_sha256": dict(file_digests[decision.selected]),
            "files_canonical_sha256": dict((canonical_digests or {}).get(decision.selected, {})),
        },
        "required_probes": list(required_probes),
        "criteria": dict(criteria),
        "ranking": [list(row) for row in decision.ranking],
        "evidence": {
            r.variant.name: {o.probe: {"passed": o.passed, "failures": list(o.failures),
                                       "advisories": list(o.advisories), "metrics": dict(o.metrics)}
                             for o in r.outcomes}
            for r in results
        },
        "reason": decision.reason,
    }


def verify_certificate(certificate: Mapping[str, Any], results: Sequence[VariantResult]) -> tuple[str, ...]:
    """独立检查：从 probe 结果重算选择与证据。"""

    failures: list[str] = []
    if certificate.get("schema") != SCHEMA:
        failures.append("schema")
    required = certificate.get("required_probes") or []
    try:
        decision = select_variant(results, required)
    except ValueError:
        return tuple(failures + ["required_probes"])
    selected = (certificate.get("selected_variant") or {}).get("name")
    if decision.selected != selected:
        failures.append("selection_mismatch")
    for r in results:
        claimed = (certificate.get("evidence") or {}).get(r.variant.name, {})
        for o in r.outcomes:
            if (claimed.get(o.probe) or {}).get("passed") != o.passed:
                failures.append(f"evidence_mismatch:{r.variant.name}:{o.probe}")
    if not (certificate.get("selected_variant") or {}).get("files_sha256"):
        failures.append("files_unbound")
    return tuple(failures)


def canonical_json_sha256(document: Any) -> str:
    """JSON 文档的 SHA-256，忽略键顺序和空白。"""

    return sha256_json(document, allow_nan=True)


def verify_run_binding(certificate: Mapping[str, Any], used: Mapping[str, str]) -> tuple[str, ...]:
    """检查一次 run 是否恰好使用了经认证的模拟器文件。

    ``used`` 把每个经认证的文件名（``material.json``、``scene.json`` 和 runtime-flag 文件）映射到
    该 run 实际使用的文件的摘要：``.json`` 文件用 :func:`canonical_json_sha256`，其他
    用字节的 SHA-256。每个经认证的文件都必须上报并匹配；上报了证书未列出的文件
    也是错误。
    """

    selected = certificate.get("selected_variant") or {}
    raw = selected.get("files_sha256") or {}
    canonical = selected.get("files_canonical_sha256") or {}
    if not raw:
        return ("files_unbound",)
    problems: list[str] = []
    for name, digest in raw.items():
        expected = canonical.get(name) if name.endswith(".json") else digest
        if expected is None:
            problems.append(f"canonical_digest_missing:{name}")
        elif name not in used:
            problems.append(f"not_reported:{name}")
        elif used[name] != expected:
            problems.append(f"mismatch:{name}")
    problems.extend(f"not_certified:{name}" for name in used if name not in raw)
    return tuple(problems)

