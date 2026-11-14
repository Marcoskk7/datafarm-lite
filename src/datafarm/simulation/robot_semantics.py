"""Conservative model semantics: named frames and opposing linear finger geometry.

No robot-specific dimensions, simulator imports, or claims of contact calibration.
Unsupported or ambiguous geometry is reported rather than guessed.
"""
from itertools import product
import hashlib
import math
from pathlib import Path
import re
import struct


def vector(text, default, size=3):
    values = list(default) if text is None else [float(v) for v in text.split()]
    if len(values) != size or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid model vector')
    return values


def multiply(a, b):
    x, y, z, w = a
    u, v, s, t = b
    return [w*u+x*t+y*s-z*v, w*v-x*s+y*t+z*u,
            w*s+x*v-y*u+z*t, w*t-x*u-y*v-z*s]


def rotate(q, v):
    return multiply(multiply(q, [*v, 0]), [-q[0], -q[1], -q[2], q[3]])[:3]


def origin(node):
    o = node.find('origin')
    xyz = vector(o.get('xyz') if o is not None else None, [0, 0, 0])
    r, p, y = vector(o.get('rpy') if o is not None else None, [0, 0, 0])
    q = multiply(multiply([0, 0, math.sin(y/2), math.cos(y/2)],
                          [0, math.sin(p/2), 0, math.cos(p/2)]),
                 [math.sin(r/2), 0, 0, math.cos(r/2)])
    return dict(position_m=xyz, quaternion_xyzw=q)


def compose(a, b):
    p = rotate(a['quaternion_xyzw'], b['position_m'])
    return dict(position_m=[u+v for u, v in zip(a['position_m'], p)],
                quaternion_xyzw=multiply(a['quaternion_xyzw'], b['quaternion_xyzw']))


def dot(a, b):
    return sum(u*v for u, v in zip(a, b))


def file_mesh_resolver(urdf_path, asset_root=None, packages=None):
    """Read only explicitly rooted relative/package URIs, including symlink checks."""
    source = Path(urdf_path).resolve()
    root = Path(asset_root).resolve() if asset_root else source.parent
    package_roots = {k: Path(v).resolve() for k, v in (packages or {}).items()}

    def read(uri):
        if uri.startswith('package://'):
            name, separator, tail = uri[len('package://'):].partition('/')
            if not separator or name not in package_roots:
                raise ValueError('Package mesh root not supplied: ' + name)
            boundary = package_roots[name]
            path = (boundary / tail).resolve()
        else:
            if '://' in uri or Path(uri).is_absolute() or ':' in uri:
                raise ValueError('Mesh URI must be relative or explicitly mapped package URI')
            boundary = root
            path = (source.parent / uri).resolve()
        if not path.is_relative_to(boundary):
            raise ValueError('Mesh escapes declared asset root')
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError('Oversized collision mesh')
        return path.read_bytes()
    return read


def stl_vertices(data):
    if len(data) > 50 * 1024 * 1024:
        raise ValueError('Oversized collision mesh')
    if len(data) >= 84 and 84 + 50*struct.unpack_from('<I', data, 80)[0] == len(data):
        points = [list(struct.unpack_from('<3f', data, start + 12 + 12*k))
                  for start in range(84, len(data), 50) for k in range(3)]
    else:
        points = [vector(' '.join(m), []) for m in re.findall(
            r'\bvertex\s+(\S+)\s+(\S+)\s+(\S+)', data.decode('ascii'))]
    if not points or not all(math.isfinite(v) for p in points for v in p):
        raise ValueError('Invalid or empty STL collision mesh')
    return points


def collision_points(link, resolver, refs):
    points = []
    for collision in link.findall('collision'):
        geometry = collision.find('geometry')
        if geometry is None or len(geometry) != 1:
            raise ValueError('Missing or ambiguous collision geometry')
        shape = geometry[0]
        if shape.tag == 'box':
            size = vector(shape.get('size'), [])
            if any(v <= 0 for v in size):
                raise ValueError('Nonpositive collision box')
            vertices = list(product(*[(-v/2, v/2) for v in size]))
        elif shape.tag == 'mesh':
            uri = shape.get('filename', '')
            if resolver is None:
                raise ValueError('Collision mesh resolver not supplied')
            data = resolver(uri)
            vertices = stl_vertices(data)
            scale = vector(shape.get('scale'), [1, 1, 1])
            if any(v <= 0 for v in scale):
                raise ValueError('Nonpositive collision mesh scale')
            vertices = [[v*s for v, s in zip(p, scale)] for p in vertices]
            refs[uri] = dict(uri=uri, sha256=hashlib.sha256(data).hexdigest(),
                             media_type='model/stl')
        else:
            raise ValueError('Unsupported collision shape: ' + shape.tag)
        pose = origin(collision)
        points.extend([[u+v for u, v in zip(pose['position_m'], rotate(pose['quaternion_xyzw'], p))]
                       for p in vertices])
    if not points:
        raise ValueError('Finger has no collision geometry')
    return points


