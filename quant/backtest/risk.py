"""风控责任链：一条规则一个类（OCP）。新增规则 = 新增子类 + 注册。"""
from __future__ import annotations

from ..core.abstractions import PortfolioView, RiskRule
from ..core.models import Bar, Order, Position, Side


class LotSizeRule(RiskRule):
    """数量合法性：A 股按 100 股整数倍。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.quantity <= 0 or order.quantity % 100 != 0:
            return f"数量非法({order.quantity})，须为100股整数倍"
        return None


class CashSufficiencyRule(RiskRule):
    """买入时现金足额校验（含佣金缓冲）。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.BUY:
            return None
        est_cost = bar.close * order.quantity * 1.002
        if est_cost > portfolio.cash:
            return f"现金不足(需约{est_cost:,.0f}，可用{portfolio.cash:,.0f})"
        return None


class AvailabilityRule(RiskRule):
    """T+1：只允许卖出 available 数量。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.SELL:
            return None
        pos: Position | None = portfolio.position(order.code)
        if pos is None or pos.available < order.quantity:
            avail = pos.available if pos else 0
            return f"可卖不足(可卖{avail}，委托{order.quantity})，T+1限制"
        return None


class TradabilityRule(RiskRule):
    """标的可交易性：停牌 / ST 拒单。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if not bar.tradable:
            return "标的停牌或为ST"
        return None


class RiskChain:
    """责任链容器：按序执行，任一拒绝即拦截。"""

    def __init__(self, rules: list[RiskRule] | None = None):
        self.rules: list[RiskRule] = rules or [
            TradabilityRule(), LotSizeRule(), CashSufficiencyRule(), AvailabilityRule(),
        ]
        self.rejects: list[dict] = []

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        for rule in self.rules:
            reason = rule.check(order, portfolio, bar)
            if reason is not None:
                self.rejects.append({"date": bar.trade_date, "code": order.code,
                                     "side": order.side.value, "reason": reason})
                return reason
        return None
