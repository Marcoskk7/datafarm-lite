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
from collections.abc import Mapping, Sequence
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
    """证据里的 prim 路径去掉开头 `/`（kernel 把 `/...` 当作宿主绝对路径拒收）；`RootNode/Geometry/...`。"""
    return str(path).lstrip("/")


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
            mass = UsdPhysics.MassAPI(prim).GetMassAttr().Get() if prim.HasAPI(UsdPhysics.MassAPI) else None
            pose = cache.GetLocalToWorldTransform(prim) * root_inv
            bodies[prim_ref(prim.GetPath())] = {"mass_kg": float(mass) if mass else None,
                                          "kinematic": bool(UsdPhysics.RigidBodyAPI(prim).GetKinematicEnabledAttr().Get()),
                                          "pose": _matrix(pose)}
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
    body_mass: dict[str, float] = {}
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
        mass = UsdPhysics.MassAPI(prim).GetMassAttr().Get() if prim.HasAPI(UsdPhysics.MassAPI) else None
        if mass:
            body_mass[str(body.GetPath())] = body_mass.get(str(body.GetPath()), 0.0) + float(mass)
            UsdPhysics.MassAPI.Apply(original).CreateMassAttr(0.0)
        points, tris = _triangles(UsdGeom.Mesh(prim))
        to_body = cache.GetLocalToWorldTransform(prim) * cache.GetLocalToWorldTransform(body).GetInverse()
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
    for body, mass in body_mass.items():
        prim = out.OverridePrim(body)
        authored = stage.GetPrimAtPath(body)
        existing = UsdPhysics.MassAPI(authored).GetMassAttr().Get() if authored.HasAPI(UsdPhysics.MassAPI) else None
        value = float(existing) if existing else mass
        UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(value)
        report["bodies"][prim_ref(body)]["mass_kg"] = value
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


def robot_collision_wrapper(source: Path, target: Path, activate_off: Sequence[str]) -> dict[str, Any]:
    """写机器人碰撞包装层：sublayer 原资产，把夹具五金的碰撞 prim `active = false`，视觉保持不变。

    为什么需要它（2026-11-19 在 lab30 实测）：机器人包的安装板碰撞带 `physics:approximation = convexHull`，
    C 型夹的开口被这一个凸包填实，基座刚体因此是一块 0.19×0.28×0.118 m 的实体；安装板一旦贴在桌面上，
    这块实体就整块穿进桌面（实测 84.6 mm，并弹飞旁边 0.2 m 处的盘子）。反过来把夹具底面当支撑面，
    就是“机械臂悬空 10.3 cm”。AIRBOT 的基座刚体已经用 `root_joint` 焊到世界，关掉五金碰撞不影响动力学。
    """
    _, _, Usd, UsdGeom, UsdPhysics, _ = _pxr()
    stage = Usd.Stage.Open(str(source))
    root = stage.GetDefaultPrim()
    if root is None:
        raise ValueError(f"{Path(source).name}: no default prim")
    lines = ["#usda 1.0", "(", f'    defaultPrim = "{root.GetName()}"', "    subLayers = [", f"        @{source}@",
             "    ]", ")", "", f'over "{root.GetName()}"', "{"]
    for prim in activate_off:
        segments = [segment for segment in prim.strip("/").split("/") if segment]
        if not segments:
            raise ValueError(f"{Path(source).name}: empty hardware collider prim")
        full = root.GetPath().AppendPath("/".join(segments))
        if not stage.GetPrimAtPath(full):
            raise ValueError(f"{Path(source).name}: hardware collider prim {prim!r} does not exist")
        for index, segment in enumerate(segments):
            pad = "    " * (index + 1)
            if index == len(segments) - 1:  # active 是 prim metadata，写在最深那一层的括号里
                lines.extend([pad + 'over "' + segment + '" (', pad + "    active = false", pad + ")", pad + "{"])
            else:
                lines.extend([pad + 'over "' + segment + '"', pad + "{"])
        for index in reversed(range(len(segments))):
            lines.append("    " * (index + 1) + "}")
    lines.append("}")
    Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
    removed = {}
    for prim in activate_off:
        subtree = stage.GetPrimAtPath(root.GetPath().AppendPath(prim.strip("/")))
        removed[prim] = sum(1 for p in Usd.PrimRange(subtree, Usd.TraverseInstanceProxies())
                            if p.HasAPI(UsdPhysics.CollisionAPI) and p.IsA(UsdGeom.Mesh))
        check_stage = Usd.Stage.Open(str(target))
        composed = check_stage.GetPrimAtPath(root.GetPath().AppendPath(prim.strip("/")))
        if composed is None or composed.IsActive():
            raise ValueError(f"{Path(target).name}: {prim!r} is still active after deactivation")
    if not any(removed.values()):
        raise ValueError(f"{Path(target).name}: {activate_off} carry no collision mesh")
    return {"schema": WRAPPER_SCHEMA, "source_layer": Path(source).name, "wrapper": Path(target).name,
            "deactivated": removed}


def anchor_to_world(wrapper: Path, body: str, joint_name: str = "datafarm_world_anchor") -> dict[str, Any]:
    """把铰接资产的基座刚体（`body`，`prim_ref` 形式）锚到世界，并补齐缺失的 articulation 根 API。

    - 固定关节：body0 指向资产根 Xform（Newton 视为世界），照搬已验收 AIRBOT 资产 `root_joint` 的写法；
      资产在场景中的位姿仍由 init_state 决定。
    - 有些 SimReady 铰接资产只写了 `PhysicsPrismaticJoint` 和刚体，没有 `ArticulationRootAPI`
      （实测 `cabinet_file_d04`）。Isaac Lab 3.0 + Newton 按 prim 路径解析铰接根，缺它的资产 spawn 时报
      “No articulation-root targets matched …”。这里在基座刚体上补 `PhysicsArticulationRootAPI` 与
      `NewtonArticulationRootAPI`，与已验证的 AIRBOT / fixture 资产写法一致；补过什么写进返回证据。
    """
    _, Sdf, Usd, _, UsdPhysics, _ = _pxr()
    stage = Usd.Stage.Open(str(wrapper))
    root = stage.GetDefaultPrim()
    body_path = Sdf.Path("/" + body.lstrip("/"))
    joint = UsdPhysics.FixedJoint.Define(stage, root.GetPath().AppendChild(joint_name))
    joint.CreateBody0Rel().SetTargets([root.GetPath()])
    joint.CreateBody1Rel().SetTargets([body_path])
    authored = []
    if not any(p.HasAPI(UsdPhysics.ArticulationRootAPI)
               for p in Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies())):
        prim = stage.GetPrimAtPath(body_path)
        if not prim or not prim.IsValid():
            raise ValueError(f"{Path(wrapper).name}: anchor body {body!r} does not exist")
        UsdPhysics.ArticulationRootAPI.Apply(prim)
        prim.AddAppliedSchema("NewtonArticulationRootAPI")
        authored.append(prim_ref(body_path))
    stage.GetRootLayer().Save()
    return {"world_anchor_body": prim_ref(body_path), "authored_articulation_root": authored}
