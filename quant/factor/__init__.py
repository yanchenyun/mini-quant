"""因子库（v0.3）：行情 → 因子 → 存库/回测。

- base.py    注册表（name → Factor 实例）
- builtin.py 内置因子集（momentum / volatility / bias / volume_ratio）
- engine.py  FactorEngine：行情宽表 → 因子宽表（纯计算，不碰存储）

核心原则：因子计算逻辑全平台只有
compute 一份（SSOT）；回测内存即时计算，落库仅为物化缓存。
"""
from .base import available, get, register
from .engine import FactorEngine
from . import builtin  # noqa: F401  import 即注册全部内置因子

__all__ = ["available", "get", "register", "FactorEngine"]
