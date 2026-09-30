"""离线冒烟测试：不依赖 MySQL/Baostock/Wind，用合成行情验证 引擎+策略+风控+撮合+绩效 全链路。

覆盖十组场景：
1. 日线回归（v0.1 行为不变）：合成日线跑双均线，断言净值/整手/T+1；
2. 5 分钟频率（v0.2 新能力）：合成分钟线，断言——
   a. 日界解禁每天仅一次（on_new_day 钩子按交易日触发）；
   b. 日内 T+1：当日买入当日卖不出去（available=0）；
   c. 次日可卖（解禁后成交）；
   d. 净值快照按交易日记录（条数=交易日数，年化口径不被放大）。
3. 因子库（v0.3 新能力）：因子计算正确性 / 预热语义 / 防未来访问 / 因子策略全链路。
4. Wind 适配器（v0.4 新能力）：代码格式转换 / WindData 组装 / 列归一 / 出口校验
   —— 用 mock WindData 覆盖，不连接 Wind 终端（含两个实测踩坑点的回归断言）。
5. 海龟趋势跟踪（v0.4 新能力）：ATR / 唐奇安通道因子手算对照、预热语义、
   参数非法校验、策略全链路（通道突破入场、吊灯止损、金字塔加仓上限）。
6. 策略注册表：规格完整性 / 类型转型 / choices 与构造校验 / overlay 钩子
   —— CLI 与 Web 共用的唯一构造路径，扩展机制的核心回归。
7. CLI 必填参数：--source / --factors / --strategy 缺失时以退出码 2 结束
   —— 数据源 / 策略 / 因子不设默认值，禁止隐式选择。
8. 通道注入：引擎 broker 参数接受任一 Broker 实现，注入实现被真正调用
   —— LSP 的直接兑现（换实盘通道不改主循环）。
9. 风控链与策略元数据：空规则列表表示不做风控（不被默认集吞掉）、
   需要链上下文的规则在装配时显式挂载、策略元数据漏赋值即报错。
10. 数据源 / 频率注册表：两份清单单一来源，CLI 选项、Web 校验与仓储
   表名映射同源派生，未知取值显式报错。
"""
from __future__ import annotations

import math
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from quant.backtest.engine import BacktestEngine
from quant.backtest.sim_broker import CostModel, SimBroker
from quant.core.abstractions import Strategy, StrategyContext
from quant.core.models import Bar, Order, Position, Side
from quant.core.portfolio import Portfolio
from quant.data import registry
from quant.data.mysql_repo import TABLES
from quant.data.wind_source import (_from_wind_code, _finalize, _to_wind_code,
                                    _winddata_to_frame)
from quant.factor import FactorEngine
from quant.strategy import (available_strategies, build_strategy,
                            get_strategy)
from quant.strategy.double_ma import DoubleMAStrategy
from quant.strategy.factor_momentum import FactorMomentumStrategy
from quant.strategy.trend import TurtleTrendStrategy


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


class IntradayT1Probe(Strategy):
    """探针策略：第一天首 bar 买入，此后每根 bar 都尝试卖出。

    用于验证：当日买入当日卖不出（ctx.sell 因 available=0 静默不发单），
    次日日界解禁后卖单才成交。同时统计 on_new_day 触发次数。
    """

    def __init__(self):
        self.bought = False
        self.new_day_events: list[str] = []
        self.sell_attempt_days: list[str] = []   # 每次"想卖"的归属交易日
        self.params: dict = {}                   # 基类不给默认值，子类须显式声明
        self.required_factors: list[str] = []

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


class FakeWindData:
    """模拟 WindPy 的 WindData 结构：Data 按字段分组（每个字段一条时间序列）。"""

    def __init__(self, fields, times, data, error_code=0):
        self.ErrorCode = error_code
        self.Fields = fields
        self.Times = times
        self.Data = data


