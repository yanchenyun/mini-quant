"""回测层用例：引擎 + 撮合 + 风控的全链路行为回归。

覆盖：日线 / 5 分钟频率的行为基线、通道注入（LSP）、引擎生命周期
（ctx.equity 估值口径 / run 幂等重跑 / _today_bar 日界清空）、
一次代码审查确认的四类静默失败缺陷锁定，以及无效价在信号链路
（history / 因子输入）的 NaN 语义隔离回归。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.backtest.engine import BacktestEngine
from quant.backtest.sim_broker import CostModel, SimBroker
from quant.core.abstractions import Strategy, StrategyContext
from quant.core.models import Bar, Order
from quant.factor import FactorEngine
from quant.strategy.double_ma import DoubleMAStrategy
from quant.strategy.trend import TurtleTrendStrategy

from tests.helpers import make_bars, make_minute_bars


class IntradayT1Probe(Strategy):
    """探针策略：第一天首 bar 买入，此后每根 bar 都尝试卖出。

    用于验证：当日买入当日卖不出（ctx.sell 因 available=0 不发单），
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
            ctx.sell(bar.code)     # available<=0 时不发单（T+1）


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
        "买入当日应存在被 T+1 拦截的卖出尝试（available=0 不发单）"
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

    def reset(self) -> None:
        self.submitted = []
        self.settle_calls = 0


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


class EquityProbe(Strategy):
    """探针策略：逐 bar 记录 ctx.equity()，用于校验估值口径与重跑幂等。"""

    def __init__(self):
        self.params: dict = {}
        self.required_factors: list[str] = []
        self.equities: list[float] = []

    def on_init(self, ctx: StrategyContext) -> None:
        self.equities = []

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        self.equities.append(ctx.equity())
        if ctx.portfolio.position(bar.code) is None:
            ctx.buy(bar.code, 0.5)


