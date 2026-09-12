"""领域模型（值对象，不可变）。core 包零第三方依赖，是全系统唯一的“宪法”。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Bar:
    """一根行情 bar（日线或分钟线，v0.2 起支持多频率）。

    时间双字段——"bar 时刻"与"归属交易日"解耦，这是分钟级兼容的关键：
    - dt: bar 时间戳。日线 'YYYY-MM-DD'；分钟 'YYYY-MM-DD HH:MM:SS'。
    - trade_date: 归属交易日 'YYYY-MM-DD'。T+1 解禁、涨跌停基准、绩效
      统计周期全部认交易日，而非 bar 时刻。
    """
    code: str
    dt: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    pre_close: float = 0.0      # 日线昨收（涨跌停基准；分钟数据由仓储关联日线表填充）
    volume: float = 0.0
    amount: float = 0.0
    trade_status: int = 1   # 1 正常 0 停牌
    is_st: int = 0          # 1 ST

    @property
    def date(self) -> str:
        """向后兼容别名：等价于 trade_date。"""
        return self.trade_date

    @property
    def tradable(self) -> bool:
        return self.trade_status == 1 and self.is_st != 1


@dataclass(frozen=True)
class Order:
    """交易意图。price 为 None 表示市价单（按次 bar 开盘价成交）。"""
    code: str
    side: Side
    quantity: int
    price: float | None = None
    created_at: str = ""


@dataclass(frozen=True)
class Fill:
    order: Order
    filled_qty: int
    filled_price: float
    commission: float
    filled_at: str          # 成交时刻（即次一 bar 的 dt；日线为日期，分钟含时分）
    slippage_cost: float = 0.0


@dataclass
class Position:
    """持仓。available 实现 T+1：当日买入的仓位当日不可卖。"""
    code: str
    quantity: int = 0
    available: int = 0
    avg_cost: float = 0.0    # 含佣金摊薄成本

    def on_buy(self, qty: int, price_incl_cost: float) -> None:
        total_cost = self.avg_cost * self.quantity + price_incl_cost * qty
        self.quantity += qty
        self.avg_cost = total_cost / self.quantity if self.quantity else 0.0

    def on_sell(self, qty: int) -> None:
        self.quantity -= qty
        self.available -= qty
        if self.quantity <= 0:
            self.quantity = 0
            self.available = 0
            self.avg_cost = 0.0

    def on_new_day(self) -> None:
        """T+1：新交易日开始，昨日买入的仓位解禁。"""
        self.available = self.quantity


@dataclass
class TradeRecord:
    """一次成交的对外展示记录（Web / 绩效统计用）。"""
    date: str
    code: str
    side: str
    price: float
    quantity: int
    commission: float
    profit: float | None = None   # 卖出时对应的本轮盈亏（含费用）
