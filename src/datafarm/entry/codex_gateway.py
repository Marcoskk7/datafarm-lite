"""Compose explicit deployment FYP settings with Codex roles; never persist secrets."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re

from datafarm.adapters.agent_harness.codex.fyp_gateway import FypGateway
from datafarm.adapters.agent_harness.codex.structured import validate_config

GATEWAY_KEYS = frozenset({'base_url_env', 'api_key_env', 'proxy_url_env', 'max_requests',
    'max_output_tokens_total', 'deadline_s', 'timeout_s'})


def bind_gateway(config, gateway=None):
    config = deepcopy(dict(config))
    settings = config.pop('gateway', None)
    config = validate_config(config)
    if settings is None:
        return config, gateway
    if gateway is not None:
        raise ValueError('Do not mix configured and injected gateways')
    if not isinstance(settings, dict) or set(settings) - GATEWAY_KEYS:
        raise ValueError('Unsupported gateway setting')
    def environment(field, required):
        name = settings.get(field)
        if name is None and not required:
            return None
        if not isinstance(name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', name):
            raise ValueError('Gateway requires explicit environment variable names')
        value = os.environ.get(name)
        if not value:
            raise ValueError('Missing explicit gateway deployment environment variable')
        return value
    bound = FypGateway(base_url=environment('base_url_env', True),
        api_key=environment('api_key_env', True), proxy_url=environment('proxy_url_env', False),
        model=config.get('model'), budget_root=Path(config['work_root']) / 'gateway-budget',
        max_requests=settings.get('max_requests', 20),
        max_output_tokens_total=settings.get('max_output_tokens_total', 120000),
        deadline_s=settings.get('deadline_s', 2400), timeout_s=settings.get('timeout_s', 180),
        config_binding={'settings': settings,
                        'composition_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return config, bound
