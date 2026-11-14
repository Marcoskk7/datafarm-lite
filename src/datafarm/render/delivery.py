"""渲染交付检查：在 task 层契约检查之上，绑定渲染契约与提交证明。

white-only 交付不经过渲染，委托给 task/white_delivery.py。语义正确性仍需独立审阅。
"""
from __future__ import annotations

import json
from pathlib import Path

from datafarm.kernel.binding import bound_file, require
from datafarm.kernel.io import sha256_file
from datafarm.render.policy import load_contract
from datafarm.task.contract import verify_grounding, verify_input, verify_review_independence, verify_visual_review
from datafarm.task.white_delivery import verify_white_delivery


def verify_delivery(root: Path) -> dict:
    root = root.resolve()
    lock = verify_input(root)
    d = json.loads((root / 'delivery.json').read_text())
    if d.get('schema') == 'datafarm.white-delivery/1':
        return verify_white_delivery(root, lock, d)
    require(lock.get('stop_after', 'render') == 'render', 'rendered delivery not requested in input lock')
    require(d.get('schema') == 'datafarm.agent-delivery/1', 'invalid delivery schema')
    require(d.get('input_sha256') == lock['sha256'], 'delivery input mismatch')
    require(isinstance(d.get('producer_id'), str) and bool(d['producer_id']), 'producer missing')
    count = d.get('target_count')
    require(type(count) is int and count > 0, 'target_count must be positive')
    expected_count = lock.get('target_count')
    require(expected_count is None or count == expected_count, 'requested target count changed')
    episodes = d.get('episodes')
    require(isinstance(episodes, list) and len(episodes) == count, 'target count not delivered')
    ids = [e['id'] for e in episodes]
    require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == count, 'duplicate/invalid episode IDs')
    quantities = verify_grounding(root, d)
    attempts = json.loads(bound_file(root, d['attempts']).read_text())
    require(isinstance(attempts, list) and bool(attempts), 'attempt denominator missing')
    require(len({a['id'] for a in attempts}) == len(attempts), 'duplicate attempts')
    require(all(a.get('outcome') in {'admitted', 'rejected', 'infrastructure_failure'} for a in attempts),
            'invalid attempt outcome')
    require({a['id'] for a in attempts if a['outcome'] == 'admitted'} == set(ids), 'attempt/admission mismatch')
    dataset = json.loads(bound_file(root, d['dataset']).read_text())
    require(dataset.get('schema') == 'datafarm.dataset-files/1', 'invalid dataset manifest')
    require(set(dataset['episode_ids']) == set(ids) and len(dataset['episode_ids']) == count,
            'dataset membership mismatch')
    require(bool(dataset.get('files')), 'dataset has no files')
    for f in dataset['files']:
        bound_file(root, f, external=True)
    independence = set()
    for e in episodes:
        artifacts = e['artifacts']
        expected = {'action', 'source', 'video', 'prompt', 'references'}
        require(set(artifacts) == expected, 'episode lacks source/prompt/reference/action/video bindings')
        for ref in artifacts.values():
            bound_file(root, ref)
        contract_path = bound_file(root, e['render_contract'])
        contract = load_contract(contract_path)
        submission = json.loads(bound_file(root, e['submission']).read_text())
        require(submission.get('schema') == 'datafarm.render-submission/1', 'submission proof missing')
        require(submission.get('contract_sha256') == sha256_file(contract_path)
                and submission.get('intent_sha256') == contract['intent_sha256']
                and submission.get('prompt_sha256') == contract['prompt_sha256'], 'stale submission policy')
        real_refs = contract['intent']['real_references']
        require(submission.get('actual_image_sha256s', [])[:len(real_refs)] == [r['sha256'] for r in real_refs],
                'submission omitted real references')
        require(artifacts['prompt']['sha256'] == contract['prompt_sha256'], 'delivered prompt differs from submission')
        require(json.loads(bound_file(root, artifacts['references']).read_text()) == real_refs,
                'delivered references differ from submission')
        input_hashes = {f['sha256'] for f in lock['files']}
        require(all(r['provenance']['source']['sha256'] in input_hashes for r in real_refs),
                'real reference provenance is not in locked inputs; include additional real photos at prepare')
        review = json.loads(bound_file(root, e['review']).read_text())
        require(review.get('schema') == 'datafarm.task-review/2', 'invalid review schema')
        require(review.get('producer_id') == d['producer_id'], 'review producer mismatch')
        require(review.get('subjects') == {k: v['sha256'] for k, v in artifacts.items()}, 'stale review subjects')
        require(review.get('verdicts') == {k: 'pass' for k in
                ['task', 'motion', 'reference', 'style', 'alignment', 'joint_margin']}, 'review not fully passed')
        independence.add(verify_review_independence(root, review, e['review'], d['producer_id'],
                                                     {v['sha256'] for v in artifacts.values()}))
        require(bool(review.get('evidence')), 'review evidence missing')
        for ref in review['evidence']:
            bound_file(root, ref)
        verify_visual_review(root, review, artifacts, real_refs, contract['intent']['appearance_views'],
                             contract['intent']['appearance_requirements'])
    return {'schema': 'datafarm.delivery-verification/1', 'valid': True, 'episodes': count,
            'attempts': len(attempts), 'delivery_sha256': sha256_file(root / 'delivery.json'),
            'review_independence': 'runtime-verified' if independence == {'runtime-verified'}
            else 'declared-execution-record', 'physical_quantities': quantities,
            'scope': 'content bindings and membership; semantics supplied by independent review'}
