# mini-quant · 扩展指南

> 本文档是 OCP（Open-Closed Principle，开闭原则）的兑现清单。
> 每一类扩展都给出"复制文件 → 改 X 行 → 注册"的精确步骤，改动量都标在表里。
>
> 共同铁律：扩展不修改任何旧代码（仅新增文件 + 注册处加一行）。

---

## 0. 扩展点速查

| 想做什么 | 改哪些文件 | 改动量 |
|---|---|---|
| 新策略 | `quant/strategy/<新文件>.py` + `app/cli.py` + `webapp/server.py` | 1 新文件 + 2 行 |
| 新因子 | `quant/factor/builtin.py`（追加一个类 + 一行 register）| 1 个类 + 1 行 |
| 新数据源 | `quant/data/<新源>.py` + `app/service.py:get_source` | 1 新文件 + 1 行 |
| 换存储 | 重写 `DataRepository` 实现 + `app/service.py:get_repo` | 1 文件 + 1 行 |
| 新频率 | `baostock_source._FREQ_MAP` + `mysql_repo.TABLES` + DDL | 2 行 + DDL |
| 新风控 | `quant/backtest/risk.py`（追加子类）+ `RiskChain.__init__` | 1 个类 + 1 行 |
| 切实盘 | 新建 `QmtBroker` 实现 `Broker` 协议 + 替换 `engine.py` 中 `SimBroker` 注入 | 1 新文件 |

---

## 1. 新策略

### 1.1 复制模板

```python
# quant/strategy/<your_strategy>.py
"""一句话描述策略信号逻辑。"""
from __future__ import annotations

from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar


class YourStrategy(Strategy):
    """策略类——继承 Strategy + 实现两个必需钩子。"""

    params: dict = {}
    required_factors: list[str] = []
    # ⚠️ 在 __init__ 中重新赋值 params / required_factors（Python 类属性陷阱）

    def __init__(self, param1: int = ..., param2: float = ...):
        self.param1 = param1
        self.param2 = param2
        self.params = {"param1": param1, "param2": param2}
        # 如需因子：声明依赖（服务层自动解析 + 预热 + 注入）
        self.required_factors = []  # 例如 ["momentum_20"]

    def on_init(self, ctx: StrategyContext) -> None:
        """策略预热钩子（回测起跑前调用一次）。"""
        pass

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        """每根 bar 回调——在此产生信号，通过 ctx.buy()/sell() 下单。"""
        # 示例：双均线交叉
        hist = ctx.history
        if len(hist) < 60:
            return
        ma = hist.rolling(self.param1).mean()
        mb = hist.rolling(self.param2).mean()
        if ma.iloc[-1] > mb.iloc[-1] and ma.iloc[-2] <= mb.iloc[-2]:
            ctx.buy(bar.code)

    def on_new_day(self, ctx: StrategyContext, bar: Bar) -> None:
        """可选：每个交易日首根 bar 触发（多标的按 code 独立触发）。"""
        pass
```

### 1.2 注册（2 处）

**`quant/app/cli.py`**（CLI 入口）：

```python
p_bt.add_argument("--strategy", default="double_ma",
                  choices=["double_ma", "factor_momentum", "your_strategy"],
                  help="...")
...
elif args.strategy == "your_strategy":
    from ..strategy.your_strategy import YourStrategy
    strategy = YourStrategy(args.param1, args.param2)
    label = f"YOUR_LABEL({args.param1},{args.param2})"
else:  # 已有逻辑
    ...
```

**`quant/webapp/server.py`**（Web 入口）：

```python
@app.get("/api/backtest")
def backtest(... strategy: str = "double_ma", ...):
    ...
    if strategy == "your_strategy":
        from ..strategy.your_strategy import YourStrategy
        strat = YourStrategy(...)
    else:
        strat = DoubleMAStrategy(fast, slow)
    ...
```

### 1.3 频率无关

- 同一份代码在日线 / 5 分钟线上直接可用；
- rolling 指标天然按 bar 计算（日线 MA20 = 月均线，5 分钟 MA20 = 日内 100 分钟均线）。

---

## 2. 新因子

### 2.1 复制模板

在 **`quant/factor/builtin.py`** 末尾追加（不需要新建文件）：

```python
class TurnoverVolatility(Factor):
    """换手率波动率：N 期换手率标准差（示例）。"""

    def __init__(self, window: int):
        self.window = window
        self.name = f"turnover_vol_{window}"
        self.min_periods = window + 1

    def compute(self, bars: pd.DataFrame) -> pd.Series:
        return bars["turn"].astype(float).rolling(self.window).std()


# ── 注册（追加到 builtin.py 末尾） ────────────────────────────────────────────
register(TurnoverVolatility(20))
```

