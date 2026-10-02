"""合成行情构造器：全部离线用例共享的假数据工厂（不依赖外部数据源）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_bars(code: str = "sh.600000", days: int = 400,
              start_price: float = 10.0) -> pd.DataFrame:
    """合成日线（列口径：date + dt + trade_date，首行因缺昨收被丢弃）。"""
    rng = np.random.default_rng(42)
    t = np.arange(days)
    trend = np.cumsum(rng.normal(0.0005, 0.02, days))
    season = 0.15 * np.sin(t / 25)
    close = start_price * np.exp(trend + season)
    open_ = close * (1 + rng.normal(0, 0.003, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, days)))
    dates = pd.bdate_range("2023-01-02", periods=days).strftime("%Y-%m-%d")
    df = pd.DataFrame({
        "code": code, "date": dates, "dt": dates, "trade_date": dates,
        "open": open_, "high": high, "low": low,
        "close": close, "volume": rng.integers(1e6, 5e6, days),
        "amount": close * 3e6, "trade_status": 1, "is_st": 0,
    })
    df["pre_close"] = df["close"].shift(1).fillna(0.0)
    return df.iloc[1:].reset_index(drop=True)


def make_minute_bars(code: str = "sh.600000", days: int = 40,
                     bars_per_day: int = 8,
                     start_price: float = 10.0) -> pd.DataFrame:
    """合成 5 分钟线：每天 bars_per_day 根，dt 含时分，pre_close 为日线昨收口径。"""
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2025-01-02", periods=days).strftime("%Y-%m-%d")
    times = ["09:35:00", "09:40:00", "09:45:00", "10:00:00",
             "13:05:00", "13:45:00", "14:30:00", "14:55:00"][:bars_per_day]
    rows, price, day_close = [], start_price, start_price
    for d in dates:
        pre_close = day_close          # 日线昨收（涨跌停基准口径）
        for tm in times:
            price *= 1 + rng.normal(0.0002, 0.003)
            o = price * (1 + rng.normal(0, 0.001))
            h = max(o, price) * 1.001
            l = min(o, price) * 0.999
            rows.append({"code": code, "dt": f"{d} {tm}", "trade_date": d,
                         "date": d, "open": o, "high": h, "low": l, "close": price,
                         "pre_close": pre_close, "volume": 1e5, "amount": price * 1e5,
                         "trade_status": 1, "is_st": 0})
        day_close = price
    return pd.DataFrame(rows)