def test_wind_source() -> None:
    """Wind 适配器离线用例：代码转换 / 组装 / 归一 / 出口校验（不需要 Wind 终端）。

    回归重点（均为实测踩过的坑，对应 wind_source.py 中的注释）：
    1. w.wsd 返回的字段名是大写（OPEN / AMT / TRADE_STATUS）；若不做
       小写归一，后续按小写字段名取列会全部落空 → 静默产出"缺行情列"的数据；
    2. trade_status 返回的是中文描述（'交易' / '停牌'）而非数字；用
       == 1 判断会把所有交易日误判为停牌 → 回测零成交且不报任何错；
    3. 出口必须强校验必需列，防止上述两类问题日后再次静默通过。
    """
    # 1) 代码格式双向转换（本系统 sh.600519 <-> Wind 600519.SH）
    assert _to_wind_code("sh.600519") == "600519.SH"
    assert _to_wind_code("sz.000001") == "000001.SZ"
    assert _to_wind_code("bj.430047") == "430047.BJ"
    assert _from_wind_code("600519.SH") == "sh.600519"

    # 1b) 指数类代码：本系统 si.801050 <-> Wind 801050.SI；
    # Wind 原生格式（数字在前）允许直接输入并原样通过
    assert _to_wind_code("si.801050") == "801050.SI"
    assert _to_wind_code("801050.SI") == "801050.SI"
    assert _to_wind_code("801050.si") == "801050.SI"      # 后缀大小写不敏感
    assert _from_wind_code("801050.SI") == "si.801050"

    # 2) 日线：按 wsd 真实形态构造（字段名大写 + trade_status 中文）
    out = FakeWindData(
        fields=["OPEN", "HIGH", "LOW", "CLOSE", "PRE_CLOSE", "VOLUME", "AMT",
                "PCT_CHG", "TURN", "TRADE_STATUS"],
        times=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        data=[[10.0, 10.2, 10.1], [10.5, 10.4, 10.3], [9.8, 10.0, 9.9],
              [10.3, 10.1, 9.95], [10.0, 10.3, 10.1], [1000, 1200, 900],
              [10300, 12120, 8955], [0.5, -1.94, -1.49], [1.2, 1.5, 1.1],
              ["交易", "交易", "停牌"]],
    )
    df = _finalize(_winddata_to_frame(out, "600519.SH"),
                   "sh.600519", "1d", "2", minute=False)
    assert df["dt"].tolist() == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert df["code"].eq("sh.600519").all(), "code 列应回填为本系统格式"
    assert df["trade_date"].eq(df["dt"]).all(), "日线 trade_date 应等于 dt"
    assert "amount" in df.columns and "AMT" not in df.columns, \
        "大写字段名（AMT）应归一为小写 amount"
    assert df["trade_status"].tolist() == [1, 1, 0], \
        "中文状态应归一：'交易'->1、'停牌'->0（直接 ==1 会全判为停牌）"
    assert df["close"].tolist() == [10.3, 10.1, 9.95]
    assert df["is_st"].eq(0).all() and df["adjust_flag"].eq(2).all()

    # 3) 分钟线：按 wsi 真实形态构造（字段名小写、请求 amt 返回 amount）
    out_m = FakeWindData(
        fields=["open", "high", "low", "close", "volume", "amount"],
        times=[datetime(2024, 1, 2, 9, 35), datetime(2024, 1, 2, 9, 40)],
        data=[[10.0, 10.05], [10.1, 10.12], [9.95, 10.0],
              [10.05, 10.1], [100, 200], [1005, 2020]],
    )
    dfm = _finalize(_winddata_to_frame(out_m, "600519.SH"),
                    "sh.600519", "5min", "2", minute=True)
    assert dfm["dt"].tolist() == ["2024-01-02 09:35:00", "2024-01-02 09:40:00"]
    assert dfm["trade_date"].tolist() == ["2024-01-02", "2024-01-02"]
    assert dfm["amount"].tolist() == [1005.0, 2020.0]
    assert dfm["pre_close"].eq(0.0).all(), "分钟线昨收应为 0（由仓储层 JOIN 日线回填）"
    assert dfm["trade_status"].eq(1).all(), "分钟线无该字段 → 安全默认可交易"

    # 4) 出口校验：缺必需列必须显式报错，而不是静默返回缺列数据
    try:
        _finalize(_winddata_to_frame(
            FakeWindData(["CLOSE", "VOLUME"], [date(2024, 1, 2)], [[10.0], [100]]),
            "600519.SH"), "sh.600519", "1d", "2", minute=False)
    except RuntimeError as e:
        assert "缺少必需列" in str(e), f"报错信息应指明缺列，实际: {e}"
    else:
        raise AssertionError("缺必需列时应抛 RuntimeError，而不是静默返回")

    print("Wind 适配器用例通过 ✓")


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


