"""因子注册表：新因子 = 写一个 Factor 实现 + register 一行，其余零改动（OCP）。

参数化因子直接注册多个实例（如 momentum_20 / momentum_60），名字即规格。
"""
from __future__ import annotations

from ..core.abstractions import Factor

# 全局因子注册表：name → Factor 实例，由 builtin.py 启动时填充
_REGISTRY: dict[str, Factor] = {}


def register(factor: Factor) -> Factor:
    """注册因子实例。name 冲突时抛 ValueError（避免静默覆盖）。"""
    if factor.name in _REGISTRY:
        raise ValueError(f"因子重复注册: {factor.name}")
    _REGISTRY[factor.name] = factor
    return factor


def get(name: str) -> Factor:
    """按名字查找因子；未注册时抛 KeyError 并列出可用因子。"""
    if name not in _REGISTRY:
        raise KeyError(f"未知因子: {name}（可用: {available()}）")
    return _REGISTRY[name]


def available() -> list[str]:
    """返回已注册因子名列表（按字母序）。"""
    return sorted(_REGISTRY)
