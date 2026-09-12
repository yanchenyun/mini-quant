"""全部扩展点的抽象接口（Protocol / ABC）。

设计约束（SOLID 落地）：
- Strategy 不知道数据来自 Baostock 还是 MySQL，只依赖 StrategyContext（ISP + DIP）；
- Broker 不知道自己在回测还是实盘，SimBroker / 未来的 QmtBroker 可互换（LSP）；
- 新增数据源 / 券商 / 风控规则 = 新增一个实现类，不改任何既有代码（OCP）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

import pandas as pd

from .models import Bar, Fill, Order, Position, Side


# ── 数据面 ────────────────────────────────────────────────────────────────
class MarketDataSource(Protocol):
    """外部行情数据源：Baostock / Tushare / AKShare……各写一个实现。"""
    name: str

    def fetch_bars(self, code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """按频率拉取行情，返回统一列：
        code / dt / trade_date / open / high / low / close / pre_close /
        volume / amount / trade_status / is_st（日线另含估值等附加列）。

        freq: '1d' 日线，'5min' 5分钟线（可扩展 15/30/60min）。
        adjust：1 后复权 2 前复权 3 不复权。
        """
        ...


class DataRepository(Protocol):
    """本地行情仓储：MySQL ods 库（日线/分钟各一张表），可替换为 Parquet 等。

    行情四方法是必备契约；MySQLBarRepo 另提供 save_factors / load_factors
    （因子物化缓存读写，选股/因子分析场景用），属可选能力，不计入本协议。
    """
    def save_bars(self, df: pd.DataFrame, freq: str = "1d") -> int: ...
    def load_bars(self, codes: list[str], start: str, end: str,
                  freq: str = "1d", adjust: str = "2") -> pd.DataFrame: ...
    def latest_bar_time(self, code: str, freq: str = "1d",
                        adjust: str = "2") -> str | None: ...
    def list_codes(self, freq: str = "1d") -> list[str]: ...


# ── 策略面 ───────────────────────────────────────────────────────────────
class OrderSink(Protocol):
    """订单入口：引擎实现它；策略只通过 Context 调用，不认识引擎。"""
    def submit(self, order: Order) -> None: ...


class PortfolioView(Protocol):
    """策略与风控可见的账户只读视图。"""
    cash: float

    def position(self, code: str) -> Position | None: ...
    def equity(self, prices: dict[str, float]) -> float: ...


class StrategyContext:
    """策略能看到的全部世界。history 只含当前 bar（含）及以前的数据（防未来函数）。"""

    def __init__(self, history: pd.Series, clock: str,
                 portfolio: PortfolioView, sink: OrderSink,
                 price: float, factors: "FactorAccessor | None" = None):
        self._history = history
        self.clock = clock
        self.portfolio = portfolio
        self._sink = sink
        self._price = price
        self._factors = factors

    @property
    def history(self) -> pd.Series:
        """截至当前 bar（含）的收盘价序列，index 为 bar 时间戳 dt
        （日线即日期，分钟线为 'YYYY-MM-DD HH:MM:SS'）。"""
        return self._history

    @property
    def price(self) -> float:
        return self._price

    @property
    def factors(self) -> "FactorAccessor | None":
        """因子只读视图；本回测未启用因子时为 None。"""
        return self._factors

    def factor(self, name: str) -> float:
        """当前 bar 的因子值（NaN 表示预热区无值）。"""
        if self._factors is None:
            raise RuntimeError(
                "本回测未启用因子：策略 required_factors 为空，或引擎未注入 factor_frame")
        return self._factors.value(name)

    def factor_history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列，index 为 bar 时间戳 dt。"""
        if self._factors is None:
            raise RuntimeError(
                "本回测未启用因子：策略 required_factors 为空，或引擎未注入 factor_frame")
        return self._factors.history(name)

    def buy(self, code: str, cash_ratio: float = 0.95) -> None:
        """按可用现金比例市价买入（100 股向下取整）。"""
        budget = self.portfolio.cash * cash_ratio
        qty = int(budget / self._price / 100) * 100
        if qty > 0:
            self._sink.submit(Order(code=code, side=Side.BUY, quantity=qty,
                                    created_at=self.clock))

    def sell(self, code: str, ratio: float = 1.0) -> None:
        """卖出可用仓位（T+1：仅 available 部分）。"""
        pos = self.portfolio.position(code)
        if pos is None or pos.available <= 0:
            return
        qty = int(pos.available * ratio / 100) * 100
        if qty <= 0 and ratio >= 1.0:
            qty = pos.available
        if qty > 0:
            self._sink.submit(Order(code=code, side=Side.SELL, quantity=qty,
                                    created_at=self.clock))


