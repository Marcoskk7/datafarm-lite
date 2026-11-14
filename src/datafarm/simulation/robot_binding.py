"""Source-bound model discovery and runtime binding (no simulator import)."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import xml.etree.ElementTree as ET

from .robot_semantics import model_semantics, origin, resolve_tcp, vector


class BindingError(ValueError):
    """Robot source, profile or runtime does not satisfy the binding contract."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BindingError('Expected a finite number')
    return float(value)


def unique(values, label):
    if not isinstance(values, list) or not all(isinstance(x, str) and x for x in values):
        raise BindingError(label + ' must be a list of names')
    if len(set(values)) != len(values):
        raise BindingError('Duplicate ' + label)


def artifact_ref(value):
    if (not isinstance(value, dict) or not isinstance(value.get('kind'), str)
            or not value['kind'] or not isinstance(value.get('media_type'), str)
            or not isinstance(value.get('sha256'), str) or len(value['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise BindingError('Calibration requires a content-bound ArtifactRef')


def discover_urdf(source: bytes, mesh_resolver=None) -> dict:
    """Describe exact URDF bytes; does not claim mesh or dynamics acceptance."""
    if len(source) > 50 * 1024 * 1024 or b'<!DOCTYPE' in source.upper() or b'<!ENTITY' in source.upper():
        raise BindingError('Unsupported XML declaration or oversized URDF')
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise BindingError('Invalid URDF XML') from exc
    if root.tag != 'robot':
        raise BindingError('Expected robot element')
    links = [x.get('name') for x in root.findall('link')]
    unique(links, 'links')
    names = [x.get('name') for x in root.findall('joint')]
    unique(names, 'joints')
    joints, children = [], set()
    for node in root.findall('joint'):
        kind = node.get('type')
        if kind not in ('fixed', 'revolute', 'continuous', 'prismatic'):
            raise BindingError('Unsupported joint type: ' + str(kind))
        parent, child = node.find('parent'), node.find('child')
        if parent is None or child is None:
            raise BindingError('Joint lacks parent or child')
        parent, child = parent.get('link'), child.get('link')
        if parent not in links or child not in links or child in children:
            raise BindingError('Unknown link or multiply parented child')
        children.add(child)
        joint = dict(name=node.get('name'), type=kind, parent=parent, child=child,
                     unit='m' if kind == 'prismatic' else 'rad', limits=None, mimic=None)
        try:
            joint['origin'] = origin(node)
            axis = node.find('axis')
            joint['axis'] = vector(axis.get('xyz') if axis is not None else None, [1, 0, 0])
            norm = math.hypot(*joint['axis'])
            if norm == 0:
                raise ValueError('Zero joint axis')
            joint['axis'] = [v/norm for v in joint['axis']]
        except ValueError as exc:
            raise BindingError('Invalid joint transform/axis: ' + joint['name']) from exc
        if kind != 'fixed':
            lim = node.find('limit')
            if lim is None:
                raise BindingError('Moving joint lacks limits: ' + joint['name'])
            try:
                velocity, effort = finite(float(lim.get('velocity'))), finite(float(lim.get('effort')))
                bounds = None if kind == 'continuous' else [finite(float(lim.get(k))) for k in ('lower', 'upper')]
            except (TypeError, ValueError) as exc:
                raise BindingError('Invalid joint limits: ' + joint['name']) from exc
            if velocity <= 0 or effort <= 0 or (bounds is not None and bounds[0] > bounds[1]):
                raise BindingError('Invalid joint limits: ' + joint['name'])
            joint['limits'] = dict(position=bounds, velocity=velocity, effort=effort)
        mimic = node.find('mimic')
        if mimic is not None:
            if kind == 'fixed':
                raise BindingError('Fixed joint cannot mimic')
            joint['mimic'] = dict(joint=mimic.get('joint'),
                                  multiplier=finite(float(mimic.get('multiplier', '1'))),
                                  offset=finite(float(mimic.get('offset', '0'))))
        joints.append(joint)
    roots = set(links) - children
    if len(roots) != 1:
        raise BindingError('Expected one kinematic root')
    visited = set(roots)
    pending = joints[:]
    while pending:
        ready = [j for j in pending if j['parent'] in visited]
        if not ready:
            raise BindingError('Disconnected or cyclic kinematic tree')
        for joint in ready:
            if joint['child'] in visited:
                raise BindingError('Cyclic kinematic tree')
            visited.add(joint['child'])
            pending.remove(joint)
    if visited != set(links):
        raise BindingError('Disconnected links')
    by_name = {j['name']: j for j in joints}
    for joint in joints:
        seen, current = set(), joint
        while current['mimic'] is not None:
            if current['name'] in seen:
                raise BindingError('Cyclic mimic chain')
            seen.add(current['name'])
            target = by_name.get(current['mimic']['joint'])
            if target is None or target['type'] != current['type']:
                raise BindingError('Missing or incompatible mimic source')
            current = target
    source_sha256 = hashlib.sha256(source).hexdigest()
    semantics = model_semantics(root, joints, source_sha256, mesh_resolver)
    collisions = []
    for link in root.findall('link'):
        for node in link.findall('collision'):
            try:
                geometry = node.find('geometry')
                collisions.append(dict(link=link.get('name'), origin=origin(node),
                                       shapes=[] if geometry is None else [
                                           dict(type=shape.tag, attributes=dict(shape.attrib)) for shape in geometry]))
            except ValueError as exc:
                raise BindingError('Invalid collision origin') from exc
    return dict(schema_version='robot-descriptor/2', source_sha256=source_sha256,
                robot_name=root.get('name'), root_link=next(iter(roots)), links=links, joints=joints,
                collision_models=collisions, model_semantics=semantics,
                verification='source_structure_and_model_semantics_only')


def bind_robot(descriptor: dict, profile: dict | None = None,
               joint_names: list | None = None, body_names: list | None = None) -> dict:
    """Prefer model semantics; a source-bound explicit profile can override them."""
    descriptor = deepcopy(descriptor)
    profile = deepcopy({'schema_version': 'robot-profile/2'} if profile is None else profile)
    if not isinstance(descriptor, dict) or not isinstance(profile, dict):
        raise BindingError('Descriptor and profile must be objects')
    if descriptor.get('schema_version') != 'robot-descriptor/2' or profile.get('schema_version') != 'robot-profile/2':
        raise BindingError('Unsupported descriptor or profile version')
    if profile.get('source_sha256', descriptor['source_sha256']) != descriptor['source_sha256']:
        raise BindingError('Profile source hash mismatch')
    moving = {j['name']: j for j in descriptor['joints'] if j['type'] != 'fixed'}
    live = joint_names is not None
    order = list(moving) if joint_names is None else list(joint_names)
    bodies = list(descriptor['links']) if body_names is None else list(body_names)
    unique(order, 'runtime joints'); unique(bodies, 'runtime bodies')
    if set(order) != set(moving) or not set(bodies).issubset(descriptor['links']):
        raise BindingError('Runtime/source joint or body mismatch')
    controllable = [n for n in order if moving[n]['mimic'] is None]
    if profile.get('control_mode', 'position') != 'position':
        raise BindingError('Only position control is currently supported')
    effective_limits = model_joint_limits(descriptor)
    groups = profile.get('groups', {})
    if not isinstance(groups, dict):
        raise BindingError('groups must be an object')
    assigned = set()
    for group, members in groups.items():
        unique(members, 'group ' + group)
        if not members or not set(members).issubset(controllable) or assigned.intersection(members):
            raise BindingError('Invalid, passive or overlapping control group')
        assigned.update(members)
    semantics, diagnostics = {}, deepcopy(descriptor['model_semantics']['diagnostics'])
    for key in ('tcp', 'gripper'):
        candidates = descriptor['model_semantics'][key + '_candidates']
        selector = profile.get('model_' + key)
        if selector is not None:
            field = 'link' if key == 'tcp' else 'master_joint'
            candidates = [v for v in candidates if v[field] == selector]
            if len(candidates) != 1:
                raise BindingError('Model semantic selector missing or ambiguous: ' + key)
        value = deepcopy(profile.get(key))
        if value is not None:
            if not isinstance(value, dict):
                raise BindingError('Semantic profile must be an object: ' + key)
            artifact_ref(value.get('calibration_ref'))
            value['selection_source'] = 'explicit_profile'
        elif len(candidates) == 1:
            value = deepcopy(candidates[0])
            value['selection_source'] = 'model'
        else:
            diagnostics.append(dict(component=key, reason='ambiguous_model_candidates' if candidates else 'no_model_definition'))
        semantics[key] = value
    tcp = semantics['tcp']
    if tcp is not None:
        if tcp.get('link') not in descriptor['links']:
            raise BindingError('TCP link not present in source model')
        xyz, quat = tcp.get('position_m', []), tcp.get('quaternion_xyzw', [])
        if len(xyz) != 3 or len(quat) != 4:
            raise BindingError('Invalid TCP transform dimensions')
        for v in xyz + quat:
            finite(v)
        if not math.isclose(sum(v*v for v in quat), 1, abs_tol=1e-6):
            raise BindingError('TCP quaternion must be normalized XYZW')
        try:
            semantics['tcp'] = resolve_tcp(tcp, descriptor['joints'], bodies)
        except ValueError as exc:
            if tcp['selection_source'] == 'explicit_profile' or profile.get('model_tcp'):
                raise BindingError(str(exc)) from exc
            diagnostics.append(dict(component='tcp', reason=str(exc)))
            semantics['tcp'] = None
    gripper = semantics['gripper']
    if gripper is not None:
        master = moving.get(gripper.get('master_joint'))
        bounds = gripper.get('width_range_m', [])
        if master is None or master['mimic'] is not None or len(bounds) != 2:
            raise BindingError('Gripper needs an independent master and calibrated width range')
        low, high = map(finite, bounds)
        scale, offset = finite(gripper.get('joint_per_m')), finite(gripper.get('joint_offset'))
        if low < 0 or low >= high or scale == 0:
            raise BindingError('Invalid or unsourced gripper calibration')
        limits = master['limits']['position']
        if limits is None or any(not limits[0]-1e-12 <= offset + scale*w <= limits[1]+1e-12 for w in bounds):
            raise BindingError('Calibrated gripper exceeds joint limits')
        # Include dependent finger limits in the accepted width interval.
        qlow, qhigh = effective_limits[master['name']]
        wlow, whigh = sorted(((qlow-offset)/scale, (qhigh-offset)/scale))
        interval = [max(low, wlow), min(high, whigh)]
        if interval[0] > interval[1]:
            raise BindingError('Model limits exclude gripper opening interval')
        gripper['width_range_m'] = interval
    result = dict(schema_version='robot-binding/2', descriptor=descriptor, profile=profile,
                  semantics=semantics, semantic_diagnostics=diagnostics, control_mode='position',
                  effective_position_limits=effective_limits,
                  joint_names=order, body_names=bodies, joint_index={n: i for i, n in enumerate(order)},
                  controllable_joints=controllable, groups=groups, runtime_bound=live,
                  capabilities=dict(control_mode='position', joint_state=True, joint_position_target=bool(controllable),
                                    tcp_pose=semantics['tcp'] is not None, move_tcp=False,
                                    set_gripper=gripper is not None,
                                    contacts='unavailable', cameras='not_discovered'))
    result['binding_sha256'] = digest(result)
    return result


def model_joint_limits(descriptor):
    """Propagate URDF mimic limits; scene collisions belong to the planner."""
    moving = {j['name']: j for j in descriptor['joints'] if j['type'] != 'fixed'}
    limits = {n: deepcopy(j['limits']['position']) for n, j in moving.items()}
    # Propagate all dependent joint bounds to their independent master.
    for name, joint in moving.items():
        current, scale, offset = joint, 1., 0.
        while current['mimic']:
            m = current['mimic']
            offset += scale*m['offset']
            scale *= m['multiplier']
            current = moving[m['joint']]
        if name == current['name'] or limits[name] is None:
            continue
        low, high = limits[name]
        if scale == 0:
            if not low <= offset <= high:
                raise BindingError('Mimic offset exceeds effective limits')
            continue
        a, b = sorted(((low-offset)/scale, (high-offset)/scale))
        own = limits[current['name']]
        limits[current['name']] = [a, b] if own is None else [max(own[0], a), min(own[1], b)]
        if limits[current['name']][0] > limits[current['name']][1]:
            raise BindingError('Mimic and master limits do not intersect')
    return limits


def verify_binding(binding):
    if binding.get('schema_version') != 'robot-binding/2':
        raise BindingError('Unsupported binding version')
    value = deepcopy(binding)
    saved = value.pop('binding_sha256', None)
    if saved != digest(value):
        raise BindingError('Binding was modified')


def validate_targets(binding, targets: dict) -> dict:
    verify_binding(binding)
    if not isinstance(targets, dict) or not targets:
        raise BindingError('Expected nonempty named joint targets')
    result = {}
    for name, value in targets.items():
        if name not in binding['controllable_joints']:
            raise BindingError('Unknown, fixed or passive joint: ' + name)
        value = finite(value)
        limits = binding['effective_position_limits'][name]
        if limits is not None and not limits[0] <= value <= limits[1]:
            raise BindingError('Target exceeds authored position limit: ' + name)
        result[name] = value
    return result


def gripper_target(binding, width_m):
    verify_binding(binding)
    g = binding['semantics'].get('gripper')
    if g is None:
        raise BindingError('CAPABILITY_UNAVAILABLE: gripper calibration required')
    width = finite(width_m)
    if not g['width_range_m'][0] <= width <= g['width_range_m'][1]:
        raise BindingError('Gripper width out of range')
    return validate_targets(binding, {g['master_joint']: g['joint_offset'] + g['joint_per_m']*width})
