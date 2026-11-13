"""Source-bound robot discovery and explicit semantic binding (no simulator import).

URDF supplies joint types/topology/mimic metadata; a live articulation supplies
the actual joint/body order. Neither naming heuristics nor a last-link guess
authorize a TCP, gripper calibration or controller.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import xml.etree.ElementTree as ET


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


def discover_urdf(source: bytes) -> dict:
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
    return dict(schema_version='robot-descriptor/1', source_sha256=hashlib.sha256(source).hexdigest(),
                robot_name=root.get('name'), root_link=next(iter(roots)), links=links, joints=joints,
                verification='source_structure_only')


def bind_robot(descriptor: dict, profile: dict | None = None,
               joint_names: list | None = None, body_names: list | None = None) -> dict:
    """Bind explicit semantics to actual order; absent calibration disables capability."""
    descriptor, profile = deepcopy(descriptor), deepcopy(profile or {'schema_version': 'robot-profile/1'})
    if descriptor.get('schema_version') != 'robot-descriptor/1' or profile.get('schema_version') != 'robot-profile/1':
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
    groups = profile.get('groups', {})
    if not isinstance(groups, dict):
        raise BindingError('groups must be an object')
    assigned = set()
    for group, members in groups.items():
        unique(members, 'group ' + group)
        if not members or not set(members).issubset(controllable) or assigned.intersection(members):
            raise BindingError('Invalid, passive or overlapping control group')
        assigned.update(members)
    tcp = profile.get('tcp')
    if tcp is not None:
        artifact_ref(tcp.get('calibration_ref'))
        if tcp.get('link') not in bodies:
            raise BindingError('TCP link not present in runtime bodies')
        xyz, quat = tcp.get('position_m', []), tcp.get('quaternion_xyzw', [])
        if len(xyz) != 3 or len(quat) != 4:
            raise BindingError('Invalid TCP transform dimensions')
        for v in xyz + quat:
            finite(v)
        if not math.isclose(sum(v*v for v in quat), 1, abs_tol=1e-6):
            raise BindingError('TCP quaternion must be normalized XYZW')
    gripper = profile.get('gripper')
    if gripper is not None:
        artifact_ref(gripper.get('calibration_ref'))
        master = moving.get(gripper.get('master_joint'))
        bounds = gripper.get('width_range_m', [])
        if master is None or master['mimic'] is not None or len(bounds) != 2:
            raise BindingError('Gripper needs an independent master and calibrated width range')
        low, high = map(finite, bounds)
        scale, offset = finite(gripper.get('joint_per_m')), finite(gripper.get('joint_offset'))
        if low < 0 or low >= high or scale == 0:
            raise BindingError('Invalid or unsourced gripper calibration')
        limits = master['limits']['position']
        if limits is None or any(not limits[0] <= offset + scale*w <= limits[1] for w in bounds):
            raise BindingError('Calibrated gripper exceeds joint limits')
    result = dict(schema_version='robot-binding/1', descriptor=descriptor, profile=profile,
                  joint_names=order, body_names=bodies, joint_index={n: i for i, n in enumerate(order)},
                  controllable_joints=controllable, groups=groups, runtime_bound=live,
                  capabilities=dict(joint_state=True, joint_position_target=bool(controllable),
                                    tcp_pose=tcp is not None, move_tcp=False,
                                    set_gripper=gripper is not None,
                                    contacts='unavailable', cameras='not_discovered'))
    result['binding_sha256'] = digest(result)
    return result


def verify_binding(binding):
    value = deepcopy(binding)
    saved = value.pop('binding_sha256', None)
    if saved != digest(value):
        raise BindingError('Binding was modified')


def validate_targets(binding, targets: dict) -> dict:
    verify_binding(binding)
    if not isinstance(targets, dict) or not targets:
        raise BindingError('Expected nonempty named joint targets')
    joints = {j['name']: j for j in binding['descriptor']['joints']}
    result = {}
    for name, value in targets.items():
        if name not in binding['controllable_joints']:
            raise BindingError('Unknown, fixed or passive joint: ' + name)
        value = finite(value)
        limits = joints[name]['limits']['position']
        if limits is not None and not limits[0] <= value <= limits[1]:
            raise BindingError('Target exceeds authored position limit: ' + name)
        result[name] = value
    return result


def gripper_target(binding, width_m):
    verify_binding(binding)
    g = binding['profile'].get('gripper')
    if g is None:
        raise BindingError('CAPABILITY_UNAVAILABLE: gripper calibration required')
    width = finite(width_m)
    if not g['width_range_m'][0] <= width <= g['width_range_m'][1]:
        raise BindingError('Gripper width out of range')
    return validate_targets(binding, {g['master_joint']: g['joint_offset'] + g['joint_per_m']*width})