class Strategy(ABC):
    """策略基类：继承它 + 实现两个必需钩子；on_new_day 可选重写。

    频率无关设计：on_bar 拿到的 history 与 bar.dt 无论是日线还是 5 分钟线，
    策略代码写法完全一致（rolling 均线等指标天然按 bar 计算）。

    因子用法：子类声明 required_factors = ["momentum_20", ...]，
    服务层自动解析依赖并注入（见 factor 包）；on_bar 里用 ctx.factor(name) 取值。
    """
    params: dict = {}
    required_factors: list[str] = []   # 声明依赖的因子名（空 = 不用因子）

    @abstractmethod
    def on_init(self, ctx: StrategyContext) -> None: ...

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None: ...

    def on_new_day(self, ctx: StrategyContext, bar: Bar) -> None:
        """交易日开始钩子（可选重写，默认空实现——向后兼容）。

        引擎在每个交易日的第一根 bar 撮合结算后、首个 on_bar 之前调用
        （bar 为当日第一根，用于确定标的与上下文）。日内策略在此重置
        当日状态（如日内开仓次数、日内均线累计）。
        """


# ── 执行面 ───────────────────────────────────────────────────────────────
class Broker(Protocol):
    """撮合/交易通道抽象。回测注入 SimBroker，实盘将来注入 QmtBroker。"""
    def submit(self, order: Order) -> None: ...
    def settle(self, bar: Bar) -> list[Fill]:
        """以本 bar（订单提交后的次一 bar）开盘价撮合挂起的订单。"""
        ...


class RiskRule(ABC):
    """风控规则：责任链上的一个节点。返回 None 放行，否则返回拒绝原因。"""
    @abstractmethod
    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None: ...


# ── 因子面 ──────────────────────────────────────────────────────────────
class Factor(ABC):
    """因子（Factor）：从原始行情派生的数值特征（动量/波动率/量比……）。

    compute 输入单标的、按 dt 升序的行情帧（open/close/volume/amount 等），
    返回与输入等长的 Series（index 对齐输入行）。

    契约（防未来函数，写新因子必须遵守）：
    - 只允许因果计算（rolling/shift），t 行的值只依赖 ≤t 的数据；
    - 历史不足时对应行为 NaN（不抛错），由策略侧判断处理；
    - min_periods = 产出首个非 NaN 值所需的最少 bar 数（引擎据此计算预热窗口）。
    """
    name: str
    min_periods: int

    @abstractmethod
    def compute(self, bars: pd.DataFrame) -> pd.Series: ...


class FactorAccessor:
    """策略可见的因子只读视图（引擎构造）。

    引擎在每根 bar 用 iloc[:n] 切片构造——策略在结构上不可能看到
    当前 bar 之后的因子行（防未来函数）。
    """

    def __init__(self, df: pd.DataFrame):
        # df：index=dt（str），columns=因子名，行序=bar 时间序
        self._df = df

    def value(self, name: str) -> float:
        """当前 bar 的因子值；无该因子列或空帧时返回 NaN。"""
        if name not in self._df.columns or len(self._df) == 0:
            return float("nan")
        return float(self._df[name].iloc[-1])

    def history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列。"""
        if name not in self._df.columns:
            raise KeyError(f"未知因子: {name}（可用: {list(self._df.columns)}）")
        return self._df[name]
