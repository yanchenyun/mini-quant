"""因子注册表：新因子 = 写一个 Factor 实现 + register 一行，其余零改动（OCP）。

参数化因子直接注册多个实例（如 momentum_20 / momentum_60），名字即规格。
"""
from __future__ import annotations

from ..core.abstractions import Factor

_REGISTRY: dict[str, Factor] = {}


def register(factor: Factor) -> Factor:
    """注册因子实例（name 冲突立即报错，避免静默覆盖）。"""
    if factor.name in _REGISTRY:
        raise ValueError(f"因子重复注册: {factor.name}")
    _REGISTRY[factor.name] = factor
    return factor


def get(name: str) -> Factor:
    if name not in _REGISTRY:
        raise KeyError(f"未知因子: {name}（可用: {available()}）")
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY)
