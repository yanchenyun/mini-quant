"""内置因子集（纯时序因子：t 行只依赖 ≤t 的数据，天然防未来）。

日线/5 分钟通用：按 bar 计算，语义随频率自然变化
（日线 momentum_20 是月动量，5 分钟 momentum_20 是日内动量）。
"""
from __future__ import annotations

import pandas as pd

from ..core.abstractions import Factor
from .base import register


class Momentum(Factor):
    """动量（Momentum）：N 期收益率。min_periods = N+1（shift 需要 N 期历史）。"""

    def __init__(self, window: int):
        self.window = window
        self.name = f"momentum_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        return close / close.shift(self.window) - 1.0


class Volatility(Factor):
    """已实现波动率（Realized Volatility）：N 期收益率标准差。"""

    def __init__(self, window: int):
        self.window = window
        self.name = f"volatility_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ret = bars["close"].astype(float).pct_change()
        return ret.rolling(self.window).std()


class Bias(Factor):
    """乖离率（Bias / BIAS）：收盘价偏离 N 期均线的比例。"""

    def __init__(self, window: int):
        self.window = window
        self.name = f"bias_{window}"
        self.min_periods = window

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        ma = close.rolling(self.window).mean()
        return (close - ma) / ma


class VolumeRatio(Factor):
    """量比（Volume Ratio）：短期均量 / 长期均量，衡量放量程度。"""

    def __init__(self, short: int, long: int):
        if short >= long:
            raise ValueError(f"短期窗口须小于长期: {short} >= {long}")
        self.short, self.long = short, long
        self.name = f"volume_ratio_{short}_{long}"
        self.min_periods = long

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        vol = bars["volume"].astype(float)
        return vol.rolling(self.short).mean() / vol.rolling(self.long).mean()


register(Momentum(20))
register(Momentum(60))
register(Volatility(20))
register(Bias(20))
register(VolumeRatio(5, 20))
