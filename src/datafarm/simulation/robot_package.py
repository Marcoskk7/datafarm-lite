"""随任务输入提供的机器人描述包：安全解包与 URDF 检查。

不属于部署硬件资产的机器人会以 URDF 包的形式到达（.zip 或文件夹，内含 .urdf
及其网格，有时还带手眼标定）。本模块在不信任归档内容的前提下解包它，并
报告 agent 在 grounding 场景之前所需的信息：主 URDF、其 link 与 joint、无
限位的驱动 joint、运动学根、引用了未定义 link 的 joint，以及无法在包内解析的网格文件。
该报告是对包本身的证据，而不是对机器人的证据：grounding 仍会对照真实视图进行验证。
"""
from __future__ import annotations

import shutil
import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

MAX_UNPACKED_BYTES = 3 * 1024 ** 3
MAX_ENTRIES = 50_000
MAX_URDF_BYTES = 50 * 1024 ** 2
ACTUATED = ("revolute", "continuous", "prismatic")
SKIPPED = ("__MACOSX",)


class PackageError(ValueError):
    pass


def safe_extract(archive: Path, dest: Path) -> list[str]:
    """将 zip 解包到 dest：不允许绝对路径或父目录路径，不允许符号链接，大小与条目数有界。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written, total = [], 0
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise PackageError(f"{archive}: not a zip file ({e})") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageError(f"{archive}: {len(infos)} entries, more than {MAX_ENTRIES}")
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise PackageError(f"{archive}: unsafe path {name[:120]!r}")
            if not parts or parts[0] in SKIPPED or parts[-1] == ".DS_Store":
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                continue   # 符号链接可能指向包外部
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise PackageError(f"{archive}: unpacks to more than {MAX_UNPACKED_BYTES // 1024 ** 3} GB")
            target = dest.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append("/".join(parts))
    return written


def _resolve_mesh(uri: str, urdf: Path, root: Path, files: list[Path]) -> Path | None:
    """在包根内解析得到的网格 URI，若无法解析则为 None。"""
    if uri.startswith("package://"):
        rest = uri[len("package://"):]
        package, _, tail = rest.partition("/")
        if not tail:
            return None
        for d in [p for p in root.rglob(package) if p.is_dir()] + [root]:
            cand = d / tail
            if cand.is_file():
                return cand
        hits = [f for f in files if f.as_posix().endswith("/" + tail)]   # 打包成 zip 时包文件夹被重命名
        return hits[0] if len(hits) == 1 else None
    if uri.startswith("file://"):
        name = Path(uri[len("file://"):]).name
        hits = [f for f in files if f.name == name]
        return hits[0] if len(hits) == 1 else None
    cand = (urdf.parent / uri).resolve()
    return cand if cand.is_file() and root.resolve() in cand.parents else None


def urdf_summary(urdf: Path, root: Path) -> dict:
    """单个 URDF 的 link、joint、根 link、缺失限位与网格解析情况；`problems` 列出阻碍使用的问题。"""
    urdf, root = Path(urdf), Path(root)
    if urdf.stat().st_size > MAX_URDF_BYTES:
        return {"urdf": str(urdf.relative_to(root)), "problems": [f"larger than {MAX_URDF_BYTES >> 20} MB"]}
    try:
        robot = ET.parse(urdf).getroot()
    except ET.ParseError as e:
        return {"urdf": str(urdf.relative_to(root)), "problems": [f"XML parse error: {e}"]}
    if robot.tag != "robot":
        return {"urdf": str(urdf.relative_to(root)), "problems": [f"root element is <{robot.tag}>, not <robot>"]}
    links = [link.get("name") for link in robot.findall("link")]
    joints = robot.findall("joint")
    problems = []
    if len(set(links)) != len(links):
        problems.append("duplicate link names")
    children, types = set(), {}
    no_limit = []
    for j in joints:
        kind = j.get("type", "?")
        types[kind] = types.get(kind, 0) + 1
        parent, child = j.find("parent"), j.find("child")
        p, c = (parent.get("link") if parent is not None else None), (child.get("link") if child is not None else None)
        for side, link in (("parent", p), ("child", c)):
            if link not in links:
                problems.append(f"joint {j.get('name')} {side} link {link!r} is not defined")
        children.add(c)
        limit = j.find("limit")
        if kind in ("revolute", "prismatic") and (limit is None or limit.get("lower") is None
                                                 or limit.get("upper") is None):
            no_limit.append(j.get("name"))
    roots = [link for link in links if link not in children]
    if len(roots) != 1:
        problems.append(f"{len(roots)} root links {roots[:5]} (a tree has exactly one)")
    files = [f for f in root.rglob("*") if f.is_file()]
    uris = sorted({m.get("filename", "") for m in robot.iter("mesh")})
    missing = [u for u in uris if _resolve_mesh(u, urdf, root, files) is None]
    if missing:
        problems.append(f"{len(missing)} mesh file(s) not found in the package")
    actuated = [j.get("name") for j in joints if j.get("type") in ACTUATED]
    return {"urdf": str(urdf.relative_to(root)), "robot_name": robot.get("name"), "links": len(links),
            "joints": len(joints), "joint_types": types, "actuated": len(actuated), "actuated_names": actuated,
            "root_link": roots[0] if len(roots) == 1 else None, "joints_without_limits": no_limit,
            "meshes": len(uris), "missing_meshes": missing[:50], "problems": problems}


def check_package(path: Path, extract_to: Path | None = None) -> dict:
    """报告一个 URDF 包（.zip、文件夹或单个 .urdf）。zip 会先解包到 extract_to。"""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        if extract_to is None:
            raise PackageError("a .zip package needs --extract-to (the folder it is unpacked into)")
        safe_extract(path, extract_to)
        root = Path(extract_to)
    elif path.is_dir():
        root = path
    elif path.suffix.lower() == ".urdf":
        root = path.parent
    else:
        raise PackageError(f"{path}: give a .zip, a folder or a .urdf")
    urdfs = [path] if path.suffix.lower() == ".urdf" else sorted(root.rglob("*.urdf"))
    if not urdfs:
        xacro = sorted(root.rglob("*.xacro"))
        raise PackageError("no .urdf in the package" + (" (only .xacro: expand it with xacro first)" if xacro else ""))
    summaries = [urdf_summary(u, root) for u in urdfs]
    main = max(summaries, key=lambda s: (not s["problems"], s.get("links", 0), -len(s["urdf"])))
    calibration = sorted(str(f.relative_to(root)) for f in root.rglob("*")
                         if f.is_file() and any(k in f.name.lower() for k in ("calib", "hand_eye", "handeye", "标定")))
    return {"schema": "datafarm.robot-package-check/1", "root": str(root.resolve()), "main": main["urdf"],
            "ok": not main["problems"], "urdfs": summaries, "calibration_files": calibration}
