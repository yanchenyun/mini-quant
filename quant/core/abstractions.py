"""全系统扩展点的抽象接口契约（Protocol / ABC）。

核心原则：策略只依赖 StrategyContext 窄接口（ISP），
新数据源 / 券商 / 风控 / 因子 = 新增实现类并登记，入口代码零改动（OCP）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Protocol

import pandas as pd

from .models import Bar, Fill, Order, Position, Side


# ── 数据面 ────────────────────────────────────────────────────────────────
class MarketDataSource(Protocol):
    """外部行情数据源契约：把任一行情接口适配成统一列 DataFrame（见 fetch_bars）。"""
    name: str

    def fetch_bars(self, code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """拉取行情，返回统一列 DataFrame。

        Args:
            code: 证券代码（如 sh.600000）。
            start: 起始日期 'YYYY-MM-DD'。
            end: 结束日期 'YYYY-MM-DD'。
            freq: '1d' | '5min'（可扩展 15/30/60min）。
            adjust: 复权标记，1=后复权 2=前复权 3=不复权。

        Returns:
            统一列：code/dt/trade_date/open/high/low/close/pre_close/
            volume/amount/trade_status/is_st。
        """
        ...


class DataRepository(Protocol):
    """本地行情仓储契约：按频率读写统一列行情 DataFrame，写入幂等（存在则更新）。

    四方法为必备契约；save_factors / load_factors 属可选能力，不计入本协议。
    """
    def save_bars(self, df: pd.DataFrame, freq: str = "1d") -> int: ...
    def load_bars(self, codes: list[str], start: str, end: str,
                  freq: str = "1d", adjust: str = "2") -> pd.DataFrame: ...
    def latest_bar_time(self, code: str, freq: str = "1d",
                        adjust: str = "2") -> str | None: ...
    def list_codes(self, freq: str = "1d") -> list[str]: ...


# ── 策略面 ───────────────────────────────────────────────────────────────
class OrderSink(Protocol):
    """订单入口：引擎实现它；策略只通过 Context 调用，不认识引擎。

    契约的可感知性：submit 返回订单是否被受理，reject 记录未能形成
    委托的下单意图。两者共同保证"策略想知道单有没有下出去"不需要
    靠猜——静默丢单会把回测错误伪装成策略行为。
    """

    def submit(self, order: Order) -> bool:
        """受理订单；返回 False 表示被拒（原因已记入拒单日志）。"""
        ...

    def reject(self, code: str, side: Side, reason: str) -> None:
        """记录未形成委托的下单意图（无持仓 / T+1 冻结 / 不足一手等）。"""
        ...


class PortfolioView(Protocol):
    """策略与风控可见的账户只读视图。"""
    @property
    def cash(self) -> float: ...

    def position(self, code: str) -> Position | None: ...
    def equity(self, prices: dict[str, float]) -> float: ...


class StrategyContext:
    """策略能看到的全部世界（接口隔离 ISP）。

    防未来函数：history / factor 只含当前 bar（含）及以前的数据。
    """

    def __init__(self, history: pd.Series, clock: str,
                 portfolio: PortfolioView, sink: OrderSink,
                 price: float, factors: "FactorAccessor | None" = None,
                 equity_fn: "Callable[[], float] | None" = None):
        """
        Args:
            history: 截至当前 bar（含）的收盘价 Series，index 为 dt。
            clock: 当前 bar 的时间戳（dt），用于标记订单 created_at。
            portfolio: 账户只读视图（现金 / 持仓 / 净值）。
            sink: 订单提交入口（引擎实现 OrderSink 协议）。
            price: 当前 bar 收盘价（下单参考价）。
            factors: 因子只读视图；未启用因子时为 None。
            equity_fn: 账户权益估值函数（引擎注入）。估值基准——当日有效
                收盘价、停牌兜底的最近有效收盘价——是引擎的运行态知识，
                策略通过 ctx.equity() 取值，不应自组价格字典。
        """
        self._history = history
        self.clock = clock
        self.portfolio = portfolio
        self._sink = sink
        self._price = price
        self._factors = factors
        self._equity_fn = equity_fn

    @property
    def history(self) -> pd.Series:
        """截至当前 bar（含）的收盘价序列，index 为 bar 时间戳 dt
        （日线即日期，分钟线为 'YYYY-MM-DD HH:MM:SS'）。"""
        return self._history

    @property
    def price(self) -> float:
        """当前 bar 收盘价（下单参考价）。"""
        return self._price

    @property
    def factors(self) -> "FactorAccessor | None":
        """因子只读视图；本回测未启用因子时为 None。"""
        return self._factors

    def _ensure_factors(self) -> FactorAccessor:
        """返回因子视图；未注入时抛 RuntimeError（调用方应确保策略声明了 required_factors）。"""
        if self._factors is None:
            raise RuntimeError(
                "本回测未启用因子：策略 required_factors 为空，或引擎未注入 factor_frame")
        return self._factors

    def factor(self, name: str) -> float:
        """当前 bar 的因子值（NaN 表示预热区无值）。"""
        return self._ensure_factors().value(name)

    def factor_history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列，index 为 bar 时间戳 dt。"""
        return self._ensure_factors().history(name)

    def equity(self) -> float:
        """当前账户权益（现金 + 持仓市值）。

        估值基准由引擎维护，与日终净值快照同一套取价兜底口径，
        策略不需要也不应该自行拼装价格字典——把估值基准的维护
        责任留在引擎（引擎外的构造方没有默认基准，调用即报错）。
        """
        if self._equity_fn is None:
            raise RuntimeError("本上下文未注入估值函数：权益估值基准由回测引擎维护")
        return self._equity_fn()

    def buy(self, code: str, cash_ratio: float = 0.95) -> bool:
        """按可用现金比例市价买入（100 股向下取整）。

        Returns:
            True 表示订单已提交至撮合通道（后续仍可能因涨停或现金
            不足未成交）；False 表示未形成委托——参考价无效、可用资金
            不足一手或被风控拒绝，原因均已记入拒单日志。
        """
        if not self._price > 0:     # 同时拦住非正价与 NaN（NaN 比较恒为假）
            self._sink.reject(code, Side.BUY, "参考价无效，无法计算买入股数")
            return False
        budget = self.portfolio.cash * cash_ratio
        qty = int(budget / self._price / 100) * 100
        if qty <= 0:
            self._sink.reject(code, Side.BUY, "可用资金不足一手")
            return False
        return self._sink.submit(Order(code=code, side=Side.BUY, quantity=qty,
                                       created_at=self.clock))

    def sell(self, code: str, ratio: float = 1.0) -> bool:
        """卖出可用仓位（T+1：仅 available 部分）。

        Returns:
            True 表示订单已提交至撮合通道；False 表示未形成委托——
            无持仓、T+1 当日无可卖份额或按比例取整后不足一手，
            原因均已记入拒单日志。依赖提交结果的策略应检查返回值，
            在 False 时保持"未离场"之类的中间状态以便重试。
        """
        pos = self.portfolio.position(code)
        if pos is None or pos.available <= 0:
            self._sink.reject(code, Side.SELL, "无持仓或T+1当日无可卖份额")
            return False
        # 全仓卖出时直接清仓，避免整手取整导致残留零股
        qty = pos.available if ratio >= 1.0 else int(pos.available * ratio / 100) * 100
        if qty <= 0:
            self._sink.reject(code, Side.SELL, "可卖份额按比例取整后不足一手")
            return False
        return self._sink.submit(Order(code=code, side=Side.SELL, quantity=qty,
                                       created_at=self.clock))


