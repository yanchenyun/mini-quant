"""事件驱动回测引擎：数据逐 bar 推进，订单次一 bar 开盘成交。

同一套引擎 + 默认 SimBroker 即回测；换实盘通道只需注入另一个 Broker 实现，
主循环与策略代码零改动（LSP）。主循环按 trade_date 分组，保证 T+1 / 日始
钩子的“日”语义在分钟频率下依然正确。策略可见的 history 索引为 bar.dt，
指标计算与频率无关。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import pandas as pd

from ..core.abstractions import (Broker, FactorAccessor, PortfolioView,
                                 RiskRule, Strategy, StrategyContext)
from ..core.models import Bar, Order, Side
from ..core.portfolio import Portfolio
from .metrics import compute_metrics
from .risk import RiskChain
from .sim_broker import CostModel, SimBroker


@dataclass
class BacktestResult:
    """回测结果：绩效指标 + 净值曲线 + 成交记录 + 拒单（风控拒绝与未成单意图）。"""
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
                 factor_frame: pd.DataFrame | None = None,
                 broker: Broker | None = None):
        """
        Args:
            bars: 统一列行情 DataFrame（至少含 code/dt/open/high/low/close）。
            strategy: 策略实例（实现 on_init / on_bar 钩子）。
            init_cash: 初始资金（默认 100 万）。
            cost: 成本模型（佣金/印花税/滑点），默认 CostModel()。
            risk_rules: 风控规则列表；None 用内置默认规则集，空列表表示不做风控。
            periods_per_year: 年化基准（默认 252 个交易日）。
            factor_frame: 因子宽表 [code, dt, <因子列…>]，由 FactorEngine.compute
                产出、服务层按 strategy.required_factors 自动注入。策略通过
                ctx.factor(name) 只读访问——引擎按 bar 进度 iloc[:n] 切片，
                结构上杜绝未来数据。
            broker: 撮合通道（实现 Broker 协议）；None 时用内置回测撮合
                SimBroker。换实盘通道只需注入一个实现类，引擎主循环零改动。
        """
        self.strategy = strategy
        self.init_cash = init_cash
        self.cost = cost or CostModel()
        self.broker: Broker = (broker if broker is not None
                               else SimBroker(self.cost))
        self.risk = RiskChain(risk_rules, cost=self.cost)
        # 净值按交易日快照，故年化基准默认 252；如改按 bar 快照可传
        # periods_per_year=252*48（5分钟）等
        self.periods_per_year = periods_per_year

        self.portfolio = Portfolio(init_cash=init_cash)
        self.bars = self._normalize(bars)
        self._factor_dfs = self._attach_factors(factor_frame)
        # 预构建每标的收盘价数组（消除主循环中逐 bar 构造 pd.Series 的 O(n²) 开销）
        self._full_prices: dict[str, np.ndarray] = {}
        self._history: dict[str, tuple[np.ndarray, np.ndarray, int]] = {}  # code → (prices, dts, count)
        self._prices: dict[str, float] = {}        # code → 当日最新收盘价（用于净值计算）
        self._last_close: dict[str, float] = {}    # code → 历史最近 close（停牌日估值兜底，避免用 avg_cost 高估）
        self._today_bar: dict[str, Bar] = {}       # code → 当日最新 bar（风控/撮合用）
        self._view = self._PortfolioViewImpl(self.portfolio)

    # 账户只读视图：把 Portfolio 收窄为 PortfolioView 协议（策略只看得到它）
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

    def submit(self, order: Order) -> bool:
        """订单提交入口（OrderSink 协议实现）：风控检查通过后转发给 Broker。

        时序保护：bar.code 尚未在 _today_bar 就绪时（例如多标的场景中策略
        在 on_bar(code_A) 里下单 code_B），不能直接 return——这会无声吞单，
        让用户在前端 / CLI 看到的"拒单数"为 0而察觉不到。改为记到 rejects，
        至少让异常暴露出来。

        Returns:
            True 表示订单已进入撮合通道；False 表示被风控拒绝或时序
            异常（bar 未就绪），原因已记入拒单日志。
        """
        bar = self._today_bar.get(order.code)
        if bar is None:
            self.risk.rejects.append({
                "date": "", "code": order.code,
                "side": order.side.value,
                "reason": "_today_bar 未就绪（多标的/时序异常）",
            })
            return False
        if self.risk.check(order, self._view, bar) is not None:
            return False
        self.broker.submit(order)
        return True

    def reject(self, code: str, side: Side, reason: str) -> None:
        """未成单意图记录（OrderSink 协议实现）。

        策略上下文在无法形成委托（无持仓 / T+1 冻结 / 不足一手等）时
        调用本方法，与风控拒单共用同一条日志——"策略想交易但没下出去"
        必须在拒单计数中可见，而不是静默消失后伪装成策略行为。
        """
        bar = self._today_bar.get(code)
        self.risk.rejects.append({
            "date": bar.trade_date if bar is not None else "",
            "code": code, "side": side.value, "reason": reason,
        })

    def _equity(self) -> float:
        """策略侧权益估值口径（ctx.equity() 的注入实现）。

        与日终快照同一套三级取价兜底：当日有效收盘价 → 最近有效
        收盘价 → 买入均价。估值基准由引擎维护，策略不自组价格字典。
        """
        return self.portfolio.equity(self._prices, self._last_close)

    # ── 数据规范化：统一 dt / trade_date / date 列，确保多频率兼容 ────────
    @staticmethod
    def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
        df = bars.copy()
        if "dt" not in df.columns:
            df["dt"] = df["date"].astype(str)
        df["dt"] = df["dt"].astype(str)
        # trade_date 优先采信数据源给定的归属交易日（夜盘/跨日归属等场景下
        # dt 的日期部分不等于归属交易日）；缺失时才从 dt 前 10 位推导。
        if "trade_date" not in df.columns:
            df["trade_date"] = df["dt"].str[:10]
        else:
            df["trade_date"] = df["trade_date"].astype(str)
        if "date" not in df.columns:
            df["date"] = df["trade_date"]
        return df.sort_values(["dt", "code"]).reset_index(drop=True)

    # ── 因子注入：宽表按 code+dt 左连接，切为每标的子帧 ────────
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

    # ── 主循环：按交易日分组驱动日界，一天内按 dt 逐 bar 推进 ────────
    def run(self) -> BacktestResult:
        # 运行态整体重建：引擎实例可重复 run（幂等）。上一轮的组合账本、
        # 通道挂单、估值基准与拒单日志若不清空，会泄漏进下一轮并叠加出
        # 静默错误的结果（持仓翻倍、拒单重复计数等）
        self.portfolio = Portfolio(init_cash=self.init_cash)
        self._view = self._PortfolioViewImpl(self.portfolio)
        self.broker.reset()
        self.risk.rejects.clear()
        self.risk.reset_pending()
        self._today_bar = {}
        self._prices = {}
        self._last_close = {}
        # 预构建每标的完整收盘价数组（O(N) 一次性，N = 该标的总 bar 数）
        self._full_prices = {}
        for code, sub in self.bars.groupby("code", sort=False):
            self._full_prices[code] = sub["close"].to_numpy(dtype=float)
        self._history = {}  # code → (prices, dts, count)

        self.strategy.on_init(StrategyContext(
            pd.Series(dtype=float), "", self._view, self,
            0.0, equity_fn=self._equity))

        for _td, day_df in self.bars.groupby("trade_date", sort=True):
            # _normalize 已保证 trade_date 列为 str；str() 是防御性转换，兼容异构输入
            trade_date: str = str(_td)
            # 日界清空：昨日 bar 不作为今日风控与撮合的依据（数据缺口的
            # 标的当日无 bar 时，残留的昨日 bar 会冒充今日行情通过校验）
            self._today_bar = {}
            self.portfolio.on_new_day()            # T+1 解禁（每个交易日仅一次）
            self.risk.reset_pending()              # 清空昨日委托追踪，防止跨日累积
            day_started: set[str] = set()          # 按 code 独立追踪 on_new_day 触发状态
        
            for row in day_df.itertuples(index=False):
                bar = self._to_bar(row, trade_date)
                self._today_bar[bar.code] = bar
        
                # ① 撮合此前提交的订单（本 bar 开盘价 ± 滑点）
                for fill in self.broker.settle(bar):
                    if self.portfolio.apply_fill(fill):
                        # 成交回冲日内委托占用：卖出回冲可卖量、买入按实际
                        # 花费释放现金占用。不回冲的话，已成交部分会在
                        # pending 里被"幽灵占用"，误拒同日后续委托
                        if fill.order.side == Side.SELL:
                            self.risk.settle_sell(fill.order.code,
                                                  fill.filled_qty)
                        else:
                            self.risk.settle_buy(
                                fill.filled_price * fill.filled_qty
                                + fill.commission)
                    else:
                        # 现金不足导致成交被跳过——记入拒单日志，避免静默丢失
                        self.risk.rejects.append({
                            "date": bar.trade_date, "code": fill.order.code,
                            "side": fill.order.side.value,
                            "reason": "现金不足（撮合时实际花费超过可用现金）",
                        })
        
                # ② 累计历史（numpy 数组 append O(1) 摊销，仅构造 ctx 时切片引用）
                if bar.code not in self._history:
                    # 首根 bar：预分配完整长度的数组，避免逐 bar 扩容
                    total = len(self._full_prices[bar.code])
                    self._history[bar.code] = (
                        np.empty(total, dtype=float),
                        np.empty(total, dtype=object),
                        0,
                    )
                prices_arr, dts_arr, cnt = self._history[bar.code]
                # 价格护栏（信号面与估值面同一判据）：无效收盘价（非正或
                # NaN）记 NaN 进策略可见历史——rolling 指标遇 NaN 自然跳过，
                # 0 价原样透传会砸穿均线/通道（动量 -100% 与 +inf、下轨失真
                # 为 0）；同时不更新估值基准，维持最近有效收盘价，组合层
                # "当日价→最近价→均价"的三级兜底只有在基准有效时才成立
                close_valid = math.isfinite(bar.close) and bar.close > 0
                prices_arr[cnt] = bar.close if close_valid else float("nan")
                dts_arr[cnt] = bar.dt
                cnt += 1
                self._history[bar.code] = (prices_arr, dts_arr, cnt)
                if close_valid:
                    self._prices[bar.code] = bar.close
                    self._last_close[bar.code] = bar.close
        
                # ③ 因子只读视图：iloc[:n] 切片，结构上排除未来行
                accessor = (FactorAccessor(self._factor_dfs[bar.code].iloc[:cnt])
                            if bar.code in self._factor_dfs else None)
        
                # 从预分配数组切片构造 Series（零拷贝，O(1) 指针操作）
                hist_series = pd.Series(
                    prices_arr[:cnt], index=dts_arr[:cnt])
                ctx = StrategyContext(
                    hist_series, bar.dt, self._view, self,
                    bar.close, factors=accessor, equity_fn=self._equity)
        
                # ④ 日始钩子：每标的当日首根 bar 触发（撮合后、on_bar 前），
                #    多标的时各 code 独立触发，避免只给第一只股票发 on_new_day
                if bar.code not in day_started:
                    self.strategy.on_new_day(ctx, bar)
                    day_started.add(bar.code)
        
                # ⑤ 策略逻辑（信号产生订单，提交到次一 bar 撮合）
                self.strategy.on_bar(ctx, bar)

            # ⑥ 日终快照（收盘口径；净值曲线每个交易日一条）
            self.portfolio.snapshot(trade_date, self._prices, self._last_close)

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
        """将 DataFrame 行转换为 Bar 值对象（处理 None / 类型转换）。"""
        raw_ts: int | None = getattr(row, "trade_status", None)
        ts: int = 1 if raw_ts is None else int(raw_ts)   # 注意 0（停牌）是有效值，不可用 or 兜底
        return Bar(
            code=row.code, dt=str(row.dt), trade_date=str(trade_date),
            open=float(row.open), high=float(row.high),
            low=float(row.low), close=float(row.close),
            pre_close=float(getattr(row, "pre_close", 0) or 0),
            volume=float(getattr(row, "volume", 0) or 0),
            amount=float(getattr(row, "amount", 0) or 0),
            trade_status=ts,
            is_st=int(getattr(row, "is_st", 0) or 0),
            # 涨跌停权威价：列缺失或 NULL/NaN 时为 0 / NaN，price_limits
            # 判 > 0 为假即回退 pre_close 估算——旧数据路径天然兼容
            upper_limit=float(getattr(row, "upper_limit", 0) or 0),
            lower_limit=float(getattr(row, "lower_limit", 0) or 0),
        )
