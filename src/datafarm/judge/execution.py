"""只执行无 I/O 的 Python 子集，并对每次判定设置进程资源边界。"""
import ast
from pathlib import Path
import subprocess
import sys

from .protocol import JudgeError, canonical, check, error_result, finite_tree, result, validate_episode, validate_result

SAFE_BUILTINS = ('abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'float', 'int', 'isinstance',
                 'len', 'list', 'max', 'min', 'range', 'reversed', 'round', 'sorted', 'str', 'sum', 'tuple', 'zip',
                 'ValueError', 'KeyError', 'IndexError', 'TypeError', 'ZeroDivisionError')
METHODS = {'get', 'items', 'keys', 'values', 'append', 'extend', 'copy', 'count', 'index'}
MATH = {'sqrt', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2', 'fabs', 'floor', 'ceil',
        'isfinite', 'hypot', 'degrees', 'radians', 'pi', 'inf', 'isclose',
        'fsum', 'ulp', 'nextafter', 'frexp', 'ldexp', 'prod', 'dist', 'copysign',
        'isnan', 'isinf', 'log', 'log2', 'log10', 'log1p', 'exp', 'expm1', 'trunc', 'modf', 'e', 'tau'}
ALLOWED = {ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Import, ast.alias, ast.Return,
           ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.If, ast.For, ast.While,
           ast.Break, ast.Continue, ast.Pass, ast.Try, ast.ExceptHandler, ast.Raise,
           ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
           ast.Subscript, ast.Slice, ast.Attribute, ast.Call, ast.keyword,
           ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
           ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.comprehension,
           ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
           ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt,
           ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot}


def audit_source(source):
    """闭集语法与能力审计：禁止文件、反射、动态执行、外部库和任意属性访问。"""
    check(isinstance(source, str) and 0 < len(source.encode()) <= 64000, 'Invalid source size')
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise JudgeError('Invalid Python syntax') from exc
    for node in tree.body:
        check(isinstance(node, (ast.FunctionDef, ast.Import)) or
              (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)),
              'Only functions, docstrings and import math allowed at top level')
    entries = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'evaluate']
    check(len(entries) == 1 and [a.arg for a in entries[0].args.args] == ['episode', 'task_contract', 'scene_metadata'],
          'Expected evaluate(episode, task_contract, scene_metadata)')
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for node in ast.walk(tree):
        check(type(node) in ALLOWED, 'Forbidden syntax: ' + type(node).__name__)
        if isinstance(node, ast.Name):
            check(not (isinstance(node.ctx, ast.Store) and node.id == 'result'), 'Do not assign the host result helper')
            check(not node.id.startswith('__') and node.id not in {'open', 'eval', 'exec', 'compile', 'getattr', 'setattr',
                  'globals', 'locals', 'vars', 'input', 'breakpoint', 'help', 'type', 'object', 'memoryview'}, 'Forbidden name')
        if isinstance(node, ast.Import):
            check(all(a.name == 'math' and a.asname in (None, 'math') for a in node.names), 'Only import math allowed')
        if isinstance(node, ast.Attribute):
            check(node.attr in METHODS or (isinstance(node.value, ast.Name) and node.value.id == 'math' and node.attr in MATH),
                  'Forbidden attribute: ' + node.attr)
        if isinstance(node, ast.Call):
            check(isinstance(node.func, ast.Attribute) or
                  (isinstance(node.func, ast.Name) and node.func.id in {*SAFE_BUILTINS, *functions, 'result'}),
                  'Forbidden call target')
        if isinstance(node, ast.FunctionDef):
            check(node.name != 'result', 'Use the host-provided result helper; do not redefine result')
            check(not node.name.startswith('__') and not node.decorator_list and node.returns is None,
                  'Decorators and annotations are not supported')
            check(not node.args.defaults and not node.args.kw_defaults and not node.args.kwonlyargs
                  and node.args.vararg is None and node.args.kwarg is None and not node.args.posonlyargs,
                  'Only positional function parameters supported')
        if isinstance(node, ast.arg):
            check(node.annotation is None and not node.arg.startswith('__') and node.arg != 'result', 'Invalid parameter')
    return tree


def run_source(source, episode, contract, scene, *, timeout_s=10, memory_mb=256):
    """返回 JudgeResult；执行错误和缺失数据返回 error，不能冒充普通任务失败。"""
    try:
        audit_source(source)
        finite_tree(episode)
        validate_episode(episode, contract)
        reason = episode['termination']['reason']
        if reason == 'aborted':
            return dict(error_result('aborted'), outcome='aborted')
        if episode.get('schema_version') == 'judge-episode/1' and reason in ('error', 'timeout', 'failed'):
            return error_result('EXECUTION_' + reason.upper())
        payload = canonical(dict(source=source, episode=episode, contract=contract, scene=scene,
                                 timeout_s=timeout_s, memory_mb=memory_mb))
        check(len(payload) <= 32*1024*1024, 'Episode exceeds 32 MiB worker input limit')
        check(type(timeout_s) in (int, float) and 0 < timeout_s <= 60, 'Invalid worker timeout')
        check(type(memory_mb) is int and 64 <= memory_mb <= 1024, 'Invalid worker memory limit')
        proc = subprocess.run([sys.executable, '-I', '-S', '-B', str(Path(__file__).with_name('worker.py'))],
                              input=payload, capture_output=True, timeout=timeout_s+1)
        check(proc.returncode == 0, 'Judge worker exited: ' + str(proc.returncode))
        check(len(proc.stdout) <= 2*1024*1024, 'Judge result exceeds output limit')
        import json
        response = json.loads(proc.stdout)
        check(response.get('ok') is True, 'Judge worker error: ' + str(response.get('error')))
        value = validate_result(response['result'], contract)
        return value
    except (JudgeError, ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
        return error_result(type(exc).__name__ + ': ' + str(exc)[:500])
