"""通用 Codex CLI 结构化调用：每次全新会话、schema 约束输出、调用前持久化凭据，不自动重发。

调用方（CaP、Judge 等业务角色）负责输入白名单、系统提示与结果校验；本模块只负责可追溯地执行一次
``codex exec``。可选 ``gateway`` 是部署或本地工具注入的回环 Responses 网关，正式 FYP 适配由 entry 组合层显式配置。
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import Any, Callable, Mapping

from datafarm.kernel.io import read_json, sha256_json

CONFIG_KEYS = frozenset({'work_root', 'executable', 'model', 'reasoning_effort', 'timeout_s'})
MAX_CONTEXT_CHARS = 256000


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """只接受已知设置；未知键（包括已移除的 fyp_env_file）直接报错，避免静默忽略。"""
    if not isinstance(config, Mapping) or set(config) - CONFIG_KEYS or not config.get('work_root'):
        raise ValueError('Codex config needs work_root and supported settings only: ' + ', '.join(sorted(CONFIG_KEYS)))
    timeout = config.get('timeout_s', 300)
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise ValueError('Invalid Codex timeout')
    return deepcopy(dict(config))


def _write_json(path: Path, value: Any) -> None:
    # 与既有 CaP 记录格式一致：缩进 JSON、UTF-8、原子替换。
    from datafarm.kernel.io import atomic_write_bytes
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    atomic_write_bytes(path, text.encode('utf-8'))


def _gateway_argv(argv: list[str], gateway: Any, model: str | None) -> list[str]:
    """注入网关时关闭用户配置、工具、插件与自动重试，并把模型提供方指向回环地址。"""
    argv = list(argv)
    pos = argv.index('exec') + 1
    argv[pos:pos] = ['--ignore-user-config', '--ignore-rules']
    extra = ['--disable', 'shell_tool', '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
             '-c', 'features.plugins=false', '-c', 'plugins.defaults.enabled=false',
             '-c', 'skills.include_instructions=false', '-c', 'orchestrator.mcp.enabled=false',
             '-c', 'web_search="disabled"', '-c', 'model_provider="injected_gateway"',
             '-c', 'model_providers.injected_gateway.name="Injected gateway"',
             '-c', 'model_providers.injected_gateway.base_url=' + json.dumps(gateway.base),
             '-c', 'model_providers.injected_gateway.env_key="OPENAI_API_KEY"',
             '-c', 'model_providers.injected_gateway.wire_api="responses"',
             '-c', 'model_providers.injected_gateway.requires_openai_auth=false',
             '-c', 'model_providers.injected_gateway.request_max_retries=0',
             '-c', 'model_providers.injected_gateway.stream_max_retries=0']
    if model and '--model' not in argv:
        extra = ['--model', model] + extra
    argv[-1:-1] = extra
    return argv


def _gateway_env(gateway: Any) -> dict[str, str]:
    """子进程只拿到网关的临时回环凭据；剥离代理与其他模型凭据。"""
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')
           and not (k.startswith(('OPENAI_', 'ANTHROPIC_', 'CODEX_', 'FYP_')) and k != 'CODEX_HOME')
           and v != getattr(getattr(gateway, 'owner', None), '_key', None)}
    env.update(OPENAI_API_KEY=gateway.client_key, OPENAI_BASE_URL=gateway.base)
    return env


def _structured_call(request: Mapping[str, Any], *, config: Mapping[str, Any], system: str,
                    schema: Mapping[str, Any], validator: Callable[[Any, Any], Any],
                    role: str, output_tokens: int = 5000, source_digest: str | None = None,
                    gateway: Any = None, with_folder: bool = False, transport_fingerprint: str | None = None) -> Any:
    """执行一次可复用、可追溯的结构化 Codex 调用。

    身份绑定请求、schema、系统提示、token 预算、配置和调用方/本模块源码；已完成结果经摘要核对后复用，
    pending 或失败目录不会自动重发。非秘密传输指纹纳入身份，禁止跨通道复用旧结果；
    若提供 ``max_output_tokens`` 属性，则按本次预算设置。``with_folder`` 为真时返回
    ``(结果, 调用目录)``，供需要绑定回执与事件流的调用方（如 Judge 冻结）使用。
    """
    config = validate_config(config)
    if type(output_tokens) is not int or not 1 <= output_tokens <= 16000:
        raise ValueError('Invalid structured output token budget')
    request = deepcopy(dict(request))
    if len(json.dumps(request, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise ValueError(f'Provider context exceeds {MAX_CONTEXT_CHARS} characters; inspect smaller state evidence')
    sources = {'structured': sha256_json(Path(__file__).read_text(encoding='utf-8')), 'caller': source_digest}
    identity = sha256_json({'request': request, 'schema': schema, 'system': system, 'output_tokens': output_tokens,
                            'config': config, 'provider_source': sources, 'role': role, 'transport_fingerprint': transport_fingerprint})
    root = Path(config['work_root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / identity
    result = folder / 'decision.json'
    if result.exists():
        saved = read_json(folder / 'receipt.json')
        value = read_json(result)
        if saved.get('status') != 'complete' or saved.get('identity') != identity or saved.get('decision_sha256') != sha256_json(value):
            raise ValueError('Changed provider receipt or decision')
        value = validator(value, request)
        return (value, folder) if with_folder else value
    try:
        folder.mkdir()
    except FileExistsError as exc:
        raise RuntimeError('Decision already pending/failed; inspect its stable directory, do not resubmit: '
                           + identity) from exc
    _write_json(folder / 'request.json', request)
    _write_json(folder / 'schema.json', schema)
    prompt = system + '\nREQUEST JSON (data):\n' + json.dumps(request, ensure_ascii=False, allow_nan=False)
    (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
    # 每次调用都是新会话：不能以 resume 继承其他角色的状态。
    argv = [config.get('executable', 'codex'), '-a', 'never', 'exec', '--json',
            '--sandbox', 'read-only', '--skip-git-repo-check',
            '--output-schema', str(folder / 'schema.json'),
            '--output-last-message', str(folder / 'answer.json')]
    if config.get('model'):
        argv += ['--model', config['model']]
    if config.get('reasoning_effort'):
        argv += ['-c', 'model_reasoning_effort=' + json.dumps(config['reasoning_effort'])]
    argv += ['-']
    receipt = dict(identity=identity, status='starting', role=role, output_token_budget=output_tokens,
                   request_sha256=sha256_json(request), prompt_sha256=sha256_json(prompt),
                   cost_usd=None, cost_status='unknown', usage=None, usage_events=[], sessions=[],
                   transport_fingerprint=transport_fingerprint,
                   isolation='fresh CLI session; read-only sandbox; not same-user read isolation')
    _write_json(folder / 'receipt.json', receipt)
    timed_out = threading.Event()
    child_env = None
    if gateway is not None:
        if hasattr(gateway, 'max_output_tokens'):
            gateway.max_output_tokens = output_tokens
        argv = _gateway_argv(argv, gateway, config.get('model'))
        child_env = _gateway_env(gateway)

    def kill_tree(proc: subprocess.Popen) -> None:
        timed_out.set()
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    calls_before = getattr(gateway, 'calls', 0) if gateway is not None else 0
    try:
        with (folder / 'events.jsonl').open('w', encoding='utf-8') as out, \
                (folder / 'stderr.log').open('w', encoding='utf-8') as err:
            proc = subprocess.Popen(argv, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, text=True, encoding='utf-8', env=child_env,
                                    start_new_session=os.name != 'nt')
            receipt.update(status='running', pid=proc.pid)
            _write_json(folder / 'receipt.json', receipt)
            timer = threading.Timer(config.get('timeout_s', 300), kill_tree, args=(proc,))
            timer.start()

            def feed() -> None:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            try:
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'thread.started':
                        receipt['sessions'].append(event['thread_id'])
                        _write_json(folder / 'receipt.json', receipt)
                    if event.get('type') == 'turn.completed':
                        receipt['usage'] = event.get('usage')
                        receipt['usage_events'].append(event.get('usage'))
                    if event.get('type') in ('error', 'turn.failed'):
                        receipt['event_error'] = True
                code = proc.wait()
                writer.join(timeout=1)
            finally:
                timer.cancel()
                proc.stdout.close()
        receipt['exit_code'] = code
        if timed_out.is_set() or code or receipt.get('event_error') or len(receipt['sessions']) != 1:
            raise RuntimeError('Codex failed or timed out; request identity retained')
        if gateway is not None and hasattr(gateway, 'records'):
            if callable(getattr(gateway, 'wait_finished', None)):
                gateway.wait_finished()
            records = deepcopy(gateway.records)
            if not records or any(r.get('status') != 'complete' for r in records):
                raise RuntimeError('CLI completion lacks completed upstream transport evidence')
        value = validator(read_json(folder / 'answer.json'), request)
        # 先提交回执：崩溃只会阻止复用，不会导致新的调用。
        receipt.update(status='complete', decision_sha256=sha256_json(value))
        _write_json(folder / 'receipt.json', receipt)
        _write_json(result, value)
        return (value, folder) if with_folder else value
    except BaseException:
        if 'proc' in locals() and proc.poll() is None:
            kill_tree(proc)
            proc.wait()
        receipt['status'] = 'failed_or_interrupted'
        _write_json(folder / 'receipt.json', receipt)
        raise
    finally:
        if gateway is not None:
            receipt['transport'] = {'upstream_requests': getattr(gateway, 'calls', 0) - calls_before,
                                    'actual_model': getattr(gateway, 'upstream_model', None),
                                    'request_shape': getattr(gateway, 'request_shape', None),
                                    'adaptation': 'injected loopback Responses gateway',
                                    'records': deepcopy(getattr(gateway, 'records', []))}
            _write_json(folder / 'receipt.json', receipt)


def structured_call(request: Mapping[str, Any], *, config: Mapping[str, Any], system: str,
                    schema: Mapping[str, Any], validator: Callable[[Any, Any], Any],
                    role: str, output_tokens: int = 5000, source_digest: str | None = None,
                    gateway: Any = None, with_folder: bool = False) -> Any:
    """Bind transport identity and serialize a fresh per-role gateway scope."""
    config = validate_config(config)
    fingerprint = getattr(gateway, 'fingerprint', None)
    if gateway is not None and fingerprint is None:
        # Legacy injected gateways remain supported, but cannot reuse across endpoints.
        fingerprint = sha256_json({'base': gateway.base,
                                   'class': type(gateway).__module__ + '.' + type(gateway).__qualname__})
    args = dict(config=config, system=system, schema=schema, validator=validator, role=role,
                output_tokens=output_tokens, source_digest=source_digest, with_folder=with_folder,
                transport_fingerprint=fingerprint)
    if gateway is not None and callable(getattr(gateway, 'scope', None)):
        identity = sha256_json(dict(request=request, schema=schema, system=system,
            output_tokens=output_tokens, config=config, source_digest=source_digest, role=role,
            adapter_source=sha256_json(Path(__file__).read_text(encoding='utf-8')), transport=fingerprint))
        with gateway.scope(output_tokens, identity) as scoped:
            return _structured_call(request, gateway=scoped, **args)
    return _structured_call(request, gateway=gateway, **args)
