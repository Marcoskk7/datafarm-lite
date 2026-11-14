"""与任务无关的交付检查。语义正确性仍需独立审阅。"""
from __future__ import annotations

from datafarm.agent.runtime import validate_completion
from datafarm.common import sha256_file, sha256_text
from datafarm.core.file_binding import ContractError, bound_file, require
from datafarm.core.grounding import GroundingError, load_grounding
from datafarm.render.policy import load_contract
import json
from pathlib import Path
import re


APPEARANCE_COVERAGE = {
    'reference_baseline',
    'robot_body_and_attached_hardware',
    'task_objects',
    'table_and_background',
    'declared_view_photography',
    'overall_photographic_realism',
}


RUNTIME_MARKERS = ('reviews', 'author.json', 'control.json')


def runtime_managed(root: Path) -> bool:
    """由 pipeline_agent drive/supervise 驱动的 workspace 带有宿主审阅记录。"""
    return any((root / '.runtime' / name).exists() for name in RUNTIME_MARKERS)


def verify_review_independence(root: Path, review: dict, review_ref: dict, producer: str,
                               artifact_sha256s: set) -> str:
    """证明某次 episode review 来自一次独立的执行。

    在 agent runtime 下，该 review 必须引用一次已完成的 runtime review
    （`runtime_review`）：其宿主写入的 completion 会被重新校验（verifier 角色、
    context、transcript 与身份哈希，且不得复用 author session），其判定必须为 pass，
    其冻结的 request 必须覆盖每一个交付产物，其 report 证据必须绑定本次 episode review
    文件。在此处，手写的执行记录会被拒绝。在 runtime 之外，
    执行记录会被接受，并报告为 declared 而非 verified。
    """
    reviewer = review.get('reviewer_id')
    require(isinstance(reviewer, str) and bool(reviewer) and reviewer != producer, 'self review refused')
    review_id = review.get('runtime_review')
    if review_id is None:
        require(not runtime_managed(root),
                'runtime-managed delivery must cite the runtime review that produced it')
        execution = json.loads(bound_file(root, review['execution']).read_text())
        require(execution.get('role') == 'reviewer' and execution.get('actor_id') == reviewer
                and execution.get('producer_id') == producer, 'independent execution binding missing')
        bound_file(root, execution['transcript'])
        return 'declared-execution-record'
    require(isinstance(review_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', review_id) is not None,
            'invalid runtime review ID')
    folder = root / '.runtime' / 'reviews' / review_id
    require((folder / 'completion.json').is_file(), 'cited runtime review has not completed')
    completion = validate_completion(root, folder)
    require(completion.get('verdict') == 'pass', 'cited runtime review did not pass')
    request = json.loads((folder / 'request.json').read_text())
    covered = {item.get('sha256') for item in request.get('subjects', [])}
    require(artifact_sha256s <= covered, 'runtime review did not cover every delivered artifact')
    report = json.loads((folder / 'report.json').read_text())
    bound = {(item.get('path'), item.get('sha256')) for item in report.get('evidence', [])}
    require((review_ref.get('path'), review_ref.get('sha256')) in bound,
            'runtime review report does not bind this episode review')
    return 'runtime-verified'


def verify_grounding(root: Path, delivery: dict) -> dict:
    """交付绑定 G；每个物理量都携带其标签（core/grounding.py）。"""
    require(isinstance(delivery.get('grounding'), dict), 'delivery must bind the grounding record')
    try:
        return load_grounding(bound_file(root, delivery['grounding']),
                              bind=lambda ref: bound_file(root, ref))
    except GroundingError as exc:
        raise ContractError(f'grounding record invalid: {exc}') from exc


def verify_input(root: Path) -> dict:
    lock = json.loads((root / 'input.json').read_text())
    for ref in lock['files']:
        bound_file(root, ref, external=True)
    expected = sha256_text(json.dumps(lock['files'], sort_keys=True))
    require(lock.get('sha256') == expected, 'input lock digest mismatch')
    return lock


def _is_visual_evidence(path: Path) -> bool:
    """解码并校验一张对比图；仅凭文件名或 magic header 不足以证明。"""

    try:
        from PIL import Image
        with Image.open(path) as image:
            supported = image.format in {'PNG', 'JPEG', 'WEBP'} and image.width > 0 and image.height > 0
            image.load()
        return supported
    except (ImportError, OSError, SyntaxError, ValueError):
        return False


def verify_visual_review(root: Path, review: dict, artifacts: dict, real_refs: list[dict],
                         expected_views: list[str], appearance_requirements: dict) -> None:
    visual = review.get('visual_review')
    require(isinstance(visual, dict), 'actual visual comparison record missing')
    require(visual.get('inspection_method') == 'decoded-real-reference-and-generated-video',
            'review must inspect decoded real references and generated video')
    require(visual.get('generated_video_sha256') == artifacts['video']['sha256'],
            'visual review is not bound to the generated video')
    expected_refs = [item['sha256'] for item in real_refs]
    require(visual.get('real_reference_sha256s') == expected_refs,
            'visual review is not bound to every real reference')

    coverage = visual.get('coverage')
    require(isinstance(coverage, dict) and set(coverage) == APPEARANCE_COVERAGE,
            'visual review lacks required full-scene appearance coverage')
    general_evidence = {(item.get('path'), item.get('sha256')) for item in review.get('evidence', [])
                        if isinstance(item, dict)}
    for name, item in coverage.items():
        require(isinstance(item, dict) and item.get('verdict') in {'pass', 'not_applicable'},
                f'visual appearance coverage did not pass: {name}')
        if item.get('verdict') == 'not_applicable':
            require(isinstance(item.get('rationale'), str) and item['rationale'].strip(),
                    f'not-applicable appearance coverage lacks rationale: {name}')
            optional_requirement = {'task_objects': 'task_objects'}.get(name)
            require(optional_requirement is not None
                    and appearance_requirements[optional_requirement]['required'] is False,
                    f'required appearance coverage cannot be not-applicable: {name}')
        else:
            require(not item.get('rationale'), f'passing appearance coverage must not use N/A rationale: {name}')
        if name in {'reference_baseline', 'declared_view_photography', 'overall_photographic_realism'}:
            require(item.get('verdict') == 'pass', f'core scene appearance cannot be not-applicable: {name}')
        require(item.get('comparison_basis') == 'real-reference-vs-generated',
                f'visual coverage lacks a real-reference comparison: {name}')
        require(item.get('scope') in {'full-view', 'local-detail'}, f'invalid visual evidence scope: {name}')
        if name in {'reference_baseline', 'table_and_background', 'declared_view_photography',
                    'overall_photographic_realism'}:
            require(item.get('scope') == 'full-view',
                    f'local detail cannot establish full-scene appearance: {name}')
        views = item.get('views')
        require(isinstance(views, list) and bool(views) and set(views) <= set(expected_views),
                f'invalid/missing reviewed views: {name}')
        if name == 'declared_view_photography':
            require(set(views) == set(expected_views), 'declared-view photographic review is incomplete')
        frames = item.get('generated_frames')
        require(isinstance(frames, list) and bool(frames)
                and all(type(frame) is int and frame >= 0 for frame in frames),
                f'generated frame evidence missing: {name}')
        compared_refs = item.get('real_reference_sha256s')
        require(isinstance(compared_refs, list) and bool(compared_refs)
                and set(compared_refs) <= set(expected_refs), f'real reference evidence missing: {name}')
        if name == 'reference_baseline':
            require(compared_refs == expected_refs, 'baseline review did not inspect every real reference')
        evidence = item.get('evidence')
        require(isinstance(evidence, list) and bool(evidence), f'visual comparison image missing: {name}')
        for evidence_ref in evidence:
            path = bound_file(root, evidence_ref)
            require((evidence_ref.get('path'), evidence_ref.get('sha256')) in general_evidence,
                    f'visual comparison is absent from review evidence: {name}')
            require(_is_visual_evidence(path), f'text-only/non-image evidence cannot prove visual comparison: {name}')


def verify_delivery(root: Path) -> dict:
    root = root.resolve()
    lock = verify_input(root)
    d = json.loads((root / 'delivery.json').read_text())
    if d.get('schema') == 'datafarm.white-delivery/1':
        from datafarm.task.white_delivery import verify_white_delivery
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
