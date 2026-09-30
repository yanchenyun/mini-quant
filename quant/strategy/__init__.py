"""策略层（Strategy Layer）—— 用户写策略信号的地方。

策略只依赖 core.abstractions.StrategyContext 窄接口（ISP）：ctx.history
是截至当前 bar（含）的收盘价序列；ctx.factor(name) 是当前 bar 的因子值
（NaN 表示预热区无值）；ctx.factor_history(name) 是截至当前 bar 的因子值
序列；ctx.portfolio 是账户只读视图；ctx.buy / ctx.sell 按现金比例买入、
按持仓比例卖出（T+1 仅 available）。

策略频率无关——同一份代码在日线 / 5 分钟线上直接可用，rolling 指标天然
按 bar 计算。

注册与自动发现
--------------
新策略 = 在本包下新建一个文件：写 Strategy 子类 + 文件尾部
register_strategy(StrategySpec(...)) 一行。本包导入时自动遍历同目录的
全部模块（见 _autodiscover），注册随之生效——cli.py 的 --strategy 选项
与策略参数、webapp 的策略下拉与参数输入框、GET /api/strategies 全部由
注册表动态生成。新增策略不需要再改动任何入口代码；注册写法示例见任一
内置策略文件的文件尾。

防未来函数（4 道保险之策略侧）
------------------------------
ctx.history 只到当前 bar（含），无法读未来；ctx.factor() 由引擎 iloc[:n]
切片注入，结构上排除未来行；策略在当前 bar 收盘产生信号，订单提交后在次一
bar 开盘价成交（信号当根不可能成交）——本层不认识具体撮合实现。

参见
----
开发文档（架构 / 新策略步骤 / 设计决策）：docs/DEVELOPMENT.md §8；
操作文档（启动 / 运维 / 排错）：根目录 ReadMe.md。
"""
from __future__ import annotations

import importlib
import pkgutil

from .registry import (ParamSpec, StrategySpec, available_strategies,
                       build_strategy, get_strategy, register_strategy)


def _autodiscover() -> None:
    """导入本包下的全部策略模块——各模块文件尾的注册随之执行。

    显式排除 registry 自身（它是注册机制的载体，不是策略）。
    """
    for mod in pkgutil.iter_modules(__path__):
        if mod.name != "registry":
            importlib.import_module(f"{__name__}.{mod.name}")


_autodiscover()

__all__ = ["ParamSpec", "StrategySpec", "available_strategies",
           "build_strategy", "get_strategy", "register_strategy"]
