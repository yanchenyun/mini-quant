"""核心层用例：风控责任链语义与策略基类的严格契约。"""
from __future__ import annotations

from quant.backtest.risk import AvailabilityRule, RiskChain
from quant.core.abstractions import Strategy
from quant.core.models import Bar, Order, Position, Side
from quant.core.portfolio import Portfolio


def test_risk_chain() -> None:
    """风控链：空规则语义 / 显式挂载 / 策略元数据的严格契约。"""
    # 1) 空列表表示"不做风控"，不再被 or 吞掉换成默认规则集
    assert len(RiskChain().rules) == 4, "缺省应装配默认规则集"
    assert RiskChain([]).rules == [], "空列表应表示无规则"

    # 2) 需要链上下文的规则在装配时显式收到上下文（不再靠 hasattr 试探）
    chain = RiskChain()
    rule = [r for r in chain.rules if isinstance(r, AvailabilityRule)][0]
    assert rule._ctx is chain, "AvailabilityRule 应在装配时挂载到链上"

    pf = Portfolio(init_cash=1_000_000)
    pf.positions["sh.600000"] = Position(code="sh.600000", quantity=100,
                                         available=100, avg_cost=10.0)
    bar = Bar(code="sh.600000", dt="2024-01-02", trade_date="2024-01-02",
              open=10.0, high=10.0, low=10.0, close=10.0)
    sell = Order(code="sh.600000", side=Side.SELL, quantity=100)

    # 3) 未挂载就单独调用 → 显式报错（而不是在 None 上取属性崩掉）
    try:
        AvailabilityRule().check(sell, pf, bar)
    except RuntimeError as e:
        assert "未挂载" in str(e), e
    else:
        raise AssertionError("未挂载的规则应抛 RuntimeError")
    assert rule.check(sell, pf, bar) is None, "可卖足额应放行"

    # 4) 策略元数据由子类在 __init__ 显式赋值：基类不留可变默认，漏写即报错
    assert not hasattr(Strategy, "params"), "基类不应带类级 params 默认值"
    assert not hasattr(Strategy, "required_factors"), "基类不应带类级因子默认值"

    class Forgetful(Strategy):
        def on_init(self, ctx) -> None:
            pass

        def on_bar(self, ctx, bar) -> None:
            pass

    forgetful = Forgetful()
    for attr in ("params", "required_factors"):
        try:
            getattr(forgetful, attr)
        except AttributeError:
            continue
        raise AssertionError(f"子类漏写 {attr} 时应抛 AttributeError")
    print("风控链与策略元数据用例通过 ✓")
