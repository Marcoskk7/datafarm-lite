"""Standalone narrow expert process; no scene, store, Judge or runtime objects.

The same ``check_source`` rules run on the host before launch (with a readable
reason) and inside the isolated guest; the guest stays stdlib-only so it can run
under ``python -I -S`` in PRoot/bwrap without importing ``datafarm``.
"""
import ast
import json
import math
from pathlib import Path
import sys

# Rules shared by the host pre-check and the guest; keep stdlib-only.
SAFE_BUILTINS = ('abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'float', 'int', 'isinstance', 'len', 'list',
                 'max', 'min', 'range', 'reversed', 'round', 'sorted', 'str', 'sum', 'tuple', 'zip', 'set',
                 'frozenset', 'map', 'filter', 'divmod', 'pow', 'print',
                 'ValueError', 'RuntimeError', 'KeyError', 'IndexError', 'TypeError', 'ZeroDivisionError',
                 'ArithmeticError', 'Exception')
FORBIDDEN_NAMES = frozenset({'open', 'eval', 'exec', 'compile', 'getattr', 'setattr', 'delattr', 'hasattr', 'globals',
                             'locals', 'vars', 'input', 'type', 'object', 'super', 'breakpoint', 'help', 'memoryview',
                             'exit', 'quit', 'dir', 'id', 'classmethod', 'staticmethod', 'property'})
FORBIDDEN_SYNTAX = (ast.ImportFrom, ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.Await, ast.Yield,
                    ast.YieldFrom, ast.Global, ast.Nonlocal, ast.Delete)
# Non-underscore attributes that still reach frames/globals or format-string introspection.
FORBIDDEN_ATTRIBUTES = frozenset({'format', 'format_map', 'mro', 'gi_frame', 'gi_code', 'cr_frame', 'cr_code',
                                  'ag_frame', 'ag_code', 'tb_frame', 'tb_next', 'f_back', 'f_globals', 'f_locals',
                                  'f_builtins', 'f_code'})
if hasattr(ast, 'Match'):
    FORBIDDEN_SYNTAX += (ast.Match,)


class ExpertPolicyError(ValueError):
    """A rule violation with a short, portable reason (no source text or traceback)."""


def _fail(node, reason):
    raise ExpertPolicyError('line %s: %s' % (getattr(node, 'lineno', 0), reason))


def check_source(source, class_name=None):
    """Return the parsed tree or raise ExpertPolicyError with a short reason.

    Allowed: plain functions/classes (no bases or decorators), ``import math``,
    ``math.<public name>``, any non-underscore attribute (``self.x``, ``dict.get``,
    observation fields), lambdas, comprehensions, try/except, constant defaults.
    Forbidden: other imports, dunder/underscore names and attributes, reflection and
    I/O builtins, ``with``, async/generators, global/nonlocal and calls of names that
    are neither safe builtins nor module-level definitions.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ExpertPolicyError('line %s: syntax error' % (exc.lineno or 0)) from None
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    if class_name is None and 'run' not in functions:
        raise ExpertPolicyError('module-level run(actions) required')
    if class_name is not None and class_name not in classes:
        raise ExpertPolicyError('expert class %r not defined at module top level' % class_name)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.Assign)) and not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            _fail(node, 'only functions, classes, constants and import math allowed at top level')
    constants = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    local_defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    lambdas = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda)
               for t in n.targets if isinstance(t, ast.Name)}
    callable_names = set(SAFE_BUILTINS) | functions | classes | local_defs | lambdas
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
        if isinstance(node, ast.ClassDef) and (node.bases or node.keywords or node.decorator_list):
            _fail(node, 'expert classes may not have bases or decorators')
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.args.vararg or node.args.kwarg:
                _fail(node, 'decorators and *args/**kwargs are not supported')
            if node.name.startswith('_') and node.name != '__init__':
                _fail(node, 'forbidden function name: ' + node.name)
            if any(not isinstance(d, ast.Constant) for d in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]):
                _fail(node, 'default arguments must be constants')
    return tree


def execute(source, class_name=None):
    tree = check_source(source, class_name)
    import builtins
    names = [k for k in SAFE_BUILTINS if hasattr(builtins, k) and k != 'print']
    scope = {'__builtins__': {k: getattr(builtins, k) for k in names}, 'math': math, '__name__': 'scene_expert'}
    # stdout is the IPC channel; expert print() goes to stderr (discarded by the host).
    scope['__builtins__']['print'] = lambda *a, **k: None
    scope['__builtins__']['__build_class__'] = builtins.__build_class__

    def import_math(name, *args, **kwargs):
        if name != 'math':
            raise ValueError('only math import allowed')
        return math
    scope['__builtins__']['__import__'] = import_math
    exec(compile(tree, '<scene-expert>', 'exec'), scope)

    class Actions:
        def request(self, method, argument):
            sys.stdout.write(json.dumps({'method': method, 'argument': argument}, allow_nan=False) + '\n')
            sys.stdout.flush()
            response = json.loads(sys.stdin.readline(1048577))
            if set(response) != {'result'}:
                raise ValueError('host rejected request')
            return response['result']

        def observe(self):
            return self.request('observe', None)

        def move_tcp(self, argument):
            return self.request('move_tcp', argument)

        def move_gripper(self, argument):
            return self.request('move_gripper', argument)
    scope['run'](Actions()) if class_name is None else scope[class_name]().run(Actions())
    sys.stdout.write('{"method":"complete","argument":null}\n')
    sys.stdout.flush()


def error_reason(exc):
    """Short portable reason; expert code faults keep only the exception type."""
    if isinstance(exc, ExpertPolicyError):
        return 'policy: ' + str(exc)[:200]
    return 'exception: ' + type(exc).__name__


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        execute(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[2] if len(sys.argv) > 2 else None)
    except Exception as exc:
        sys.stdout.write(json.dumps({'method': 'error', 'argument': error_reason(exc)}) + '\n')
        sys.stdout.flush()
        sys.exit(2)
