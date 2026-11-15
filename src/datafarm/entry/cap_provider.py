"""CaP provider composition root: binds the CaP role contract to the Codex structured-call adapter.

Use as ``--provider-factory datafarm.entry.cap_provider:build_provider``. The main flow uses the
deployment's Codex login; a loopback gateway can only be injected programmatically (local tooling).
"""
from __future__ import annotations

from typing import Any, Mapping

from datafarm.adapters.agent_harness.codex.structured import structured_call, validate_config
from datafarm.cap.codex_provider import CodexProvider


def build_provider(config: Mapping[str, Any], *, gateway: Any = None) -> CodexProvider:
    return CodexProvider(validate_config(config), structured_call, gateway=gateway)
