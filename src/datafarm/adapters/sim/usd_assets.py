"""SimReady USD 资产的检查与碰撞预处理（需要 pxr；凸分解另需 coacd）。

`inspect_asset` 读取已下载资产（`simready-fetch.json` 所在目录）的根 USD：刚体、碰撞网格、
质量、物理材质、关节（轴与限位换算到资产坐标系）和整体包围盒，供 AssetRecord 使用。

`prepare_collision` 生成一个包装层：引用原资产、选中 Newton 物理变体、关闭原碰撞网格，
在每个刚体下写入预先算好的凸包（CoACD）作为碰撞体，并把原碰撞网格上的质量移到刚体上。
这样做的原因（2026-11-15 在 lab30 上实测）：
- SimReady 碰撞是三角网格（approximation=none），MuJoCo Warp 只能把网格当凸包，抽屉和柜体会被填实；
- 让 Newton 在加载时自己跑 convexDecomposition 会产生近乎平面的凸包，MuJoCo 接触直接拒绝，且每次加载重算；
预计算的凸包按网格内容和参数缓存，可复现、可审计。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

FETCH_MANIFEST = "simready-fetch.json"
WRAPPER_SCHEMA = "datafarm.collision-prep/1"
HULL_SCOPE = "datafarm_collision"
DEFAULT_COACD = {"threshold": 0.05, "max_convex_hull": 64, "preprocess_resolution": 50, "seed": 0}
PLANAR_MIN_EXTENT_M = 0.002


def _pxr():
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade  # noqa: F401  (pxr 必须先于 coacd 导入)
    return Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade


def root_usd(fetch_root: Path) -> tuple[dict[str, Any], Path]:
    manifest = json.loads((Path(fetch_root) / FETCH_MANIFEST).read_text(encoding="utf-8"))
    entry = manifest["root_usds"][0]
    relative = entry["path"] if isinstance(entry, dict) else entry
    return manifest, Path(fetch_root) / relative


def open_stage(path: Path, physics_variant: str = "Newton"):
    """打开资产并只启用一个物理变体（Newton），与 Isaac Lab 3.0 + Newton 后端一致。

    变体选择写进 session layer：源资产是 BOM 校验收到的原始字节，检查与预处理不得改动它。
    """
    _, _, Usd, _, _, _ = _pxr()
    stage = Usd.Stage.Open(str(path))
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    prim = stage.GetDefaultPrim()
    sets = prim.GetVariantSets()
    for name in sets.GetNames():
        if name in ("PhysX", "Newton", "MuJoCo"):
            sets.GetVariantSet(name).SetVariantSelection("Enabled" if name == physics_variant else "Disabled")
    return stage


def prim_ref(path) -> str:
    """资产内 prim 路径去掉开头 `/`（kernel 把 `/...` 当作宿主绝对路径拒收）；`RootNode/Geometry/...`。

    这是相对资产根的名字，不是合成场景里的 prim 引用——场景引用由 `isaac_scene.body_ref` 写成
    `usd:/World/...`，两者不要互相转换。
    """
    return str(path).lstrip("/")


INERTIA_TOLERANCE = 1e-9


def quat_matrix(quaternion_xyzw) -> "np.ndarray":
    """单位四元数（xyzw）→ 3×3 旋转矩阵。"""
    import numpy as np
    x, y, z, w = (float(v) for v in quaternion_xyzw)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm == 0:
        raise ValueError("principal axes must be a non-zero quaternion")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def matrix_quat(matrix) -> list[float]:
    """3×3 旋转矩阵 → 单位四元数 xyzw（取 w >= 0 的那一支）。"""
    import numpy as np
    m = np.asarray(matrix, dtype=float)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    return [x, y, z, w]


def check_diagonal_inertia(diagonal, label: str) -> None:
    """对角惯量必须有限、非负并满足三角不等式；不满足就失败，不用 balanceinertia 掩盖。"""
    values = [float(v) for v in diagonal]
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        raise ValueError(f"{label}: diagonal inertia must be 3 finite numbers, got {values}")
    if any(v < 0 for v in values):
        raise ValueError(f"{label}: diagonal inertia must be non-negative, got {values}")
    a, b, c = values
    if not (a + b >= c - INERTIA_TOLERANCE and b + c >= a - INERTIA_TOLERANCE
            and c + a >= b - INERTIA_TOLERANCE):
        raise ValueError(f"{label}: diagonal inertia {values} violates the triangle inequality A + B >= C")


def aggregate_inertia(parts) -> dict[str, Any] | None:
    """把同一刚体下各碰撞体的质量属性合成到刚体坐标系（平行轴定理 + 特征分解）。

    `parts[i]` 是 `{mass_kg, center_of_mass_m, diagonal_inertia_kg_m2, principal_axes_xyzw, to_body}`：
    前四个在碰撞体自己的坐标系里，`to_body` 是 USD 行向量约定的 4×4 碰撞体→刚体变换
    （`p' = p @ M`，平移在最后一行）。返回刚体坐标系下的 mass/COM/主惯量/主轴；
    没有可用的惯量时返回 None（交给引擎估计，不编造）。
    """
    import numpy as np
    if not parts:
        return None
    masses = [float(p["mass_kg"]) for p in parts]
    total = sum(masses)
    if total <= 0:
        return None
    centres, rotations = [], []
    for p in parts:
        check_diagonal_inertia(p["diagonal_inertia_kg_m2"], "collider")
        transform = np.asarray(p["to_body"], dtype=float)
        linear, offset = transform[:3, :3], transform[3, :3]
        centres.append(np.asarray(p["center_of_mass_m"], dtype=float) @ linear + offset)
        rotations.append(linear.T @ quat_matrix(p["principal_axes_xyzw"]))
    centre = sum(m * c for m, c in zip(masses, centres)) / total
    tensor = np.zeros((3, 3))
    for mass, offset, rotation, p in zip(masses, centres, rotations, parts):
        inertia = rotation @ np.diag(np.asarray(p["diagonal_inertia_kg_m2"], dtype=float)) @ rotation.T
        arm = offset - centre
        tensor += inertia + mass * ((arm @ arm) * np.eye(3) - np.outer(arm, arm))
    values, vectors = np.linalg.eigh(tensor)
    if np.linalg.det(vectors) < 0:
        vectors[:, 0] *= -1.0
    return {"mass_kg": total, "center_of_mass_m": centre.tolist(),
            "diagonal_inertia_kg_m2": np.clip(values, 0.0, None).tolist(),
            "principal_axes_xyzw": matrix_quat(vectors)}


def _matrix(gf) -> list[list[float]]:
    return [[float(gf[r][c]) for c in range(4)] for r in range(4)]


def _apply(matrix, points):
    import numpy as np
    m = np.asarray(matrix, dtype=float)  # USD 行向量约定：p' = p @ M
    pts = np.asarray(points, dtype=float)
    return pts @ m[:3, :3] + m[3, :3]


def _triangles(mesh) -> tuple[Any, Any]:
    import numpy as np
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float64)
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = mesh.GetFaceVertexIndicesAttr().Get()
    tris, offset = [], 0
    for count in counts:
        face = indices[offset:offset + count]
        tris.extend((face[0], face[k], face[k + 1]) for k in range(1, count - 1))
        offset += count
    return points, np.asarray(tris, dtype=np.int32)


def _rigid_body_of(prim, UsdPhysics):
    node = prim
    while node and node.IsValid() and not node.IsPseudoRoot():
        if node.HasAPI(UsdPhysics.RigidBodyAPI):
            return node
        node = node.GetParent()
    return None


def _material(prim, UsdShade, UsdPhysics) -> dict[str, float] | None:
    bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial("physics")
    if not bound:
        return None
    api = UsdPhysics.MaterialAPI(bound.GetPrim())
    values = {"static_friction": api.GetStaticFrictionAttr().Get(), "dynamic_friction": api.GetDynamicFrictionAttr().Get(),
              "restitution": api.GetRestitutionAttr().Get(), "density": api.GetDensityAttr().Get()}
    return {"material": prim_ref(bound.GetPrim().GetPath()), **{k: float(v) for k, v in values.items() if v is not None}}


LEGACY_PROPERTIES = {"mass_kg": "pxr:usd:physics_mass",
                     "center_of_mass_m": "pxr:usd:physics_centerofmass",
                     "diagonal_inertia_kg_m2": "pxr:usd:physics_inertia",
                     "principal_axes_xyzw": "pxr:physics:principalAxes"}


def _quat_xyzw(value) -> list[float] | None:
    if value is None:
        return None
    if hasattr(value, "GetImaginary"):
        imaginary = value.GetImaginary()
        return [float(imaginary[0]), float(imaginary[1]), float(imaginary[2]), float(value.GetReal())]
    # Isaac 的 pxr:physics:principalAxes 是 wxyz 顺序的 Vec4
    w, x, y, z = (float(v) for v in value)
    return [x, y, z, w]


def _mass_properties(prim, UsdPhysics) -> dict[str, Any]:
    """刚体的质量属性：标准 MassAPI 优先，其次 Isaac 的 `pxr:usd:physics_*` 旧属性。

    `properties_source` 记录生效来源（`usd-physics` / `isaac-legacy` / None）：只有质量而没有惯量的
    资产也如实返回，调用方据此决定聚合碰撞体还是交给引擎估计。
    """
    properties: dict[str, Any] = {"mass_kg": None, "center_of_mass_m": None, "diagonal_inertia_kg_m2": None,
                                  "principal_axes_xyzw": None, "properties_source": None}
    if prim.HasAPI(UsdPhysics.MassAPI):
        api = UsdPhysics.MassAPI(prim)
        mass = api.GetMassAttr().Get()
        properties["mass_kg"] = float(mass) if mass else None
        center = api.GetCenterOfMassAttr().Get()
        diagonal = api.GetDiagonalInertiaAttr().Get()
        if center is not None and diagonal is not None:
            properties["center_of_mass_m"] = [float(v) for v in center]
            properties["diagonal_inertia_kg_m2"] = [float(v) for v in diagonal]
            properties["principal_axes_xyzw"] = _quat_xyzw(api.GetPrincipalAxesAttr().Get()) or [0.0, 0.0, 0.0, 1.0]
            properties["properties_source"] = "usd-physics"
    if properties["diagonal_inertia_kg_m2"] is None:
        legacy = {}
        for key, name in LEGACY_PROPERTIES.items():
            attribute = prim.GetAttribute(name)
            legacy[key] = attribute.Get() if attribute else None
        if legacy["diagonal_inertia_kg_m2"] is not None and legacy["center_of_mass_m"] is not None:
            properties["center_of_mass_m"] = [float(v) for v in legacy["center_of_mass_m"]]
            properties["diagonal_inertia_kg_m2"] = [float(v) for v in legacy["diagonal_inertia_kg_m2"]]
            properties["principal_axes_xyzw"] = _quat_xyzw(legacy["principal_axes_xyzw"]) or [0.0, 0.0, 0.0, 1.0]
            properties["properties_source"] = "isaac-legacy"
            if properties["mass_kg"] is None and legacy["mass_kg"]:
                properties["mass_kg"] = float(legacy["mass_kg"])
    if properties["diagonal_inertia_kg_m2"] is not None:
        check_diagonal_inertia(properties["diagonal_inertia_kg_m2"], prim_ref(prim.GetPath()))
    return properties


def inspect_asset(fetch_root: Path) -> dict[str, Any]:
    """读取资产的物理事实（全部来自 USD 本身），坐标为资产根坐标系，单位米。"""
    import numpy as np
    Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade = _pxr()
    manifest, path = root_usd(fetch_root)
    stage = open_stage(path)
    root = stage.GetDefaultPrim()
    cache = UsdGeom.XformCache()
    root_inv = cache.GetLocalToWorldTransform(root).GetInverse()
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    bodies, colliders, joints = {}, [], []
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for prim in Usd.PrimRange(root, Usd.TraverseInstanceProxies()):
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            properties = _mass_properties(prim, UsdPhysics)
            pose = cache.GetLocalToWorldTransform(prim) * root_inv
            bodies[prim_ref(prim.GetPath())] = {
                "mass_kg": properties["mass_kg"],
                "kinematic": bool(UsdPhysics.RigidBodyAPI(prim).GetKinematicEnabledAttr().Get()),
                "pose": _matrix(pose),
                "center_of_mass_m": properties["center_of_mass_m"],
                "diagonal_inertia_kg_m2": properties["diagonal_inertia_kg_m2"],
                "principal_axes_xyzw": properties["principal_axes_xyzw"],
                "properties_source": properties["properties_source"]}
        if prim.HasAPI(UsdPhysics.CollisionAPI) and prim.IsA(UsdGeom.Mesh):
            mesh = UsdGeom.Mesh(prim)
            points, tris = _triangles(mesh)
            world = _apply(_matrix(cache.GetLocalToWorldTransform(prim) * root_inv), points) * meters
            lo, hi = np.minimum(lo, world.min(0)), np.maximum(hi, world.max(0))
            body = _rigid_body_of(prim, UsdPhysics)
            approx = (UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get()
                      if prim.HasAPI(UsdPhysics.MeshCollisionAPI) else None)
            mass = UsdPhysics.MassAPI(prim).GetMassAttr().Get() if prim.HasAPI(UsdPhysics.MassAPI) else None
            colliders.append({"prim": prim_ref(prim.GetPath()), "body": prim_ref(body.GetPath()) if body else None,
                              "approximation": str(approx) if approx else None, "vertices": int(len(points)),
                              "triangles": int(len(tris)), "mass_kg": float(mass) if mass else None,
                              "bbox_min_m": world.min(0).round(5).tolist(), "bbox_max_m": world.max(0).round(5).tolist(),
                              "physics_material": _material(prim, UsdShade, UsdPhysics)})
        if prim.IsA(UsdPhysics.Joint):
            joints.append(_joint(prim, stage, cache, root_inv, meters))
    if not colliders:
        raise ValueError(f"{path.name}: no collision meshes")
    return {
        "asset": manifest["asset"],
        "root_usd": path.relative_to(fetch_root).as_posix(),
        "default_prim": prim_ref(root.GetPath()),
        "meters_per_unit": meters,
        "up_axis": str(UsdGeom.GetStageUpAxis(stage)),
        "articulation_root": any(p.HasAPI(UsdPhysics.ArticulationRootAPI)
                                 for p in Usd.PrimRange(root, Usd.TraverseInstanceProxies())),
        "bbox_min_m": lo.round(5).tolist(),
        "bbox_max_m": hi.round(5).tolist(),
        "bodies": bodies,
        "colliders": colliders,
        "joints": joints,
    }


def _joint(prim, stage, cache, root_inv, meters) -> dict[str, Any]:
    import numpy as np
    Gf, Sdf, Usd, UsdGeom, UsdPhysics, _ = _pxr()
    joint = UsdPhysics.Joint(prim)
    kind = ("prismatic" if prim.IsA(UsdPhysics.PrismaticJoint) else "revolute" if prim.IsA(UsdPhysics.RevoluteJoint)
            else "fixed" if prim.IsA(UsdPhysics.FixedJoint) else prim.GetTypeName())
    targets = [joint.GetBody0Rel().GetTargets(), joint.GetBody1Rel().GetTargets()]
    body0, body1 = (str(t[0]) if t else None for t in targets)
    out: dict[str, Any] = {"name": prim.GetName(), "prim": prim_ref(prim.GetPath()), "type": kind,
                           "parent": prim_ref(body0) if body0 else None,
                           "child": prim_ref(body1) if body1 else None}
    if kind in ("prismatic", "revolute"):
        typed = UsdPhysics.PrismaticJoint(prim) if kind == "prismatic" else UsdPhysics.RevoluteJoint(prim)
        axis_token = typed.GetAxisAttr().Get()
        local = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[axis_token]
        rot0 = joint.GetLocalRot0Attr().Get()
        frame = cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body0)) * root_inv if body0 else Gf.Matrix4d(1)
        axis_body = Gf.Rotation(rot0).TransformDir(Gf.Vec3d(*local))
        axis = np.asarray(frame.TransformDir(axis_body), dtype=float)
        axis /= np.linalg.norm(axis)
        scale = meters if kind == "prismatic" else math.pi / 180.0
        lower, upper = typed.GetLowerLimitAttr().Get(), typed.GetUpperLimitAttr().Get()
        anchor = frame.Transform(Gf.Vec3d(*joint.GetLocalPos0Attr().Get()))
        out.update(axis=[round(float(v), 6) for v in axis], axis_token=axis_token,
                   limits={"lower": float(lower) * scale, "upper": float(upper) * scale},
                   anchor_m=[round(float(v) * meters, 5) for v in anchor])
    return out


def _mesh_digest(points, tris, params: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(points.astype("<f8").tobytes())
    h.update(tris.astype("<i4").tobytes())
    h.update(json.dumps(params, sort_keys=True).encode())
    return h.hexdigest()


def decompose(points, tris, params: dict[str, Any], cache_dir: Path) -> list[Any]:
    """CoACD 凸分解，按（网格内容, 参数）缓存为 npz。"""
    import numpy as np
    digest = _mesh_digest(points, tris, params)
    cached = Path(cache_dir) / f"{digest}.npz"
    if cached.is_file():
        data = np.load(cached)
        return [data[k] for k in sorted(data.files, key=lambda k: int(k.split("_")[1]))]
    import coacd
    coacd.set_log_level("error")
    parts = coacd.run_coacd(coacd.Mesh(points, tris), **params)
    hulls = [np.asarray(v, dtype=np.float64) for v, _ in parts]
    cached.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cached, **{f"hull_{i}": v for i, v in enumerate(hulls)})
    return hulls


def _hull_faces(vertices):
    from scipy.spatial import ConvexHull
    hull = ConvexHull(vertices)
    used = sorted(set(hull.simplices.ravel().tolist()))
    remap = {old: new for new, old in enumerate(used)}
    faces = [[remap[i] for i in tri] for tri in hull.simplices]
    return vertices[used], faces


def prepare_collision(fetch_root: Path, output_dir: Path, cache_dir: Path, *,
                      params: dict[str, Any] | None = None) -> dict[str, Any]:
    """写 `<output_dir>/<asset>.usda` 包装层；返回预处理报告（可存为证据）。"""
    import numpy as np
    Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade = _pxr()
    params = dict(DEFAULT_COACD if params is None else params)
    manifest, source = root_usd(fetch_root)
    stage = open_stage(source)
    root = stage.GetDefaultPrim()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{manifest['asset']}.usda"
    out = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(out, UsdGeom.GetStageMetersPerUnit(stage))
    UsdGeom.SetStageUpAxis(out, UsdGeom.GetStageUpAxis(stage))
    top = out.DefinePrim(root.GetPath())
    top.GetReferences().AddReference(str(source.resolve()))
    out.SetDefaultPrim(top)
    for name in root.GetVariantSets().GetNames():
        if name in ("PhysX", "Newton", "MuJoCo"):
            top.GetVariantSets().AddVariantSet(name).SetVariantSelection("Enabled" if name == "Newton" else "Disabled")
    cache = UsdGeom.XformCache()
    report: dict[str, Any] = {"schema": WRAPPER_SCHEMA, "asset": manifest["asset"], "coacd": params,
                              "source_root_usd": source.relative_to(fetch_root).as_posix(), "bodies": {}}
    body_parts: dict[str, list[dict[str, Any]]] = {}
    root_inv = cache.GetLocalToWorldTransform(root).GetInverse()
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for prim in list(Usd.PrimRange(root, Usd.TraverseInstanceProxies())):
        if not (prim.HasAPI(UsdPhysics.CollisionAPI) and prim.IsA(UsdGeom.Mesh)):
            continue
        body = _rigid_body_of(prim, UsdPhysics)
        if body is None:
            raise ValueError(f"{prim.GetPath()}: collider without a rigid body")
        # 去实例化碰撞网格到根的整条链，才能在实例内部写覆盖
        anc = prim.GetPath()
        while anc != Sdf.Path.absoluteRootPath:
            if stage.GetPrimAtPath(anc).IsInstance():
                out.OverridePrim(anc).SetInstanceable(False)
            anc = anc.GetParentPath()
        original = out.OverridePrim(prim.GetPath())
        UsdPhysics.CollisionAPI.Apply(original).CreateCollisionEnabledAttr(False)
        to_body = cache.GetLocalToWorldTransform(prim) * cache.GetLocalToWorldTransform(body).GetInverse()
        properties = _mass_properties(prim, UsdPhysics)
        if properties["mass_kg"]:
            body_parts.setdefault(str(body.GetPath()), []).append({
                "mass_kg": properties["mass_kg"],
                "center_of_mass_m": properties["center_of_mass_m"],
                "diagonal_inertia_kg_m2": properties["diagonal_inertia_kg_m2"],
                "principal_axes_xyzw": properties["principal_axes_xyzw"],
                "to_body": _matrix(to_body)})
            # 质量搬到刚体上，碰撞体上的质量/惯量/质心一并清零，避免"零质量却带惯量"的矛盾状态
            api = UsdPhysics.MassAPI.Apply(original)
            api.CreateMassAttr(0.0)
            api.CreateCenterOfMassAttr(Gf.Vec3f(0.0, 0.0, 0.0))
            api.CreateDiagonalInertiaAttr(Gf.Vec3f(0.0, 0.0, 0.0))
            api.CreatePrincipalAxesAttr(Gf.Quatf(1.0))
        points, tris = _triangles(UsdGeom.Mesh(prim))
        local = _apply(_matrix(to_body), points)
        hulls = decompose(np.ascontiguousarray(local), tris, params, cache_dir)
        to_root = np.asarray(_matrix(cache.GetLocalToWorldTransform(body) * root_inv))
        scope_path = body.GetPath().AppendChild(HULL_SCOPE)
        UsdGeom.Scope.Define(out, scope_path)
        kept, dropped = 0, []
        for index, vertices in enumerate(hulls):
            extent = vertices.max(0) - vertices.min(0)
            if extent.min() < PLANAR_MIN_EXTENT_M:
                dropped.append({"index": index, "extent_m": extent.round(5).tolist()})
                continue
            verts, faces = _hull_faces(vertices)
            mesh = UsdGeom.Mesh.Define(out, scope_path.AppendChild(f"{prim.GetName()}_hull_{index:03d}"))
            mesh.CreatePointsAttr([Gf.Vec3f(*map(float, v)) for v in verts])
            mesh.CreateFaceVertexCountsAttr([3] * len(faces))
            mesh.CreateFaceVertexIndicesAttr([i for f in faces for i in f])
            mesh.CreatePurposeAttr(UsdGeom.Tokens.guide)
            UsdPhysics.CollisionAPI.Apply(mesh.GetPrim())
            UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr("convexHull")
            material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial("physics")[0]
            if material:
                UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(
                    material, UsdShade.Tokens.weakerThanDescendants, "physics")
            in_root = _apply(to_root, verts) * meters
            lo, hi = np.minimum(lo, in_root.min(0)), np.maximum(hi, in_root.max(0))
            kept += 1
        if kept == 0:
            raise ValueError(f"{prim.GetPath()}: every convex piece was planar")
        report["bodies"].setdefault(prim_ref(body.GetPath()), {"colliders": []})["colliders"].append(
            {"mesh": prim_ref(prim.GetPath()), "hulls": kept, "dropped_planar": dropped})
    for body, parts in body_parts.items():
        prim = out.OverridePrim(body)
        authored = _mass_properties(stage.GetPrimAtPath(body), UsdPhysics)
        total_mass = float(authored["mass_kg"] or sum(float(p["mass_kg"]) for p in parts))
        if authored["diagonal_inertia_kg_m2"] is not None:
            effective, source = authored, authored["properties_source"]
        else:
            usable = [p for p in parts if p["diagonal_inertia_kg_m2"] is not None]
            effective, source = aggregate_inertia(usable), "aggregated" if usable else None
        api = UsdPhysics.MassAPI.Apply(prim)
        api.CreateMassAttr(total_mass)
        entry = report["bodies"][prim_ref(body)]
        entry["mass_kg"] = total_mass
        entry["properties_source"] = source
        if effective is not None:
            center = [float(v) for v in effective["center_of_mass_m"]]
            diagonal = [float(v) for v in effective["diagonal_inertia_kg_m2"]]
            axes = [float(v) for v in effective["principal_axes_xyzw"]]
            check_diagonal_inertia(diagonal, prim_ref(body))
            api.CreateCenterOfMassAttr(Gf.Vec3f(*center))
            api.CreateDiagonalInertiaAttr(Gf.Vec3f(*diagonal))
            api.CreatePrincipalAxesAttr(Gf.Quatf(axes[3], Gf.Vec3f(*axes[:3])))
            entry["center_of_mass_m"] = [round(v * meters, 9) for v in center]
            entry["diagonal_inertia_kg_m2"] = [round(v * meters ** 2, 9) for v in diagonal]
            entry["principal_axes_xyzw"] = [round(v, 9) for v in axes]
    if root.HasAPI(UsdPhysics.ArticulationRootAPI) or any(
            p.HasAPI(UsdPhysics.ArticulationRootAPI) for p in Usd.PrimRange(root, Usd.TraverseInstanceProxies())):
        report["articulation"] = True
    # 物理实际接触的是凸包，不是渲染网格：布局的支撑高度应以这个包围盒为准（资产根坐标系、关节零位、米）
    report["hull_bbox_min_m"] = lo.round(5).tolist()
    report["hull_bbox_max_m"] = hi.round(5).tolist()
    out.GetRootLayer().Export(str(target))
    report["wrapper"] = target.name
    return report


def fixed_base_collision_bbox(usd_path: Path) -> dict[str, Any]:
    """机器人基座刚体（ArticulationRootAPI 所在刚体）上全部碰撞体的包围盒，基座坐标系、米。

    AIRBOT 的安装板碰撞体在基座原点以下；放到桌面上时支撑高度要按它的底面算，不能按基座原点。
    """
    import numpy as np
    _, _, Usd, UsdGeom, UsdPhysics, _ = _pxr()
    stage = Usd.Stage.Open(str(usd_path))
    roots = [p for p in Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies())
             if p.HasAPI(UsdPhysics.ArticulationRootAPI) and p.HasAPI(UsdPhysics.RigidBodyAPI)]
    if len(roots) != 1:
        raise ValueError(f"{Path(usd_path).name}: expected one rigid articulation root, found {len(roots)}")
    base = roots[0]
    cache = UsdGeom.XformCache()
    base_inv = cache.GetLocalToWorldTransform(base).GetInverse()
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    lo, hi, colliders = np.full(3, np.inf), np.full(3, -np.inf), []
    for prim in Usd.PrimRange(base, Usd.TraverseInstanceProxies()):
        if not (prim.HasAPI(UsdPhysics.CollisionAPI) and prim.IsA(UsdGeom.Mesh)):
            continue
        if _rigid_body_of(prim, UsdPhysics) != base:
            continue
        points, _ = _triangles(UsdGeom.Mesh(prim))
        local = _apply(_matrix(cache.GetLocalToWorldTransform(prim) * base_inv), points) * meters
        lo, hi = np.minimum(lo, local.min(0)), np.maximum(hi, local.max(0))
        colliders.append(prim_ref(prim.GetPath()))
    if not colliders:
        raise ValueError(f"{Path(usd_path).name}: articulation root body has no colliders")
    return {"base_body": prim_ref(base.GetPath()), "colliders": colliders,
            "bbox_min_m": lo.round(5).tolist(), "bbox_max_m": hi.round(5).tolist()}


def anchor_to_world(wrapper: Path, body: str, joint_name: str = "datafarm_world_anchor") -> None:
    """给铰接资产的基座刚体（`body` 为 `prim_ref` 形式）加固定关节，body0 指向资产根 Xform（Newton 视为世界）。

    SimReady 的 ArticulationRootAPI 在 Xform 根上，Isaac Lab 的 `fix_root_link` 对非刚体根报错；
    这里照搬已验收 AIRBOT 资产 `root_joint` 的写法，资产在场景中的位姿仍由 init_state 决定。
    """
    _, Sdf, Usd, _, UsdPhysics, _ = _pxr()
    stage = Usd.Stage.Open(str(wrapper))
    root = stage.GetDefaultPrim()
    joint = UsdPhysics.FixedJoint.Define(stage, root.GetPath().AppendChild(joint_name))
    joint.CreateBody0Rel().SetTargets([root.GetPath()])
    joint.CreateBody1Rel().SetTargets([Sdf.Path("/" + body.lstrip("/"))])
    stage.GetRootLayer().Save()