### 2.2 契约（写新因子必须遵守）

1. **只允许因果计算**（rolling / shift / pct_change 等 t 行只依赖 ≤ t 的方法）；
2. **历史不足时输出 NaN**（不抛错），由策略侧判断处理；
3. **min_periods 准确**：产出首个非 NaN 值所需的最少 bar 数（引擎据此计算预热窗口）。

### 2.3 验证

```bash
python tests/smoke_test.py   # 因子计算正确性 + 防未来访问 + 全链路
```

---

## 3. 新数据源

### 3.1 复制模板

```python
# quant/data/<your_source>.py
"""<数据源名>数据源适配器（MarketDataSource 实现）。"""
from __future__ import annotations

import pandas as pd


class YourSource:
    """实现 MarketDataSource 协议：name 属性 + fetch_bars 方法。"""

    name = "your_source"

    @staticmethod
    def fetch_bars(code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """按频率拉取行情，返回统一列 DataFrame。

        必返回列：
            code, dt, trade_date, open, high, low, close,
            pre_close, volume, amount, trade_status, is_st

        Args:
            code: 证券代码（本系统口径 sh.600000）
            start: 起始日期 'YYYY-MM-DD'
            end: 结束日期 'YYYY-MM-DD'
            freq: '1d' / '5min'（如支持更多频率请扩 freq 分发表）
            adjust: 复权标记 1=后复权 2=前复权 3=不复权
        """
        # 1) 调外部 API 拉数据
        raw = ...  # 你的拉取逻辑
        # 2) 归一列名 / 时间戳 / pre_close / trade_status / is_st
        df = raw.rename(columns={...})
        df["trade_date"] = df["dt"].str[:10]
        df["pre_close"] = ...   # 若源不提供，按需计算或留 0
        df["trade_status"] = 1
        df["is_st"] = 0
        return df
```

### 3.2 注册

**`quant/app/service.py`**：

```python
def get_source(name: str = "baostock"):
    sources = {
        "baostock": BaostockSource,
        "akshare": AkshareSource,
        "wind": WindSource,           # ← 已内置（v0.4，需本机 Wind 终端）
        "your_source": YourSource,    # ← 加这一行
    }
    if name not in sources:
        raise ValueError(f"未知数据源: {name}（可选: {list(sources)}）")
    return sources[name]
```

> **已内置三个源**：`baostock`（默认免费稳定）、`akshare`（免费聚合多源）、
> `wind`（万得，需本机安装并登录 Wind 终端）。三者输出**完全同构**的统一列，
> 上层引擎 / 策略 / 仓储零感知——这正是适配器模式的价值。
>
> `wind_source.py` 另有两个可借鉴的实现技巧：
> ① **延迟导入**第三方 SDK（未装终端的机器不受影响）；
> ② **字段降级容错**（全量字段请求失败时降级核心字段重试）；

### 3.3 CLI 暴露

**`quant/app/cli.py`**：

```python
p_in.add_argument("--source", default="baostock",
                  choices=["baostock", "akshare", "wind", "your_source"],   # ← 加进来
                  help="...")
```

---

## 4. 换存储（如改 Parquet + DuckDB）

### 4.1 复制模板

```python
# quant/data/parquet_repo.py
"""Parquet + DuckDB 仓储（DataRepository 实现）。"""
from __future__ import annotations

import pandas as pd
from ..core.abstractions import DataRepository


class ParquetBarRepo:
    """以本地 Parquet 文件 + DuckDB 元数据表实现 DataRepository 协议。"""

    def save_bars(self, df: pd.DataFrame, freq: str = "1d") -> int: ...
    def load_bars(self, codes: list[str], start: str, end: str,
                  freq: str = "1d", adjust: str = "2") -> pd.DataFrame: ...
    def latest_bar_time(self, code: str, freq: str = "1d",
                        adjust: str = "2") -> str | None: ...
    def list_codes(self, freq: str = "1d") -> list[str]: ...
```

### 4.2 装配切换

**`quant/app/service.py`**：

```python
def get_repo(settings=None):
    backend = os.getenv("QUANT_REPO_BACKEND", "mysql")   # "mysql" | "parquet"
    if backend == "parquet":
        from .data.parquet_repo import ParquetBarRepo
        return ParquetBarRepo(settings)
    return MySQLBarRepo(settings.db)
```

---

## 5. 新频率（如 15min / 30min / 60min）

只需 3 处：

**① `quant/data/baostock_source.py`**：