def test_cli_required_args() -> None:
    """CLI 三项强制显式指定：缺 --source / --factors / --strategy 应退出。

    数据源 / 策略 / 因子不设默认值——选错会静默产出错误结论。这里按
    argparse 契约断言：缺必填参数时以退出码 2 结束（错误信息在 stderr）。
    """
    import contextlib
    import io

    from quant.app import cli

    cases = [
        ["ingest", "--code", "sh.600000", "--start", "2020-01-01"],
        ["compute-factors", "--code", "sh.600000", "--start", "2024-01-01"],
        ["backtest", "--code", "sh.600000", "--start", "2021-01-01"],
    ]
    saved = sys.argv
    try:
        for argv in cases:
            sys.argv = ["mini-quant", *argv]
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    cli.main()
                except SystemExit as e:
                    assert e.code == 2, (argv, e.code)
                else:
                    raise AssertionError(f"缺少必填参数应退出: {argv}")
            assert "--" in err.getvalue(), (argv, err.getvalue())
    finally:
        sys.argv = saved
    print("CLI 必填参数用例通过 ✓")


class DummyBroker:
    """注入用假通道：只记录调用、不产生成交（用于验证引擎用的是注入实现）。"""

    def __init__(self):
        self.submitted: list[Order] = []
        self.settle_calls = 0

    def submit(self, order: Order) -> None:
        self.submitted.append(order)

    def settle(self, bar: Bar) -> list:
        self.settle_calls += 1
        return []


def test_broker_injection() -> None:
    """引擎通道可注入：注入的实现被真正使用，主循环与策略零改动（LSP）。"""
    bars = make_bars()
    dummy = DummyBroker()
    engine = BacktestEngine(bars=bars, strategy=DoubleMAStrategy(5, 20),
                            init_cash=1_000_000, cost=CostModel(), broker=dummy)
    assert engine.broker is dummy, "引擎应直接使用注入的通道"
    r = engine.run()
    assert dummy.settle_calls == len(bars), \
        f"每根 bar 都应向注入通道请求撮合（{len(bars)}），实际 {dummy.settle_calls}"
    assert dummy.submitted, "策略订单应提交到注入通道"
    assert r.trades == [], "假通道不成交，成交记录应为空"
    assert r.metrics["trade_count"] == 0

    # 不注入时仍装配内置回测撮合（默认行为不变）
    default_engine = BacktestEngine(bars=bars, strategy=DoubleMAStrategy(5, 20))
    assert type(default_engine.broker) is SimBroker, "缺省应装配内置 SimBroker"
    print("通道注入用例通过 ✓")


