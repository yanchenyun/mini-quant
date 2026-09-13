# mini-quant · API 手册

> 本文档列出所有对外可用的 API：CLI 子命令、Web HTTP 端点、`app/service`
> 公共函数。每个条目给出"签名 / 参数 / 返回 / 示例"。
>
> 三类入口共用底层业务逻辑（`service.py`），改一处即生效。

---

## 1. CLI 子命令

入口：`python -m quant.app.cli <command>`

### 1.1 子命令速查

| 子命令 | 作用 | 是否需联网 |
|---|---|---|
| `init-schema` | 幂等建库建表（行情日表/分钟表/因子表） | 否 |
| `ingest` | 增量抓取行情入库（Baostock / AKShare） | 是 |
| `compute-factors` | 计算因子并落库（物化缓存，upsert 幂等） | 否 |
| `backtest` | 运行回测并打印绩效 | 否 |
| `repair-dt` | 修复分钟表历史脏时间戳（幂等） | 否 |
| `serve` | 启动 Web 控制台 | 否 |

### 1.2 `init-schema`

```bash
python -m quant.app.cli init-schema
```

幂等执行 `MySQLBarRepo.init_schema()`：创建数据库 `ods`、日表
`ods_d_stock_quotation_i`、分钟表 `ods_mi_stock_quotation_i`、因子长表
`dwd_factor_value_i`。重复执行无副作用。

### 1.3 `ingest`

```bash
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 [options]
```

**参数**：

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--code` | ✅ | — | 证券代码（`sh.600000` / `sz.000001`） |
| `--start` | ✅ | — | 起始日期 `YYYY-MM-DD` |
| `--end` | — | 今天 | 结束日期 `YYYY-MM-DD` |
| `--adjust` | — | `2` | 复权：`1` 后复权 / `2` 前复权 / `3` 不复权 |
| `--freq` | — | `1d` | 频率：`1d` 日线 / `5min` 5 分钟线 |
| `--source` | — | `baostock` | 数据源：`baostock` / `akshare` |

**自动增量**：从库中已有最新 bar 的次日开始续抓（库为空时用 `--start`）。

**示例**：

```bash
# 日线前复权
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01

# AKShare 日线
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 --source akshare

# 5 分钟线（建议同时保留日线：分钟表的涨跌停基准依赖日线昨收关联）
python -m quant.app.cli ingest --code sh.600000 --start 2025-01-01 --freq 5min
```

### 1.4 `compute-factors`

```bash
python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01 [options]
```

**参数**：

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--code` | ✅ | — | 证券代码 |
| `--start` | ✅ | — | 起始日期 |
| `--end` | — | 今天 | 结束日期 |
| `--freq` | — | `1d` | 频率 |
| `--factors` | — | 全部内置 | 逗号分隔因子名（可选: `momentum_20,momentum_60,volatility_20,bias_20,volume_ratio_5_20`） |

**示例**：

```bash
# 计算全部内置因子
python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01

# 仅计算 2 个因子（5 分钟频率）
python -m quant.app.cli compute-factors --code sh.600000 --start 2025-01-01 \
    --freq 5min --factors momentum_20,volatility_20
```

### 1.5 `backtest`

```bash
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --end 2024-12-31 [options]
```

**参数**：

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--code` | ✅ | — | 证券代码 |
| `--start` | ✅ | — | 起始日期 |
| `--end` | — | 今天 | 结束日期 |
| `--freq` | — | `1d` | 频率：`1d` / `5min` |
| `--strategy` | — | `double_ma` | 策略：`double_ma` / `factor_momentum` |
| `--fast` | — | `5` | 双均线快线周期（按 bar 计）|
| `--slow` | — | `20` | 双均线慢线周期 |
| `--window` | — | `20` | 动量窗口（`factor_momentum`，如 `20` / `60`）|
| `--json` | — | `False` | 输出完整 JSON 而非人类可读格式 |

**示例**：

```bash
# 日线双均线 MA5/MA20
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 \
    --end 2024-12-31 --fast 5 --slow 20

# 5 分钟双均线 MA8/MA48（5 分钟粒度天然适配更短周期）
python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 \
    --freq 5min --fast 8 --slow 48

# 动量因子策略（momentum_60）
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 \
    --strategy factor_momentum --window 60
```

### 1.6 `repair-dt`

```bash
python -m quant.app.cli repair-dt
```

把分钟表 `ods_mi_stock_quotation_i` 中历史入库的脏时间戳（17 位纯数字串
`YYYYMMDDHHMMSSmmm`）就地 UPDATE 为标准 `YYYY-MM-DD HH:MM:SS`。幂等，
只影响脏行（`CHAR_LENGTH = 17 AND REGEXP '^[0-9]+$'`）。

### 1.7 `serve`

```bash
python -m quant.app.cli serve [--host 127.0.0.1] [--port 8000]
```

启动 FastAPI Web 服务（uvicorn），浏览器打开 `http://127.0.0.1:8000`。
详见 §2 与 `webapp/README.md`。

---

## 2. Web API 端点

### 2.1 `GET /`

返回 `quant/webapp/index.html` 静态页（K 线 + 买卖点 + 净值对比 + 指标卡片 + 成交明细）。

### 2.2 `GET /api/codes`

返回 DB 中所有标的代码（用于前端下拉框）。

**响应**：`["sh.600000", "sh.600001", ...]`

**异常**：数据库连接失败 → `500 { "detail": "读取数据库失败: ..." }`

### 2.3 `GET /api/backtest`

运行回测并返回前端所需的全部数据。

