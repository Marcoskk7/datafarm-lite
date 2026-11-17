"""`StructuredLLM` 的脚本化替身：按顺序返回预置的 JSON 对象，并记下每次调用。"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from datafarm.request.ports import StructuredLLMError, StructuredResult


class FakeStructuredLLM:
    def __init__(self, responses: Iterable[Mapping[str, Any]], *, model: str = "fake-model") -> None:
        self.responses = [copy.deepcopy(dict(item)) for item in responses]
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any],
                      schema_name: str) -> StructuredResult:
        self.calls.append({"messages": [dict(m) for m in messages], "schema": copy.deepcopy(dict(schema)),
                           "schema_name": schema_name})
        if len(self.calls) > len(self.responses):
            raise StructuredLLMError("FakeStructuredLLM has no response left", status="error")
        index = len(self.calls) - 1
        return StructuredResult(data=copy.deepcopy(self.responses[index]), model=self.model,
                                response_id=f"fake-{index}", input_tokens=None, output_tokens=None)
