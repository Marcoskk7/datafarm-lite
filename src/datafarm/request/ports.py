"""1a 的结构化 LLM 端口：调用方只依赖这个协议，具体厂商的实现放在 adapters/ 由 entry/ 注入。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StructuredResult:
    """一次结构化调用的结果：`data` 是按 schema 解析出的 JSON 对象，其余字段供账本记录。"""

    data: dict[str, Any]
    model: str
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None


class StructuredLLMError(RuntimeError):
    """一次结构化调用失败。`status` 取 `datafarm.agent-call/1` 的状态（`invalid_response` / `error` / `timeout`）；
    消息不含 prompt、响应正文或凭据。"""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


class StructuredLLM(Protocol):
    def complete_json(self, *, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any],
                      schema_name: str) -> StructuredResult:
        """发送 `messages`（`{role, content}`），要求输出符合 JSON Schema `schema` 的对象。"""
        ...