**Query 参数**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` | str | ✅ | 证券代码 |
| `start` | str | ✅ | 起始日期 |
| `end` | str | ✅ | 结束日期 |
| `freq` | str | `1d` | `1d` / `5min` |
| `strategy` | str | `double_ma` | `double_ma` / `factor_momentum` |
| `fast` | int | `5` | 双均线快线周期 |
| `slow` | int | `20` | 双均线慢线周期 |
| `window` | int | `20` | 动量窗口（仅 factor_momentum）|

**校验**：
- `freq ∉ {1d, 5min}` → `400`
- `strategy ∉ {double_ma, factor_momentum}` → `400`
- `strategy == double_ma and fast >= slow` → `400 "快线周期必须小于慢线"`

**响应（8 段 JSON）**：

```json
{
  "params": {
    "code": "sh.600000", "fast": 5, "slow": 20, "freq": "1d",
    "strategy": "double_ma", "window": 20,
    "start": "2022-01-01", "end": "2025-12-31"
  },
  "metrics": {
    "start_date": "2022-01-04", "end_date": "2025-12-31",
    "trading_days": 974,
    "final_equity": 1287654.32,
    "total_return": 28.77, "annual_return": 7.43,
    "max_drawdown": -8.21,
    "max_dd_range": "2024-08-12 ~ 2024-10-15",
    "sharpe": 1.32, "calmar": 0.91,
    "annual_volatility": 12.45,
    "trade_count": 47, "win_rate": 53.19,
    "profit_factor": 1.42, "avg_win": 1823.4, "avg_loss": -1284.5,
    "total_commission": 4521.32
  },
  "kline": {
    "dates": ["2022-01-04", "2022-01-05", ...],
    "open": [10.05, ...], "high": [...], "low": [...], "close": [...],
    "ma_fast": [...], "ma_slow": [...]
  },
  "equity_curve": [
    {"date": "2022-01-04", "cash": 800000.0, "market_value": 200000.0, "equity": 1000000.0},
    ...
  ],
  "benchmark": [1000000.0, 999800.5, ...],   // 期初全仓买入持有，按 bar 粒度
  "buys":  [["2022-01-05", 10.10, 9500, 23.75], ...],   // [日期, 价格, 数量, 佣金]
  "sells": [["2022-02-15", 10.80, 9500, 1026.0, 23.75], ...],  // + 本轮盈亏
  "rejects": 3   // 风控拒单数
}
```

**异常**：
- 库内无数据 → `404 { "detail": "无数据：sh.600000 ...（请先执行 ingest）" }`
- 其它 → `500`

---

## 3. `app/service` 公共函数

供 CLI 与 Web 共用的业务编排层。新写脚本时优先复用：

### 3.1 `get_repo(settings: Settings | None = None) -> MySQLBarRepo`

构造仓储（工厂）。

```python
from quant.app.service import get_repo
repo = get_repo()
repo.init_schema()                 # 幂等建库建表
repo.save_bars(df, freq="1d")      # 入库
df = repo.load_bars(["sh.600000"], "2020-01-01", "2024-12-31")
```

### 3.2 `get_source(name: str = "baostock") -> type`

按名称获取数据源类（工厂）。返回**类**而非实例，因为适配器方法都是 `@staticmethod`。

```python
from quant.app.service import get_source
SrcCls = get_source("akshare")
df = SrcCls.fetch_bars("sh.600000", "2024-01-01", "2024-12-31", freq="1d")
```

可选：`baostock`（默认）、`akshare`。

### 3.3 `ingest_bars(code, start, end=None, adjust="2", freq="1d", source="baostock", init_schema=False) -> int`

增量抓取并存入 MySQL。从库中已有最新 bar 的当日/次日续抓（自动、幂等）。

```python
from quant.app.service import ingest_bars
n = ingest_bars("sh.600000", "2020-01-01", freq="1d", source="baostock")
print(n)   # 本次入库行数
```

**别名**：`ingest_daily(code, start, end, adjust, init_schema)`（v0.1 兼容）。

### 3.4 `compute_factors(code, start, end=None, freq="1d", names=None, init_schema=False) -> int`

加载行情 → 计算因子 → 落库（物化缓存，upsert 幂等）。

```python
from quant.app.service import compute_factors
compute_factors("sh.600000", "2024-01-01",
                freq="1d", names=["momentum_20", "volatility_20"])
```

- `names=None` → 计算全部内置因子；
- NaN 行跳过（预热区无意义不落库）。

### 3.5 `run_backtest(code, start, end=None, strategy=None, fast=5, slow=20, freq="1d", settings=None) -> tuple[BacktestResult, pd.DataFrame]`

从 MySQL 取数 → 跑回测。返回 `(结果, 行情帧)` 供 CLI / Web 渲染。

```python
from quant.app.service import run_backtest
from quant.strategy.factor_momentum import FactorMomentumStrategy

result, bars = run_backtest(
    code="sh.600000", start="2021-01-01", end="2024-12-31",
    strategy=FactorMomentumStrategy(window=60), freq="1d",
)
print(result.metrics["total_return"])
```

**因子预热**：当 `strategy.required_factors` 非空时，自动多加载
`max(min_periods) × 1.6 + 10` 日历日的行情算因子，再裁剪回测区间注入引擎。

### 3.6 配置（`quant/config.py`）

| 配置项 | 环境变量 | 默认 |
|---|---|---|
| DB host | `QUANT_DB_HOST` | `127.0.0.1` |
| DB port | `QUANT_DB_PORT` | `3306` |
| DB user | `QUANT_DB_USER` | `root` |
| DB password | `QUANT_DB_PASSWORD` | `""` |
| DB name | `QUANT_DB_NAME` | `ods` |
| data_source | — | `baostock` |
| backtest.init_cash | — | `1_000_000` |
| backtest.commission_rate | — | `0.00025` |
| backtest.slippage | — | `0.002` |

详见 `config/settings.example.yaml`。