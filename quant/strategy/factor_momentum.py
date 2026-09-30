"""动量因子策略：因子库用法示例。

与 DoubleMAStrategy 的本质区别：on_bar 不再手写 rolling 指标，
而是声明 required_factors（服务层自动解析依赖、预热计算并注入），
策略只关心信号语义。

信号逻辑：
- 动量值 > threshold 且未持仓 → 买入
- 动量值 < -threshold 且持仓 → 卖出
- threshold=0 时：动量为正买入，为负卖出，无死区
- threshold>0 时：[-threshold, threshold] 为持仓不变死区

因子值在回测起点前已由预热窗口（lookback）算好，第一天即有效。
"""
from __future__ import annotations

import math

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar
from .registry import ParamSpec, StrategySpec, register_strategy


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


# ── 注册：CLI 选项、Web 下拉与参数输入框由此自动生成 ─────────────────────────
# window 限定为已注册动量因子的规格（名字即规格），构造期即拦截非法窗口。
register_strategy(StrategySpec(
    name="factor_momentum", cls=FactorMomentumStrategy, label="动量因子",
    params=(ParamSpec("window", int, 20, choices=(20, 60),
                      help="动量窗口（已注册规格 20/60）"),
            ParamSpec("threshold", float, 0.0, help="信号死区阈值"),
            ParamSpec("buy_ratio", float, 0.95, help="买入动用的现金比例")),
    describe=lambda p: f"momentum_{p['window']}"))