class BuyHoldProbe(Strategy):
    """探针策略：无持仓即买入并持有（用于停牌估值与多标的场景）。"""

    def __init__(self):
        self.params: dict = {}
        self.required_factors: list[str] = []

    def on_init(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        if ctx.portfolio.position(bar.code) is None:
            ctx.buy(bar.code, 0.95)


def test_engine_lifecycle() -> None:
    """引擎生命周期：估值口径、run 幂等重跑、_today_bar 不跨日残留。"""
    bars = make_bars(days=60)
    probe = EquityProbe()
    engine = BacktestEngine(bars=bars, strategy=probe,
                            init_cash=1_000_000, cost=CostModel())
    r1 = engine.run()

    # 1) equity 泄漏回归：策略侧 ctx.equity() 与引擎日终快照同一套估值
    #    基准（三级兜底），策略不再自组价格字典
    assert len(probe.equities) == len(bars)
    for eq, snap in zip(probe.equities, r1.equity_curve):
        assert abs(eq - snap["equity"]) < 0.01, \
            f"ctx.equity() 应与日终快照同口径: {eq} vs {snap['equity']}"

    # 2) 同一引擎实例重复 run：两轮结果完全一致——组合账本、通道挂单、
    #    拒单日志与估值基准在 run 开始时整体重建，上一轮运行态不泄漏
    r2 = engine.run()
    assert r2.equity_curve == r1.equity_curve, "重跑的净值曲线应与首轮一致"
    assert r2.trades == r1.trades, "重跑的成交记录应与首轮一致"
    assert r2.metrics["final_equity"] == r1.metrics["final_equity"]
    assert len(probe.equities) == len(bars), "探针状态应随 on_init 重置"

    # 3) _today_bar 按日清空：当日无 bar 的标的不残留昨日 bar——残留会让
    #    缺数据标的的昨日行情冒充今日通过风控校验
    two = pd.concat([make_bars(code="sh.600000", days=3),
                     make_bars(code="sz.000001", days=3)], ignore_index=True)
    last_day = two["dt"].max()
    two = two[~((two["code"] == "sh.600000") & (two["dt"] == last_day))]
    engine2 = BacktestEngine(bars=two.reset_index(drop=True),
                             strategy=BuyHoldProbe(), init_cash=1_000_000,
                             cost=CostModel())
    engine2.run()
    assert "sh.600000" not in engine2._today_bar, \
        "次日无 bar 的标的不应残留昨日的 _today_bar（跨日残留回归）"
    print("引擎生命周期用例通过 ✓")


def _turtle_trap_bars() -> pd.DataFrame:
    """海龟离场闩锁缺陷的最小复现行情：突破入场，成交当日即跌破离场线。"""
    n = 60
    close = np.full(n, 10.0)
    open_ = np.full(n, 10.0)
    high = np.full(n, 10.05)
    low = np.full(n, 9.95)
    high[30], open_[30], close[30] = 11.0, 10.1, 10.8      # 突破上轨
    open_[31], high[31], low[31], close[31] = 11.0, 11.0, 8.8, 9.0
    open_[32:], high[32:], low[32:], close[32:] = 9.0, 9.2, 8.5, 8.8
    dates = pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d")
    df = pd.DataFrame({
        "code": "sh.600000", "date": dates, "dt": dates, "trade_date": dates,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": 1e6, "amount": close * 1e6, "trade_status": 1, "is_st": 0,
    })
    df["pre_close"] = df["close"].shift(1).fillna(0.0)
    return df


class HalfSellerProbe(Strategy):
    """探针策略：首根 bar 买入，此后每根 bar 卖出可用仓位的一半。"""

    def __init__(self):
        self.bought = False
        self.params: dict = {}
        self.required_factors: list[str] = []

    def on_init(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        if not self.bought:
            ctx.buy(bar.code, 0.95)
            self.bought = True
            return
        pos = ctx.portfolio.position(bar.code)
        if pos is not None and pos.available > 0:
            ctx.sell(bar.code, 0.5)


class DoubleBuyProbe(Strategy):
    """探针策略：首根 bar 连续两次按同一现金比例买入同一标的。"""

    def __init__(self):
        self.bought = False
        self.results: list[bool] = []
        self.params: dict = {}
        self.required_factors: list[str] = []

    def on_init(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        if not self.bought:
            self.results = [ctx.buy(bar.code, 0.95), ctx.buy(bar.code, 0.95)]
            self.bought = True


def test_silent_failure_regressions() -> None:
    """四类静默失败的缺陷锁定（修复前以下断言全部失败）。

    覆盖：入场成交当日触发离场时 T+1 拦截导致海龟仓位永久卡死；
    停牌零价无条件覆盖估值基准造成净值单日塌陷；卖出成交不回冲
    当日委托量导致日内后续卖出被误拒；同 bar 两笔买入各自按全额
    现金通过风控、拒单原因误导排查方向。
    """
    # 1) 海龟离场闩锁：T+1 冻结当日的离场意图被拒后应可重试
    trap = _turtle_trap_bars()
    frame = FactorEngine.compute(trap, ["atr_20", "donchian_high_20",
                                        "donchian_low_10"])
    strat = TurtleTrendStrategy(20, 10, max_units=1)
    engine = BacktestEngine(bars=trap, strategy=strat, init_cash=1_000_000,
                            cost=CostModel(), factor_frame=frame)
    r = engine.run()
    sells = [t for t in r.trades if t["side"] == "sell"]
    assert len(sells) == 1, \
        f"入场当日触发离场后应在解禁次日正常离场，实际卖出 {len(sells)} 笔"
    assert r.metrics["final_equity"] > 0
    final_pos = engine.portfolio.position("sh.600000")
    assert final_pos is None or final_pos.quantity == 0, \
        "离场完成后不应残留持仓（闩锁卡死回归）"
    assert any("T+1" in x["reason"] for x in r.rejects), \
        "被 T+1 拦截的卖出意图应在拒单日志中留痕"

    # 2) 停牌零价：估值基准应维持最近有效收盘价而不是塌陷为 0
    bars = make_bars(days=40)
    susp = bars.copy()
    idx = 25
    for col in ("open", "high", "low", "close"):
        susp.loc[idx, col] = 0.0
    susp.loc[idx, "trade_status"] = 0
    r2 = BacktestEngine(bars=susp, strategy=BuyHoldProbe(),
                        init_cash=1_000_000, cost=CostModel()).run()
    mv_prev = r2.equity_curve[idx - 1]["market_value"]
    mv_susp = r2.equity_curve[idx]["market_value"]
    assert mv_prev > 0 and mv_susp == mv_prev, \
        f"停牌日市值应沿用最近有效收盘价: 前日 {mv_prev}，停牌日 {mv_susp}"
    assert r2.metrics["max_drawdown"] > -50, \
        f"单日停牌不应造出极端假回撤: {r2.metrics['max_drawdown']}"
    assert any(t["side"] == "buy" for t in r2.trades), "探针应完成建仓"

    # 3) 卖出成交回冲：日内连续部分卖出不应被"幽灵占用"误拒
    r3 = BacktestEngine(bars=make_minute_bars(days=8),
                        strategy=HalfSellerProbe(),
                        init_cash=1_000_000, cost=CostModel()).run()
    sells3 = [t for t in r3.trades if t["side"] == "sell"]
    assert len(sells3) >= 2, "日内多次部分卖出应连续成交"
    assert not any("可卖不足" in x["reason"] for x in r3.rejects), \
        f"成交回冲后不应出现可卖不足误拒: {r3.rejects}"

    # 4) 同 bar 重复买入：第二笔应在风控层按现金占用拦截
    probe = DoubleBuyProbe()
    r4 = BacktestEngine(bars=make_bars(days=30), strategy=probe,
                        init_cash=1_000_000, cost=CostModel()).run()
    buys4 = [t for t in r4.trades if t["side"] == "buy"]
    assert probe.results == [True, False], \
        f"同 bar 两笔买入应首笔受理、次笔被拒: {probe.results}"
    assert len(buys4) == 1, "只应成交一笔买入"
    cash_rejects = [x for x in r4.rejects if "现金不足" in x["reason"]]
    assert len(cash_rejects) == 1, \
        f"重复买入应记一条现金不足拒单: {r4.rejects}"
    print("静默失败回归用例通过 ✓")


class HistoryTailProbe(Strategy):
    """探针策略：逐 bar 记录 ctx.history 末值（校验无效价不进入策略可见历史）。"""

    def __init__(self):
        self.params: dict = {}
        self.required_factors: list[str] = []
        self.tail_values: list[float] = []

    def on_init(self, ctx: StrategyContext) -> None:
        self.tail_values = []

    def on_bar(self, ctx: StrategyContext, bar) -> None:
        self.tail_values.append(float(ctx.history.iloc[-1]))


def test_invalid_price_signal_isolation() -> None:
    """无效价隔离：停牌 0 价不得进入策略可见 history 与因子输入。

    估值基准的护栏（_prices 不被 0 覆盖）此前已修；本用例锁定同一根
    脏 bar 在信号链路上的两个入口——引擎 history 写入与 FactorEngine
    输入，均按 NaN 语义传播（rolling 指标因 NaN 自然跳过，窗口滑过
    后恢复正常值，不残留）。
    """
    bars = make_bars(days=60)
    idx = 25
    for col in ("open", "high", "low", "close"):
        bars.loc[idx, col] = 0.0
    bars.loc[idx, "trade_status"] = 0

    # 1) 引擎侧：停牌 bar 在策略可见 history 中记 NaN（而非 0 砸穿均线）
    probe = HistoryTailProbe()
    engine = BacktestEngine(bars=bars, strategy=probe, init_cash=1_000_000,
                            cost=CostModel())
    engine.run()
    assert math.isnan(probe.tail_values[idx]), \
        "停牌 0 价不应进入策略可见 history"
    assert probe.tail_values[idx - 1] > 0 and probe.tail_values[idx + 1] > 0, \
        "停牌前后正常 bar 的 history 值不受影响"

    # 2) 因子侧：停牌行及其污染窗口内因子值为 NaN，窗口滑过后恢复真实值
    frame = FactorEngine.compute(bars, ["momentum_20", "donchian_low_10"])
    assert math.isnan(frame["momentum_20"].iloc[idx]), \
        "停牌日动量应为 NaN（不得被 0 价算成 -100%）"
    assert math.isnan(frame["momentum_20"].iloc[idx + 20]), \
        "shift 分母为停牌 0 价时动量应为 NaN（不得 +inf）"
    assert not math.isnan(frame["momentum_20"].iloc[idx + 21]), \
        "窗口滑过后动量应恢复正常值"
    assert math.isnan(frame["donchian_low_10"].iloc[idx + 1]), \
        "停牌 0 价进入窗口时下轨应为 NaN（不得被砸穿为 0）"
    assert frame["donchian_low_10"].iloc[idx + 11] > 0, \
        "窗口滑过后下轨应恢复真实值"

    # 3) 全链路冒烟：history 含 NaN 时双均线不产生假信号（交叉比较对
    #    NaN 恒为 False，策略静默跳过该段），权益曲线保持健康
    r = BacktestEngine(bars=bars, strategy=DoubleMAStrategy(5, 20),
                       init_cash=1_000_000, cost=CostModel()).run()
    assert len(r.equity_curve) == len(bars)
    assert r.metrics["final_equity"] > 0
    print("无效价隔离用例通过 ✓")