def model_semantics(root, joints, source_sha256, mesh_resolver=None):
    source_ref = dict(kind='robot-model', sha256=source_sha256, media_type='application/urdf+xml')
    tcp = []
    for link in root.findall('link'):
        name = link.get('name')
        if re.search(r'(^|_)(tcp|tcp_link|tool0|tool_frame|end_link)$', name):
            tcp.append(dict(link=name, position_m=[0, 0, 0], quaternion_xyzw=[0, 0, 0, 1],
                            source_ref=source_ref, verification='named_model_frame'))
    by_name = {j['name']: j for j in joints}
    links = {link.get('name'): link for link in root.findall('link')}
    families = {}
    for joint in joints:
        if joint['type'] != 'prismatic':
            continue
        current, scale, offset = joint, 1.0, 0.0
        while current['mimic']:
            m = current['mimic']
            offset += scale*m['offset']
            scale *= m['multiplier']
            current = by_name[m['joint']]
        families.setdefault(current['name'], []).append((joint, scale, offset))
    grippers, diagnostics, refs = [], [], {}
    for master_name, family in families.items():
        if len(family) < 2:
            continue
        family_refs = {}
        try:
            # A virtual master may coexist with two physical mimic fingers.
            fingers = [f for f in family if links[f[0]['child']].find('collision') is not None]
            if len(fingers) != 2 or fingers[0][0]['parent'] != fingers[1][0]['parent']:
                raise ValueError('Need two sibling fingers with collision geometry')
            master = by_name[master_name]
            lo, hi = master['limits']['position']
            for j, scale, offset in family:
                lower, upper = j['limits']['position']
                if scale == 0:
                    if not lower <= offset <= upper:
                        raise ValueError('Mimic offset exceeds finger limits')
                else:
                    a, b = sorted(((lower-offset)/scale, (upper-offset)/scale))
                    lo, hi = max(lo, a), min(hi, b)
            axes = [rotate(j['origin']['quaternion_xyzw'], j['axis']) for j, _, _ in fingers]
            velocities = [[a*s for a in axis] for axis, (_, s, _) in zip(axes, fingers)]
            if dot(axes[0], axes[1]) > -1 + 1e-8 or dot(velocities[0], velocities[1]) >= 0:
                raise ValueError('Finger motions are not opposing and parallel')
            relative = [v-u for u, v in zip(*velocities)]
            rate = math.sqrt(dot(relative, relative))
            direction = [v/rate for v in relative]
            clouds = []
            for (j, _, offset), axis in zip(fingers, axes):
                cloud = collision_points(links[j['child']], mesh_resolver, family_refs)
                clouds.append([[p+t+a*offset for p, t, a in zip(
                    rotate(j['origin']['quaternion_xyzw'], v), j['origin']['position_m'], axis)] for v in cloud])
            # Both envelopes must overlap in the plane normal to opening.
            helper = [1, 0, 0] if abs(direction[0]) < .9 else [0, 1, 0]
            tangent = [helper[i]-dot(helper, direction)*direction[i] for i in range(3)]
            norm = math.sqrt(dot(tangent, tangent))
            tangent = [v/norm for v in tangent]
            normal = [direction[1]*tangent[2]-direction[2]*tangent[1],
                      direction[2]*tangent[0]-direction[0]*tangent[2],
                      direction[0]*tangent[1]-direction[1]*tangent[0]]
            for axis in (tangent, normal):
                ranges = [(min(dot(p, axis) for p in c), max(dot(p, axis) for p in c)) for c in clouds]
                if min(r[1] for r in ranges) <= max(r[0] for r in ranges):
                    raise ValueError('Finger envelopes do not face each other')
            gap = min(dot(p, direction) for p in clouds[1]) - max(dot(p, direction) for p in clouds[0])
            coefficients = [-dot(axes[0], direction), dot(axes[1], direction)]
            observed_offset = gap - sum(c*f[2] for c, f in zip(coefficients, fingers))
            lo = max(lo, -gap/rate)
            if not lo < hi:
                raise ValueError('No nonnegative opening interval within joint limits')
            low_width, high_width = max(0., gap+rate*lo), gap+rate*hi
            grippers.append(dict(master_joint=master_name, width_range_m=[low_width, high_width],
                                 joint_per_m=1/rate, joint_offset=-gap/rate,
                                 source_ref=source_ref, verification='model_geometry_only',
                                 width_semantics='collision_envelope_gap',
                                 finger_joints=[f[0]['name'] for f in fingers],
                                 width_observation=dict(offset_m=observed_offset,
                                     joint_coefficients={f[0]['name']: c for f, c in zip(fingers, coefficients)}),
                                 geometry_refs=[family_refs[k] for k in sorted(family_refs)]))
        except (ValueError, OSError, UnicodeError, struct.error) as exc:
            diagnostics.append(dict(component='gripper', master_joint=master_name, reason=str(exc)))
        refs.update(family_refs)
    return dict(tcp_candidates=tcp, gripper_candidates=grippers, diagnostics=diagnostics,
                geometry_refs=[refs[k] for k in sorted(refs)])


def resolve_tcp(tcp, joints, bodies):
    """Compose merged fixed frames onto a surviving body; never cross a moving joint."""
    result = dict(tcp)
    frame = tcp['link']
    pose = {k: tcp[k] for k in ('position_m', 'quaternion_xyzw')}
    parents = {j['child']: j for j in joints}
    while frame not in bodies:
        joint = parents.get(frame)
        if joint is None or joint['type'] != 'fixed':
            raise ValueError('TCP frame has no fixed path to a runtime body')
        pose = compose(joint['origin'], pose)
        frame = joint['parent']
    result.update(pose)
    result.update(link=frame, model_frame=tcp['link'])
    return result
