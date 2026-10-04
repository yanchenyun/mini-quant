"""Web 控制台（FastAPI）：K线 + 策略辅助线 + 买卖点 + 净值曲线 + 绩效指标。

策略与参数均由策略注册表动态发现（GET /api/strategies），新增策略零入口
改动。webapp 只调用 app.service 与 strategy 注册表，不直接触碰数据层 /
引擎——依赖方向不倒置。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .. import __version__
from ..app.service import get_repo, run_backtest
from ..data.registry import FREQS, available_freqs, default_freq
from ..strategy import available_strategies, build_strategy, get_strategy

WEB_DIR = Path(__file__).resolve().parent

# 版本号单一来源：quant.__version__（避免与包版本各写一份而漂移）
app = FastAPI(title="mini-quant", version=__version__)


def _round_list(values) -> list:
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


@app.get("/api/freqs")
def freqs() -> list[dict[str, str]]:
    """频率清单：前端据此渲染频率下拉（注册表驱动，新增频率前端零改动）。"""
    return [{"name": f.name, "label": f.label} for f in FREQS]


@app.get("/api/strategies")
def strategies() -> list[dict[str, Any]]:
    """策略规格清单：前端据此渲染策略下拉与参数输入框（注册表驱动）。"""
    out: list[dict[str, Any]] = []
    for name in available_strategies():
        spec = get_strategy(name)
        out.append({
            "name": spec.name,
            "label": spec.label,
            "params": [{"name": p.name, "type": p.type.__name__,
                        "default": p.default,
                        "choices": list(p.choices) if p.choices else None,
                        "help": p.help} for p in spec.params],
        })
    return out


@app.get("/api/backtest")
def backtest(request: Request) -> dict[str, Any]:
    """运行回测。策略与参数由查询串动态解析（规格见 /api/strategies）。

    strategy 必传（前端下拉恒有值）——不设默认策略，避免沿用首项导致
    静默跑错策略。
    """
    q = request.query_params
    code, start = q.get("code"), q.get("start")
    end = q.get("end")
    freqs = available_freqs()
    freq = q.get("freq") or default_freq()
    strategy = q.get("strategy")

    if not code or not start:
        raise HTTPException(400, "code / start 必填")
    if not strategy:
        raise HTTPException(400,
                            f"strategy 必填（可选值: {available_strategies()}）")
    if freq not in freqs:
        raise HTTPException(400, f"freq 仅支持 {' / '.join(freqs)}")
    try:
        spec = get_strategy(strategy)
    except KeyError as e:
        raise HTTPException(400,
                            f"strategy 仅支持 {available_strategies()}") from e

    raw = {k: v for k, v in q.items()
           if k in {p.name for p in spec.params}}
    try:
        strat = build_strategy(strategy, raw)
        result, bars = run_backtest(code, start, end,
                                    strategy=strat, freq=freq)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
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

    # K 线辅助线：由策略规格的 overlay 钩子声明（均线 / 通道等），
    # 声明几条画几条，无钩子的策略为零条。
    overlays: list[dict[str, Any]] = []
    if spec.overlay is not None:
        for line_name, series in spec.overlay(bars, strat.params).items():
            overlays.append({"name": line_name, "data": _round_list(series)})

    buys = [[t["date"], t["price"], t["quantity"], t["commission"]] for t in result.trades
            if t["side"] == "buy"]
    sells = [[t["date"], t["price"], t["quantity"], t["profit"], t["commission"]]
             for t in result.trades if t["side"] == "sell"]

    return {
        "params": {"code": code, "freq": freq, "strategy": strategy,
                   "strategy_params": strat.params,
                   "start": start, "end": end},
        "strat_label": (spec.describe(strat.params) if spec.describe
                        else spec.label),
        "metrics": result.metrics,
        "kline": {
            "dates": bars[x_key].tolist(),
            "open": _round_list(bars["open"]),
            "high": _round_list(bars["high"]),
            "low": _round_list(bars["low"]),
            "close": _round_list(bars["close"]),
            "overlays": overlays,
        },
        "equity_curve": result.equity_curve,
        "benchmark": benchmark,
        "buys": buys, "sells": sells,
        "rejects": len(result.rejects),
    }
