"""Claude Code 后端：``claude -p --output-format stream-json --verbose`` 的命令构建与事件解析。"""

from __future__ import annotations

import json
from typing import Any, Mapping

from datafarm.adapters.agent_harness.session import (API_ERROR_PATTERNS, AUTH_ERROR_PATTERNS, AgentBackend,
                                                     ParsedSession, SessionSpec, estimate_cost)


class ClaudeStreamParser:
    """解析 ``claude -p --output-format stream-json --verbose`` 的输出。"""

    def __init__(self, prices: Mapping[str, Any] | None = None) -> None:
        self.prices = dict(prices or {})
        self.p = ParsedSession()
        self._usage_by_message: dict[str, dict[str, Any]] = {}
        self._tool_ids: set[str] = set()

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            self.p.bad_lines += 1
            return
        if not isinstance(event, dict):
            self.p.bad_lines += 1
            return
        self.p.events += 1
        kind = event.get("type")
        if event.get("session_id"):
            self.p.session_id = event["session_id"]
        if kind == "system":
            if event.get("subtype") == "init" and event.get("model"):
                self.p.model = event["model"]
            elif "error" in str(event.get("subtype", "")) or "retry" in str(event.get("subtype", "")):
                self.p.error_texts.append(json.dumps(event)[:500])
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("model"):
                self.p.model = self.p.model or message["model"]
            if isinstance(message.get("usage"), dict):
                self._usage_by_message[str(message.get("id") or len(self._usage_by_message))] = message["usage"]
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._tool_ids.add(str(block.get("id") or len(self._tool_ids)))
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
                    if any(p in text for p in API_ERROR_PATTERNS + AUTH_ERROR_PATTERNS) and len(text) < 2000:
                        self.p.error_texts.append(text)
            if event.get("error"):
                self.p.error_texts.append(str(event.get("error"))[:500])
        elif kind == "result":
            self.p.saw_result = True
            self.p.is_error = bool(event.get("is_error"))
            self.p.subtype = event.get("subtype")
            self.p.result_text = event.get("result") if isinstance(event.get("result"), str) else None
            if isinstance(event.get("num_turns"), int):
                self.p.turns = event["num_turns"]
            usage = event.get("usage") or {}
            if isinstance(usage, dict) and usage:
                self.p.input_tokens = usage.get("input_tokens")
                self.p.output_tokens = usage.get("output_tokens")
                self.p.cache_read_tokens = usage.get("cache_read_input_tokens")
                self.p.cache_write_tokens = usage.get("cache_creation_input_tokens")
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                self.p.cost_usd = float(cost)
                self.p.cost_source = "provider"
            if self.p.is_error and self.p.result_text:
                self.p.error_texts.append(self.p.result_text[:500])

    def finish(self) -> ParsedSession:
        self.p.tool_uses = len(self._tool_ids)
        if self.p.input_tokens is None and self._usage_by_message:
            totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                      "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0}
            for usage in self._usage_by_message.values():
                totals["input_tokens"] += int(usage.get("input_tokens") or 0)
                totals["output_tokens"] += int(usage.get("output_tokens") or 0)
                totals["cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
                creation = usage.get("cache_creation") or {}
                one_hour = int(creation.get("ephemeral_1h_input_tokens") or 0) if isinstance(creation, dict) else 0
                totals["cache_write_1h_tokens"] += one_hour
                totals["cache_write_5m_tokens"] += int(usage.get("cache_creation_input_tokens") or 0) - one_hour
            self.p.input_tokens = totals["input_tokens"]
            self.p.output_tokens = totals["output_tokens"]
            self.p.cache_read_tokens = totals["cache_read_tokens"]
            self.p.cache_write_tokens = totals["cache_write_5m_tokens"] + totals["cache_write_1h_tokens"]
            if self.p.cost_usd is None:
                estimate = estimate_cost(self.p.model, totals, self.prices)
                if estimate is not None:
                    self.p.cost_usd = estimate
                    self.p.cost_source = "price_table"
        elif self.p.cost_usd is None and self.p.input_tokens is not None:
            estimate = estimate_cost(self.p.model, {
                "input_tokens": self.p.input_tokens, "output_tokens": self.p.output_tokens,
                "cache_read_tokens": self.p.cache_read_tokens,
                "cache_write_5m_tokens": self.p.cache_write_tokens}, self.prices)
            if estimate is not None:
                self.p.cost_usd = estimate
                self.p.cost_source = "price_table"
        return self.p


class ClaudeCliBackend(AgentBackend):
    name = "claude"
    call_backend = "claude-cli"

    def command(self, spec: SessionSpec) -> list[str]:
        cfg = self.cfg
        argv = [cfg.get("cli", "claude"), "-p", spec.prompt, "--output-format", "stream-json",
                "--verbose", "--model", self.model]
        if spec.resume_id:
            argv += ["--resume", spec.resume_id]
        elif spec.new_session_id:
            argv += ["--session-id", spec.new_session_id]
        tools = cfg.get("tools", ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite"])
        argv += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
        disallowed = cfg.get("disallowed_tools", ["WebFetch", "WebSearch"])
        if disallowed:
            argv += ["--disallowedTools", ",".join(disallowed)]
        if cfg.get("permission_mode"):
            argv += ["--permission-mode", cfg["permission_mode"]]
        argv += ["--strict-mcp-config", "--setting-sources", cfg.get("setting_sources", "user")]
        if cfg.get("settings_file"):
            argv += ["--settings", cfg["settings_file"]]
        if cfg.get("effort"):
            argv += ["--effort", cfg["effort"]]
        if spec.max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{max(0.01, spec.max_budget_usd):.2f}"]
        return argv + list(cfg.get("extra_args", []))

    def parser(self) -> Any:
        return ClaudeStreamParser(self.prices)

    def env(self) -> dict[str, str]:
        return {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "IS_SANDBOX": "1"}
