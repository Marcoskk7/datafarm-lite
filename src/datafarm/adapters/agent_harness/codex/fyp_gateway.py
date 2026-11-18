"""Bounded, server-local Responses transport for an explicitly configured FYP endpoint.

Personal credentials stay in this host process. CLI children receive an ephemeral
loopback capability, never the upstream token. No SDK, ambient proxy or retry is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import secrets
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from datafarm.kernel.file_lock import exclusive_lock
from datafarm.kernel.io import atomic_write_bytes, read_json, sha256_json

MAX_BODY = 4 * 1024 * 1024
MAX_STREAM = 16 * 1024 * 1024
SEMANTIC_FIELDS = frozenset({
    'model', 'input', 'instructions', 'stream', 'reasoning', 'text', 'tools',
    'tool_choice', 'parallel_tool_calls', 'temperature', 'top_p', 'include',
    'truncation', 'max_output_tokens',
})
# Codex sends these transport/cache extensions; none contains tool semantics.
TRANSPORT_FIELDS = frozenset({'store', 'prompt_cache_key', 'prompt_cache_retention', 'client_metadata'})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError('Gateway endpoint must be an explicit HTTPS URL')
    u = urlsplit(value)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password or
            u.query or u.fragment or u.path.rstrip('/') != '/v1' or
            any(c.isspace() for c in value)):
        raise ValueError('Gateway endpoint needs HTTPS, /v1 and no credentials/query/fragment')
    return value.rstrip('/')


def proxy_endpoint(value):
    if not value:
        return None
    u = urlsplit(value)
    if (u.scheme != 'http' or u.hostname not in ('127.0.0.1', '::1', 'localhost') or
            not u.port or u.username or u.password or u.query or u.fragment or
            u.path not in ('', '/') or any(c.isspace() for c in value)):
        raise ValueError('Userspace proxy must be an explicit server-loopback HTTP URL')
    return value.rstrip('/')


def normalize_request(raw, *, model, output_tokens, allow_tools):
    if not isinstance(raw, dict) or set(raw) - SEMANTIC_FIELDS - TRANSPORT_FIELDS:
        raise ValueError('Unsupported Responses fields; semantic data is never silently dropped')
    if raw.get('model') != model or raw.get('stream') is not True:
        raise ValueError('Responses needs the configured model and streaming')
    if not isinstance(raw.get('input'), (str, list)):
        raise ValueError('Responses needs inline input, not server-side history')
    tools = raw.get('tools', [])
    if not isinstance(tools, list):
        raise ValueError('Invalid tool definitions')
    if not allow_tools and isinstance(raw['input'], list) and any(
            isinstance(i, dict) and i.get('type') in ('function_call', 'custom_tool_call',
                'function_call_output', 'custom_tool_call_output') for i in raw['input']):
        raise ValueError('Tool calls/results are forbidden for structured roles')
    if any(not isinstance(t, dict) or t.get('type') not in ('function', 'custom') for t in tools):
        raise ValueError('Only client-executed function/custom tools are supported')
    value = {k: v for k, v in raw.items() if k in SEMANTIC_FIELDS}
    if not allow_tools:
        value['tool_choice'] = 'none'
    if 'max_output_tokens' in value:
        n = value['max_output_tokens']
        if type(n) is not int or not 0 < n <= output_tokens:
            raise ValueError('Requested output exceeds the per-call allowance')
    value.update(store=False, max_output_tokens=min(value.get('max_output_tokens', output_tokens), output_tokens))
    # Strict finite JSON and bounded input: no credential/body logging here.
    json.dumps(value, allow_nan=False)
    return value, sorted(set(raw) & TRANSPORT_FIELDS)


class RequestBudget:
    """Persist reservations BEFORE sending; failures consume their allowance, never retry."""

    def __init__(self, root, *, fingerprint, max_requests, max_output_tokens_total, deadline_s):
        for value, upper in ((max_requests,100),(max_output_tokens_total,1600000),(deadline_s,3600)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError('Invalid persistent gateway allowance')
        self.root = Path(root)
        self.fingerprint = fingerprint
        self.limits = dict(max_requests=max_requests, max_output_tokens_total=max_output_tokens_total,
                           deadline_s=deadline_s)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'budget.json'
        self.lock = self.root / 'budget.lock'
        with self.lock.open('a+b') as handle, exclusive_lock(handle):
            if self.path.exists():
                self._read()
            else:
                self._save(dict(fingerprint=fingerprint, limits=self.limits,
                                started_at=time.time(), reservations=[]))

    def _read(self):
        state = read_json(self.path)
        if state.get('fingerprint') != self.fingerprint or state.get('limits') != self.limits:
            raise ValueError('Gateway budget binding changed; use a new version, not an old budget')
        return state

    def _save(self, state):
        atomic_write_bytes(self.path, (json.dumps(state, allow_nan=False, indent=2) + '\n').encode())

    def reserve(self, identity, tokens, request_sha256):
        if (not isinstance(identity,str) or not identity or not isinstance(request_sha256,str) or not request_sha256
                or type(tokens) is not int or not 1 <= tokens <= 16000):
            raise ValueError('Invalid request reservation')
        with self.lock.open('a+b') as handle, exclusive_lock(handle):
            state = self._read()
            rows = state['reservations']
            if any(r['call_identity'] == identity and r.get('request_sha256') == request_sha256 for r in rows):
                raise RuntimeError('Previously sent request cannot be resubmitted automatically')
            if any(r['status'] == 'pending' for r in rows):
                raise RuntimeError('An upstream request remains incomplete; inspect before any further spend')
            if (time.time() >= state['started_at'] + self.limits['deadline_s'] or
                    len(rows) >= self.limits['max_requests'] or
                    sum(r['output_token_allowance'] for r in rows) + tokens > self.limits['max_output_tokens_total']):
                raise RuntimeError('FYP request/time/output allowance exhausted')
            rid = len(rows) + 1
            rows.append(dict(id=rid, call_identity=identity, request_sha256=request_sha256, output_token_allowance=tokens,
                             status='pending', started_at=time.time()))
            self._save(state)
            return rid, state['started_at'] + self.limits['deadline_s']

    def finish(self, rid, record):
        with self.lock.open('a+b') as handle, exclusive_lock(handle):
            state = self._read()
            state['reservations'][rid - 1].update(record, ended_at=time.time())
            self._save(state)


class FypGateway:
    """Serial scope factory. Each CLI role gets its own listener and temporary credential."""

    def __init__(self, *, base_url, api_key, model, budget_root, proxy_url=None,
                 max_requests=20, max_output_tokens_total=120000, deadline_s=2400,
                 timeout_s=180, allow_tools=False, config_binding=None):
        self.url = endpoint(base_url)
        self.proxy = proxy_endpoint(proxy_url)
        if not isinstance(api_key, str) or not api_key.startswith('fyp_') or any(c.isspace() for c in api_key):
            raise ValueError('Invalid FYP deployment credential')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('An explicit Responses model is required')
        for n, upper in ((max_requests, 100), (max_output_tokens_total, 1600000), (deadline_s, 3600)):
            if type(n) is not int or not 1 <= n <= upper:
                raise ValueError('Invalid gateway allowance')
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 600 or type(allow_tools) is not bool:
            raise ValueError('Invalid gateway timeout/tools policy')
        self._key, self.model, self.timeout_s, self.allow_tools = api_key, model, timeout_s, allow_tools
        self.fingerprint = sha256_json(dict(protocol='fyp-responses/1', endpoint_sha256=sha256_json(self.url),
            proxy_sha256=sha256_json(self.proxy), model=model, timeout_s=timeout_s, allow_tools=allow_tools,
            source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), config=config_binding,
            max_requests=max_requests, max_output_tokens_total=max_output_tokens_total, deadline_s=deadline_s))
        self.budget = RequestBudget(budget_root, fingerprint=self.fingerprint, max_requests=max_requests,
            max_output_tokens_total=max_output_tokens_total, deadline_s=deadline_s)
        self._scope_lock = threading.Lock()

    @contextmanager
    def scope(self, output_tokens, identity):
        if type(output_tokens) is not int or not 1 <= output_tokens <= 16000:
            raise ValueError('Invalid scope output allowance')
        if not self._scope_lock.acquire(blocking=False):
            raise RuntimeError('Parallel CLI scopes are not permitted on this gateway')
        transport = None
        try:
            transport = LoopbackTransport(self, output_tokens, identity)
            yield transport
        finally:
            try:
                if transport is not None:
                    transport.close()
            finally:
                self._scope_lock.release()


class LoopbackTransport:
    def __init__(self, owner, output_tokens, identity):
        self.owner, self.max_output_tokens, self.identity = owner, output_tokens, identity
        self.client_key = secrets.token_hex(32)
        self.calls, self.upstream_model, self.request_shape = 0, None, None
        self.records = []
        self._request_lock = threading.Lock()
        self._closing = threading.Event()
        self._response = None
        transport = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(owner.timeout_s)

            def error(self, status, code):
                body = json.dumps({'error': {'type': 'gateway_error', 'code': code,
                                            'message': code}}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_GET(self):
                self.error(405, 'model_discovery_requires_explicit_host_request')

            def do_POST(self):
                if self.path != '/v1/responses' or not hmac.compare_digest(
                        self.headers.get('Authorization', ''), 'Bearer ' + transport.client_key):
                    self.error(403, 'invalid_loopback_capability')
                    return
                if not transport._request_lock.acquire(blocking=False):
                    self.error(409, 'concurrent_request_forbidden')
                    return
                sent, rid, record = False, None, {}
                try:
                    if transport._closing.is_set():
                        self.error(503, 'scope_closed')
                        return
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
                        self.error(400, 'invalid_body_length')
                        return
                    size = int(lengths[0])
                    if not 0 < size <= MAX_BODY:
                        self.error(413, 'request_body_limit')
                        return
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError('Incomplete request body')
                    def unique(pairs):
                        d = {}
                        for k, v in pairs:
                            if k in d:
                                raise ValueError('Duplicate JSON key')
                            d[k] = v
                        return d
                    raw = json.loads(body, object_pairs_hook=unique)
                    value, removed = normalize_request(raw, model=owner.model,
                        output_tokens=output_tokens, allow_tools=owner.allow_tools)
                    transport.request_shape = dict(keys=sorted(raw), model=value['model'],
                        tools=[{'type': t.get('type'), 'name': t.get('name')} for t in value.get('tools', [])],
                        input_types=[i.get('type') for i in value.get('input', []) if isinstance(i, dict)],
                        removed_transport_fields=removed, tool_policy='client_tools' if owner.allow_tools else 'none')
                    payload = json.dumps(value, allow_nan=False).encode()
                    rid, run_deadline = owner.budget.reserve(transport.identity, output_tokens, hashlib.sha256(payload).hexdigest())
                    deadline = min(run_deadline, time.time()+owner.timeout_s)
                    record = dict(request_id=rid, request_sha256=hashlib.sha256(payload).hexdigest(),
                        shape=transport.request_shape, status='pending', actual_model=None, usage=None)
                    transport.calls += 1
                    transport.records.append(record)
                    proxy = {'https': owner.proxy} if owner.proxy else {}
                    opener = build_opener(ProxyHandler(proxy), NoRedirect(),
                                          HTTPSHandler(context=ssl.create_default_context()))
                    request = Request(owner.url + '/responses', data=payload, headers={
                        'Authorization': 'Bearer ' + owner._key, 'Content-Type': 'application/json',
                        'Accept': 'text/event-stream'}, method='POST')
                    with opener.open(request, timeout=min(owner.timeout_s, max(.1, deadline-time.time()))) as response:
                        transport._response = response
                        if response.headers.get_content_type() != 'text/event-stream':
                            raise ValueError('Streaming Responses required')
                        self.send_response(response.status)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Transfer-Encoding', 'chunked')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        sent = True
                        total, pending, complete = 0, b'', False
                        while not transport._closing.is_set():
                            if time.time() >= deadline:
                                raise TimeoutError('Run deadline')
                            # Bound the next read by the remaining wall allowance, not
                            # the original socket inactivity timeout. HTTPResponse uses
                            # this socket; doubles without it retain their own deadline.
                            socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                            if socket is not None:
                                socket.settimeout(max(.001, deadline-time.time()))
                            chunk = response.read1(8192)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_STREAM:
                                raise ValueError('Stream size limit')
                            pending += chunk
                            if len(pending) > MAX_BODY:
                                raise ValueError('SSE line limit')
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                if line.startswith(b'data:') and line[5:].strip() != b'[DONE]':
                                    event = json.loads(line[5:])
                                    item = event.get('response') or {}
                                    if not owner.allow_tools:
                                        outputs = item.get('output', []) + ([event['item']] if isinstance(event.get('item'), dict) else [])
                                        if any(o.get('type') in ('function_call', 'custom_tool_call') for o in outputs):
                                            raise ValueError('Tool call returned to a no-tools role')
                                    if item.get('model') and item['model'] != owner.model:
                                        raise ValueError('Upstream returned a different model')
                                    if item.get('model'):
                                        transport.upstream_model = item['model']
                                        record['actual_model'] = item['model']
                                    if event.get('type') == 'response.completed':
                                        if complete or item.get('status') not in (None,'completed'):
                                            raise ValueError('Invalid completion event')
                                        usage = item.get('usage')
                                        if usage is not None:
                                            count = usage.get('output_tokens') if isinstance(usage,dict) else None
                                            if type(count) is not int or not 0 <= count <= value['max_output_tokens']:
                                                raise ValueError('Invalid or exceeded output usage')
                                        complete = True
                                        record['usage'] = usage
                                    if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                                        raise ValueError('Upstream failed/incomplete event')
                            self.wfile.write(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                            self.wfile.flush()
                        if pending.strip():
                            raise ValueError('Unterminated SSE data')
                        if time.time() >= deadline:
                            raise TimeoutError('Run deadline')
                        if not complete or not record['actual_model'] or transport._closing.is_set():
                            raise ConnectionError('Incomplete upstream stream or model evidence')
                        record.update(status='complete', response_bytes=total)
                        self.wfile.write(b'0\r\n\r\n')
                        self.wfile.flush()
                        self.close_connection = True
                except HTTPError as exc:
                    record.update(status='rejected', http_status=exc.code)
                    if not sent:
                        self.error(502, 'upstream_http_' + str(exc.code))
                    else:
                        self.close_connection = True
                except (ValueError, TypeError, KeyError) as exc:
                    record.update(status='failed', error_type=type(exc).__name__)
                    if not sent:
                        self.error(400 if rid is None else 502, 'invalid_request_or_response')
                    else:
                        self.close_connection = True
                except Exception as exc:
                    record.update(status='incomplete', error_type=type(exc).__name__)
                    if not sent:
                        self.error(503, 'request_failed_no_retry')
                    else:
                        self.close_connection = True
                finally:
                    self.close_connection = True
                    transport._response = None
                    try:
                        if rid is not None:
                            # Unknown outcomes remain pending, blocking future spend.
                            saved = dict(record)
                            if saved.get('status') == 'incomplete':
                                saved['status'] = 'pending'
                            try:
                                owner.budget.finish(rid, saved)
                            except Exception:
                                record['status'] = 'receipt_error'
                                raise
                    finally:
                        transport._request_lock.release()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def wait_finished(self):
        """Wait for receipt persistence before a CLI result can be accepted."""
        if not self._request_lock.acquire(timeout=self.owner.timeout_s + 1):
            raise RuntimeError('Gateway receipt remains pending')
        self._request_lock.release()

    def close(self):
        self._closing.set()
        if self._response is not None:
            self._response.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        # Wait for request receipt finalization, not just the listener thread.
        if not self._request_lock.acquire(timeout=self.owner.timeout_s + 1):
            raise RuntimeError('Gateway request still running; inspect pending budget')
        self._request_lock.release()
