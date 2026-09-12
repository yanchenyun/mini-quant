"""事件驱动回测引擎：数据逐 bar 推进，订单次一 bar 开盘成交。

同一套引擎 + SimBroker 即回测；将来换成 LiveFeed + QmtBroker 即实盘
（主循环不改）—— 这是本架构最重要的一条投资。

v0.2 频率无关改造（日线/5分钟共用本引擎）：
- 主循环按 trade_date 分组（而非 dt），保证 T+1 解禁、日始钩子、日终快照
  的"日"语义在分钟频率下依然正确——这正是"bar 时刻"与"归属交易日"
  解耦（Bar.dt / Bar.trade_date 双字段）的意义；
- 净值快照按交易日收盘口径记录，绩效年化仍以 252 个交易日为基准；
- 策略可见的 history 索引为 bar.dt，指标计算（rolling 等）与频率无关。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..core.abstractions import (FactorAccessor, PortfolioView, RiskRule,
                                 Strategy, StrategyContext)
from ..core.models import Bar, Order
from ..core.portfolio import Portfolio
from .metrics import compute_metrics
from .risk import RiskChain
from .sim_broker import CostModel, SimBroker


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    equity_curve: list[dict]
    trades: list[dict]
    rejects: list[dict]


class BacktestEngine:
    def __init__(self, bars: pd.DataFrame, strategy: Strategy,
                 init_cash: float = 1_000_000,
                 cost: CostModel | None = None,
                 risk_rules: list[RiskRule] | None = None,
                 periods_per_year: int = 252,
                 factor_frame: pd.DataFrame | None = None):
        """
        factor_frame：因子宽表 [code, dt, <因子列…>]（FactorEngine.compute 产出，
        服务层按 strategy.required_factors 自动计算注入）。策略通过
        ctx.factor(name) / ctx.factor_history(name) 只读访问——引擎按
        bar 进度切片，结构上杜绝未来数据（防未来函数）。
        """
        self.strategy = strategy
        self.init_cash = init_cash
        self.cost = cost or CostModel()
        self.broker = SimBroker(self.cost)
        self.risk = RiskChain(risk_rules)
        # 净值按交易日快照，故年化基准默认 252；如改按 bar 快照可传
        # periods_per_year=252*48（5分钟）等
        self.periods_per_year = periods_per_year

        self.portfolio = Portfolio(init_cash=init_cash)
        self.bars = self._normalize(bars)
        self._factor_dfs = self._attach_factors(factor_frame)
        self._history: dict[str, pd.Series] = {}   # code -> 收盘价累计序列
        self._prices: dict[str, float] = {}        # code -> 最新收盘价
        self._view = self._PortfolioViewImpl(self.portfolio)

    # 引擎自身作为 OrderSink（策略只认识抽象入口）
    class _PortfolioViewImpl(PortfolioView):
        def __init__(self, pf: Portfolio):
            self._pf = pf

        @property
        def cash(self) -> float:
            return self._pf.cash

        def position(self, code: str):
            return self._pf.position(code)

        def equity(self, prices: dict[str, float]) -> float:
            return self._pf.equity(prices)

    def submit(self, order: Order) -> None:
        bar = self._today_bar.get(order.code)
        if bar is None or self.risk.check(order, self._view, bar) is not None:
            return
        self.broker.submit(order)

    # ── 数据规范化：统一 dt / trade_date / date 列 ─────────
    @staticmethod
    def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        if "dt" not in df.columns:
            df["dt"] = df["date"].astype(str)
        df["dt"] = df["dt"].astype(str)
        df["trade_date"] = df["dt"].str[:10]
        if "date" not in df.columns:
            df["date"] = df["trade_date"]
        return df.sort_values(["dt", "code"]).reset_index(drop=True)

    # ── 因子注入：宽表按 code+dt 左连接，切为每标的子帧 ─────
    def _attach_factors(self,
                        factor_frame: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
        """返回 code → 子帧（index=dt，列=因子名，行序=bar 时间序）。

        左连接保持 self.bars 行数不变（因子缺行 → NaN），因此子帧第 n 行
        与该标的第 n 根 bar 严格对齐——主循环用 iloc[:n] 切片即防未来视图。
        """
        if factor_frame is None or factor_frame.empty:
            return {}
        factor_cols = [c for c in factor_frame.columns if c not in ("code", "dt")]
        if not factor_cols:
            return {}
        merged = self.bars.merge(
            factor_frame[["code", "dt", *factor_cols]],
            on=["code", "dt"], how="left")
        out: dict[str, pd.DataFrame] = {}
        for code, g in merged.groupby("code", sort=False):
            out[code] = g.set_index("dt")[factor_cols]
        return out

    # ── 主循环 ──────────────────────────────────────────
    def run(self) -> BacktestResult:
        self._today_bar: dict[str, Bar] = {}
        self._seen: dict[str, int] = {}          # code -> 已推进 bar 数（因子切片用）
        self.strategy.on_init(StrategyContext(
            pd.Series(dtype=float), "", self._view, self, 0.0))

        # 按交易日驱动：一天内的分钟 bar 依 dt 顺序逐根推进
        for trade_date, day_df in self.bars.groupby("trade_date", sort=True):
            self.portfolio.on_new_day()            # T+1 解禁（每个交易日仅一次）
            first_bar_of_day = True

            for row in day_df.itertuples(index=False):
                bar = self._to_bar(row, trade_date)
                self._today_bar[bar.code] = bar

                # 1) 撮合此前提交的订单（本 bar 开盘价；分钟级即 5 分钟后成交）
                for fill in self.broker.settle(bar):
                    self.portfolio.apply_fill(fill)

                # 2) 累计历史并构造上下文（策略只看得到截至本 bar 的数据）
                hist = self._history.setdefault(
                    bar.code, pd.Series(dtype=float))
                self._history[bar.code] = pd.concat(
                    [hist, pd.Series([bar.close], index=[bar.dt])])
                self._prices[bar.code] = bar.close

                # 因子只读视图：iloc[:n] 切片，结构上排除未来行
                n = self._seen.get(bar.code, 0) + 1
                self._seen[bar.code] = n
                accessor = (FactorAccessor(self._factor_dfs[bar.code].iloc[:n])
                            if bar.code in self._factor_dfs else None)

                ctx = StrategyContext(
                    self._history[bar.code], bar.dt, self._view, self,
                    bar.close, factors=accessor)

                # 3) 日始钩子：仅当日第一根 bar 触发（撮合结算之后、on_bar 之前）
                if first_bar_of_day:
                    self.strategy.on_new_day(ctx, bar)
                    first_bar_of_day = False

                # 4) 策略逻辑
                self.strategy.on_bar(ctx, bar)

            # 5) 日终快照（收盘口径；净值曲线每个交易日一条）
            self.portfolio.snapshot(trade_date, self._prices)

        metrics = compute_metrics(self.portfolio.equity_curve,
                                  self.portfolio.trades,
                                  periods_per_year=self.periods_per_year)
        metrics["init_cash"] = self.init_cash
        return BacktestResult(
            metrics=metrics,
            equity_curve=self.portfolio.equity_curve,
            trades=[t.__dict__ for t in self.portfolio.trades],
            rejects=self.risk.rejects,
        )

    @staticmethod
    def _to_bar(row, trade_date: str) -> Bar:
        ts = getattr(row, "trade_status", None)
        ts = 1 if ts is None else int(ts)   # 注意 0（停牌）是有效值，不可 or 兜底
        return Bar(
            code=row.code, dt=str(row.dt), trade_date=str(trade_date),
            open=float(row.open), high=float(row.high),
            low=float(row.low), close=float(row.close),
            pre_close=float(getattr(row, "pre_close", 0) or 0),
            volume=float(getattr(row, "volume", 0) or 0),
            amount=float(getattr(row, "amount", 0) or 0),
            trade_status=ts,
            is_st=int(getattr(row, "is_st", 0) or 0),
        )
