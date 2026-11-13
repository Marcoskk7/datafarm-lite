"""LLM / agent 调用的只追加账本，每行一个 JSON 对象。

pipeline 代码发起的每一次模型调用（规划评审、VLM 筛选、task-expert 编写会话）都由
发起它的 *controller* 记录，绝不由 agent 记录。记录在调用方依据响应行动之前写入，
因此失败和被放弃的调用仍留在分母中。凭据、prompt 和响应从不存放在这里，只保存
它们的 SHA-256 摘要和 provider 元数据。
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datafarm.common import append_jsonl, sha256_json, sha256_text  # noqa: F401
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "datafarm.agent-call/1"

STATUSES = frozenset(
    {
        "ok",  # 已收到并解析响应
        "invalid_response",  # 已收到响应，但解析或 schema 校验失败
        "error",  # 传输或 provider 错误
        "timeout",
        "cache_hit",  # 由确定性重放缓存提供，未调用 provider
        "aborted",  # controller 停止了该调用（预算、信号、操作员）
        "budget_exhausted",
    }
)


def new_call_id() -> str:
    return "call-" + uuid.uuid4().hex


@dataclasses.dataclass(frozen=True)
class AgentCallRecord:
    call_id: str
    component: str  # 哪个 controller 发起了该调用，例如 "expert-harness/session"
    backend: str  # "claude-cli" | "codex-exec" | "<provider>-chat" | "fake" | ...
    model: str
    started_at: str
    status: str
    ended_at: str | None = None
    request_sha256: str | None = None
    prompt_sha256: str | None = None
    response_sha256: str | None = None
    response_id: str | None = None
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None  # "provider" | "price_table" | None
    turns: int | None = None
    latency_s: float | None = None
    error_type: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id or not self.component or not self.backend or not self.model:
            raise ValueError("call_id, component, backend and model are required")
        if self.status not in STATUSES:
            raise ValueError(f"unknown agent-call status {self.status!r}")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["extra"] = dict(self.extra)
        return {"schema": SCHEMA, **data}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentCallRecord":
        if data.get("schema") != SCHEMA:
            raise ValueError(f"not an {SCHEMA} record: {data.get('schema')!r}")
        fields = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in fields})


def append_call(path: str | Path, record: AgentCallRecord) -> None:
    """在独占锁下追加一条记录并 fsync。"""

    append_jsonl(path, record.to_dict(), allow_nan=True)


def read_calls(path: str | Path) -> list[AgentCallRecord]:
    target = Path(path)
    if not target.exists():
        return []
    records = []
    for number, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            records.append(AgentCallRecord.from_dict(json.loads(raw)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{target}:{number}: invalid agent-call record: {exc}") from exc
    return records


def summarize(records: Iterable[AgentCallRecord]) -> dict[str, Any]:
    """保持分母的汇总；未知费用计入统计，而不是当作零。"""

    items = list(records)
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item.status] = by_status.get(item.status, 0) + 1

    def total(name: str) -> int:
        return sum(getattr(item, name) or 0 for item in items)

    known_cost = [item.cost_usd for item in items if item.cost_usd is not None]
    return {
        "calls": len(items),
        "by_status": by_status,
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "turns": total("turns"),
        "cost_usd_known": round(sum(known_cost), 6),
        "calls_with_unknown_cost": len(items) - len(known_cost),
        "latency_s": round(sum(item.latency_s or 0.0 for item in items), 3),
    }