def test_risk_chain() -> None:
    """风控链：空规则语义 / 显式挂载 / 策略元数据的严格契约。"""
    from quant.backtest.risk import AvailabilityRule, RiskChain

    # 1) 空列表表示"不做风控"，不再被 or 吞掉换成默认规则集
    assert len(RiskChain().rules) == 4, "缺省应装配默认规则集"
    assert RiskChain([]).rules == [], "空列表应表示无规则"

    # 2) 需要链上下文的规则在装配时显式收到上下文（不再靠 hasattr 试探）
    chain = RiskChain()
    rule = [r for r in chain.rules if isinstance(r, AvailabilityRule)][0]
    assert rule._ctx is chain, "AvailabilityRule 应在装配时挂载到链上"

    pf = Portfolio(init_cash=1_000_000)
    pf.positions["sh.600000"] = Position(code="sh.600000", quantity=100,
                                         available=100, avg_cost=10.0)
    bar = Bar(code="sh.600000", dt="2024-01-02", trade_date="2024-01-02",
              open=10.0, high=10.0, low=10.0, close=10.0)
    sell = Order(code="sh.600000", side=Side.SELL, quantity=100)

    # 3) 未挂载就单独调用 → 显式报错（而不是在 None 上取属性崩掉）
    try:
        AvailabilityRule().check(sell, pf, bar)
    except RuntimeError as e:
        assert "未挂载" in str(e), e
    else:
        raise AssertionError("未挂载的规则应抛 RuntimeError")
    assert rule.check(sell, pf, bar) is None, "可卖足额应放行"

    # 4) 策略元数据由子类在 __init__ 显式赋值：基类不留可变默认，漏写即报错
    assert not hasattr(Strategy, "params"), "基类不应带类级 params 默认值"
    assert not hasattr(Strategy, "required_factors"), "基类不应带类级因子默认值"

    class Forgetful(Strategy):
        def on_init(self, ctx) -> None:
            pass

        def on_bar(self, ctx, bar) -> None:
            pass

    forgetful = Forgetful()
    for attr in ("params", "required_factors"):
        try:
            getattr(forgetful, attr)
        except AttributeError:
            continue
        raise AssertionError(f"子类漏写 {attr} 时应抛 AttributeError")
    print("风控链与策略元数据用例通过 ✓")


def test_data_registry() -> None:
    """数据源 / 频率清单单一来源：CLI 选项、Web 校验与仓储表名同源派生。"""
    import contextlib
    import io

    from fastapi import HTTPException

    from quant.app.cli import build_parser
    from quant.webapp.server import backtest as backtest_endpoint

    sources, freqs = registry.available_sources(), registry.available_freqs()
    assert sources == ["baostock", "akshare", "wind"], sources
    assert freqs[0] == "1d" and "5min" in freqs, freqs

    # 1) 仓储表名映射由注册表派生（不再是第二份清单）
    assert TABLES == registry.freq_tables(), "仓储表名应与注册表同源"
    for f in registry.FREQS:
        assert registry.get_freq(f.name).table == f.table

    # 2) CLI 的 choices 由注册表派生：非法取值时 argparse 列出全部合法值
    parser = build_parser()
    cases = ((["ingest", "--code", "sh.600000", "--start", "2020-01-01",
               "--source", "nope"], sources),
             (["backtest", "--code", "sh.600000", "--start", "2020-01-01",
               "--strategy", "double_ma", "--freq", "3min"], freqs))
    for argv, expected in cases:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                parser.parse_args(argv)
            except SystemExit:
                pass
        msg = err.getvalue()
        assert "invalid choice" in msg, msg
        for name in expected:
            assert name in msg, f"错误提示应列出注册表取值 {name}: {msg}"

    # 3) Web 端点的频率校验与注册表同源
    class FakeRequest:
        def __init__(self, q: dict):
            self.query_params = q

    try:
        backtest_endpoint(FakeRequest({"code": "sh.600000", "start": "2020-01-01",
                                       "strategy": "double_ma", "freq": "3min"}))
    except HTTPException as e:
        assert e.status_code == 400, e
        assert "1d" in str(e.detail) and "5min" in str(e.detail), e.detail
    else:
        raise AssertionError("非法 freq 应返回 400")

    # 4) 未知数据源 / 频率在注册表层显式报错并列出可选值
    for bad, fn in (("nope", registry.get_source), ("3min", registry.get_freq)):
        try:
            fn(bad)
        except ValueError as e:
            assert bad in str(e), e
        else:
            raise AssertionError(f"{bad} 应抛 ValueError")
    print("数据源/频率注册表用例通过 ✓")


def main() -> None:
    test_daily()
    test_minute()
    test_factor()
    test_trend()
    test_wind_source()
    test_strategy_registry()
    test_cli_required_args()
    test_broker_injection()
    test_risk_chain()
    test_data_registry()
    print("\n全部断言通过 ✓")


if __name__ == "__main__":
    main()
