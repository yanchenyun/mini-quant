"""Portfolio：现金 / 持仓 / 净值曲线的记账本（单一职责——只管账，不管策略与撮合）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fill, Position, Side, TradeRecord


@dataclass
class Portfolio:
    init_cash: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    equity_curve: list[dict] = field(default_factory=list)   # {date, cash, market_value, equity}
    trades: list[TradeRecord] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.init_cash

    # ── 查询 ─────────────────────────────────────────────
    def position(self, code: str) -> Position | None:
        return self.positions.get(code)

    def equity(self, prices: dict[str, float]) -> float:
        mv = sum(
            pos.quantity * prices.get(pos.code, pos.avg_cost)
            for pos in self.positions.values()
        )
        return self.cash + mv

    # ── 记账 ─────────────────────────────────────────────
    def on_new_day(self) -> None:
        for pos in self.positions.values():
            pos.on_new_day()

    def apply_fill(self, fill: Fill) -> None:
        """按成交更新现金与持仓。卖出时结算本轮盈亏。"""
        o = fill.order
        gross = fill.filled_price * fill.filled_qty
        pos = self.positions.setdefault(o.code, Position(code=o.code))

        if o.side == Side.BUY:
            self.cash -= gross + fill.commission
            # avg_cost 须含买入佣金摊薄（on_buy 契约：price_incl_cost 为含佣单价），
            # 否则卖出利润公式 (sell_price - avg_cost)*qty - sell_commission
            # 会重复扣减买入侧费用（avg_cost 偏低 → 利润偏低）。
            pos.on_buy(fill.filled_qty,
                       fill.filled_price + fill.commission / fill.filled_qty)
            profit = None
        else:
            self.cash += gross - fill.commission
            profit = round(
                (fill.filled_price - pos.avg_cost) * fill.filled_qty - fill.commission, 2
            )
            pos.on_sell(fill.filled_qty)

        self.fills.append(fill)
        self.trades.append(TradeRecord(
            date=fill.filled_at, code=o.code, side=o.side.value,
            price=fill.filled_price, quantity=fill.filled_qty,
            commission=round(fill.commission, 2), profit=profit,
        ))

    def snapshot(self, date: str, prices: dict[str, float]) -> None:
        mv = sum(
            pos.quantity * prices.get(pos.code, pos.avg_cost)
            for pos in self.positions.values()
        )
        self.equity_curve.append({
            "date": date,
            "cash": round(self.cash, 2),
            "market_value": round(mv, 2),
            "equity": round(self.cash + mv, 2),
        })
