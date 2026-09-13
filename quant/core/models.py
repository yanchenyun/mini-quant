"""领域模型（值对象，不可变）。core 包零第三方依赖，是全系统唯一的“宪法”。"""
from __future__ import annotations

from dataclasses import dataclass
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
    code: str                  # 标的代码，如 'sh.600000'
    dt: str                    # bar 时间戳：日线 'YYYY-MM-DD'；分钟线 'YYYY-MM-DD HH:MM:SS'
    trade_date: str            # 归属交易日 'YYYY-MM-DD'，T+1 解禁 / 涨跌停 / 绩效统计均以此为准
    open: float                # 开盘价
    high: float                # 最高价
    low: float                 # 最低价
    close: float               # 收盘价
    pre_close: float = 0.0     # 昨收价（涨跌停计算基准；分钟数据由仓储关联日线表填充）
    volume: float = 0.0        # 成交量（股）
    amount: float = 0.0        # 成交金额（元）
    trade_status: int = 1      # 交易状态：1 正常交易，0 停牌
    is_st: int = 0             # 是否 ST：1 为 ST / *ST，0 为正常

    @property
    def date(self) -> str:
        """向后兼容别名：等价于 trade_date。"""
        return self.trade_date

    @property
    def tradable(self) -> bool:
        """是否可交易：排除停牌与 ST 标的。"""
        return self.trade_status == 1 and self.is_st != 1


@dataclass(frozen=True)
class Order:
    """交易意图（不可变值对象）。

    由策略在 T 日 bar 结束时生成，经 Broker 在 T+1 bar 开盘时撮合成交。
    price 为 None 表示市价单（按次 bar 开盘价成交）；非 None 则为限价单。
    """
    code: str                        # 标的代码，如 'sh.600000'
    side: Side                       # 买卖方向：Side.BUY / Side.SELL
    quantity: int                    # 委托数量（股），须为 100 的整数倍
    price: float | None = None       # 委托价格；None = 市价单（次 bar 开盘价成交）
    created_at: str = ""             # 委托生成时刻（对应触发 bar 的 trade_date）


@dataclass(frozen=True)
class Fill:
    """成交记录（不可变值对象）。

    由 Broker 撮合后生成，记录一笔 Order 的实际成交细节。
    回测引擎据此更新 Position 与 Portfolio 现金，并作为绩效统计的原始输入。
    """
    order: Order                     # 关联的原始委托
    filled_qty: int                  # 实际成交数量（股）
    filled_price: float              # 实际成交价格（含滑点）
    commission: float                # 佣金费用
    filled_at: str                   # 成交时刻（即次一 bar 的 dt；日线为日期，分钟含时分）
    slippage_cost: float = 0.0       # 滑点成本 = |成交价 - 次bar开盘价| × 成交数量


@dataclass
class Position:
    """持仓。available 实现 T+1：当日买入的仓位当日不可卖。

    与 Bar/Order/Fill 等不可变值对象不同，Position 是可变的——回测引擎
    在每笔成交后原地更新其状态，避免频繁创建新对象。

    T+1 机制通过 quantity 与 available 的双字段差值实现：
    - quantity:   总持仓（含当日冻结部分）。
    - available:  当日可卖数量。买入时只增加 quantity，不动 available；
      次日 on_new_day() 将 available 对齐到 quantity，完成解禁。
    """
    code: str                        # 标的代码，如 'sh.600000'
    quantity: int = 0                # 总持仓数量（股），含当日买入冻结部分
    available: int = 0               # 当日可卖数量（股），<= quantity
    avg_cost: float = 0.0            # 含佣金摊薄成本（元/股），卖出清仓时归零

    def on_buy(self, qty: int, price_incl_cost: float) -> None:
        """买入后按加权平均更新摊薄成本，再累加持仓数量。

        注意：买入只增加 quantity，不增加 available——当日买入的仓位
        须等到次日 on_new_day() 才解禁，以此实现 T+1 约束。

        avg_cost 含买入佣金摊薄（price_incl_cost = filled_price + commission/qty），
        这保证卖出利润公式 (sell_price - avg_cost)*qty - sell_commission
        不会重复扣减买入侧费用。若遗漏佣金摊薄，avg_cost 偏低，利润会被低估。

        Args:
            qty: 本次买入股数（须 > 0）。
            price_incl_cost: 含佣金的买入单价（用于摊薄成本计算）。
        """
        if qty <= 0:
            return
        # 加权平均：(旧持仓总成本 + 新买入总成本) / 合并后总股数
        self.avg_cost = (self.avg_cost * self.quantity + price_incl_cost * qty) / (self.quantity + qty)
        self.quantity += qty

    def on_sell(self, qty: int) -> None:
        """卖出扣减持仓与可卖数量；清仓时同步归零成本基准。

        Args:
            qty: 本次卖出股数（调用方须保证 qty <= available）。
        """
        self.quantity -= qty
        self.available -= qty
        if self.quantity <= 0:       # 清仓保护：浮点/整数误差导致负数时一并归零
            self.quantity = 0
            self.available = 0
            self.avg_cost = 0.0      # 清仓后成本归零，避免残留脏数据影响下次买入

    def on_new_day(self) -> None:
        """T+1：新交易日开始，昨日买入的仓位解禁。

        在回测引擎的每日循环中，于处理信号之前调用，
        将 available 对齐到 quantity，使前日买入变为可卖。
        """
        self.available = self.quantity


@dataclass
class TradeRecord:
    """一次成交的对外展示记录（Web / 绩效统计用）。

    每条记录对应回测过程中的一笔买入或卖出成交，
    由 SimBroker 在撮合成功后生成，供前端交易列表展示
    以及绩效模块计算收益率、手续费等指标。
    """
    date: str              # 成交日期，格式 "YYYY-MM-DD"
    code: str              # 证券代码，如 "sh.600000"
    side: str              # 交易方向："buy" 或 "sell"
    price: float           # 成交价格（元）
    quantity: int          # 成交数量（股）
    commission: float      # 本次交易手续费（元），买卖双向均收取
    profit: float | None = None   # 卖出时对应的本轮盈亏（含费用）；买入时为 None
