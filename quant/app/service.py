"""服务层：数据接入 / 因子计算 / 回测执行（供 CLI 与 Web 共用，避免业务逻辑散落）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResult
from ..backtest.sim_broker import CostModel
from ..config import BacktestSettings, Settings, load_settings
from ..core.abstractions import Strategy
from ..data.baostock_source import BaostockSource
from ..data.mysql_repo import MySQLBarRepo
from ..factor import FactorEngine, available as available_factors, get as get_factor
from ..strategy.double_ma import DoubleMAStrategy


def get_repo(settings: Settings | None = None) -> MySQLBarRepo:
    settings = settings or load_settings()
    return MySQLBarRepo(settings.db)


def get_source() -> BaostockSource:
    return BaostockSource()


def ingest_bars(code: str, start: str, end: str | None = None,
                adjust: str = "2", freq: str = "1d",
                init_schema: bool = False) -> int:
    """增量抓取并存入 MySQL。从库中已有最新 bar 的当日/次日续抓。

    分钟线的增量粒度为"日"（baostock start_date 只接受日期）：
    从最新 bar 的归属交易日整天重抓，upsert 幂等不产生重复。
    """
    repo, src = get_repo(), get_source()
    if init_schema:
        repo.init_schema()

    end = end or datetime.now().strftime("%Y-%m-%d")
    first_start = start
    latest = repo.latest_bar_time(code, freq=freq, adjust=adjust)
    if latest:
        # 取最新 bar 的日期部分，整天重抓（幂等）
        latest_day = latest[:10]
        first_start = max(first_start, latest_day)

    if first_start > end:
        print(f"[{code}/{freq}] 数据已是最新（截至 {latest}），无需更新")
        return 0

    df = src.fetch_bars(code, first_start, end, freq=freq, adjust=adjust)
    n = repo.save_bars(df, freq=freq)
    print(f"[{code}/{freq}] {first_start} ~ {end} 入库 {len(df)} 行"
          f"（upsert 影响行数 {n}）")
    return len(df)


# v0.1 兼容别名
def ingest_daily(code: str, start: str, end: str | None = None,
                 adjust: str = "2", init_schema: bool = False) -> int:
    return ingest_bars(code, start, end, adjust=adjust, freq="1d",
                       init_schema=init_schema)


def run_backtest(code: str, start: str, end: str | None = None,
                 strategy: Strategy | None = None,
                 fast: int = 5, slow: int = 20,
                 freq: str = "1d",
                 settings: Settings | None = None) -> tuple[BacktestResult, pd.DataFrame]:
    """从 MySQL 取数 → 跑回测。返回 (结果, 行情帧) 供 CLI / Web 渲染。

    freq: '1d' 日线；'5min' 5分钟线（策略写法完全一致，指标按 bar 计算）。
    因子：strategy.required_factors 声明依赖时，自动多加载预热窗口的历史
    即时计算（与行情同帧同口径——SSOT），再裁剪回测区间注入引擎。
    """
    settings = settings or load_settings()
    bt: BacktestSettings = settings.backtest

    # end 未指定时默认今天（与 ingest 的语义一致）
    end = end or datetime.now().strftime("%Y-%m-%d")
    strategy = strategy or DoubleMAStrategy(fast, slow)

    names = list(getattr(strategy, "required_factors", None) or [])
    load_start = start
    if names:
        # 因子预热（lookback）：多加载 max(min_periods) 的历史算因子，
        # 保证回测首日即有有效值（日历日 ≈ 1.4×交易日，取 1.6 倍裕量）
        lookback = max(get_factor(n).min_periods for n in names)
        load_start = (date.fromisoformat(start)
                      - timedelta(days=int(lookback * 1.6) + 10)).isoformat()

    repo = get_repo(settings)
    bars = repo.load_bars([code], load_start, end, freq=freq, adjust="2")
    if bars.empty:
        raise RuntimeError(f"无数据：{code} {load_start}~{end}（请先执行 ingest）")

    factor_frame = None
    if names:
        factor_frame = FactorEngine().compute(bars, names)
    if load_start < start:
        # 裁剪回测区间：预热行情只参与因子计算，不进入回测主循环
        bars = bars[bars["trade_date"] >= start]
    if bars.empty:
        raise RuntimeError(f"无数据：{code} {start}~{end}（请先执行 ingest）")

    engine = BacktestEngine(
        bars=bars,
        strategy=strategy,
        init_cash=bt.init_cash,
        cost=CostModel(commission_rate=bt.commission_rate,
                        min_commission=bt.min_commission,
                        stamp_tax=bt.stamp_tax, slippage=bt.slippage,
                        price_limit=bt.price_limit),
        factor_frame=factor_frame,
    )
    return engine.run(), bars


def compute_factors(code: str, start: str, end: str | None = None,
                    freq: str = "1d", names: list[str] | None = None,
                    init_schema: bool = False) -> int:
    """加载行情 → 计算因子 → 落库（upsert 幂等，重算即覆盖）。

    落库是"物化缓存"：供选股/因子分析等非回测场景读取
    （回测永远内存即时计算，与行情同帧同口径）。
    """
    repo = get_repo()
    if init_schema:
        repo.init_schema()

    end = end or datetime.now().strftime("%Y-%m-%d")
    names = names or available_factors()
    bars = repo.load_bars([code], start, end, freq=freq, adjust="2")
    if bars.empty:
        raise RuntimeError(f"无数据：{code} {start}~{end}（请先执行 ingest）")

    frame = FactorEngine().compute(bars, names)
    n = repo.save_factors(frame, freq)
    for c in names:
        valid = int(frame[c].notna().sum())
        print(f"  [{code}/{freq}] {c:20s} 有效值 {valid}/{len(frame)} 行")
    print(f"[{code}/{freq}] {start} ~ {end} 因子入库完成"
          f"（{len(names)} 个因子，upsert 影响行数 {n}）")
    return n
