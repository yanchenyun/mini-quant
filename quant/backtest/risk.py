"""风控责任链：一条规则一个类（OCP）。新增规则 = 新增子类 + 注册。"""
from __future__ import annotations

from ..core.abstractions import PortfolioView, RiskRule
from ..core.models import Bar, Order, Position, Side
from .sim_broker import CostModel


class LotSizeRule(RiskRule):
    """数量合法性：A 股按 100 股整数倍。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.quantity <= 0 or order.quantity % 100 != 0:
            return f"数量非法({order.quantity})，须为100股整数倍"
        return None


class CashSufficiencyRule(RiskRule):
    """买入时现金足额校验（含佣金缓冲）。

    估算口径与撮合对齐：以 bar.open 为基准，
    加 slippage + commission_rate 缓冲，避免 close≈open 时误判。
    """

    def __init__(self, cost: CostModel | None = None):
        self._cost = cost or CostModel()

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.BUY:
            return None
        # 估算口径与 SimBroker 撮合对齐：open × (1 + slippage + commission_rate)
        buffer = 1 + self._cost.slippage + self._cost.commission_rate
        est_cost = bar.open * order.quantity * buffer
        if est_cost > portfolio.cash:
            return f"现金不足(需约{est_cost:,.0f}，可用{portfolio.cash:,.0f})"
        return None


class AvailabilityRule(RiskRule):
    """T+1：只允许卖出 available 数量。

    同日内多笔卖出防重：同一 bar 策略可能为同一标的提交多笔卖出订单，
    而 Position.available 要等到次 bar 结算时才扣减。本规则通过
    RiskChain._pending_sell 追踪当日已委托量，确保累计委托不超过可卖。
    """
    _chain: RiskChain | None = None  # 由 RiskChain.check() 注入，用于读取 pending_sell

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.SELL:
            return None
        pos: Position | None = portfolio.position(order.code)
        if pos is None:
            return f"可卖不足(可卖0，委托{order.quantity})，T+1限制"
        # 扣减同日内已通过风控的累计委托量，防止同一 bar 重复委托
        pending = self._chain.pending_sell(order.code)
        effective = pos.available - pending
        if effective < order.quantity:
            return f"可卖不足(可卖{effective}，委托{order.quantity})，T+1限制"
        return None


class TradabilityRule(RiskRule):
    """标的可交易性：停牌 / ST 拒单。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if not bar.tradable:
            return "标的停牌或为ST"
        return None


class RiskChain:
    """责任链容器：按序执行，任一拒绝即拦截。

    内置同日内卖出防重：订单通过全部规则后，若为 SELL 则自动记录
    已委托量到 _pending_sell，后续同标的卖出检查会扣减该量。
    """

    def __init__(self, rules: list[RiskRule] | None = None,
                 cost: CostModel | None = None):
        self.rules: list[RiskRule] = rules or [
            TradabilityRule(), LotSizeRule(),
            CashSufficiencyRule(cost), AvailabilityRule(),
        ]
        self.rejects: list[dict] = []
        self._pending_sell: dict[str, int] = {}  # code → 当日累计已委托卖出量

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        for rule in self.rules:
            if hasattr(rule, '_chain'):
                rule._chain = self
            reason = rule.check(order, portfolio, bar)
            if reason is not None:
                self.rejects.append({"date": bar.trade_date, "code": order.code,
                                     "side": order.side.value, "reason": reason})
                return reason
        # 全部通过：若为卖出，记录已委托量供后续检查扣减
        if order.side == Side.SELL:
            self._pending_sell[order.code] = (
                self._pending_sell.get(order.code, 0) + order.quantity
            )
        return None

    def pending_sell(self, code: str) -> int:
        """该标的当日累计已委托卖出量（供 AvailabilityRule 扣减）。"""
        return self._pending_sell.get(code, 0)

    def reset_pending(self) -> None:
        """新交易日开始时清空委托追踪（由引擎在 on_new_day 前调用）。"""
        self._pending_sell.clear()
