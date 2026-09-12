"""全系统扩展点的抽象接口契约（Protocol / ABC）。

SOLID 落地：
- ISP + DIP：Strategy 只依赖 StrategyContext 窄接口，不感知数据源 / 引擎细节；
- LSP：SimBroker / 未来 QmtBroker 实现同一 Broker 协议，引擎零改动；
- OCP：新数据源 / 券商 / 风控 / 因子 = 新增实现类 + 注册，改 0 行旧代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

import pandas as pd

from .models import Bar, Fill, Order, Position, Side


# ── 数据面 ────────────────────────────────────────────────────────────────
class MarketDataSource(Protocol):
    """外部行情数据源：Baostock / Tushare / AKShare……。"""
    name: str

    def fetch_bars(self, code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """拉取行情，返回统一列 DataFrame。

        Args:
            code: 证券代码（如 sh.600000）。
            start: 起始日期 'YYYY-MM-DD'。
            end: 结束日期 'YYYY-MM-DD'。
            freq: '1d' | '5min'（可扩展 15/30/60min）。
            adjust: 复权标记，1=后复权 2=前复权 3=不复权。

        Returns:
            统一列：code/dt/trade_date/open/high/low/close/pre_close/
            volume/amount/trade_status/is_st。
        """
        ...


class DataRepository(Protocol):
    """本地行情仓储契约。当前实现：MySQLBarRepo（freq 分表 + upsert 幂等写入）。

    四方法为必备契约；save_factors / load_factors 属可选能力，不计入本协议。
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
    @property
    def cash(self) -> float: ...

    def position(self, code: str) -> Position | None: ...
    def equity(self, prices: dict[str, float]) -> float: ...


class StrategyContext:
    """策略能看到的全部世界（接口隔离 ISP）。

    防未来函数：history / factor 只含当前 bar（含）及以前的数据。
    """

    def __init__(self, history: pd.Series, clock: str,
                 portfolio: PortfolioView, sink: OrderSink,
                 price: float, factors: "FactorAccessor | None" = None):
        """
        Args:
            history: 截至当前 bar（含）的收盘价 Series，index 为 dt。
            clock: 当前 bar 的时间戳（dt），用于标记订单 created_at。
            portfolio: 账户只读视图（现金 / 持仓 / 净值）。
            sink: 订单提交入口（引擎实现 OrderSink 协议）。
            price: 当前 bar 收盘价（下单参考价）。
            factors: 因子只读视图；未启用因子时为 None。
        """
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
        """当前 bar 收盘价（下单参考价）。"""
        return self._price

    @property
    def factors(self) -> "FactorAccessor | None":
        """因子只读视图；本回测未启用因子时为 None。"""
        return self._factors

    def _ensure_factors(self) -> FactorAccessor:
        """返回因子视图；未注入时抛 RuntimeError（调用方应确保策略声明了 required_factors）。"""
        if self._factors is None:
            raise RuntimeError(
                "本回测未启用因子：策略 required_factors 为空，或引擎未注入 factor_frame")
        return self._factors

    def factor(self, name: str) -> float:
        """当前 bar 的因子值（NaN 表示预热区无值）。"""
        return self._ensure_factors().value(name)

    def factor_history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列，index 为 bar 时间戳 dt。"""
        return self._ensure_factors().history(name)

    def buy(self, code: str, cash_ratio: float = 0.95) -> None:
        """按可用现金比例市价买入（100 股向下取整）。"""
        if self._price <= 0:
            return
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
        # 全仓卖出时直接清仓，避免整手取整导致残留零股
        qty = pos.available if ratio >= 1.0 else int(pos.available * ratio / 100) * 100
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
    required_factors: list[str] = []
    # ⚠️ 子类须在 __init__ 中重新赋值 params / required_factors，
    #    否则所有实例共享同一可变对象（Python 类属性陷阱）。

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
    """策略可见的因子只读视图（引擎每根 bar 用 iloc[:n] 切片构造）。

    防未来函数：策略在结构上不可能看到当前 bar 之后的因子行。
    """

    def __init__(self, df: pd.DataFrame):
        # df：index=dt（str），columns=因子名，行序=bar 时间序
        self._df = df
        self._empty = len(df) == 0  # 缓存空帧标志，避免每次 value() 重复检查

    def value(self, name: str) -> float:
        """当前 bar 的因子值；无该因子列或空帧时返回 NaN。"""
        if self._empty or name not in self._df.columns:
            return float("nan")
        return float(self._df[name].iloc[-1])

    def history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列。"""
        if name not in self._df.columns:
            raise KeyError(f"未知因子: {name}（可用: {list(self._df.columns)}）")
        return self._df[name]
