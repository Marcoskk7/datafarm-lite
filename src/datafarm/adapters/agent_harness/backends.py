"""按 ``agent.backend`` 配置选择具体后端；唯一同时导入所有厂商模块的地方。"""

from __future__ import annotations

from typing import Any, Mapping

from datafarm.adapters.agent_harness.claude_code import ClaudeCliBackend
from datafarm.adapters.agent_harness.codex.exec_backend import CodexExecBackend, MeteredCodexBackend
from datafarm.adapters.agent_harness.fake import FakeAgentBackend
from datafarm.adapters.agent_harness.session import AgentBackend
from datafarm.expert.util import ConfigError


def make_agent_backend(agent_cfg: Mapping[str, Any], prices: Mapping[str, Any]) -> AgentBackend:
    kind = agent_cfg.get("backend")
    if kind == "claude":
        return ClaudeCliBackend(agent_cfg, prices)
    if kind == "codex-metered":
        return MeteredCodexBackend(agent_cfg, prices)
    if kind == "codex":
        codex = dict(agent_cfg.get("codex") or {})
        cfg = {**agent_cfg, **{k: v for k, v in codex.items() if k != "model"}}
        model = agent_cfg.get("model")
        if not model or str(model).startswith("claude"):
            model = codex.get("model")  # Claude 的默认值不是 Codex 的 model
        if not model:
            raise ConfigError("codex backend needs an explicit model: set agent.codex.model or pass --model")
        cfg["model"] = model
        return CodexExecBackend(cfg, prices)
    if kind == "fake":
        return FakeAgentBackend(agent_cfg, prices)
    raise ConfigError(f"unknown agent backend {kind!r}")
