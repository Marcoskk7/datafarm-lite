"""把 task session 绑定到 source/experience 版本，并记录可见的反馈。"""
from __future__ import annotations

from datafarm.common import sha256_file
import json
from pathlib import Path

from datafarm.evolution.events import record, select, scope
from datafarm.evolution.store import Store, read, write
from datafarm.evolution.versions import initialize, resolve, snapshot, version


def bind(work, root, *, store_path=None, selectors=None):
    work = Path(work).resolve()
    explicit = store_path is not None
    store = Store(store_path or (Path(root) / '.datafarm/evolution'))
    from datafarm.evolution.store import digest
    selectors = scope({'run_id': work.name + '-' + digest(str(work))[:12], **(selectors or {})})
    # 普通的 source 开发使用用户所调用的 source。托管部署（显式指定）则使用当前
    # 已评估的生效版本。
    selected = (initialize(store, root) if explicit else version(store, snapshot(store, root)))
    binding = {'schema': 'datafarm.run-capability/1', 'store': str(store.root),
               'version': selected['id'], 'scope': selectors, 'managed': explicit}
    binding['experience'] = select(store, selectors, version=selected['id'])
    write(work / 'capability.json', binding)
    return binding


def binding(work):
    path = Path(work) / 'capability.json'
    return read(path) if path.exists() else None


def source_for(work, fallback):
    value = binding(work)
    return resolve(Store(value['store']), value['version']) if value else Path(fallback)


def context_for(work):
    value = binding(work)
    if not value:
        return None
    # 在已有 run 进行期间，必须遵循新的显式需求。已学到的
    # capability/lesson 版本保持钉住不变。每个 session 把实际选中的
    # 需求及其哈希保存在 context receipt 中。
    return select(Store(value['store']), value['scope'], version=value['version'])


def capture(work, *, kind, statement, ref, details=None):
    value = binding(work)
    if not value:
        return None
    evidence = ({'path': ref, 'sha256': sha256_file(Path(ref))}
                if Path(ref).is_file() else None)
    return record(Store(value['store']), {'kind': kind, 'statement': statement,
        'source': {'actor': 'pipeline-driver', 'ref': ref}, 'scope': value['scope'],
        'version': value['version'], 'details': details or {}, 'evidence': evidence})


def feedback(work, event):
    value = binding(work)
    if not value:
        raise ValueError('this work has no capability binding; bind it explicitly before importing feedback')
    return record(Store(value['store']), {'scope': value['scope'], 'version': value['version'], **event})


def experience_text(value):
    return ('\nREPOSITORY EXPERIENCE (scoped; lessons are advisory, not new authority):\n'
            + json.dumps(value, ensure_ascii=False)) if value else ''
