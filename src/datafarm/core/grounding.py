"""grounding 记录 G 的物理量账本。

一段 calibration 视频能确定一部分量，其余量则无法辨识。因此模拟器使用的每个
物理量都会列出其已知方式：

- measured：来自传感器、数据手册或机器人描述；需要证据。
- fitted：从真实 episode 估计（相机拟合、尺度、桌面高度）；需要证据。
- assumed：没有任务证据的选定值（例如摩擦先验）。
- unknown：未确定；值可以为 null。

assumed 或 unknown 的量必须被随机化覆盖（一个范围或一组选项），或带有明确的
不随机化理由，这样自信但无依据的值就不会悄悄进入模拟器，也不会
在无人察觉的情况下影响结果。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from datafarm.common import is_finite_number

GROUNDING_SCHEMA = 'datafarm.grounding/1'
QUANTITY_LABELS = ('measured', 'fitted', 'assumed', 'unknown')
EVIDENCE_LABELS = {'measured', 'fitted'}
UNCERTAIN_LABELS = {'assumed', 'unknown'}


class GroundingError(ValueError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise GroundingError(message)


def _randomization(name: str, value: Any, spec: Any) -> None:
    _check(isinstance(spec, dict), f'{name}: randomization must be an object')
    if 'choices' in spec:
        _check(isinstance(spec['choices'], list) and len(spec['choices']) >= 2,
               f'{name}: randomization choices need at least two values')
        return
    low, high = spec.get('low'), spec.get('high')
    _check(is_finite_number(low) and is_finite_number(high) and low < high, f'{name}: randomization needs low < high')
    if is_finite_number(value):
        _check(low <= value <= high, f'{name}: randomization range must contain the nominal value')


def validate_quantities(entries: Any, *, bind: Callable[[dict], Any] | None = None) -> dict[str, int]:
    """校验 physical_quantities 列表。

    `bind` 会校验每个证据引用（交付物对应受约束的工作区文件，IR 中对应按内容寻址的
    ArtifactRef）；未提供时则要求 path/sha256 形状。

    返回每个 label 对应的数量。
    """
    _check(isinstance(entries, list), 'physical_quantities must be a list')
    counts = {label: 0 for label in QUANTITY_LABELS}
    names = set()
    for index, entry in enumerate(entries):
        _check(isinstance(entry, dict), f'physical_quantities[{index}] must be an object')
        name = entry.get('name')
        _check(isinstance(name, str) and bool(name.strip()), f'physical_quantities[{index}] needs a name')
        _check(name not in names, f'duplicate physical quantity: {name}')
        names.add(name)
        label = entry.get('label')
        _check(label in QUANTITY_LABELS, f'{name}: label must be one of {list(QUANTITY_LABELS)}')
        _check(isinstance(entry.get('unit'), str), f'{name}: unit must be a string ("" if dimensionless)')
        value = entry.get('value')
        _check(value is None or is_finite_number(value) or isinstance(value, (list, str)),
               f'{name}: value must be a finite number, list, string or null')
        _check(value is not None or label == 'unknown', f'{name}: only unknown quantities may omit the value')
        evidence = entry.get('evidence', [])
        _check(isinstance(evidence, list), f'{name}: evidence must be a list')
        if label in EVIDENCE_LABELS:
            _check(bool(evidence), f'{name}: {label} quantity needs evidence')
        for ref in evidence:
            _check(isinstance(ref, dict), f'{name}: evidence entries must be objects')
            if bind is None:
                _check(isinstance(ref.get('path'), str) and isinstance(ref.get('sha256'), str),
                       f'{name}: evidence must be path/sha256 references')
            else:
                bind(ref)
        if label in UNCERTAIN_LABELS:
            reason = entry.get('not_randomized_reason')
            if 'randomization' in entry:
                _randomization(name, value, entry['randomization'])
            else:
                _check(isinstance(reason, str) and bool(reason.strip()),
                       f'{name}: {label} quantity needs randomization or not_randomized_reason')
        elif 'randomization' in entry:
            _randomization(name, value, entry['randomization'])
        counts[label] += 1
    return counts


def validate_grounding(document: Any, *, bind: Callable[[dict], Any] | None = None) -> dict[str, int]:
    _check(isinstance(document, dict) and document.get('schema') == GROUNDING_SCHEMA,
           f'grounding record must use schema {GROUNDING_SCHEMA}')
    return validate_quantities(document.get('physical_quantities'), bind=bind)


def load_grounding(path: Path, *, bind: Callable[[dict], Any] | None = None) -> dict[str, int]:
    import json
    return validate_grounding(json.loads(Path(path).read_text()), bind=bind)
