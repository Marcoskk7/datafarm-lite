"""1a：一句话 → `datafarm.request/1`。

一次结构化 LLM 调用抽取句子里说了（或明确蕴含）的字段，`fill_defaults` 补齐其余字段，最后 `validate_request`。
不重试：输出不合格就带着原因抛 `DraftError`，由调用方决定是否换句子或换模型再来一次。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from datafarm.kernel.io import sha256_json
from datafarm.request.defaults import OPTIONAL_PATHS, Source, fill_defaults
from datafarm.request.ports import StructuredLLM, StructuredResult
from datafarm.request.schema import RequestDoc, RequestError, request_json_schema, validate_request

DRAFT_SCHEMA_NAME = "datafarm_request_draft"


class DraftError(RequestError):
    pass


@dataclass(frozen=True)
class DraftResult:
    request: RequestDoc
    provenance: dict[str, Source]
    sentence: str
    llm: StructuredResult
    prompt_sha256: str  # 发给模型的 messages + schema 的规范 JSON 摘要


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    return schema["$defs"][ref.rsplit("/", 1)[-1]] if ref else node


def draft_json_schema() -> dict[str, Any]:
    """1a 的输出 schema：由 `request_json_schema()` 派生，把有默认值的字段（及其所在对象）从 `required` 里去掉。"""
    schema = request_json_schema()
    for path in OPTIONAL_PATHS:
        node = schema
        for key in path.split("."):
            node["required"] = [name for name in node.get("required", []) if name != key]
            node = _resolve(schema, node["properties"][key])
    return schema


def build_messages(sentence: str, instructions: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": instructions}, {"role": "user", "content": sentence}]


def draft_request(sentence: str, llm: StructuredLLM, *, instructions: str) -> DraftResult:
    """抽取 → 补默认 → 校验。LLM 错误原样上抛（`StructuredLLMError`）；输出不合格抛 `DraftError`。"""
    sentence = sentence.strip()
    if not sentence:
        raise DraftError("sentence is empty")
    if not instructions.strip():
        raise DraftError("instructions are empty")
    messages = build_messages(sentence, instructions)
    schema = draft_json_schema()
    prompt_sha256 = sha256_json({"messages": messages, "schema": schema, "schema_name": DRAFT_SCHEMA_NAME})
    result = llm.complete_json(messages=messages, schema=schema, schema_name=DRAFT_SCHEMA_NAME)
    if not isinstance(result.data, dict):
        raise DraftError(f"LLM draft must be a JSON object, got {type(result.data).__name__}")
    document, provenance = fill_defaults(copy.deepcopy(result.data))
    try:
        request = validate_request(document)
    except RequestError as exc:
        defaulted = sorted(path for path, source in provenance.items() if source == "default")
        raise DraftError(f"LLM draft (model {result.model}, response {result.response_id}) is not a valid request; "
                         f"defaulted fields {defaulted}\n{exc}") from None
    return DraftResult(request=request, provenance=provenance, sentence=sentence, llm=result,
                       prompt_sha256=prompt_sha256)
