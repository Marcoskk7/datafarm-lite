"""从模型提取末端坐标系，并由相向平移的两指几何推导夹爪开度。

所有尺寸来自 URDF 和碰撞网格，不内置特定机器人的参数，也不依赖仿真器。
推导结果只表示模型中的几何关系；遇到不支持或有歧义的结构时保留诊断，
不能将结果当作实物指垫标定、接触检测或抓取成功的证明。
"""
from itertools import product
import hashlib
import math
from pathlib import Path
import re
import struct


def vector(text, default, size=3):
    """解析 URDF 的空格分隔向量；缺省值、维度和有限性在此统一处理。"""
    values = list(default) if text is None else [float(v) for v in text.split()]
    if len(values) != size or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid model vector')
    return values


def multiply(a, b):
    """计算 XYZW 四元数的 Hamilton 积，乘法顺序与变换组合顺序一致。"""
    x, y, z, w = a
    u, v, s, t = b
    return [w*u+x*t+y*s-z*v, w*v-x*s+y*t+z*u,
            w*s+x*v-y*u+z*t, w*t-x*u-y*v-z*s]


def rotate(q, v):
    """用单位四元数执行主动旋转：v' = q · (v, 0) · q 的共轭。"""
    return multiply(multiply(q, [*v, 0]), [-q[0], -q[1], -q[2], q[3]])[:3]


def origin(node):
    """读取子节点 origin，返回局部坐标到父坐标的平移和 XYZW 旋转。"""
    o = node.find('origin')
    xyz = vector(o.get('xyz') if o is not None else None, [0, 0, 0])
    r, p, y = vector(o.get('rpy') if o is not None else None, [0, 0, 0])
    # URDF 的固定轴 RPY 对应 Rz(yaw) · Ry(pitch) · Rx(roll)，不能调换顺序。
    q = multiply(multiply([0, 0, math.sin(y/2), math.cos(y/2)],
                          [0, math.sin(p/2), 0, math.cos(p/2)]),
                 [math.sin(r/2), 0, 0, math.cos(r/2)])
    return dict(position_m=xyz, quaternion_xyzw=q)


def compose(a, b):
    """组合 T_A_B 与 T_B_C，得到 T_A_C；先旋转 b 的位移，再叠加 a。"""
    p = rotate(a['quaternion_xyzw'], b['position_m'])
    return dict(position_m=[u+v for u, v in zip(a['position_m'], p)],
                quaternion_xyzw=multiply(a['quaternion_xyzw'], b['quaternion_xyzw']))


def dot(a, b):
    """计算向量点积，用于运动轴判断和碰撞包络的方向投影。"""
    return sum(u*v for u, v in zip(a, b))


