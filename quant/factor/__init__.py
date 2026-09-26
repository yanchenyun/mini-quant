"""因子层（Factor Layer，v0.3 新增）—— 行情 → 因子，纯计算不碰存储。

核心原则：SSOT（Single Source of Truth，单一事实来源）。因子计算逻辑全平台
只有 ``Factor.compute`` 一份；**回测内存即时计算**（与行情同帧同口径，永远是
权威值），落库 ``dwd_factor_value_i`` 长表仅为"物化缓存"——供选股 / 因子
分析等非回测场景读取，回测永远走内存计算，规避"库里旧口径 vs 回测新口径"漂移。

模块划分
--------
- ``base``     因子注册表：``register`` / ``get`` / ``available``。新因子 =
               一个 ``Factor`` 子类 + 一行 ``register()``。
- ``builtin``  内置因子集：``Momentum`` / ``Volatility`` / ``Bias`` /
               ``VolumeRatio``。全部 rolling / shift 因果计算（t 行只依赖 ≤ t
               的数据），天然防未来函数。注册了 5 个实例：
               ``momentum_20`` / ``momentum_60`` / ``volatility_20`` /
               ``bias_20`` / ``volume_ratio_5_20``。
- ``engine``   ``FactorEngine.compute(bars, names)``：行情宽表 → 因子宽表
               ``[code, dt, <因子列…>]``。纯计算函数，不碰存储，可离线单测。

契约与防未来
------------
- ``Factor`` 契约（写新因子必须遵守）：
  1. 只允许因果计算（``rolling`` / ``shift``），t 行的值只依赖 ≤ t 的数据；
  2. 历史不足时对应行为 NaN（不抛错），由策略侧判断处理；
  3. ``min_periods`` = 产出首个非 NaN 值所需的最少 bar 数（引擎据此计算预热窗口）。
- ``FactorAccessor`` 防未来：引擎逐 bar 用 ``iloc[:n]`` 切片构造访问器，
  策略在结构上不可能看到当前 bar 之后的因子行。

参见
----
- 开发文档（架构 / 新因子步骤 / SSOT 设计决策）：``docs/DEVELOPMENT.md``
- 操作文档（启动 / 运维 / 排错）：根目录 ``ReadMe.md``
"""
from .base import available, get, register
from .engine import FactorEngine
from . import builtin  # noqa: F401  导入即注册全部内置因子

__all__ = ["available", "get", "register", "FactorEngine"]