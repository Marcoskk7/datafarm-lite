"""刚体惯量的合成与校验：平行轴定理、坐标变换、张量对角化、三角不等式。"""
import math

import numpy as np
import pytest

from datafarm.adapters.sim.usd_assets import aggregate_inertia, check_diagonal_inertia, quat_matrix

IDENTITY = [0.0, 0.0, 0.0, 1.0]
NO_SHIFT = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def part(mass, com, diagonal, axes=IDENTITY, to_body=NO_SHIFT):
    return {"mass_kg": mass, "center_of_mass_m": list(com), "diagonal_inertia_kg_m2": list(diagonal),
            "principal_axes_xyzw": list(axes), "to_body": to_body}


def test_offset_point_masses_pick_up_parallel_axis_terms():
    """两个各 1 kg 的质点放在 x=±1：绕各自质心无惯量，合成后是 (0, 2, 2)。"""
    result = aggregate_inertia([part(1.0, (1, 0, 0), (0, 0, 0)), part(1.0, (-1, 0, 0), (0, 0, 0))])
    assert result["mass_kg"] == 2.0
    assert result["center_of_mass_m"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["diagonal_inertia_kg_m2"] == pytest.approx([0.0, 2.0, 2.0], abs=1e-12)


def test_collider_transform_moves_its_centre_of_mass_into_the_body_frame():
    shift = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0.5, 0, 0, 1]]  # USD 行向量：平移在最后一行
    result = aggregate_inertia([part(2.0, (0, 0, 0), (1, 1, 1), to_body=shift)])
    assert result["center_of_mass_m"] == pytest.approx([0.5, 0.0, 0.0])
    assert result["diagonal_inertia_kg_m2"] == pytest.approx([1.0, 1.0, 1.0])


def test_rotated_principal_axes_are_recovered_as_the_same_tensor():
    """绕 z 转 45° 的盒子：特征值不变，四元数重建出的张量与输入一致。"""
    angle = math.pi / 4
    axes = [0.0, 0.0, math.sin(angle / 2), math.cos(angle / 2)]
    diagonal = [1.0, 2.0, 3.0]
    result = aggregate_inertia([part(1.0, (0, 0, 0), diagonal, axes=axes)])
    assert result["diagonal_inertia_kg_m2"] == pytest.approx(diagonal)
    rotation = quat_matrix(result["principal_axes_xyzw"])
    reconstructed = rotation @ np.diag(result["diagonal_inertia_kg_m2"]) @ rotation.T
    expected = quat_matrix(axes) @ np.diag(diagonal) @ quat_matrix(axes).T
    assert reconstructed == pytest.approx(expected, abs=1e-12)


def test_inconsistent_diagonal_inertia_is_rejected():
    with pytest.raises(ValueError, match="triangle inequality"):
        check_diagonal_inertia([1.0, 1.0, 10.0], "object.table")
    with pytest.raises(ValueError, match="non-negative"):
        check_diagonal_inertia([1.0, -1.0, 1.0], "object.table")
    check_diagonal_inertia([1.0, 2.0, 3.0], "object.table")


def test_a_part_with_invalid_inertia_fails_the_whole_aggregation():
    with pytest.raises(ValueError, match="triangle inequality"):
        aggregate_inertia([part(1.0, (0, 0, 0), (1.0, 1.0, 10.0))])


class _GfQuat:
    """标准 `physics:principalAxes` 的 `Gf.Quatf` 形状。"""

    def __init__(self, x, y, z, w):
        self._imaginary, self._real = (x, y, z), w

    def GetImaginary(self):
        return self._imaginary

    def GetReal(self):
        return self._real


def test_principal_axes_accept_usd_quaternions_and_legacy_wxyz_vectors():
    from datafarm.adapters.sim.usd_assets import _quat_xyzw
    assert _quat_xyzw([1.0, 0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0, 1.0]   # Isaac 旧属性：wxyz
    assert _quat_xyzw(_GfQuat(0.0, 0.0, 0.0, 1.0)) == [0.0, 0.0, 0.0, 1.0]
