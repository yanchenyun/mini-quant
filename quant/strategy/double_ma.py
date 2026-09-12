"""双均线策略：快线上穿慢线买入，下穿卖出（全仓）。

写新策略 = 继承 Strategy + 实现两个钩子，引擎/数据/撮合零改动。
"""
from __future__ import annotations

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar


class DoubleMAStrategy(Strategy):
    params = {"fast": 5, "slow": 20, "buy_ratio": 0.95}

    def __init__(self, fast: int = 5, slow: int = 20, buy_ratio: float = 0.95):
        if fast >= slow:
            raise ValueError(f"快线周期须小于慢线: fast={fast}, slow={slow}")
        self.fast, self.slow, self.buy_ratio = fast, slow, buy_ratio
        self.params = {"fast": fast, "slow": slow, "buy_ratio": buy_ratio}

    def on_init(self, ctx: StrategyContext) -> None:
        pass  # 本策略无状态需要预热（均线由 history 即时计算）

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        hist = ctx.history
        if len(hist) < self.slow + 1:      # 需要前一日的均线判断交叉
            return

        ma = hist.rolling(self.fast).mean()
        mb = hist.rolling(self.slow).mean()
        f0, s0 = ma.iloc[-1], mb.iloc[-1]     # t 日
        f1, s1 = ma.iloc[-2], mb.iloc[-2]     # t-1 日

        pos = ctx.portfolio.position(bar.code)
        held = pos is not None and pos.quantity > 0

        golden = f1 <= s1 and f0 > s0        # 金叉
        death = f1 >= s1 and f0 < s0          # 死叉

        if golden and not held:
            ctx.buy(bar.code, self.buy_ratio)
        elif death and held:
            ctx.sell(bar.code, 1.0)
