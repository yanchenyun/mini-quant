"""服务层：数据接入 / 因子计算 / 回测执行（供 CLI 与 Web 共用，避免业务逻辑散落）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResult
from ..backtest.sim_broker import CostModel
from ..config import BacktestSettings, Settings, load_settings
from ..core.abstractions import Strategy
from ..data.mysql_repo import MySQLBarRepo
from ..data.registry import (default_freq, get_freq, get_source,
                             periods_per_year)
from ..factor import FactorEngine, get as get_factor


def get_repo(settings: Settings | None = None) -> MySQLBarRepo:
    settings = settings or load_settings()
    return MySQLBarRepo(settings.db)


def ingest_bars(code: str, start: str, end: str | None = None,
                adjust: str = "2", freq: str | None = None,
                *, source: str, init_schema: bool = False) -> int:
    """增量抓取并存入 MySQL。从库中已有最新 bar 的当日/次日续抓。

    source 为必填关键字参数，取值见 data.registry 的数据源注册表。
    freq 为 None 时取 registry 的默认频率（'1d'）。派生频率
    （15/30/60min，读取时从 5min 数据聚合）不可 ingest——在抓取前
    即拒绝，避免空跑一次网络请求。
    """
    freq = freq or default_freq()
    spec = get_freq(freq)
    if spec.derived_from:
        raise ValueError(
            f"{freq} 为派生频率（读取时从 {spec.derived_from} 数据自动聚合），"
            f"无需也无法单独入库；请 ingest 源频率 {spec.derived_from}")
    repo, src = get_repo(), get_source(source)
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
        print(f"[{code}/{freq}] 数据已是最新（截至 {first_start}），无需更新")
        return 0

    df = src.fetch_bars(code, first_start, end, freq=freq, adjust=adjust)
    n = repo.save_bars(df, freq=freq)
    print(f"[{code}/{freq}] {first_start} ~ {end} 入库 {len(df)} 行"
          f"（upsert 影响行数 {n}）")
    return len(df)


def run_backtest(code: str, start: str, end: str | None = None,
                 *, strategy: Strategy, freq: str | None = None,
                 adjust: str | None = None,
                 settings: Settings | None = None) -> tuple[BacktestResult, pd.DataFrame]:
    """从 MySQL 取数 → 跑回测。返回 (结果, 行情帧) 供 CLI / Web 渲染。

    strategy 必传（由调用方经 quant.strategy.build_strategy 构造，service
    不感知任何具体策略）。freq 取值见 data.registry 的频率注册表；策略
    写法与频率无关，指标按 bar 计算。因子：strategy.required_factors 声明依赖时，
    自动多加载预热窗口的历史即时计算（与行情同帧同口径——SSOT），再裁剪
    回测区间注入引擎。

    freq 为 None 时取 registry 默认频率；adjust 为 None 时取 backtest 配置
    （默认前复权 '2'）。年化基准由频率推导（日线 252，分钟线按每日 bar 数
    折算），避免分钟级回测的年化收益被错误放大或压缩。
    """
    settings = settings or load_settings()
    bt: BacktestSettings = settings.backtest
    freq = freq or default_freq()
    adjust = adjust or bt.adjust

    # end 未指定时默认今天（与 ingest 的语义一致）
    end = end or datetime.now().strftime("%Y-%m-%d")

    names = list(strategy.required_factors)
    load_start = start
    if names:
        # 因子预热（lookback）：多加载 max(min_periods) 的历史算因子，
        # 保证回测首日即有有效值（日历日 ≈ 1.4×交易日，取 1.6 倍裕量）
        lookback = max(get_factor(n).min_periods for n in names)
        load_start = (date.fromisoformat(start)
                      - timedelta(days=int(lookback * 1.6) + 10)).isoformat()

    repo = get_repo(settings)
    bars = repo.load_bars([code], load_start, end, freq=freq, adjust=adjust)
    if bars.empty:
        raise RuntimeError(f"无数据：{code} {load_start}~{end}（请先执行 ingest）")

    factor_frame = None
    if names:
        factor_frame = FactorEngine.compute(bars, names)
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
        periods_per_year=periods_per_year(freq),
    )
    return engine.run(), bars


def compute_factors(code: str, start: str, end: str | None = None,
                    freq: str | None = None, *, names: list[str],
                    adjust: str | None = None,
                    init_schema: bool = False) -> int:
    """加载行情 → 计算因子 → 落库（upsert 幂等，重算即覆盖）。

    names 为必填关键字参数（要算哪些因子由调用方明确指定，不隐式算全部）。
    落库是"物化缓存"：供选股/因子分析等非回测场景读取
    （回测永远内存即时计算，与行情同帧同口径）。
    """
    repo = get_repo()
    if init_schema:
        repo.init_schema()

    freq = freq or default_freq()
    adjust = adjust or load_settings().backtest.adjust
    end = end or datetime.now().strftime("%Y-%m-%d")
    bars = repo.load_bars([code], start, end, freq=freq, adjust=adjust)
    if bars.empty:
        raise RuntimeError(f"无数据：{code} {start}~{end}（请先执行 ingest）")

    frame = FactorEngine.compute(bars, names)
    n = repo.save_factors(frame, freq)
    for c in names:
        valid = int(frame[c].notna().sum())
        print(f"  [{code}/{freq}] {c:20s} 有效值 {valid}/{len(frame)} 行")
    print(f"[{code}/{freq}] {start} ~ {end} 因子入库完成"
          f"（{len(names)} 个因子，upsert 影响行数 {n}）")
    return n
