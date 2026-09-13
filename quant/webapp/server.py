"""Web 控制台（FastAPI）：K线 + 均线 + 买卖点 + 净值曲线 + 绩效指标。

支持日线/5分钟、双均线/动量因子策略切换。
webapp 只调用 app.service，不直接触碰数据层 / 引擎 —— 依赖方向不倒置。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..app.service import get_repo, run_backtest
from ..strategy.double_ma import DoubleMAStrategy

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="mini-quant", version="0.3.0")


def _round_list(values: pd.Series) -> list:
    return [None if pd.isna(v) else round(float(v), 4) for v in values]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/codes")
def codes() -> list[str]:
    try:
        return get_repo().list_codes()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"读取数据库失败: {e}") from e


@app.get("/api/backtest")
def backtest(code: str, start: str, end: str,
             fast: int = 5, slow: int = 20,
             freq: str = "1d",
             strategy: str = "double_ma",
             window: int = 20) -> dict[str, Any]:
    if freq not in ("1d", "5min"):
        raise HTTPException(400, "freq 仅支持 1d / 5min")
    if strategy not in ("double_ma", "factor_momentum"):
        raise HTTPException(400, "strategy 仅支持 double_ma / factor_momentum")
    if strategy == "double_ma" and fast >= slow:
        raise HTTPException(400, "快线周期必须小于慢线")
    try:
        if strategy == "factor_momentum":
            from ..strategy.factor_momentum import FactorMomentumStrategy
            strat = FactorMomentumStrategy(window)
        else:
            strat = DoubleMAStrategy(fast, slow)
        result, bars = run_backtest(code, start, end,
                                    strategy=strat, freq=freq)
    except RuntimeError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"回测失败: {e}") from e

    # x 轴口径：分钟频率用 dt（每根 bar 唯一），日线 dt == date 天然兼容。
    # trades[].date 引擎侧即为 bar 的 dt，买卖点散点与 K 线 x 轴自动对齐。
    x_key = "dt" if "dt" in bars.columns else "date"
    bars = bars.sort_values(x_key)
    close = bars["close"].astype(float)

    # 基准：期初全仓买入持有。按 bar 粒度计算，确保与 K 线 x 轴等长
    # （分钟频率下每天 48 根 bar，基准也有 48 个点，图表自动对齐）。
    first = float(close.iloc[0])
    benchmark = [round(float(v) / first * result.metrics["init_cash"], 2)
                 for v in close]

    if strategy == "double_ma":
        ma_fast = _round_list(close.rolling(fast).mean())
        ma_slow = _round_list(close.rolling(slow).mean())
    else:
        ma_fast, ma_slow = [], []

    buys = [[t["date"], t["price"], t["quantity"], t["commission"]] for t in result.trades
            if t["side"] == "buy"]
    sells = [[t["date"], t["price"], t["quantity"], t["profit"], t["commission"]]
             for t in result.trades if t["side"] == "sell"]

    return {
        "params": {"code": code, "fast": fast, "slow": slow, "freq": freq,
                   "strategy": strategy, "window": window,
                   "start": start, "end": end},
        "metrics": result.metrics,
        "kline": {
            "dates": bars[x_key].tolist(),
            "open": _round_list(bars["open"]),
            "high": _round_list(bars["high"]),
            "low": _round_list(bars["low"]),
            "close": _round_list(bars["close"]),
            "ma_fast": ma_fast, "ma_slow": ma_slow,
        },
        "equity_curve": result.equity_curve,
        "benchmark": benchmark,
        "buys": buys, "sells": sells,
        "rejects": len(result.rejects),
    }
