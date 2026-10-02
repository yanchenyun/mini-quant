"""策略层与因子层用例：因子正确性、海龟全链路与注册表机制。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.backtest.engine import BacktestEngine
from quant.backtest.sim_broker import CostModel
from quant.core.abstractions import Strategy, StrategyContext
from quant.factor import FactorEngine
from quant.strategy import (available_strategies, build_strategy,
                            get_strategy)
from quant.strategy.double_ma import DoubleMAStrategy
from quant.strategy.factor_momentum import FactorMomentumStrategy
from quant.strategy.trend import TurtleTrendStrategy

from tests.helpers import make_bars


class FactorProbe(Strategy):
    """因子探针：逐 bar 记录 ctx.factor 与 factor_history 的末值和长度，
    用于验证防未来访问（值与全量因果计算逐点一致，长度随 bar 增长）。"""

    def __init__(self):
        self.params: dict = {}
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


def test_trend() -> None:
    """海龟趋势跟踪：因子手算对照 + 预热语义 + 参数校验 + 策略全链路回归。"""
    bars = make_bars()
    names = ["atr_20", "donchian_high_20", "donchian_low_10"]
    frame = FactorEngine.compute(bars, names)
    assert list(frame.columns) == ["code", "dt", *names], "输出列序应为 code/dt/因子列"

    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    pc = np.concatenate(([np.nan], close[:-1]))
    tr = np.nanmax(np.vstack([high - low, np.abs(high - pc), np.abs(low - pc)]), axis=0)
    tr[0] = np.nan                              # 首根 bar 无昨收，TR 无定义
    atr_expected = pd.Series(tr).rolling(20).mean().to_numpy()

    # 1) 计算正确性：手算对照（独立于实现，覆盖牛/震荡不同区间）
    for i in (100, 200, 300):
        assert abs(frame["atr_20"].iloc[i] - atr_expected[i]) < 1e-9, f"ATR 不匹配 bar#{i}"
        assert abs(frame["donchian_high_20"].iloc[i] - high[i - 20:i].max()) < 1e-9, \
            f"上轨应等于前 20 根最高价（不含当根）bar#{i}"
        assert abs(frame["donchian_low_10"].iloc[i] - low[i - 10:i].min()) < 1e-9, \
            f"下轨应等于前 10 根最低价（不含当根）bar#{i}"

    # 2) 预热语义：min_periods = window + 1（shift 需额外一期历史）
    assert frame["atr_20"].iloc[:20].isna().all() and frame["atr_20"].iloc[20:].notna().all()
    assert frame["donchian_high_20"].iloc[:20].isna().all()
    assert frame["donchian_high_20"].iloc[20:].notna().all()
    assert frame["donchian_low_10"].iloc[:10].isna().all()
    assert frame["donchian_low_10"].iloc[10:].notna().all()

    # 3) 参数非法校验：入场通道须长于离场通道
    for entry, exit_ in [(10, 10), (10, 20)]:
        try:
            TurtleTrendStrategy(entry, exit_)
        except ValueError:
            continue
        raise AssertionError(f"非法通道参数应抛 ValueError: entry={entry}, exit={exit_}")

    # 4) 全链路之一：单单位口径（无加仓）→ 严格 T+1 配对断言
    engine = BacktestEngine(
        bars=bars, strategy=TurtleTrendStrategy(20, 10, max_units=1),
        init_cash=1_000_000, cost=CostModel(), factor_frame=frame)
    r = engine.run()
    m = r.metrics
    print("\n== 海龟趋势 20/10 单单位 ==")
    for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
              "trade_count", "win_rate", "final_equity", "total_commission"):
        print(f"  {k:18s} {m.get(k)}")
    assert len(r.equity_curve) == len(bars), "净值曲线天数应等于交易日数"
    assert m["final_equity"] > 0, "权益必须为正"
    assert all(t["quantity"] % 100 == 0 for t in r.trades), "成交量须为100整数倍"
    assert len(r.trades) > 0, "合成行情应产生唐奇安突破交易"
    open_buy_day = None
    for t in r.trades:
        day = t["date"][:10]
        if t["side"] == "buy":
            open_buy_day = day
        else:
            assert open_buy_day is not None and day > open_buy_day, \
                f"当日买入({open_buy_day})当日卖出({day})，违反 T+1"
            open_buy_day = None

    # 5) 全链路之二：四单位金字塔 → 单轮持仓的买入笔数不得超过 max_units
    engine2 = BacktestEngine(
        bars=bars, strategy=TurtleTrendStrategy(20, 10, max_units=4),
        init_cash=1_000_000, cost=CostModel(), factor_frame=frame)
    r2 = engine2.run()
    assert len(r2.equity_curve) == len(bars)
    assert r2.metrics["final_equity"] > 0
    units = 0
    for t in r2.trades:
        if t["side"] == "buy":
            units += 1
            assert units <= 4, "单轮持仓加仓不得超过 max_units=4（金字塔上限）"
        else:
            units = 0
    assert all("reason" in x for x in r2.rejects), "风控拒单须带原因"
    print("海龟趋势跟踪用例通过 ✓")


def test_strategy_registry() -> None:
    """策略注册表：CLI 与 Web 共用的构造路径，扩展机制的核心回归。

    覆盖：自动发现（三个内置策略均已注册）/ 规格与构造默认值一致 /
    字符串参数转型（Web 查询参数形态）/ choices 拦截未注册因子规格 /
    未知参数显式报错 / 跨参数与范围校验走策略类构造 / overlay 钩子输出。
    """
    names = available_strategies()
    assert set(names) >= {"double_ma", "factor_momentum", "turtle"}, \
        "三个内置策略应已注册（自动发现）"

    for name in names:
        spec = get_strategy(name)
        assert spec.label, f"{name} 缺中文显示标签"
        strat = build_strategy(name)
        for p in spec.params:
            assert strat.params[p.name] == p.default, \
                f"{name}.{p.name} 的默认值应与规格声明一致"

    # 1) 字符串参数（Web 查询参数形态）应正确转型
    s = build_strategy("turtle", {"entry": "55", "exit": "20",
                                  "risk_pct": "0.02"})
    assert s.entry == 55 and s.exit == 20 and abs(s.risk_pct - 0.02) < 1e-12

    # 2) choices 校验：未注册因子规格的窗口应在构造期拦截
    for strat_name, bad in [("turtle", {"entry": 30}),
                            ("factor_momentum", {"window": 30})]:
        try:
            build_strategy(strat_name, bad)
        except ValueError:
            continue
        raise AssertionError(f"非法取值应抛 ValueError: {strat_name} {bad}")

    # 3) 未知参数：调用方拼写错误应显式报错而非静默忽略
    try:
        build_strategy("double_ma", {"fast": 5, "nope": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("未声明的参数应抛 ValueError")

    # 4) 跨参数与范围校验走策略类 __init__（CLI 与 Web 同一条错误路径）
    for strat_name, bad in [("double_ma", {"fast": 20, "slow": 5}),
                            ("turtle", {"entry": 10, "exit": 20}),
                            ("turtle", {"entry": 20, "exit": 10,
                                        "risk_pct": 0.5})]:
        try:
            build_strategy(strat_name, bad)
        except ValueError:
            continue
        raise AssertionError(f"非法参数应抛 ValueError: {strat_name} {bad}")

    # 5) overlay 钩子：双均线应产出两条与 rolling 口径一致的均线
    bars = make_bars()
    spec = get_strategy("double_ma")
    lines = spec.overlay(bars, {"fast": 5, "slow": 20})
    assert set(lines) == {"MA5", "MA20"}
    expected = bars["close"].astype(float).rolling(20).mean()
    assert np.allclose(lines["MA20"].to_numpy()[25:], expected.to_numpy()[25:],
                       equal_nan=True)

    print("策略注册表用例通过 ✓")
