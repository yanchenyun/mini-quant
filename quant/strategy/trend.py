"""海龟式趋势跟踪策略（经典趋势跟踪的 A 股多头本土化版）。

信号与仓位规则
--------------
默认 System 1 口径（entry 取 20、exit 取 10），可参数化为 System 2
（55 / 20）。入场为当根 bar 最高价突破前 entry 根 bar 的最高价（唐奇安
上轨）；离场为持仓期间最低价跌破前 exit 根 bar 的最低价（唐奇安下轨），
或触及吊灯止损线（持仓期最高价减 stop_atr 倍 ATR）。

仓位按 Unit 股数 = risk_pct × 权益 / (k × ATR) 计算，再折算为现金比例经
ctx.buy 下单（框架只认现金比例，撮合侧自动按 100 股取整）。入场后每上穿
add_step 倍 ATR 追加一个单位，上限 max_units。

风险口径：k 取 2、stop_atr 取 2 时，每个单位在吊灯止损触发时的最大亏损约为
risk_pct × 权益（2 倍 ATR 的价格回撤与 ATR 倒数定仓相互抵消）。

保真度说明
----------
相对经典海龟系统有以下取舍。框架只有市价单（次一 bar 开盘成交），没有盘中
止损委托，本策略用当根 bar.low 判定触及、执行延到次一 bar 开盘，日线跳空会
放大偏差，追求贴近盘中止损的效果请用 5min 频率回测。A 股个股不可做空，本
策略仅保留多头腿。T+1 约束下当日买入的仓位当日不可卖，止损最早次日执行；
离场委托因 T+1 冻结或风控拒绝而未能提交时，离场条件仍成立即逐 bar 重试，
直至确认空仓——离场意图不设闩锁，避免与实际持仓状态脱节。
当前回测为单标的，经典趋势跟踪赖以生存的多市场分散效应无法体现。
"""
from __future__ import annotations

import math

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar
from .registry import ParamSpec, StrategySpec, register_strategy


