"""Standalone worker for the CaP author self-check (stdlib-only; run as ``python -I -S``).

The self-check is the author's own success criterion, written together with the
first program: a module-level ``check(episode)`` that reads the public execution
evidence (trace, privileged state, execution status) and returns ``bool`` or
``{"success": bool, "score": float in [0, 1], "reason": str}``. It is NOT the
frozen Judge and never sees Judge source, hidden seeds or holdout evidence.

The same ``check_source`` rules run on the host before launch and inside this
worker. stdin: ``{"source": str, "episodes": [...]}``; stdout: one JSON line
``{"results": [{"success", "score", "reason"}, ...]}``.
"""
import ast
import json
import math
import sys

SAFE_BUILTINS = ('abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'float', 'int', 'isinstance', 'len', 'list',
                 'max', 'min', 'range', 'reversed', 'round', 'sorted', 'str', 'sum', 'tuple', 'zip', 'set',
                 'frozenset', 'map', 'filter', 'divmod', 'pow',
                 'ValueError', 'KeyError', 'IndexError', 'TypeError', 'ZeroDivisionError', 'Exception')
FORBIDDEN_NAMES = frozenset({'open', 'eval', 'exec', 'compile', 'getattr', 'setattr', 'delattr', 'hasattr', 'globals',
                             'locals', 'vars', 'input', 'type', 'object', 'super', 'breakpoint', 'help', 'memoryview',
                             'exit', 'quit', 'dir', 'id', 'classmethod', 'staticmethod', 'property', 'print'})
FORBIDDEN_SYNTAX = (ast.ImportFrom, ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.Await, ast.Yield,
                    ast.YieldFrom, ast.Global, ast.Nonlocal, ast.Delete, ast.ClassDef)
FORBIDDEN_ATTRIBUTES = frozenset({'format', 'format_map', 'mro', 'gi_frame', 'gi_code', 'cr_frame', 'cr_code',
                                  'ag_frame', 'ag_code', 'tb_frame', 'tb_next', 'f_back', 'f_globals', 'f_locals',
                                  'f_builtins', 'f_code'})
if hasattr(ast, 'Match'):
    FORBIDDEN_SYNTAX += (ast.Match,)
MAX_SOURCE = 12000
MAX_REASON = 500


class SelfCheckPolicyError(ValueError):
    """A rule violation with a short reason (no source text or traceback)."""


def _fail(node, reason):
    raise SelfCheckPolicyError('line %s: %s' % (getattr(node, 'lineno', 0), reason))


def check_source(source):
    """Return the parsed tree or raise SelfCheckPolicyError."""
    if not isinstance(source, str) or not 1 <= len(source) <= MAX_SOURCE:
        raise SelfCheckPolicyError('self-check source must be 1..%d characters' % MAX_SOURCE)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SelfCheckPolicyError('line %s: syntax error' % (exc.lineno or 0)) from None
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if 'check' not in functions:
        raise SelfCheckPolicyError('module-level check(episode) required')
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Import, ast.Assign)) and not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            _fail(node, 'only functions, constants and import math allowed at top level')
    local_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    lambdas = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda)
               for t in n.targets if isinstance(t, ast.Name)}
    callable_names = set(SAFE_BUILTINS) | local_defs | lambdas
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_SYNTAX):
            _fail(node, 'unsupported syntax: ' + type(node).__name__)
        if isinstance(node, ast.Import) and any(a.name != 'math' or a.asname not in (None, 'math') for a in node.names):
            _fail(node, 'only "import math" is allowed')
        if isinstance(node, ast.Name) and (node.id.startswith('_') or node.id in FORBIDDEN_NAMES):
            _fail(node, 'forbidden name: ' + node.id)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith('_') or node.attr in FORBIDDEN_ATTRIBUTES:
                _fail(node, 'forbidden attribute: ' + node.attr)
            if isinstance(node.value, ast.Name) and node.value.id == 'math' and not hasattr(math, node.attr):
                _fail(node, 'unknown math attribute: ' + node.attr)
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Attribute) or
                                               isinstance(node.func, ast.Name) and node.func.id in callable_names):
            _fail(node, 'unsupported call target: ' + (node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__))
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.args.vararg or node.args.kwarg:
                _fail(node, 'decorators and *args/**kwargs are not supported')
            if any(not isinstance(d, ast.Constant) for d in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]):
                _fail(node, 'default arguments must be constants')
    return tree


def normalize(value):
    """Map a check return value onto {success, score, reason}; raise ValueError if malformed."""
    if type(value) is bool:
        return {'success': value, 'score': float(value), 'reason': ''}
    if not isinstance(value, dict) or type(value.get('success')) is not bool or set(value) - {'success', 'score', 'reason'}:
        raise ValueError('check must return bool or {success: bool, score?, reason?}')
    score = value.get('score', float(value['success']))
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError('check score must be finite and in [0, 1]')
    reason = value.get('reason', '')
    if not isinstance(reason, str):
        raise ValueError('check reason must be a string')
    return {'success': value['success'], 'score': float(score), 'reason': reason[:MAX_REASON]}


def load(source):
    tree = check_source(source)
    import builtins
    scope = {'__builtins__': {k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)},
             'math': math, '__name__': 'cap_self_check'}

    def import_math(name, *args, **kwargs):
        if name != 'math':
            raise ValueError('only math import allowed')
        return math
    scope['__builtins__']['__import__'] = import_math
    exec(compile(tree, '<cap-self-check>', 'exec'), scope)
    return scope['check']


def evaluate(source, episodes):
    check = load(source)
    results = []
    for episode in episodes:
        try:
            results.append(normalize(check(json.loads(json.dumps(episode)))))
        except Exception as exc:  # The author's check is fallible data; its fault fails that episode.
            results.append({'success': False, 'score': 0.0,
                            'reason': 'self_check_error: ' + type(exc).__name__ + ': ' + str(exc)[:200]})
    return results


def main():
    request = json.loads(sys.stdin.read())
    try:
        out = {'results': evaluate(request['source'], request['episodes'])}
    except SelfCheckPolicyError as exc:
        out = {'policy_error': str(exc)[:300]}
    sys.stdout.write(json.dumps(out, allow_nan=False) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