class Strategy(ABC):
    """策略基类：继承它 + 实现两个必需钩子；on_new_day 可选重写。

    频率无关设计：on_bar 拿到的 history 与 bar.dt 无论是日线还是 5 分钟线，
    策略代码写法完全一致（rolling 均线等指标天然按 bar 计算）。

    因子用法：子类声明 required_factors（因子名列表，取值须来自因子注册表），
    服务层自动解析依赖并注入（见 factor 包）；on_bar 里用 ctx.factor(name) 取值。

    两个元数据属性 params 与 required_factors 由子类在 __init__ 里赋值，
    基类只声明类型不给默认值：漏赋值时访问即 AttributeError（暴露问题），
    而不是静默继承基类的可变默认对象、让所有实例共享同一份数据。
    """
    params: dict
    required_factors: list[str]

    @abstractmethod
    def on_init(self, ctx: StrategyContext) -> None: ...

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        """每根 bar 回调：策略在此产生信号并通过 ctx.buy()/sell() 下单。

        bar 是当前正在处理的行情 bar（日线=当日，分钟=当根 5min）。
        策略通过 ctx.history 获取截至当前 bar（含）的收盘价序列，
        通过 ctx.price 获取当前 bar 收盘价，通过 ctx.factor() 读取因子值。

        信号产生的订单会在次一 bar 开盘时撮合成交（防未来函数）。
        ctx.buy / ctx.sell 返回订单是否已提交，未提交的原因记入拒单
        日志；用标志位记忆"已下单"的策略应以提交成功为准。
        """
        ...

    def on_new_day(self, ctx: StrategyContext, bar: Bar) -> None:
        """交易日开始钩子（可选重写，默认空实现——向后兼容）。

        引擎在每个交易日的撮合结算后、首个 on_bar 之前调用
        （bar 为当日第一根，用于确定标的与上下文）。日内策略在此重置
        当日状态（如日内开仓次数、日内均线累计）。
        """