class TurtleTrendStrategy(Strategy):
    """海龟式趋势跟踪：通道突破入场 + 波动率定仓 + 吊灯止损 + 金字塔加仓。"""

    def __init__(self, entry: int = 20, exit: int = 10,
                 atr_window: int = 20, risk_pct: float = 0.01,
                 k: float = 2.0, stop_atr: float = 2.0,
                 add_step: float = 0.5, max_units: int = 4):
        """
        Args:
            entry: 入场通道窗口（前 N 根最高价），System 1 取 20、System 2 取 55。
            exit: 离场通道窗口（前 N 根最低价），System 1 取 10、System 2 取 20。
            atr_window: ATR 窗口，经典取 20（即海龟系统里的 N）。
            risk_pct: 每单位目标风险占权益的比例，经典 1%。
            k: 定仓公式中的 ATR 倍数（Unit = risk_pct × 权益 / (k × ATR)）。
            stop_atr: 吊灯止损的 ATR 倍数，经典 2。
            add_step: 金字塔加仓的价格步长（ATR 倍数），经典 0.5。
            max_units: 单一标的最大持仓单位数，经典 4。

        Raises:
            ValueError: 入场通道不长于离场通道，或风险参数非正。
        """
        if entry <= exit:
            raise ValueError(f"入场通道须长于离场通道: entry={entry}, exit={exit}")
        if not 0 < risk_pct <= 0.2:
            raise ValueError("risk_pct 须在 (0, 0.2] 区间（单单位风险占权益比例）")
        if k <= 0 or stop_atr <= 0 or add_step <= 0 or max_units < 1:
            raise ValueError(
                "k / stop_atr / add_step 须为正数，max_units 须 >= 1")

        self.entry = entry
        self.exit = exit
        self.atr_window = atr_window
        self.risk_pct = risk_pct
        self.k = k
        self.stop_atr = stop_atr
        self.add_step = add_step
        self.max_units = max_units
        # 实例级重新赋值（类属性是可变对象的共享陷阱，见 core.abstractions.Strategy）
        self.params = {"entry": entry, "exit": exit, "atr_window": atr_window,
                       "risk_pct": risk_pct, "k": k, "stop_atr": stop_atr,
                       "add_step": add_step, "max_units": max_units}
        self.required_factors = [f"donchian_high_{entry}",
                                 f"donchian_low_{exit}",
                                 f"atr_{atr_window}"]

        # 内部状态：跨 bar 记忆（引擎对同一实例全程复用）。
        # 离场不设闩锁标志：离场条件成立时逐 bar 补发卖单直到空仓，
        # 避免意图记账与实际状态脱节（T+1 冻结或风控拒绝时把仓位
        # 永久锁死的正是旧版的 _exiting 闩锁）
        self._units = 0        # 已建单位数（0 = 空仓）
        self._peak = 0.0       # 持仓期最高价（吊灯止损基准）
        self._next_add = 0.0   # 下一加仓触发价

    def on_init(self, ctx: StrategyContext) -> None:
        """回测开始前重置状态。

        注意：引擎以空上下文调用本钩子（history 为空、price 为 0），
        因此这里不能读取行情，只能初始化内部变量。
        """
        self._units, self._peak, self._next_add = 0, 0.0, 0.0

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        """每根 bar：先判离场/止损，再判加仓或入场（信号次一 bar 开盘成交）。"""
        upper = ctx.factor(f"donchian_high_{self.entry}")
        lower = ctx.factor(f"donchian_low_{self.exit}")
        atr = ctx.factor(f"atr_{self.atr_window}")
        if math.isnan(upper) or math.isnan(lower) or math.isnan(atr) or atr <= 0:
            return      # 预热区（或 ATR 异常）无法定仓与设止损，跳过本根

        pos = ctx.portfolio.position(bar.code)
        holding = pos is not None and pos.quantity > 0

        if holding:
            # 持仓期间持续维护吊灯基准（含卖出未成交时的重新武装）
            self._peak = bar.high if self._peak <= 0 else max(self._peak, bar.high)
            chandelier = self._peak - self.stop_atr * atr
            if bar.low <= lower or bar.low <= chandelier:
                # 跌破下轨或触及吊灯线 → 清仓（次一 bar 开盘执行）。
                # T+1 未解禁（available=0）或被风控拒绝时提交失败，
                # 下根 bar 条件仍成立会自动重试，直至确认空仓
                if ctx.sell(bar.code, 1.0):
                    self._units = 0
                    self._next_add = float("inf")   # 离场途中禁止加仓
            elif (self._units < self.max_units
                  and bar.high >= self._next_add):
                self._submit_unit(ctx, bar, atr, pyramid=True)
        else:
            if bar.high > upper:
                # 突破上轨 → 开第一个单位；以当根最高价为吊灯初值
                # （实际成交在次一 bar 开盘，此处为可接受的保守近似）
                if self._submit_unit(ctx, bar, atr, pyramid=False):
                    self._peak = bar.high

    def _submit_unit(self, ctx: StrategyContext, bar: Bar, atr: float,
                     pyramid: bool) -> bool:
        """按 ATR 定仓提交一个单位的买单。

        经典海龟按「股数」定仓，而 ctx.buy 只接受现金比例，故先算出 Unit
        股数，再折算为现金比例（撮合侧还会按 100 股向下取整，存在微小偏差）。

        Args:
            ctx: 策略上下文。
            bar: 当前 bar（提供收盘价与代码）。
            atr: 当前 ATR 值，用于波动率定仓。
            pyramid: True 表示加仓（触发价按步长递增），False 表示首次建仓。

        Returns:
            True 表示买单已提交；False 表示定仓股数不足一手、现金不足
            或被风控拒绝——金字塔额度只在提交成功时占用。
        """
        equity = ctx.equity()
        shares = int(self.risk_pct * equity / (self.k * atr) / 100) * 100
        if shares <= 0 or ctx.portfolio.cash <= 0:
            return False
        budget = shares * ctx.price
        if budget <= 0:
            return False

        if not ctx.buy(bar.code, min(1.0, budget / ctx.portfolio.cash)):
            return False
        self._units += 1
        self._next_add = (self._next_add if pyramid else bar.close) + self.add_step * atr
        return True


def _channel_overlay(bars: pd.DataFrame, params: dict) -> dict[str, pd.Series]:
    """K 线辅助线钩子：唐奇安上下轨（前 N 根极值，不含当根）。"""
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    return {f"上轨{params['entry']}": high.rolling(params["entry"]).max().shift(1),
            f"下轨{params['exit']}": low.rolling(params["exit"]).min().shift(1)}


# ── 注册：CLI 选项、Web 下拉与参数输入框由此自动生成 ─────────────────────────
# entry / exit 限定为已注册唐奇安因子的规格（名字即规格）；atr_window 固定
# 取构造默认值 20（atr_20 已注册），如需其它窗口，先在 factor/builtin.py
# 注册对应实例再把它加进 choices。
register_strategy(StrategySpec(
    name="turtle", cls=TurtleTrendStrategy, label="海龟趋势",
    params=(ParamSpec("entry", int, 20, choices=(20, 55),
                      help="入场通道窗口（海龟两系统 20/55）"),
            ParamSpec("exit", int, 10, choices=(10, 20),
                      help="离场通道窗口（海龟两系统 10/20）"),
            ParamSpec("risk_pct", float, 0.01,
                      help="每单位风险占权益比例（0.01 即 1%）"),
            ParamSpec("k", float, 2.0, help="定仓的 ATR 倍数"),
            ParamSpec("stop_atr", float, 2.0, help="吊灯止损的 ATR 倍数"),
            ParamSpec("add_step", float, 0.5, help="金字塔加仓步长（ATR 倍数）"),
            ParamSpec("max_units", int, 4, help="最大持仓单位数")),
    overlay=_channel_overlay,
    describe=lambda p: f"海龟 {p['entry']}/{p['exit']}"))
