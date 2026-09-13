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

    def apply_fill(self, fill: Fill) -> bool:
        """按成交更新现金与持仓。卖出时结算本轮盈亏。

        买入时检查现金足额：若现金不足则跳过该笔成交（防止现金变负）。
        这是风控与撮合口径不一致时的最后防线（风控用当前 bar open 估算，
        撮合用次 bar open 实际成交，跳空高开时实际花费可能超过估算）。

        Returns:
            True 表示成交已记账；False 表示现金不足被跳过（调用方应记拒单日志）。
        """
        o = fill.order
        gross = fill.filled_price * fill.filled_qty
        pos = self.positions.setdefault(o.code, Position(code=o.code))

        if o.side == Side.BUY:
            # 现金足额校验：防止风控漏判导致现金变负
            cost_needed = gross + fill.commission
            if cost_needed > self.cash:
                return False  # 现金不足，跳过该笔成交
            self.cash -= cost_needed
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
            date=fill.filled_at, code=o.code, side=o.side,
            price=fill.filled_price, quantity=fill.filled_qty,
            commission=round(fill.commission, 2), profit=profit,
        ))
        return True

    def snapshot(self, date: str, prices: dict[str, float],
                 last_prices: dict[str, float] | None = None) -> None:
        """记录当日净值快照。

        Args:
            date: 交易日 'YYYY-MM-DD'。
            prices: 当日最新价（code → close），用于持仓市值估值。
            last_prices: 各标的历史最近 close 快照。回测引擎在主循环中
                维护该映射（持仓标的被停牌当日无 bar 时，self._prices
                不会有该 code，此时优先用 last_prices，最后才退到 avg_cost——
                避免"一字跌停日的停牌股估值仍按买入均价"的高估偏差）。
        """
        last_prices = last_prices or {}
        mv = sum(
            pos.quantity * prices.get(
                pos.code, last_prices.get(pos.code, pos.avg_cost))
            for pos in self.positions.values()
        )
        self.equity_curve.append({
            "date": date,
            "cash": round(self.cash, 2),
            "market_value": round(mv, 2),
            "equity": round(self.cash + mv, 2),
        })