```python
_FREQ_MAP = {"1d": "d", "5min": "5", "15min": "15",
             "30min": "30", "60min": "60"}   # ← 已支持全部
```

**② `quant/data/mysql_repo.py`**：

```python
TABLES = {
    "1d": "ods_d_stock_quotation_i",
    "5min": "ods_mi_stock_quotation_i",
    "15min": "ods_15m_stock_quotation_i",   # ← 加一行
    ...
}
```

**③ 建表 DDL**（追加到 `mysql_repo.DDL` 字符串）：

```sql
CREATE TABLE IF NOT EXISTS `ods`.`ods_15m_stock_quotation_i` (
  ...
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ...;
```

执行 `python -m quant.app.cli init-schema` 幂等建表。

---

## 6. 新风控规则

### 6.1 复制模板

在 **`quant/backtest/risk.py`** 追加：

```python
class MaxPositionRule(RiskRule):
    """单标的持仓上限：禁止单一仓位超过总资产的 30%。"""

    def __init__(self, max_ratio: float = 0.3):
        self.max_ratio = max_ratio

    def check(self, order: Order, portfolio: PortfolioView, bar: Bar) -> str | None:
        if order.side != Side.BUY:
            return None
        est_value = order.quantity * bar.open
        total = portfolio.equity({order.code: bar.open})   # 简化估算
        if est_value / total > self.max_ratio:
            return f"单标的仓位超限({est_value/total:.1%} > {self.max_ratio:.0%})"
        return None
```

### 6.2 注册（顺序即优先级）

**`quant/backtest/risk.py:RiskChain.__init__`**：

```python
def __init__(self, rules=None, cost=None):
    self.rules = rules or [
        TradabilityRule(), LotSizeRule(),
        CashSufficiencyRule(cost), AvailabilityRule(),
        MaxPositionRule(),    # ← 加到链尾
    ]
```

或调用方注入：

```python
risk_rules = [TradabilityRule(), MyNewRule()]
engine = BacktestEngine(bars=bars, strategy=strat, risk_rules=risk_rules)
```

---

## 7. 切实盘（miniQMT 通道示例）

> 这是 LSP（里氏替换原则）的直接兑现：引擎依赖 `Broker` 协议，
> 注入 `QmtBroker` 即可——`engine.py` 与所有策略代码 0 改动。

### 7.1 复制模板

```python
# quant/broker/qmt_broker.py
"""QmtBroker：miniQMT 实盘撮合（Broker 协议实现）。"""
from __future__ import annotations

from ..core.abstractions import Broker
from ..core.models import Bar, Fill, Order


class QmtBroker:
    """通过 miniQMT 通道提交订单 / 查询成交。"""

    def __init__(self, account_id: str, qmt_client):
        self.account_id = account_id
        self.qmt = qmt_client
        self._order_map = {}  # order_id → Order

    def submit(self, order: Order) -> None:
        """实盘下单：通过 QMT API 提交，记录 order_id 用于成交回调。"""
        order_id = self.qmt.submit_order(
            account=self.account_id, code=order.code,
            side=order.side.value, quantity=order.quantity,
            price=order.price,  # None = 市价单
        )
        self._order_map[order_id] = order

    def settle(self, bar: Bar) -> list[Fill]:
        """拉取本 bar 时段的成交回报（实盘中由推送回调触发，此处轮询）。"""
        fills = []
        for order_id, order in list(self._order_map.items()):
            report = self.qmt.query_deals(order_id)
            if report.is_filled:
                fill = Fill(
                    order=order, filled_qty=report.filled_qty,
                    filled_price=report.filled_price,
                    commission=report.commission,
                    filled_at=report.filled_time,
                )
                fills.append(fill)
                del self._order_map[order_id]
        return fills
```

### 7.2 切换（回测 → 实盘）

```python
# quant/app/service.py
def run_live(code, strategy):
    from ..broker.qmt_broker import QmtBroker
    import qmt  # 第三方 QMT SDK

    broker = QmtBroker(account_id="...", qmt_client=qmt.Client())
    engine = BacktestEngine(bars=..., strategy=strategy, broker=broker)   # ← 注入
    engine.run()
```

---

## 8. 验证清单

每次扩展后跑：

```bash
python tests/smoke_test.py           # 离线冒烟（不依赖外部服务）
python -m quant.app.cli backtest ...  # 真实数据回归
python -m quant.app.cli serve         # Web 端冒烟
```

如新增因子 / 策略，建议在 `tests/smoke_test.py` 加一组对应的合成行情用例（详见 `tests/README.md`）。