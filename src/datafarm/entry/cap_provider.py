"""CaP provider composition root: binds the CaP role contract to the Codex structured-call adapter.

Use as ``--provider-factory datafarm.entry.cap_provider:build_provider``. The main flow uses the
deployment's Codex login unless an explicit FYP gateway is configured.
"""
from __future__ import annotations

from typing import Any, Mapping

from datafarm.adapters.agent_harness.codex.structured import structured_call
from datafarm.entry.codex_gateway import bind_gateway
from datafarm.cap.codex_provider import CodexProvider


def build_provider(config: Mapping[str, Any], *, gateway: Any = None) -> CodexProvider:
    config, gateway = bind_gateway(config, gateway)
    return CodexProvider(config, structured_call, gateway=gateway)
