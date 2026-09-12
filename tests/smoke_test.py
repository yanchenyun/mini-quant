"""离线冒烟测试：不依赖 MySQL/Baostock，用合成行情验证 引擎+策略+风控+撮合+绩效 全链路。

覆盖三组场景：
1. 日线回归（v0.1 行为不变）：合成日线跑双均线，断言净值/整手/T+1；
2. 5 分钟频率（v0.2 新能力）：合成分钟线，断言——
   a. 日界解禁每天仅一次（on_new_day 钩子按交易日触发）；
   b. 日内 T+1：当日买入当日卖不出去（available=0）；
   c. 次日可卖（解禁后成交）；
   d. 净值快照按交易日记录（条数=交易日数，年化口径不被放大）。
3. 因子库（v0.3 新能力）：因子计算正确性 / 预热语义 / 防未来访问 / 因子策略全链路。
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from quant.backtest.engine import BacktestEngine
from quant.backtest.sim_broker import CostModel
from quant.core.abstractions import Strategy, StrategyContext
from quant.factor import FactorEngine
from quant.strategy.double_ma import DoubleMAStrategy
from quant.strategy.factor_momentum import FactorMomentumStrategy


def make_bars(code: str = "sh.600000", days: int = 400,
              start_price: float = 10.0) -> pd.DataFrame:
    """合成日线（v0.2 列口径：date + dt + trade_date）。"""
    rng = np.random.default_rng(42)
    t = np.arange(days)
    trend = np.cumsum(rng.normal(0.0005, 0.02, days))
    season = 0.15 * np.sin(t / 25)
    close = start_price * np.exp(trend + season)
    open_ = close * (1 + rng.normal(0, 0.003, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, days)))
    dates = pd.bdate_range("2023-01-02", periods=days).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "code": code, "date": dates, "dt": dates, "trade_date": dates,
        "open": open_, "high": high, "low": low,
        "close": close, "pre_close": np.roll(close, 1),
        "volume": rng.integers(1e6, 5e6, days), "amount": close * 3e6,
        "trade_status": 1, "is_st": 0,
    }).iloc[1:].reset_index(drop=True)


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


class IntradayT1Probe(Strategy):
    """探针策略：第一天首 bar 买入，此后每根 bar 都尝试卖出。

    用于验证：当日买入当日卖不出（ctx.sell 因 available=0 静默不发单），
    次日日界解禁后卖单才成交。同时统计 on_new_day 触发次数。
    """

    def __init__(self):
        self.bought = False
        self.new_day_events: list[str] = []
        self.sell_attempt_days: list[str] = []   # 每次"想卖"的归属交易日

    def on_init(self, ctx: StrategyContext) -> None:
        pass

    def on_new_day(self, ctx: StrategyContext, bar) -> None:
        self.new_day_events.append(bar.trade_date)

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        if not self.bought:
            ctx.buy(bar.code, 0.5)
            self.bought = True
            return
        pos = ctx.portfolio.position(bar.code)
        if pos is not None and pos.quantity > 0:
            self.sell_attempt_days.append(bar.trade_date)
            ctx.sell(bar.code)     # available<=0 时静默不发单（T+1）


def test_daily() -> None:
    bars = make_bars()
    for fast, slow in [(5, 20), (10, 60)]:
        engine = BacktestEngine(
            bars=bars, strategy=DoubleMAStrategy(fast, slow),
            init_cash=1_000_000, cost=CostModel(),
        )
        result = engine.run()
        m = result.metrics
        print(f"\n== 日线 双均线 MA{fast}/MA{slow} ==")
        for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
                  "calmar", "trade_count", "win_rate", "final_equity",
                  "total_commission"):
            print(f"  {k:18s} {m.get(k)}")
        assert len(result.equity_curve) == len(bars), "净值曲线天数应等于交易日数"
        assert m["final_equity"] > 0, "权益必须为正"
        assert all(t["quantity"] % 100 == 0 for t in result.trades), \
            "成交量须为100整数倍"
        # T+1 验证（逐轮配对：每笔卖出必须晚于其对应轮次的买入）
        open_buy_day = None
        for t in result.trades:
            day = t["date"][:10]
            if t["side"] == "buy":
                open_buy_day = day
            else:
                assert open_buy_day is not None, "卖出前必有买入"
                assert day > open_buy_day, f"当日买入({open_buy_day})当日卖出({day})，违反T+1"
                open_buy_day = None
    print("日线回归用例通过 ✓")


def test_minute() -> None:
    bars = make_minute_bars()
    days = bars["trade_date"].nunique()

    # 1) 探针策略：日界 / T+1 / 次日可卖
    probe = IntradayT1Probe()
    engine = BacktestEngine(bars=bars, strategy=probe,
                            init_cash=1_000_000, cost=CostModel())
    result = engine.run()

    assert len(probe.new_day_events) == days, \
        f"on_new_day 应每交易日触发一次（{days}），实际 {len(probe.new_day_events)}"
    assert probe.new_day_events == sorted(set(bars["trade_date"])), "日界顺序应与日历一致"

    buys = [t for t in result.trades if t["side"] == "buy"]
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(buys) == 1 and len(sells) == 1, "探针策略应一买一卖"
    buy_day, sell_day = buys[0]["date"][:10], sells[0]["date"][:10]
    assert sell_day > buy_day, f"当日买({buy_day})当日卖({sell_day})，违反T+1"
    assert buy_day in probe.sell_attempt_days, \
        "买入当日应存在被 T+1 拦截的卖出尝试（available=0 静默不发单）"
    # 成交时刻含时分（分钟级时间戳）
    assert " " in sells[0]["date"], "分钟级成交记录应含时间戳"

    # 2) 双均线在 5 分钟序列上直接可用（策略代码与日线完全一致）
    engine2 = BacktestEngine(bars=bars, strategy=DoubleMAStrategy(4, 24),
                            init_cash=1_000_000, cost=CostModel())
    r2 = engine2.run()
    assert len(r2.equity_curve) == days, \
        f"净值快照应按交易日记录（{days}），实际 {len(r2.equity_curve)}"
    assert r2.metrics["final_equity"] > 0
    # T+1 验证（逐轮配对）
    open_buy_day = None
    for t in r2.trades:
        day = t["date"][:10]
        if t["side"] == "buy":
            open_buy_day = day
        else:
            assert open_buy_day is not None and day > open_buy_day, \
                f"分钟级当日买({open_buy_day})当日卖({day})，违反T+1"
            open_buy_day = None

    m = r2.metrics
    print(f"\n== 5分钟 双均线 MA4/MA24 ==")
    for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
              "trade_count", "total_commission", "final_equity"):
        print(f"  {k:18s} {m.get(k)}")
    print("5 分钟频率用例通过 ✓")


class FactorProbe(Strategy):
    """因子探针：逐 bar 记录 ctx.factor 与 factor_history 的末值和长度，
    用于验证防未来访问（值与全量因果计算逐点一致，长度随 bar 增长）。"""

    def __init__(self):
        self.required_factors = ["momentum_20"]
        self.snapshots: list[tuple[str, float, int]] = []

    def on_init(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        hist = ctx.factor_history("momentum_20")
        self.snapshots.append((bar.dt, ctx.factor("momentum_20"), len(hist)))


def test_factor() -> None:
    bars = make_bars()
    names = ["momentum_20", "volatility_20", "bias_20", "volume_ratio_5_20"]

    # 1) 计算正确性 + 预热语义
    frame = FactorEngine.compute(bars, names)
    assert list(frame.columns) == ["code", "dt", *names], "输出列序应为 code/dt/因子列"
    assert len(frame) == len(bars), "因子帧行数应与行情一致"
    close = bars["close"].to_numpy()
    idx = 100
    expected = close[idx] / close[idx - 20] - 1.0
    assert abs(frame["momentum_20"].iloc[idx] - expected) < 1e-12, \
        "momentum_20 应等于 close/close.shift(20)-1"
    assert frame["momentum_20"].iloc[:20].isna().all(), "前 20 行应 NaN（预热区）"
    assert frame["momentum_20"].iloc[20:].notna().all(), "预热后应全部有效"

    # 2) 防未来：探针逐 bar 对照因果口径
    probe = FactorProbe()
    engine = BacktestEngine(bars=bars, strategy=probe, init_cash=1_000_000,
                            cost=CostModel(), factor_frame=frame)
    engine.run()
    mom = pd.Series(close) / pd.Series(close).shift(20) - 1.0
    for i, (dt, v, n) in enumerate(probe.snapshots):
        assert n == i + 1, "factor_history 长度应恰含截至当前 bar 的行（防未来切片）"
        assert math.isnan(v) == math.isnan(float(mom.iloc[i])), f"bar#{i} NaN 位置应一致"
        if not math.isnan(v):
            assert abs(v - float(mom.iloc[i])) < 1e-9, \
                f"bar#{i} 因子值应与因果计算一致（防未来）"

    # 3) 因子策略全链路（动量>0 持有，<0 清仓）
    engine2 = BacktestEngine(bars=bars, strategy=FactorMomentumStrategy(20),
                              init_cash=1_000_000, cost=CostModel(),
                              factor_frame=frame)
    r = engine2.run()
    m = r.metrics
    print(f"\n== 因子策略 momentum_20 ==")
    for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
              "trade_count", "win_rate", "final_equity"):
        print(f"  {k:18s} {m.get(k)}")
    assert len(r.equity_curve) == len(bars), "净值曲线天数应等于交易日数"
    assert m["final_equity"] > 0
    assert all(t["quantity"] % 100 == 0 for t in r.trades), "成交量须为100整数倍"
    # T+1 逐轮配对
    open_buy_day = None
    for t in r.trades:
        day = t["date"][:10]
        if t["side"] == "buy":
            open_buy_day = day
        else:
            assert open_buy_day is not None and day > open_buy_day, "违反 T+1"
            open_buy_day = None
    # 因子值驱动信号：至少应有交易发生（合成行情涨跌交替，动量必然变号）
    assert len(r.trades) > 0, "动量策略应产生交易"
    print("因子库用例通过 ✓")


def main() -> None:
    test_daily()
    test_minute()
    test_factor()
    print("\n全部断言通过 ✓")


if __name__ == "__main__":
    main()
