"""双均线策略：快线上穿慢线买入，下穿卖出（全仓）。

写新策略 = 继承 Strategy + 实现两个钩子 + 文件尾注册一行，
引擎/数据/撮合/CLI/Web 全部零改动。
"""
from __future__ import annotations

import pandas as pd

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar
from .registry import ParamSpec, StrategySpec, register_strategy


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

        # 容差交叉检测：避免浮点精度问题导致信号丢失
        # 当两均线差值在容差范围内时视为“无交叉”，防止 f1≈s1 时
        # f1<=s1 与 f1>=s1 同时为 True 但 f0>s0 与 f0<s0 同时为 False
        eps = 1e-8
        golden = (f1 - s1 < eps) and (f0 - s0 > eps)   # 快线从下方穿越慢线
        death  = (s1 - f1 < eps) and (s0 - f0 > eps)   # 快线从上方穿越慢线

        if golden and not held:
            ctx.buy(bar.code, self.buy_ratio)
        elif death and held:
            ctx.sell(bar.code, 1.0)


def _ma_overlay(bars: pd.DataFrame, params: dict) -> dict[str, pd.Series]:
    """K 线辅助线钩子：快慢两条均线（Web 端自动渲染）。"""
    close = bars["close"].astype(float)
    return {f"MA{params['fast']}": close.rolling(params["fast"]).mean(),
            f"MA{params['slow']}": close.rolling(params["slow"]).mean()}


# ── 注册：CLI 选项、Web 下拉与参数输入框由此自动生成 ─────────────────────────
register_strategy(StrategySpec(
    name="double_ma", cls=DoubleMAStrategy, label="双均线",
    params=(ParamSpec("fast", int, 5, help="快线周期（按 bar 计）"),
            ParamSpec("slow", int, 20, help="慢线周期"),
            ParamSpec("buy_ratio", float, 0.95, help="买入动用的现金比例")),
    overlay=_ma_overlay,
    describe=lambda p: f"MA{p['fast']}/{p['slow']}"))