# ── 执行面 ───────────────────────────────────────────────────────────────
class Broker(Protocol):
    """撮合 / 交易通道契约：submit 收单，settle 按当前 bar 撮合并返回成交。

    本层只声明能力，不关心是回测撮合还是真实的柜面通道——引擎换一个实现
    即可在回测与实盘之间切换（具体实现类不进入核心层）。

    reset 由引擎在每次回测开始时调用：有状态的实现（挂单簿等）借它清空
    自身，使引擎实例可以重复运行；无状态的实现给空实现即可。
    """
    def submit(self, order: Order) -> None: ...
    def settle(self, bar: Bar) -> list[Fill]:
        """以本 bar（订单提交后的次一 bar）开盘价撮合挂起的订单。"""
        ...
    def reset(self) -> None: ...


class RiskContext(Protocol):
    """责任链暴露给规则的上下文窄接口（ISP）。

    绝大多数规则只需 check 的三个入参；少数要读链上日内状态的规则
    （如 T+1 委托防重、日内现金占用）通过本接口取值，而不是直接
    持有整条链。
    """
    def pending_sell(self, code: str) -> int: ...
    def pending_buy_cost(self) -> float: ...


class RiskRule(ABC):
    """风控规则：责任链上的一个节点。返回 None 放行，否则返回拒绝原因。

    需要链上下文的规则重写 attach 保存它；不需要的沿用默认空实现，
    链在装配时统一调用 attach，规则侧不依赖任何试探性判断。
    """
    def attach(self, ctx: RiskContext) -> None:
        """由责任链在装配时调用（可选重写，默认不关心上下文）。"""

    @abstractmethod
    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None: ...


# ── 因子面 ──────────────────────────────────────────────────────────────
class Factor(ABC):
    """因子（Factor）：从原始行情派生的数值特征（动量/波动率/量比……）。

    compute 输入单标的、按 dt 升序的行情帧（open/close/volume/amount 等），
    返回与输入等长的 Series（index 对齐输入行）。

    契约（防未来函数，写新因子必须遵守）：
    - 只允许因果计算（rolling/shift），t 行的值只依赖 ≤t 的数据；
    - 历史不足时对应行为 NaN（不抛错），由策略侧判断处理；
    - min_periods = 产出首个非 NaN 值所需的最少 bar 数（引擎据此计算预热窗口）。
    """
    name: str
    min_periods: int

    @abstractmethod
    def compute(self, bars: pd.DataFrame) -> pd.Series: ...


class FactorAccessor:
    """策略可见的因子只读视图（引擎每根 bar 用 iloc[:n] 切片构造）。

    防未来函数：策略在结构上不可能看到当前 bar 之后的因子行。
    """

    def __init__(self, df: pd.DataFrame):
        # df：index=dt（str），columns=因子名，行序=bar 时间序
        self._df = df
        self._empty = len(df) == 0  # 缓存空帧标志，避免每次 value() 重复检查

    def value(self, name: str) -> float:
        """当前 bar 的因子值；无该因子列或空帧时返回 NaN。"""
        if self._empty or name not in self._df.columns:
            return float("nan")
        return float(self._df[name].iloc[-1])

    def history(self, name: str) -> pd.Series:
        """截至当前 bar（含）的因子值序列。"""
        if name not in self._df.columns:
            raise KeyError(f"未知因子: {name}（可用: {list(self._df.columns)}）")
        return self._df[name]