def file_mesh_resolver(urdf_path, asset_root=None, packages=None):
    """构造网格字节读取器，仅允许声明的目录和 package:// 包映射。

    相对路径以 URDF 所在目录解析；asset_root 只决定允许访问的外层边界。
    resolve() 后再检查边界，避免通过 .. 或符号链接读出资产包以外的文件。
    """
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
    """读取 ASCII 或二进制 STL 的顶点，不把法向量和属性字节当作坐标。"""
    if len(data) > 50 * 1024 * 1024:
        raise ValueError('Oversized collision mesh')
    # 二进制 STL：80 字节头 + 4 字节面数 + 每面 50 字节。
    # 文件头也可能以 solid 开头，因此先按长度和面数识别，不能只检查文本前缀。
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
    """将一个 link 的碰撞顶点转换到 link 坐标系，并登记网格内容摘要。

    使用 collision 而非 visual；先应用 mesh scale，再应用 collision origin。
    当前只支持 box 和 STL，其他形状明确拒绝，不能用猜测的尺寸替代。
    """
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
    """生成 TCP/夹爪候选及来源；候选选择由后续绑定层处理。

    joints 必须先完成运动树和 mimic 链校验，本函数据此安全地沿主从关系回溯。
    """
    source_ref = dict(kind='robot-model', sha256=source_sha256, media_type='application/urdf+xml')
    tcp = []
    for link in root.findall('link'):
        name = link.get('name')
        # 名称只用于发现模型声明的末端 frame，不证明它就是实际接触中心。
        # 左右臂都匹配时保留全部候选，不按出现顺序挑选其中一个。
        if re.search(r'(^|_)(tcp|tcp_link|tool0|tool_frame|end_link)$', name):
            tcp.append(dict(link=name, position_m=[0, 0, 0], quaternion_xyzw=[0, 0, 0, 1],
                            source_ref=source_ref, verification='named_model_frame'))
    by_name = {j['name']: j for j in joints}
    links = {link.get('name'): link for link in root.findall('link')}
    families = {}
    for joint in joints:
        if joint['type'] != 'prismatic':
            continue
        # 将多级 mimic 展平为 q_finger = scale * q_master + offset。
        # 向上追溯时先累加当前比例下的偏置，再更新比例，避免丢失中间级 offset。
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
            # 主关节可能驱动无碰撞几何的虚拟 link；真正的两根手指以碰撞几何识别。
            # 此处只接受同父 link 的两根平移手指，不推测旋转夹爪或非线性连杆机构。
            fingers = [f for f in family if links[f[0]['child']].find('collision') is not None]
            if len(fingers) != 2 or fingers[0][0]['parent'] != fingers[1][0]['parent']:
                raise ValueError('Need two sibling fingers with collision geometry')
            master = by_name[master_name]
            lo, hi = master['limits']['position']
            # 将每个从关节的限位反算为主关节区间，再与主关节自身限位取交集。
            for j, scale, offset in family:
                lower, upper = j['limits']['position']
                if scale == 0:
                    if not lower <= offset <= upper:
                        raise ValueError('Mimic offset exceeds finger limits')
                else:
                    a, b = sorted(((lower-offset)/scale, (upper-offset)/scale))
                    lo, hi = max(lo, a), min(hi, b)
            # URDF axis 在关节局部系中定义；转入公共父系后才能比较两指的运动方向。
            axes = [rotate(j['origin']['quaternion_xyzw'], j['axis']) for j, _, _ in fingers]
            velocities = [[a*s for a in axis] for axis, (_, s, _) in zip(axes, fingers)]
            if dot(axes[0], axes[1]) > -1 + 1e-8 or dot(velocities[0], velocities[1]) >= 0:
                raise ValueError('Finger motions are not opposing and parallel')
            # direction 指向两指相对分离的方向，rate 是单位主关节位移引起的开度增量。
            relative = [v-u for u, v in zip(*velocities)]
            rate = math.sqrt(dot(relative, relative))
            direction = [v/rate for v in relative]
            # 将碰撞点放到公共父系，并加入 q_master=0 时 mimic 自带的手指偏置。
            clouds = []
            for (j, _, offset), axis in zip(fingers, axes):
                cloud = collision_points(links[j['child']], mesh_resolver, family_refs)
                clouds.append([[p+t+a*offset for p, t, a in zip(
                    rotate(j['origin']['quaternion_xyzw'], v), j['origin']['position_m'], axis)] for v in cloud])
            # 在垂直于开合轴的平面检查两方向投影是否重叠，排除明显错开的指体。
            # 这是包络推导的必要检查，不能替代精确碰撞或真实指垫接触判断。
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
            # 对向内侧包络的投影差满足 width(q_master) = gap + rate * q_master。
            # gap 已含 mimic offset；它可能略小于零，表示零位模型有轻微几何重叠。
            gap = min(dot(p, direction) for p in clouds[1]) - max(dot(p, direction) for p in clouds[0])
            coefficients = [-dot(axes[0], direction), dot(axes[1], direction)]
            # 观测时使用两根手指各自的实测 q，先扣除 gap 中已计入的 mimic 偏置。
            # 这样主从约束有滞后时，观测开度不会被主关节目标或理想 mimic 关系覆盖。
            observed_offset = gap - sum(c*f[2] for c, f in zip(coefficients, fingers))
            # 只保留硬限位内且几何开度非负的区间；负开度不能当作有效闭合量。
            lo = max(lo, -gap/rate)
            if not lo < hi:
                raise ValueError('No nonnegative opening interval within joint limits')
            low_width, high_width = max(0., gap+rate*lo), gap+rate*hi
            # 将上述关系反解为控制映射 q_master = joint_offset + joint_per_m * width。
            grippers.append(dict(master_joint=master_name, width_range_m=[low_width, high_width],
                                 joint_per_m=1/rate, joint_offset=-gap/rate,
                                 source_ref=source_ref, verification='model_geometry_only',
                                 width_semantics='collision_envelope_gap',
                                 finger_joints=[f[0]['name'] for f in fingers],
                                 width_observation=dict(offset_m=observed_offset,
                                     joint_coefficients={f[0]['name']: c for f, c in zip(fingers, coefficients)}),
                                 geometry_refs=[family_refs[k] for k in sorted(family_refs)]))
        # 某个夹爪无法推导不应抹掉已发现的运动链或 TCP；绑定层据此关闭对应能力。
        except (ValueError, OSError, UnicodeError, struct.error) as exc:
            diagnostics.append(dict(component='gripper', master_joint=master_name, reason=str(exc)))
        refs.update(family_refs)
    return dict(tcp_candidates=tcp, gripper_candidates=grippers, diagnostics=diagnostics,
                geometry_refs=[refs[k] for k in sorted(refs)])


def resolve_tcp(tcp, joints, bodies):
    """将被导入器合并的 TCP 固定 frame 解析到仍存在的 runtime body。

    沿 parent 链逐级左乘固定变换，保留原 model_frame 便于追溯；不能跨越可动
    关节，因为可动关节两侧的变换取决于当前 q，无法用常量 TCP 偏置代替。
    """
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
