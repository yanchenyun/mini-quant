"""内置因子集（纯时序因子：t 行只依赖 ≤t 的数据，天然防未来）。

日线/5 分钟通用：按 bar 计算，语义随频率自然变化
（日线 momentum_20 = 月动量，5 分钟 momentum_20 = 日内动量）。

新增因子：继承 Factor → 实现 compute → 在文件末尾 register() 一行。
"""
from __future__ import annotations

import pandas as pd

from ..core.abstractions import Factor
from .base import register


class Momentum(Factor):
    """动量（Momentum）：N 期收益率。

    公式：close / close.shift(N) - 1
    min_periods = N+1（shift 需要 N 期历史）。
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"momentum_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        return close / close.shift(self.window) - 1.0


class Volatility(Factor):
    """已实现波动率（Realized Volatility）：N 期收益率标准差。

    公式：std(pct_change(), window=N)
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"volatility_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        ret = bars["close"].astype(float).pct_change()
        return ret.rolling(self.window).std()


class Bias(Factor):
    """乖离率（Bias / BIAS）：收盘价偏离 N 期均线的比例。

    公式：(close - MA(N)) / MA(N)
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"bias_{window}"
        self.min_periods = window

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        ma = close.rolling(self.window).mean()
        return (close - ma) / ma


class VolumeRatio(Factor):
    """量比（Volume Ratio）：短期均量 / 长期均量，衡量放量程度。

    公式：MA(volume, short) / MA(volume, long)
    """

    def __init__(self, short: int, long: int):
        if short >= long:
            raise ValueError(f"短期窗口须小于长期: {short} >= {long}")
        self.short, self.long = short, long
        self.name = f"volume_ratio_{short}_{long}"
        self.min_periods = long

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        vol = bars["volume"].astype(float)
        return vol.rolling(self.short).mean() / vol.rolling(self.long).mean()


class ATR(Factor):
    """真实波幅均值（ATR，Average True Range）——趋势跟踪的波动率刻度尺。

    公式：
    TR = max(high - low, |high - pre_close|, |low - pre_close|)
    ATR = TR 的 window 期简单移动平均

    pre_close 取收盘价 shift(1)（与前复权价自洽，避免复权跳空污染 TR）。
    首根 bar 无昨收，TR 记 NaN 不参与均值，故 min_periods = window + 1。
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"atr_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        high = bars["high"].astype(float)
        low = bars["low"].astype(float)
        pc = bars["close"].astype(float).shift(1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()],
                       axis=1).max(axis=1)
        tr = tr.where(pc.notna())      # 首根 bar 无昨收 → NaN（保证预热语义准确）
        return tr.rolling(self.window).mean()


class DonchianHigh(Factor):
    """唐奇安通道上轨（Donchian Channel Upper）：前 N 根 bar 的最高价。

    公式：high.rolling(N).max().shift(1)

    shift(1) 排除当根 bar 自身——"突破前 N 根最高价"比较的是历史区间，
    若含当根则恒有 high >= max(high)，突破信号永不触发（也是防未来的关键）。
    min_periods = N + 1。
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"donchian_high_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        return bars["high"].astype(float).rolling(self.window).max().shift(1)


class DonchianLow(Factor):
    """唐奇安通道下轨（Donchian Channel Lower）：前 N 根 bar 的最低价。

    公式：low.rolling(N).min().shift(1)，与上轨同理排除当根 bar。
    min_periods = N + 1。
    """

    def __init__(self, window: int):
        self.window = window
        self.name = f"donchian_low_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        return bars["low"].astype(float).rolling(self.window).min().shift(1)


# ── 注册内置因子实例 ────────────────────────────────────────────────────────
# 参数化因子直接注册多个实例，名字即规格（如 momentum_20 / momentum_60）。
register(Momentum(20))
register(Momentum(60))
register(Volatility(20))
register(Bias(20))
register(VolumeRatio(5, 20))

# 通道与波动率刻度（唐奇安上下轨 + ATR）：趋势类策略的通用构件。
# 窗口即规格——按需注册实例，策略侧再用 choices 把取值限定到已注册规格。
register(ATR(20))
register(DonchianHigh(20))
register(DonchianHigh(55))
register(DonchianLow(10))
register(DonchianLow(20))
