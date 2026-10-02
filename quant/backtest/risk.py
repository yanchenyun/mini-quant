"""风控责任链：一条规则一个类（OCP）。新增规则 = 新增子类 + 注册。"""
from __future__ import annotations

from ..core.abstractions import PortfolioView, RiskContext, RiskRule
from ..core.models import Bar, Order, Position, Side
from .sim_broker import CostModel


class LotSizeRule(RiskRule):
    """数量合法性：A 股按 100 股整数倍。"""

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.quantity <= 0 or order.quantity % 100 != 0:
            return f"数量非法({order.quantity})，须为100股整数倍"
        return None


class CashSufficiencyRule(RiskRule):
    """买入时现金足额校验（含佣金缓冲与日内已委托占用）。

    估算口径与撮合对齐：以 bar.open 为基准，加 slippage + commission_rate
    缓冲，避免 close≈open 时误判。日内已通过风控但尚未撮合的买单会
    占用现金（经链上下文读取），同一根 bar 内的多笔买入不会各自按
    全额现金通过——否则只能靠撮合层现金校验兜底，拒单原因还会被
    误写成"撮合时现金不足"，把诊断方向带偏。
    """

    def __init__(self, cost: CostModel | None = None):
        self._cost = cost or CostModel()
        self._ctx: RiskContext | None = None

    def attach(self, ctx: RiskContext) -> None:
        self._ctx = ctx

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.BUY:
            return None
        if self._ctx is None:
            raise RuntimeError(
                "CashSufficiencyRule 未挂载责任链（attach 未调用）：本规则"
                "需读取链上的日内现金占用，请通过 RiskChain 装配而非单独"
                "调用 check")
        # 估算口径与 SimBroker 撮合对齐：open × (1 + slippage + commission_rate)
        buffer = 1 + self._cost.slippage + self._cost.commission_rate
        est_cost = bar.open * order.quantity * buffer
        available = portfolio.cash - self._ctx.pending_buy_cost()
        if est_cost > available:
            return f"现金不足(需约{est_cost:,.0f}，可用{available:,.0f}，" \
                   f"已含日内已委托占用)"
        return None


class AvailabilityRule(RiskRule):
    """T+1：只允许卖出 available 数量。

    同日内多笔卖出防重：同一 bar 策略可能为同一标的提交多笔卖出订单，
    而 Position.available 要等到次 bar 结算时才扣减。本规则挂载责任链
    上下文后，用链上的 pending_sell 追踪当日已委托量，确保累计委托不超过可卖。
    """

    def __init__(self):
        self._ctx: RiskContext | None = None

    def attach(self, ctx: RiskContext) -> None:
        self._ctx = ctx

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.SELL:
            return None
        pos: Position | None = portfolio.position(order.code)
        if pos is None:
            return f"可卖不足(可卖0，委托{order.quantity})，T+1限制"
        if self._ctx is None:
            raise RuntimeError(
                "AvailabilityRule 未挂载责任链（attach 未调用）：本规则需读取"
                "链上的当日委托量，请通过 RiskChain 装配而非单独调用 check")
        # 扣减同日内已通过风控的累计委托量，防止同一 bar 重复委托
        pending = self._ctx.pending_sell(order.code)
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

    内置同日内的委托防重：订单通过全部规则后记入链上追踪——卖出按
    数量、买入按估算占用现金，后续检查会扣减该量，防止同一根 bar 或
    同一交易日内重复委托。

    rules 为 None 时装配默认规则集；显式传空列表表示"不做风控"
    （空列表是有意义的取值，不与"未指定"混为一谈）。
    """

    def __init__(self, rules: list[RiskRule] | None = None,
                 cost: CostModel | None = None):
        self.rules: list[RiskRule] = (
            [TradabilityRule(), LotSizeRule(),
             CashSufficiencyRule(cost), AvailabilityRule()]
            if rules is None else list(rules))
        self.rejects: list[dict] = []
        self._pending_sell: dict[str, int] = {}  # code → 当日累计已委托卖出量
        self._pending_buy_cost: float = 0.0       # 当日已委托买入的估算占用现金
        self._cost = cost or CostModel()
        # 装配时一次性把上下文交给需要它的规则，规则侧无需任何试探
        for rule in self.rules:
            rule.attach(self)

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        for rule in self.rules:
            reason = rule.check(order, portfolio, bar)
            if reason is not None:
                self.rejects.append({"date": bar.trade_date, "code": order.code,
                                     "side": order.side.value, "reason": reason})
                return reason
        # 全部通过：记入当日委托追踪，供后续检查扣减
        if order.side == Side.SELL:
            self._pending_sell[order.code] = (
                self._pending_sell.get(order.code, 0) + order.quantity
            )
        else:
            self._pending_buy_cost += self._estimate_cost(order, bar)
        return None

    def _estimate_cost(self, order: Order, bar: Bar) -> float:
        """按撮合同口径估算买入占用现金（无效开盘价视为零占用）。"""
        if not bar.open > 0:
            return 0.0
        buffer = 1 + self._cost.slippage + self._cost.commission_rate
        return order.quantity * bar.open * buffer

    def pending_sell(self, code: str) -> int:
        """该标的当日累计已委托卖出量（供 AvailabilityRule 扣减）。"""
        return self._pending_sell.get(code, 0)

    def pending_buy_cost(self) -> float:
        """当日已委托买入的估算占用现金（供 CashSufficiencyRule 扣减）。"""
        return self._pending_buy_cost

    def settle_sell(self, code: str, qty: int) -> None:
        """卖出成交后回冲当日委托量。

        成交时持仓已实际扣减，不回冲会让已成交部分继续留在 pending 里
        被重复计入，误拒同日后续卖出。委托被撮合层丢弃（如一字跌停）
        时不回冲，宁可保守拒单也不放超额卖出。
        """
        remaining = self._pending_sell.get(code, 0) - qty
        if remaining > 0:
            self._pending_sell[code] = remaining
        else:
            self._pending_sell.pop(code, None)

    def settle_buy(self, cost: float) -> None:
        """买入成交后按实际花费回冲当日现金占用。"""
        self._pending_buy_cost = max(0.0, self._pending_buy_cost - cost)

    def reset_pending(self) -> None:
        """新交易日开始时清空委托追踪（由引擎在 on_new_day 前调用）。"""
        self._pending_sell.clear()
        self._pending_buy_cost = 0.0
