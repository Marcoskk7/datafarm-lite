"""从模拟器侧记录绑定逐 episode 的权威信息（事件、接缝）。

事件来自模拟器自身的命令流，绝不来自生成的视频：夹爪闭合/张开的转换，
正是接触产生或释放的时刻。事件涉及的 role 由 task spec 声明。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from datafarm.verify.spec import EventSpec


def gripper_events(state: Mapping[str, Any], *, roles: Sequence[str], arms: Sequence[str] = ("left", "right"),
                   threshold: float = 0.5, min_gap: int = 6,
                   wrist_panel: str = "{arm}_wrist", fixed_panels: Sequence[str] = ("head",)) -> list[EventSpec]:
    records = state["records"]
    events: list[EventSpec] = []
    for arm in arms:
        previous = None
        last_frame = -10 ** 9
        for record in records:
            command = (record.get("command") or {}).get(arm) or {}
            value = command.get("gripper_command")
            if value is None:
                continue
            closed = float(value) < threshold
            if previous is not None and closed != previous:
                frame = int(record["frame_index"])
                if frame - last_frame >= min_gap:
                    kind = "close" if closed else "open"
                    events.append(EventSpec(
                        event_id=f"{arm}-{kind}-{frame}", frame=frame, roles=tuple(roles),
                        panels=tuple(fixed_panels) + (wrist_panel.format(arm=arm),), kind=kind))
                    last_frame = frame
            previous = closed
    return sorted(events, key=lambda e: e.frame)


def load_state(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def frame_events(frames: Sequence[int], *, roles: Sequence[str], panels: Sequence[str] = (),
                 kind: str = "contact") -> list[EventSpec]:
    return [EventSpec(f"{kind}-{i}", int(f), tuple(roles), tuple(panels), kind) for i, f in enumerate(frames)]
