"""绩效指标：全部由净值曲线与成交记录推导，纯函数、可单测。

年化基准 periods_per_year：引擎按"交易日"记录净值快照，故日线与分钟级
回测均默认 252；若改为按 bar 快照（分钟级日内权益），应传 252×48。
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def compute_metrics(equity_curve: list[dict], trades: list,
                    periods_per_year: int = 252) -> dict[str, Any]:
    if not equity_curve:
        return {"error": "无净值数据"}

    eq = pd.Series(
        [r["equity"] for r in equity_curve],
        index=pd.to_datetime([r["date"] for r in equity_curve]),
    )
    ret = eq.pct_change().dropna()
    n_days = len(eq)

    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    ppy = max(periods_per_year, 1)
    annual_return = (1 + total_return) ** (ppy / max(n_days, 1)) - 1 if n_days > 1 else 0.0

    # 最大回撤（含起止区间）
    cummax = eq.cummax()
    dd = eq / cummax - 1
    max_dd = float(dd.min()) if len(dd) else 0.0
    dd_end = dd.idxmin() if len(dd) and max_dd < 0 else None
    dd_start = eq.loc[:dd_end].idxmax() if dd_end is not None else None

    vol = float(ret.std(ddof=1) * (ppy ** 0.5)) if len(ret) > 2 else 0.0
    sharpe = float(ret.mean() / ret.std(ddof=1) * (ppy ** 0.5)) \
        if len(ret) > 2 and ret.std(ddof=1) > 0 else 0.0
    calmar = annual_return / abs(max_dd) if max_dd < 0 else 0.0

    # 交易统计（卖出即平仓视为一轮）
    sell_trades = [t for t in trades if t.side == "sell" and t.profit is not None]
    wins = [t for t in sell_trades if t.profit > 0]
    losses = [t for t in sell_trades if t.profit <= 0]
    gross_win = sum(t.profit for t in wins)
    gross_loss = abs(sum(t.profit for t in losses))
    total_commission = sum(t.commission for t in trades)

    return {
        "start_date": equity_curve[0]["date"],
        "end_date": equity_curve[-1]["date"],
        "trading_days": n_days,
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_return": _pct(total_return),
        "annual_return": _pct(annual_return),
        "max_drawdown": _pct(max_dd),
        "max_dd_range": (f"{pd.Timestamp(dd_start).strftime('%Y-%m-%d')} ~ "
                         f"{pd.Timestamp(dd_end).strftime('%Y-%m-%d')}")
                        if dd_start is not None and dd_end is not None else "-",
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "annual_volatility": _pct(vol),
        "trade_count": len(sell_trades),
        "win_rate": _pct(len(wins) / len(sell_trades)) if sell_trades else None,
        "profit_factor": round(gross_win / gross_loss, 3)
                          if gross_loss > 0 else (None if gross_win == 0 else float("inf")),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "total_commission": round(total_commission, 2),
    }


def _pct(x: float | None) -> float | None:
    return round(x * 100, 3) if x is not None else None
