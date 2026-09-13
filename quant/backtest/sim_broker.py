"""SimBroker：模拟撮合（Broker 实现），内置 A 股交易规则。

订单在次一 bar 以开盘价撮合（避免未来函数）；市价单按开盘价 ± 滑点，
限价单在 bar 内触及限价时成交（开盘跳空但盘中回到限价仍可成交）。
涨停一字板拒买、跌停一字板拒卖；佣金双边、印花税仅卖出。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Bar, Fill, Order, Side

# _try_fill 的哨兵返回值：表示"不成交但保留订单"
_SKIP = object()


@dataclass
class CostModel:
    commission_rate: float = 0.00025   # 佣金 万2.5
    min_commission: float = 5.0        # 最低佣金
    stamp_tax: float = 0.0005          # 印花税（仅卖出）
    slippage: float = 0.002            # 滑点 0.2%（千2）
    price_limit: float = 0.095         # 涨跌停近似阈值

    def commission(self, side: Side, amount: float) -> float:
        fee = amount * self.commission_rate
        fee = max(fee, self.min_commission)
        if side == Side.SELL:
            fee += amount * self.stamp_tax
        return fee


class SimBroker:
    """回测撮合。将来的 QmtBroker 实现同一 Broker 接口即可切实盘。

    挂单 TTL（order.ttl_bars）：本 bar 尝试撮合未成交时递减，TTL 到期自动撤。
    默认 ttl_bars=1（A 股当日有效）—— 信号当根的下一根 bar 仍未成交，
    则按"收市前撤单"语义自动丢弃，规避"信号 t 在 t+30 才成交"的假象。
    过期的单不入 rejects —— 撤单是预期行为，不是异常。
    """

    def __init__(self, cost: CostModel | None = None):
        self.cost = cost or CostModel()
        # (order, remaining_bars) 元组列表：Settle 时逐根 bar 倒计时
        self._pending: list[tuple[Order, int]] = []

    def submit(self, order: Order) -> None:
        self._pending.append((order, order.ttl_bars))

    def settle(self, bar: Bar) -> list[Fill]:
        """以本 bar 开盘价撮合挂起的订单（日线=次一交易日；分钟=次一 bar）。

        TTL 处理：未成交的挂单 remaining_bars -= 1；<= 0 时到期自动撤（不入 rejects）。
        """
        if not self._pending:
            return []

        fills: list[Fill] = []
        still_pending: list[tuple[Order, int]] = []
        for order, remaining in self._pending:
            if order.code != bar.code:
                # 多标的时留待各自 bar 处理，TTL 不递减（不归本 bar 计数）
                still_pending.append((order, remaining))
                continue
            if remaining <= 0:
                # TTL 已耗尽，自动撤单（非异常，不入 rejects）
                continue
            new_remaining = remaining - 1
            result = self._try_fill(order, bar)
            if result is None:
                continue                      # 涨跌停/停牌：放弃该订单
            if result == _SKIP:
                still_pending.append((order, new_remaining))   # 限价单跳空未成交，递减 TTL 后待下根 bar 再试
                continue
            assert isinstance(result, Fill)
            fills.append(result)
        self._pending = still_pending
        return fills

    def _try_fill(self, order: Order, bar: Bar) -> Fill | None | object:
        if not bar.tradable:
            return _SKIP   # 停牌/ST：保留订单，复牌后继续撮合（而非丢弃）
        # 涨跌停：只有一字板才拒买/拒卖（全天封死，无成交机会）。
        # 开盘触板但盘中打开（非一字板）时，交由后续限价单逻辑处理。
        if bar.pre_close > 0:
            upper = bar.pre_close * (1 + self.cost.price_limit)
            lower = bar.pre_close * (1 - self.cost.price_limit)
            if order.side == Side.BUY and bar.open >= upper and bar.low >= upper:
                return None   # 一字涨停：全天封死在涨停，买不进
            if order.side == Side.SELL and bar.open <= lower and bar.high <= lower:
                return None   # 一字跌停：全天封死在跌停，卖不出

        # 限价单：整根 bar 的价格区间未触及限价 → 不成交，保留待下根 bar
        if order.price:
            if order.side == Side.BUY and bar.low > order.price:
                return _SKIP    # 整根 bar 最低价都高于买限价，买不到
            if order.side == Side.SELL and bar.high < order.price:
                return _SKIP    # 整根 bar 最高价都低于卖限价，卖不出

        if order.side == Side.BUY:
            base = bar.open * (1 + self.cost.slippage)
            # 涨停开盘但盘中打开：成交价不能超过涨停价
            if bar.pre_close > 0:
                upper = bar.pre_close * (1 + self.cost.price_limit)
                base = min(base, upper)
            if order.price:
                # 限价单：开盘跳空高于限价但盘中回落触及 → 以限价成交
                price = order.price if bar.open > order.price else min(order.price, base)
            else:
                price = base
        else:
            base = bar.open * (1 - self.cost.slippage)
            # 跌停开盘但盘中打开：成交价不能低于跌停价
            if bar.pre_close > 0:
                lower = bar.pre_close * (1 - self.cost.price_limit)
                base = max(base, lower)
            if order.price:
                # 限价单：开盘跳空低于限价但盘中回升触及 → 以限价成交
                price = order.price if bar.open < order.price else max(order.price, base)
            else:
                price = base

        amount = price * order.quantity
        commission = self.cost.commission(order.side, amount)
        return Fill(
            order=order, filled_qty=order.quantity, filled_price=round(price, 4),
            commission=round(commission, 4), filled_at=bar.dt,
            slippage_cost=round(abs(price - bar.open) * order.quantity, 2),
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)
