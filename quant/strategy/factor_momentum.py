"""动量因子策略：因子库用法示例。

与 DoubleMAStrategy 的本质区别：on_bar 不再手写 rolling 指标，
而是声明 required_factors（服务层自动解析依赖、预热计算并注入），
策略只关心信号语义——"动量为正持有，为负清仓"。

因子值在回测起点前已由预热窗口（lookback）算好，第一天即有效。
"""
from __future__ import annotations

import math

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar


class FactorMomentumStrategy(Strategy):
    params = {"window": 20, "threshold": 0.0, "buy_ratio": 0.95}

    def __init__(self, window: int = 20, threshold: float = 0.0,
                 buy_ratio: float = 0.95):
        self.window = window
        self.threshold = threshold
        self.buy_ratio = buy_ratio
        # 实例级声明（参数决定因子名，如 momentum_20）
        self.required_factors = [f"momentum_{window}"]
        self.params = {"window": window, "threshold": threshold,
                       "buy_ratio": buy_ratio}

    def on_init(self, ctx: StrategyContext) -> None:
        pass  # 无状态：因子值由 ctx 即时读取

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        v = ctx.factor(f"momentum_{self.window}")
        if math.isnan(v):
            return                          # 预热区无因子值（理论上已被 lookback 消除）

        pos = ctx.portfolio.position(bar.code)
        held = pos is not None and pos.quantity > 0

        if v > self.threshold and not held:
            ctx.buy(bar.code, self.buy_ratio)
        elif v < -self.threshold and held:
            ctx.sell(bar.code, 1.0)
