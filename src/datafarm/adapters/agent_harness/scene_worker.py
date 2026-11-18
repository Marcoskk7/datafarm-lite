"""Standalone narrow expert process; no scene, store, Judge or runtime objects."""
import ast
import json
import math
from pathlib import Path
import sys


def execute(source, class_name=None):
    tree = ast.parse(source)
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    if class_name is None and 'run' not in functions or class_name is not None and class_name not in classes:
        raise ValueError('run(actions) or named expert class required')
    forbidden = (ast.ImportFrom, ast.With, ast.AsyncWith, ast.Lambda, ast.Global, ast.Nonlocal)
    allowed_calls = {'abs','all','any','bool','dict','enumerate','float','int','isinstance','len','list','max','min','range','reversed','round','sorted','str','sum','tuple','zip','ValueError', *functions, *classes}
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ValueError('unsupported expert syntax')
        if isinstance(node, ast.Name) and (node.id.startswith('_') or node.id in {'open','eval','exec','compile','getattr','setattr','globals','locals','vars','input','type','object'}):
            raise ValueError('unsupported expert access')
        if isinstance(node, ast.Import) and any(n.name != 'math' or n.asname not in (None,'math') for n in node.names):
            raise ValueError('only math import allowed')
        if isinstance(node, ast.Attribute) and (node.attr.startswith('_') or node.attr not in {'observe','move_tcp','move_gripper','get','items','keys','values','append','extend','copy','count','index','sqrt','sin','cos','acos','atan2','hypot','isfinite','isclose','pi'}):
            raise ValueError('unsupported expert attribute')
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Attribute) or isinstance(node.func, ast.Name) and node.func.id in allowed_calls):
            raise ValueError('unsupported expert call')
        if isinstance(node, ast.ClassDef) and (node.bases or node.keywords or node.decorator_list):
            raise ValueError('unsupported expert class')
        if isinstance(node, ast.arg) and node.annotation is not None:
            raise ValueError('expert annotations unsupported')
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.args.defaults or node.args.kw_defaults or node.returns or node.args.vararg or node.args.kwarg):
            raise ValueError('unsupported function declaration')
    if any(not isinstance(n,(ast.FunctionDef,ast.ClassDef,ast.Import)) and not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)) for n in tree.body):
        raise ValueError('only functions and math import allowed at top level')
    import builtins
    scope = {'__builtins__': {k:getattr(builtins,k) for k in allowed_calls if hasattr(builtins,k)}, 'math':math, '__name__':'scene_expert'}
    scope['__builtins__']['__build_class__'] = builtins.__build_class__
    def import_math(name, *args, **kwargs):
        if name != 'math':
            raise ValueError('only math import allowed')
        return math
    scope['__builtins__']['__import__'] = import_math
    exec(compile(tree,'<scene-expert>','exec'),scope)
    class Actions:
        def request(self, method, argument):
            sys.stdout.write(json.dumps({'method':method,'argument':argument},allow_nan=False)+'\n');sys.stdout.flush()
            response = json.loads(sys.stdin.readline(1048577))
            if set(response) != {'result'}:
                raise ValueError('host rejected request')
            return response['result']
        def observe(self):return self.request('observe', None)
        def move_tcp(self, argument):return self.request('move_tcp', argument)
        def move_gripper(self, argument):return self.request('move_gripper', argument)
    scope['run'](Actions()) if class_name is None else scope[class_name]().run(Actions())
    sys.stdout.write('{"method":"complete","argument":null}\n');sys.stdout.flush()


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        execute(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[2] if len(sys.argv)>2 else None)
    except Exception:
        sys.stdout.write('{"method":"error","argument":null}\n');sys.stdout.flush();sys.exit(2)
