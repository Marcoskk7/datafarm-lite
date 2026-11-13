"""当前任务的渲染意图、prompt 展开与实际 payload 绑定。

不发起任何网络调用。provenance 与质量的语义真值需要独立审查。
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from string import Template

from datafarm.common import sha256_bytes, sha256_file, sha256_text
from datafarm.core.file_binding import bound_file, require

REFERENCE_AREAS = {
    'robot_body',
    'attached_cameras_and_cables',
    'task_objects',
    'table_and_background',
    'photographic_character',
}


def _decode_image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.load()
            require(image.format in {'PNG', 'JPEG', 'WEBP'}, 'real reference has unsupported image format')
            return image.size
    except (ImportError, OSError, SyntaxError, ValueError) as error:
        raise ValueError(f'real reference is not a decodable image: {path}') from error


def json_sha(value):
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


# 获得授权的外观轴绝不会扩展到机器人、物体的数量或身份、
# 相机布局或运动。这是对轴名进行的词法层面失败关闭防护（fail closed）；
# 渲染审查者仍会对照它检查渲染内容。
PROTECTED_OOD_AXIS = re.compile(
    r'\b(robots?|arms?|grippers?|end[- ]effectors?|cameras?|viewpoints?|camera layout|view layout|'
    r'object count|number of objects|objects? added|add(ed|ing)? objects?|remov(e|ed|ing) objects?|'
    r'extra objects?|distractors?|motion|trajector(y|ies)|timing|actions?|poses?)\b'
    # 轴名必须是英文；任何 CJK 轴名一律失败关闭，而不是绕过英文列表。
    r'|[\u3400-\u9fff]', re.IGNORECASE)


def validate_intent(intent: dict, root: Path) -> None:
    require(intent.get('schema') == 'datafarm.render-intent/1', 'invalid render intent schema')
    require(intent.get('mode') in {'sim2real', 'ood'}, 'render mode unresolved: ask for current user intent')
    require(all(isinstance(intent.get(k), str) and intent[k].strip() for k in ['run_id', 'task_id', 'real_baseline']),
            'run/task/baseline missing')
    confirm = intent.get('mode_confirmation', {})
    require(all(confirm.get(k) == intent[k] for k in ['run_id', 'task_id', 'mode'])
            and bool(confirm.get('quote')) and bool(confirm.get('source')), 'mode confirmation is not scoped to this task/run')
    axes = intent.get('authorized_ood_axes', [])
    require(isinstance(axes, list), 'OOD axes must be a list')
    require(bool(axes) if intent['mode'] == 'ood' else not axes, 'mode and OOD axes disagree')
    require(confirm.get('authorized_ood_axes', []) == axes, 'OOD axes not authorized by this confirmation')
    for axis in axes:
        name = axis.get('axis') if isinstance(axis, dict) else axis
        require(isinstance(name, str) and name.strip(), 'each OOD axis needs a nonempty name')
        protected = PROTECTED_OOD_AXIS.search(json.dumps(axis, ensure_ascii=False))
        require(protected is None, f'OOD axis {axis!r} touches the robot, the objects, the cameras '
                f'or the motion ({protected.group(0) if protected else ""}); appearance axes only')
    views = intent.get('appearance_views')
    require(isinstance(views, list) and bool(views)
            and all(isinstance(view, str) and view.strip() for view in views)
            and len(set(views)) == len(views), 'appearance views must be distinct nonempty names')
    requirements = intent.get('appearance_requirements')
    require(isinstance(requirements, dict) and set(requirements) == REFERENCE_AREAS,
            'appearance requirements must classify every real-scene area')
    for area, requirement in requirements.items():
        require(isinstance(requirement, dict) and type(requirement.get('required')) is bool
                and isinstance(requirement.get('description'), str) and requirement['description'].strip(),
                f'invalid appearance requirement: {area}')
    require(all(requirements[area]['required'] for area in
                ('robot_body', 'table_and_background', 'photographic_character')),
            'robot, workspace and photographic character are mandatory real-scene requirements')
    refs = intent.get('real_references')
    require(isinstance(refs, list) and bool(refs), 'real-world image references required')
    for ref in refs:
        reference_path = bound_file(root, ref, external=True)
        width, height = _decode_image_size(reference_path)
        origin = ref.get('provenance', {})
        require(origin.get('kind') in {'real-episode-frame', 'real-photo'}, 'structural/continuity image is not real-world evidence')
        bound_file(root, origin['source'], external=True)
        if origin['kind'] == 'real-episode-frame':
            require(type(origin.get('frame')) is int and origin['frame'] >= 0, 'real frame index missing')
        require(bool(origin.get('description')), 'reference origin description missing')
        require(ref.get('scope') in {'full-view', 'local-detail'}, 'real reference scope missing')
        ref_views = ref.get('views')
        require(isinstance(ref_views, list) and bool(ref_views) and set(ref_views) <= set(views),
                'real reference has invalid/missing view coverage')
        regions = ref.get('view_regions')
        require(isinstance(regions, dict) and set(regions) == set(ref_views),
                'real reference must bind an image region to every claimed view')
        for view, region in regions.items():
            require(isinstance(region, list) and len(region) == 4
                    and all(type(value) is int for value in region),
                    f'invalid real-reference region for view: {view}')
            x, y, w, h = region
            require(x >= 0 and y >= 0 and w > 0 and h > 0
                    and x + w <= width and y + h <= height,
                    f'real-reference region falls outside image for view: {view}')
        coverage = ref.get('coverage')
        require(isinstance(coverage, list) and bool(coverage)
                and set(coverage) <= REFERENCE_AREAS | {'full_scene'},
                'real reference has invalid/missing appearance coverage')
    for view in views:
        require(any(ref['scope'] == 'full-view' and view in ref['views']
                    and 'full_scene' in ref['coverage'] for ref in refs),
                f'no full-scene real reference for declared view: {view}')
    supplied = {area for ref in refs for area in ref['coverage']}
    require(all(area in supplied for area, requirement in requirements.items() if requirement['required']),
            'real references do not cover every required real-scene appearance area')
    require(bool(intent.get('appearance_invariants')), 'appearance invariants required')


VARIABLES = {'TASK_DESCRIPTION', 'SOURCE_VIDEO_ROLE', 'CAMERA_TIMING_CONTRACT',
             'TASK_EVENT_CONTRACT', 'CONTINUITY_CONTRACT'}
# 这是词法层面的兜底，而不是声称能理解任意自相矛盾的表述。
CONFLICT = re.compile(r'(?:ignore|omit|remove|disable|without|no)\s+(?:all\s+)?(?:real[- ]world\s+|real\s+|appearance\s+)?references|'
                      r'source[- ]only|structure[- ]reference[- ]only|(?:do\s+not|don\'t)\s+(?:use|provide|reference)\s+(?:the\s+)?real\b|'
                      r'cancel\s+(?:the\s+)?real[- ]references?', re.IGNORECASE)


def compile_prompt(root: Path, intent: dict, overlay: dict, *, assets: Path) -> dict:
    validate_intent(intent, root)
    require(set(overlay) == VARIABLES, 'task overlay has missing/unknown slots; mode/reference policy cannot be overridden')
    require(all(isinstance(v, str) and v.strip() for v in overlay.values()), 'empty task prompt slot')
    require(not any(CONFLICT.search(v) for v in overlay.values()), 'task overlay contradicts real-reference policy')
    variables = {**overlay, 'MODE': intent['mode'],
        'REAL_REFERENCE_MAP': json.dumps([{'image': n + 1, 'sha256': r['sha256'],
               'origin': r['provenance']['description'], 'scope': r['scope'],
               'views': r['views'], 'view_regions': r['view_regions'], 'coverage': r['coverage']}
              for n, r in enumerate(intent['real_references'])]),
        'APPEARANCE_VIEWS': json.dumps(intent['appearance_views'], ensure_ascii=False),
        'REAL_BASELINE_DESCRIPTION': intent['real_baseline'],
        'AUTHORIZED_OOD_AXES': json.dumps(intent['authorized_ood_axes'], ensure_ascii=False),
        'APPEARANCE_INVARIANTS': json.dumps(intent['appearance_invariants'], ensure_ascii=False)}
    parts, templates = [], []
    for name in ['common-v1.txt', f"{intent['mode']}-v1.txt"]:
        p = assets / 'prompts/render' / name
        parts.append(Template(p.read_text()).substitute(variables))
        templates.append({'path': 'prompts/render/' + name, 'sha256': sha256_file(p)})
    text = '\n'.join(parts)
    require('${' not in text and '{{' not in text, 'unexpanded prompt slot')
    return {'schema': 'datafarm.render-contract/1', 'intent': intent, 'intent_root': str(root.resolve()),
            'overlay': overlay, 'prompt': text, 'prompt_sha256': sha256_text(text),
            'templates': templates, 'intent_sha256': json_sha(intent)}


def load_contract(path: Path) -> dict:
    c = json.loads(path.read_text())
    require(c.get('schema') == 'datafarm.render-contract/1', 'invalid compiled render contract')
    validate_intent(c['intent'], Path(c['intent_root']))
    require(json_sha(c['intent']) == c.get('intent_sha256'), 'render intent changed')
    require(sha256_text(c['prompt']) == c.get('prompt_sha256'), 'render prompt changed')
    # 用当前 core 模板重新编译，因此修改被冻结的 prompt 或 core 都会使其失效。
    from datafarm.agent.context import repository_root
    expected = compile_prompt(Path(c['intent_root']), c['intent'], c['overlay'], assets=repository_root())
    require(c == expected, 'compiled render contract differs from current templates/intent')
    override = os.environ.get('DATAFARM_RENDER_MODE')
    require(not override or override == c['intent']['mode'], 'environment mode conflicts with confirmed intent')
    return c


def check_seedance_payload(body: dict, contract_path: Path) -> dict:
    c = load_contract(contract_path)
    content = body.get('content', [])
    texts = [x.get('text', '') for x in content if x.get('type') == 'text']
    require(texts == [c['prompt']], 'actual provider prompt differs from compiled prompt')
    images = [x.get('image_url', {}).get('url', '') for x in content if x.get('type') == 'image_url']
    hashes = []
    for url in images:
        require(isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url,
                'image payload must carry verifiable inline bytes; opaque URLs require a new verified transport adapter')
        try:
            raw = base64.b64decode(url.split(';base64,', 1)[1], validate=True)
        except ValueError as e:
            raise ValueError('invalid inline reference image') from e
        hashes.append(sha256_bytes(raw))
    wanted = [r['sha256'] for r in c['intent']['real_references']]
    require(hashes[:len(wanted)] == wanted, 'actual payload omits/reorders/replaces required real images')
    require(any(x.get('type') == 'video_url' and x.get('video_url', {}).get('url') for x in content),
            'source video absent from actual request')
    return {'schema': 'datafarm.render-submission/1', 'contract_sha256': sha256_file(contract_path),
            'intent_sha256': c['intent_sha256'], 'prompt_sha256': c['prompt_sha256'],
            'actual_image_sha256s': hashes, 'payload_sha256': json_sha(body),
            'provider': 'seedance', 'model': body.get('model')}


H3_LOWERINGS = frozenset({'h3-caption-chunkvis-noevents', 'h3-caption-chunkvis-notask'})
_VIEW_NAMES = {'head': 'head camera', 'left_wrist': 'left wrist camera', 'right_wrist': 'right wrist camera'}


def lower_h3_prompt(contract: dict, lowering: str, *, assets: Path, chunk_facts: dict | None = None) -> str:
    """对已校验渲染 contract 的确定性 H3 provider 降级转换。

    H3 以 caption 风格的呈现方式进行条件控制（先是 `<Picture i>`/`<Video k>` 标签，随后是文本），因此该
    降级会把已确认的 contract 复述为正向描述：white 载体（Video 1）拥有运动、
    时序与任务状态；真实图像拥有外观；mode/OOD 轴与任务语义逐字来自
    已确认的 intent 与 overlay。它不引入新的权限，也无法丢弃真实引用，因为
    gate 仍会绑定实际图像并根据 contract 重新计算该文本。
    """
    require(lowering in H3_LOWERINGS, f'unknown H3 prompt lowering: {lowering}')
    intent, overlay = contract['intent'], contract['overlay']
    views = intent['appearance_views']
    if len(views) == 1:
        layout = f"One camera, the {_VIEW_NAMES.get(views[0], views[0].replace('_', ' '))}, fills the whole frame"
    else:
        names = [_VIEW_NAMES.get(v, v.replace('_', ' ')) for v in views]
        layout = ('Synchronized cameras tiled in one frame: the head camera view fills the top, the left wrist camera '
                  'view is at the bottom left and the right wrist camera view is at the bottom right'
                  if views == ['head', 'left_wrist', 'right_wrist']
                  else 'Synchronized cameras tiled in one frame: ' + ', '.join(names))
    n = len(intent['real_references'])
    pictures = 'Picture 1 is a' if n == 1 else ('Picture 1 and Picture 2 are' if n == 2 else f'Pictures 1 to {n} are')
    if intent['mode'] == 'sim2real':
        mode = ('The robot, the task objects, the table, the background and the lighting look exactly like in the '
                'pictures.')
    else:
        mode = ('The robot, the task objects, the cameras and the background look exactly like in the pictures; only '
                'these authorized appearance axes are changed: ' + '; '.join(map(str, intent['authorized_ood_axes'])) + '.')
    variables = {'LAYOUT': layout, 'PICTURES': pictures, 'MODE_SENTENCE': mode,
                 'REAL_BASELINE_DESCRIPTION': intent['real_baseline'].rstrip('. '),
                 'TASK_DESCRIPTION': overlay['TASK_DESCRIPTION'].rstrip('. '),
                 'TASK_EVENT_CONTRACT': overlay['TASK_EVENT_CONTRACT'].rstrip('. ')}
    # '-noevents' 降级会丢弃 overlay 的 TASK_EVENT_CONTRACT（为整段视频类 provider 编写的物体清单与
    # 终态数量措辞）；改由载体与 chunk 事实承载每个分段的状态
    # '-notask' 降级还会丢弃整段 episode 的 TASK_DESCRIPTION：在某个 chunk 中它会陈述未来的任务状态（物体
    # 已经放置而目标仍为空），生成器会提前画出这些状态；此时任务进展仅
    # 来自载体（Video 1）以及测得的 chunk 事实
    template = {'-noevents': 'h3-caption-noevents-v1.txt', '-notask': 'h3-caption-notask-v1.txt'}[lowering[lowering.rfind('-'):]]
    text = Template((assets / 'prompts/render' / template).read_text()).substitute(variables)
    require('${' not in text, 'unexpanded H3 lowering slot')
    text = text.strip()
    if chunk_facts is not None:
        text = text + ' ' + chunk_visibility_sentences(chunk_facts, views)
    return text


_SIDES = ('left', 'right')
_WRISTS = {'left_wrist': 'left', 'right_wrist': 'right'}


_COUNT_WORDS = ('no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')


def chunk_visibility_sentences(facts: dict, views: list) -> str:
    """从 white 载体（权威来源）测得的、固定措辞的 chunk 局部可见性陈述。

    `facts` 是封闭 schema——{"head_absent_arms": [...sides], "empty_wrists": [...wrist views],
    "head_max_coloured_objects": int 0..9, "wrist_large_objects": [...wrist views]}——因此任何自由文本都无法进入
    provider prompt；packager 会从该 chunk 的载体片段中测量它。物体数量弥合了
    prompt 中任务物体清单与载体在某分段中所显示内容之间的差距（一个嵌套堆叠、一个被收进
    容器中的物体），否则生成器会用臆造的物体填补这一差距。"""
    keys = {'head_absent_arms', 'empty_wrists', 'head_max_coloured_objects', 'wrist_large_objects'}
    require(isinstance(facts, dict) and set(facts) <= keys, 'invalid chunk facts')
    absent = facts.get('head_absent_arms', [])
    empty = facts.get('empty_wrists', [])
    require(isinstance(absent, list) and set(absent) <= set(_SIDES) and len(set(absent)) == len(absent),
            'invalid head_absent_arms')
    require(isinstance(empty, list) and set(empty) <= set(_WRISTS) and len(set(empty)) == len(empty)
            and set(empty) <= set(views), 'invalid empty_wrists')
    large = facts.get('wrist_large_objects', [])
    require(isinstance(large, list) and set(large) <= set(_WRISTS) and len(set(large)) == len(large)
            and set(large) <= set(views) and not set(large) & set(empty), 'invalid wrist_large_objects')
    out = []
    if 'head' in views:
        if set(absent) == set(_SIDES):
            out.append('In this segment no robot arm is visible in the head camera view.')
        elif absent:
            gone = absent[0]
            other = 'right' if gone == 'left' else 'left'
            out.append(f'In this segment the head camera view shows only the {other} robot arm; the {gone} arm is out '
                       f'of view.')
    if 'head' in views and 'head_max_coloured_objects' in facts:
        k = facts['head_max_coloured_objects']
        require(isinstance(k, int) and not isinstance(k, bool) and 0 <= k < len(_COUNT_WORDS),
                'invalid head_max_coloured_objects')
        if k == 0:
            out.append('In this segment no coloured task object is visible in the head camera view; draw none there.')
        else:
            out.append(f'In this segment the head camera view shows at most {_COUNT_WORDS[k]} separate coloured task '
                       f'object{"s" if k > 1 else ""} at any moment (objects nested or stacked together count as one); '
                       f'draw no other coloured object there.')
    for w in ('left_wrist', 'right_wrist'):
        if w in empty:
            out.append(f'In this segment the {_WRISTS[w]} wrist camera view shows only the tabletop, the background '
                       f'and its own gripper fingers, with no task object in it.')
        elif w in large:
            out.append(f'In this segment the {_WRISTS[w]} wrist camera view has a large, very close task object right at '
                       f'its gripper fingers, filling a big part of that view exactly where and as large as Video 1 shows '
                       f'it; draw it at that size and position in its real colours, never smaller and never missing.')
    if any(w in views for w in _WRISTS):
        out.append('No cable is visible in either wrist camera view.')
    return ' '.join(out)


def check_h3_payload(manifest: dict, contract_path: Path) -> dict:
    c = load_contract(contract_path)
    refs = manifest.get('reference_images', [])
    hashes = [sha256_file(Path(p)) for p in refs]
    wanted = [r['sha256'] for r in c['intent']['real_references']]
    require(hashes[:len(wanted)] == wanted, 'H3 manifest lacks bound real images')
    lowering = manifest.get('prompt_lowering')
    if lowering:
        from datafarm.agent.context import repository_root
        expected = lower_h3_prompt(c, lowering, assets=repository_root())
    else:
        expected = c['prompt']
    require(Path(manifest['prompt']).read_text().strip() == expected.strip(), 'H3 prompt mismatch')
    for job in manifest.get('jobs', []):
        require(job.get('reference_images') == refs, 'H3 job overrides core reference/prompt')
        if lowering and job.get('chunk_facts') is not None:
            job_expected = lower_h3_prompt(c, lowering, assets=repository_root(), chunk_facts=job['chunk_facts'])
            require(Path(job['prompt']).read_text().strip() == job_expected.strip(), 'H3 chunk prompt mismatch')
        else:
            require(job.get('prompt') == manifest['prompt'], 'H3 job overrides core reference/prompt')
    return {'schema': 'datafarm.render-submission/1', 'contract_sha256': sha256_file(contract_path),
            'intent_sha256': c['intent_sha256'], 'prompt_sha256': c['prompt_sha256'],
            'actual_image_sha256s': hashes, 'payload_sha256': json_sha(manifest), 'provider': 'h3',
            'prompt_lowering': lowering, 'submitted_prompt_sha256': sha256_text(expected.strip())}


def submission_contract_path() -> Path:
    value = os.environ.get('DATAFARM_RENDER_CONTRACT')
    require(bool(value), 'DATAFARM_RENDER_CONTRACT required: compile this new task intent/overlay before rendering')
    return Path(value)
