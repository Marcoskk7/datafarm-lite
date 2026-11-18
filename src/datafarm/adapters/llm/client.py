"""用官方 `openai` SDK 实现 `StructuredLLM`：一次 chat completion，`response_format` 约束 JSON 输出。

不用 strict 模式：OpenAI 的 strict json_schema 要求每个对象的所有属性都列入 `required`，可选字段只能写成
`anyOf [..., null]`，而 1a 的草稿 schema 正是靠“不写就用默认值”工作的。所以发 `strict: false` 的
json_schema（或对只支持 JSON mode 的兼容服务发 `json_object` 并把 schema 放进 system 消息），
输出是否合格一律由 `validate_request` 决定。

SDK 自带重试关闭（`max_retries=0`）：每次尝试都要在 `datafarm.agent-call/1` 账本里留一行，记录先于返回或抛错写入。
凭据只从配置指定的环境变量读取，不进日志、异常、argv 或账本。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import openai

from datafarm.adapters.agent_harness.call_log import AgentCallRecord, append_call, new_call_id
from datafarm.kernel.io import sha256_json, sha256_text, utc_now
from datafarm.request.ports import StructuredLLMError, StructuredResult

BACKEND = "openai-chat"


def _unfence(text: str) -> str:
    """去掉模型包在 JSON 外面的单个 Markdown 代码块（```json ... ```）。

    `json_object` 只校验“是 JSON”这个响应格式，兼容服务仍可能把对象包进代码块。那只是包装，
    不是内容错误，剥掉后再交给 `json.loads`；其它任何不合法的输出照旧判 `not_json`。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


CONFIG_KEYS = frozenset({"provider", "model", "base_url", "api_key_env", "timeout_s", "response_format"})
RESPONSE_FORMATS = ("json_schema", "json_object")


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """只接受已知键；`api_key_env` 是环境变量名，不是 key 本身。"""
    if not isinstance(config, Mapping):
        raise ValueError("LLM config must be a JSON object")
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown LLM config keys {sorted(unknown)}; supported: {sorted(CONFIG_KEYS)}")
    if config.get("provider", "openai") != "openai":
        raise ValueError("LLM config provider must be 'openai' (any OpenAI-compatible endpoint via base_url)")
    if not isinstance(config.get("model"), str) or not config["model"]:
        raise ValueError("LLM config needs a model")
    env = config.get("api_key_env", "OPENAI_API_KEY")
    if not isinstance(env, str) or not env.replace("_", "").isalnum():
        raise ValueError("api_key_env must be an environment variable name")
    base_url = config.get("base_url")
    if base_url is not None and not (isinstance(base_url, str) and base_url.startswith(("http://", "https://"))):
        raise ValueError("base_url must be an http(s) URL or null")
    timeout = config.get("timeout_s", 120)
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise ValueError("timeout_s must be in (0, 3600]")
    response_format = config.get("response_format", "json_schema")
    if response_format not in RESPONSE_FORMATS:
        raise ValueError(f"response_format must be one of {list(RESPONSE_FORMATS)}")
    return {"provider": "openai", "model": config["model"], "base_url": base_url, "api_key_env": env,
            "timeout_s": float(timeout), "response_format": response_format}


class OpenAIStructuredLLM:
    def __init__(self, config: Mapping[str, Any], *, call_log: str | Path, component: str = "request-draft",
                 run_id: str | None = None) -> None:
        self.config = validate_config(config)
        self.model = self.config["model"]
        self.call_log = Path(call_log)
        self.component = component
        self.run_id = run_id
        key = os.environ.get(self.config["api_key_env"])
        if not key:
            raise StructuredLLMError(f"environment variable {self.config['api_key_env']} is not set", status="error")
        self._client = openai.OpenAI(api_key=key, base_url=self.config["base_url"],
                                     timeout=self.config["timeout_s"], max_retries=0)

    def _body(self, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any],
              schema_name: str) -> dict[str, Any]:
        messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        if self.config["response_format"] == "json_schema":
            response_format = {"type": "json_schema",
                               "json_schema": {"name": schema_name, "schema": dict(schema), "strict": False}}
        else:
            response_format = {"type": "json_object"}
            messages.insert(0, {"role": "system", "content": "Reply with one JSON object matching this JSON Schema:\n"
                                + json.dumps(schema, ensure_ascii=False)})
        return {"model": self.model, "messages": messages, "response_format": response_format}

    def complete_json(self, *, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any],
                      schema_name: str) -> StructuredResult:
        body = self._body(messages, schema, schema_name)
        started_at, start = utc_now(), time.monotonic()
        response = None
        record: dict[str, Any] = {"status": "ok"}
        result: StructuredResult | None = None
        try:
            response = self._client.chat.completions.create(**body)
            result = self._parse(response, record)
        except openai.APITimeoutError:
            record.update(status="timeout", error_type="APITimeoutError")
        except openai.APIStatusError as exc:
            record.update(status="error", error_type=f"http_{exc.status_code}")
        except openai.APIError as exc:
            record.update(status="error", error_type=type(exc).__name__)
        usage = getattr(response, "usage", None)
        call = AgentCallRecord(
            call_id=new_call_id(), component=self.component, backend=BACKEND,
            model=getattr(response, "model", None) or self.model, started_at=started_at, ended_at=utc_now(),
            status=record["status"], request_sha256=sha256_json(body), prompt_sha256=sha256_json(body["messages"]),
            response_sha256=record.get("response_sha256"), response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "prompt_tokens", None), output_tokens=getattr(usage, "completion_tokens", None),
            turns=1 if response is not None else None, latency_s=round(time.monotonic() - start, 3),
            error_type=record.get("error_type"), run_id=self.run_id, attempt=1,
            extra={"base_url": self.config["base_url"], "response_format": self.config["response_format"],
                   "schema_name": schema_name, "finish_reason": record.get("finish_reason")})
        append_call(self.call_log, call)
        if result is None:
            raise StructuredLLMError(f"{BACKEND} call {call.call_id} failed: {call.status} ({call.error_type})",
                                     status=call.status)
        return result

    def _parse(self, response: Any, record: dict[str, Any]) -> StructuredResult | None:
        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        record["finish_reason"] = getattr(choice, "finish_reason", None)
        if not isinstance(content, str):
            record.update(status="invalid_response", error_type="no_content")
            return None
        try:
            data = json.loads(_unfence(content))
        except ValueError:
            record.update(status="invalid_response", error_type="not_json", response_sha256=sha256_text(content))
            return None
        record["response_sha256"] = sha256_json(data)
        if record["finish_reason"] == "length":
            record.update(status="invalid_response", error_type="truncated")
            return None
        if not isinstance(data, dict):
            record.update(status="invalid_response", error_type="not_object")
            return None
        usage = response.usage
        return StructuredResult(data=data, model=response.model or self.model, response_id=response.id,
                                input_tokens=getattr(usage, "prompt_tokens", None),
                                output_tokens=getattr(usage, "completion_tokens", None))
