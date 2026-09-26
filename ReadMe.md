# mini-quant 项目完整说明（合并版）

> 本文合并自《总体架构设计》《系统说明》《README》《因子库设计》四份文档，是理解与迭代本系统的唯一入口。
> 当前版本：**v0.3**（因子库）。历史演进：v0.1 日线回测 → v0.2 多频率（5 分钟）→ v0.3 因子库。
> 配套规格：`mini-quant/tests/smoke_test.py`（可执行的行为规格）。
> 合规提示：本平台仅限个人研究自用，不对外提供服务、不构成投资建议。

---

## 1. 使用说明

### 1.1 环境准备

```bash
cd mini-quant
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt                    # pandas / baostock / akshare / PyMySQL / FastAPI / uvicorn
                                                   # 注：WindPy 随 Wind 终端分发，不能 pip 安装（见 data 层说明）
```

依赖：Python 3.12 · MySQL 8（本机或局域网均可）。

**配置**：复制模板 `config/settings.example.yaml` → `config/settings.yaml`，填入 MySQL 连接与回测成本参数（也可不建文件，直接设 `QUANT_DB_*` 环境变量覆盖）。
> ⚠️ `settings.yaml` 含密码，已被 `.gitignore` 排除，**不要提交入库**；新环境一律从模板复制。

### 1.2 标准使用流程（五步）

```bash
# ① 初始化库表（幂等：日线表 + 分钟表 + 因子表，重复执行无副作用）
python -m quant.app.cli init-schema

# ② 抓取日线入库（前复权；增量——自动从库里最新日期的次日开始续抓）
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01

# ②b 用 AKShare 抓取日线（免费聚合多源，--source akshare）
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 --source akshare

# ②c 抓取 5 分钟线（建议同时保留日线：分钟表的涨跌停基准依赖日线昨收关联）
python -m quant.app.cli ingest --code sh.600000 --start 2025-01-01 --freq 5min

# ②d 用 AKShare 抓取 5 分钟线（注意：AKShare 分钟数据仅保留近期）
python -m quant.app.cli ingest --code sh.600000 --start 2025-01-01 --freq 5min --source akshare

# ②e 用 Wind 抓取（需本机安装并登录 Wind 金融终端；--source wind 同样支持 5/15/30/60min）
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 --source wind

# ③ 命令行回测（日线/分钟、双均线/因子策略，同一套代码）
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --end 2025-12-31 --fast 5 --slow 20
python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 --freq 5min --fast 8 --slow 48
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --strategy factor_momentum --window 60

# ④ 因子计算落库（物化缓存，供选股/因子分析；回测不走库——内存即时算，口径权威）
python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01
python -m quant.app.cli compute-factors --code sh.600000 --start 2025-01-01 --freq 5min --factors momentum_20,volatility_20

# ⑤ Web 控制台（浏览器打开 http://127.0.0.1:8000：K线+买卖点+净值对比+指标卡片，
#    支持日线/5分钟、双均线/动量因子策略切换）
python -m quant.app.cli serve --port 8000
```

辅助命令：

```bash
python -m quant.app.cli repair-dt    # 修复分钟表历史脏时间戳（17位数字串→标准格式，幂等）
python tests/smoke_test.py           # 离线冒烟测试（不依赖 MySQL/Baostock，改完代码必跑）
```

### 1.3 证券代码格式

Baostock 口径：沪市 `sh.600000`、深市 `sz.000001`（带交易所前缀）。

### 1.4 使用注意

- 回测 `--end` 不传默认到今天；数据必须先 `ingest`（否则报"无数据"）；
- 分钟级回测交易频率高，最低佣金 5 元/笔对小资金侵蚀显著（合成随机行情下佣金可达初始资金近 1%），关注 `total_commission` 指标；
- 前复权（默认）历史价会随后续分红除权漂移——长周期严格回测的建议见 §8 数据面。

---

## 2. 项目结构说明

```
mini-quant/
├── config/
│   ├── settings.yaml            # 实际配置（含密码，已 gitignore）
│   └── settings.example.yaml    # 配置模板（入库，新环境复制它）
├── quant/                        # 主包，六层，依赖只能朝下
│   ├── core/            ★ 领域层（零第三方依赖，全系统"宪法"）
│   │   ├── models.py       值对象：Bar（dt+trade_date 双时间字段）/ Order / Fill / Position / TradeRecord
│   │   ├── abstractions.py 接口：MarketDataSource / DataRepository / Strategy（含
│   │   │                   required_factors）/ StrategyContext / Factor / FactorAccessor /
│   │   │                   Broker / RiskRule（Protocol + ABC 混用）
│   │   └── portfolio.py    记账本：现金/持仓/净值曲线（T+1、含费摊薄成本）
│   ├── data/            数据层（core 抽象的实现）
│   │   ├── baostock_source.py  Baostock 适配器（fetch_bars 支持 1d/5/15/30/60min）
│   │   ├── akshare_source.py   AKShare 适配器（免费聚合多源，中文列名归一，分钟分段请求）
│   │   ├── wind_source.py      Wind 适配器（需 Wind 终端；延迟导入 + 字段降级 + 连接幂等复用）
│   │   ├── mysql_repo.py      仓储：freq 分表 ods_d（日线）+ ods_mi（分钟）
│   │   │                      + dwd_factor_value_i（因子长表）
│   │   └── timeutil.py        时间戳归一（norm_dt：识别 17位毫秒串等 5 种形态）
│   ├── factor/          因子层（v0.3，纯计算不碰存储）
│   │   ├── base.py       注册表：register / get / available
│   │   ├── builtin.py    内置因子：momentum_20/60、volatility_20、bias_20、volume_ratio_5_20
│   │   └── engine.py     FactorEngine：行情宽表 → 因子宽表
│   ├── strategy/
│   │   ├── double_ma.py       双均线策略（日线/分钟同一份代码）
│   │   └── factor_momentum.py 因子策略示例（声明式依赖 + ctx.factor 取值）
│   ├── backtest/
│   │   ├── engine.py      事件驱动引擎（按 trade_date 驱动日界，bar 级推进；
│   │   │                  factor_frame 注入 → FactorAccessor 只读切片防未来）
│   │   ├── sim_broker.py  模拟撮合：滑点/佣金/印花税/涨跌停拒单（日线昨收基准）
│   │   ├── risk.py        风控责任链：停牌ST → 手数 → 现金 → T+1 可卖
│   │   └── metrics.py     绩效：年化/回撤/夏普/卡玛/胜率/盈亏比（periods_per_year 参数化）
│   ├── app/
│   │   ├── service.py     服务层（CLI 与 Web 共用：ingest / compute_factors /
│   │   │                  run_backtest 含因子预热加载）
│   │   └── cli.py         命令行入口（--freq {1d,5min}，--strategy 选择策略）
│   └── webapp/
│       ├── server.py      FastAPI（只调 service，不触碰引擎细节）
│       └── index.html     ECharts 前端：K线+均线+买卖点+净值对比+指标卡片+成交明细
├── tests/
    └── smoke_test.py     离线冒烟测试（合成行情全链路：日线/5分钟/因子/防未来断言）

```

---

## 3. 每份代码的功能简要

| 文件 | 一句话职责 | 关键点 |
|---|---|---|
| `core/models.py` | 全系统值对象（不可变 dataclass） | `Bar.dt`（bar 时刻，含时分）+ `Bar.trade_date`（归属交易日）双字段是多频率兼容的命门；`Position.available` 实现 T+1 |
| `core/abstractions.py` | 全系统接口契约（"宪法"） | Protocol（鸭子类型接口，实现类无需显式继承）+ ABC（抽象基类，需继承）混用；`Strategy.required_factors` 声明因子依赖 |
| `core/portfolio.py` | 记账本 | `apply_fill` 按成交更新现金/持仓并结算盈亏；`snapshot` 每交易日记一条净值；单一职责样板 |
| `data/baostock_source.py` | 数据源适配器（Adapter，吸收外部 API 的脏格式） | 归一 baostock 17 位毫秒数字串时间戳；`_FREQ_MAP` 支持到 60min |
| `data/akshare_source.py` | AKShare 数据源适配器（免费、无需注册） | 中文列名→英文归一；代码格式转换 `sh.600000`↔`600000`；分钟线按 30 天分段请求拼接；3 次重试应对网络抖动 |
| `data/wind_source.py` | Wind（万得）数据源适配器（需本机 Wind 终端） | WindPy **延迟导入**（未装终端不影响其它源）；代码转换 `sh.600519`↔`600519.SH`；`wsd`/`wsi` 频率分发 + `PriceAdj` 复权映射；字段降级容错；连接幂等复用；出口必需列强校验 |
| `data/mysql_repo.py` | 仓储（Repository，对上屏蔽数据库细节） | freq 分表 + upsert 幂等写入；分钟读取 LEFT JOIN 日线表补涨跌停基准；因子长表读写 |
| `data/timeutil.py` | 时间戳归一工具 | `norm_dt()` 兼容 17位毫秒串/14位/8位/无空格等形态 |
| `factor/base.py` | 因子注册表 | `register/get/available`；新因子 = 一个子类 + 一行注册 |
| `factor/builtin.py` | 内置因子集 | momentum/volatility/bias/volume_ratio，全部因果计算（rolling/shift） |
| `factor/engine.py` | FactorEngine 纯计算 | 行情宽表 → 因子宽表 `[code, dt, <因子列…>]`，不碰存储 |
| `strategy/double_ma.py` | 双均线策略 | 频率无关：日线和 5 分钟同一份代码直接跑 |
| `strategy/factor_momentum.py` | 因子策略示例 | 声明 `required_factors=["momentum_N"]`，`ctx.factor()` 取值写信号 |
| `backtest/engine.py` | 事件驱动回测引擎 | 主循环按 `trade_date` 分组驱动日界（T+1 解禁/日终快照每交易日一次），bar 级推进 |
| `backtest/sim_broker.py` | 模拟撮合（SimBroker） | 信号次一 bar 开盘价成交；佣金万2.5（最低5元）+ 卖出印花税万5 + 滑点 0.2%（千2）；涨跌停基准=日线昨收 |
| `backtest/risk.py` | 风控责任链（Chain of Responsibility） | 多规则串行检查、任一拒绝即拦截；停牌ST→手数→现金→T+1可卖 |
| `backtest/metrics.py` | 绩效计算 | 全部由净值曲线+成交记录推导，纯函数可单测 |
| `app/service.py` | 服务层 | CLI 与 Web 共用的装配地（依赖注入发生地）；因子依赖解析 + 预热加载 |
| `app/cli.py` | 命令行入口 | init-schema / ingest / backtest / compute-factors / repair-dt / serve |
| `webapp/server.py` | Web 后端 | FastAPI，只调 service；`/api/backtest` 支持 freq/strategy/window 参数 |
| `webapp/index.html` | Web 前端 | ECharts：K线+均线+买卖点散点+净值对比+指标卡片+成交明细 |
| `tests/smoke_test.py` | 离线冒烟测试 | 合成行情验证全链路；含 T+1、日界、因子防未来的逐点断言 |

---

## 4. 技术架构、业务架构、技术栈

### 4.1 技术架构（六层，依赖只能朝下）

```
┌────────────────────────────────────────────────────────────┐
│ app / webapp  应用层                                        │
│   cli.py（命令行） service.py（服务装配） server.py+html（Web）│
├────────────────────────────────────────────────────────────┤
│ strategy      策略层     double_ma.py / factor_momentum.py  │
├──────────────────────┬─────────────────────────────────────┤
│ factor        因子层 │  base（注册表）builtin（内置因子）      │
│               （v0.3）│  engine（FactorEngine 纯计算）        │
├──────────────────────┴─────────────────────────────────────┤
│ backtest      回测层  │  engine（引擎）sim_broker（模拟撮合）  │
│                      │  risk（风控链）metrics（绩效）          │
├──────────────────────┴─────────────────────────────────────┤
│ data          数据层     baostock_source（数据源适配器）        │
│                       mysql_repo（本地仓储）timeutil（时间归一）│
├────────────────────────────────────────────────────────────┤
│ core          领域层 ★宪法：models（值对象）abstractions（接口）│
│               portfolio（记账本）—— 零 import 第三方            │
└────────────────────────────────────────────────────────────┘
```

架构风格：**分层架构 + 事件驱动 + 插件化（组合优于继承）**。

三条铁律（比 SOLID 更具体的约束）：

1. **core 零第三方依赖**——它定义 `Bar/Order/Fill/Position/Strategy/Broker` 等契约，其余层实现这些契约；依赖箭头永远指向抽象（依赖倒置 DIP：高层定义接口，低层实现接口，高层不 import 低层）；
2. **webapp 只调 app.service**，不直接触碰 data / backtest；
3. **策略只见 `StrategyContext`**，看不见引擎、数据源、数据库（接口隔离 ISP + 防未来函数）。

SOLID 落地速查：

| 原则 | 落地方式 |
|---|---|
| **S** 单一职责 | core(接口)/data(取数)/backtest(撮合)/strategy(信号)/webapp(展示) 各司其职，每个模块只有一个变化原因 |
| **O** 开闭 | 新数据源/新策略/新因子/新风控 = 新增一个实现类 + 注册一行，改 0 行旧代码 |
| **L** 里氏替换 | `SimBroker` 与未来 `QmtBroker` 实现同一 `Broker` 协议且行为契约一致 → 回测代码 0 修改跑实盘 |
| **I** 接口隔离 | 策略只见 `StrategyContext` 窄接口（history 只到当前 bar），不见引擎内部 |
| **D** 依赖倒置 | 引擎/策略依赖 core 抽象；MySQL、Baostock 都是可替换插件 |

### 4.2 业务架构（一条完整业务流水线）

**数据接入 → 因子加工 → 策略信号 → 风控审核 → 模拟撮合 → 记账 → 绩效分析 → 网页展示**

```
数据源（Baostock / AKShare）──ingest──▶ MySQL(ods.日线表/分钟表) ──load_bars──▶ 行情 DataFrame
                                                                    │
                                                    FactorEngine.compute（内存即时算）
                                                                    │
                              策略 on_bar(ctx, bar) ◀── StrategyContext（history/因子只读切片）
                                    │ ctx.buy()/sell()             │
                                    ▼                              │
                              RiskChain（四道风控）                 │
                                    ▼                              │
                              SimBroker（次 bar 开盘价撮合，含 A 股规则）│
                                    ▼                              │
                              Portfolio（现金/持仓/日终净值）        │
                                    ▼                              │
                              metrics（年化/回撤/夏普/卡玛/胜率）    │
                                    ▼                              │
                              CLI 打印 / Web K线+净值渲染 ◀──────────┘
```

A 股交易规则内置清单（回测可信的根基）：

| 规则 | 实现位置 |
|---|---|
| T+1（当日买入次日才能卖） | `Position.available` + 引擎日界 `Portfolio.on_new_day()` |
| 100 股整手 | `ctx.buy/sell` 取整 + `LotSizeRule` |
| 佣金 万2.5（最低 5 元） | `CostModel.commission` |
| 印花税（仅卖出）万5 | 同上 |
| 滑点 0.2%（千2） | `SimBroker._try_fill` |
| 涨停一字板拒买 / 跌停一字板拒卖 | `SimBroker._try_fill`（基准 = 日线昨收，±9.5% 近似；仅一字板拒单，非一字板交由限价单逻辑处理） |
| 停牌 / ST 拒单 | `TradabilityRule` + 分钟表 LEFT JOIN 日线回填状态 |
| 信号次一 bar 开盘价成交 | `SimBroker.settle`（防未来函数） |

### 4.3 技术栈

| 维度 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | 量化生态无可替代；策略研究表达力强 |
| 数据处理 | pandas | DataFrame 贯穿全链路，与因子计算天然契合 |
| 数据源 | Baostock + AKShare（免费）+ Wind（万得，需本机终端） | 三源互备，`--source` 一键切换且输出列完全同构；适配器模式可再加 Tushare |
| 存储 | MySQL 8（PyMySQL） | 用户既有环境；仓储接口隔离，换 Parquet+DuckDB 只动 data 层 |
| 回测 | 自研事件驱动内核 | 每一行都懂、可控；比 vn.py 轻、比 backtrader 透明 |
| Web | FastAPI + ECharts（原生 HTML/JS） | 异步 + 自动文档；K 线图生态成熟 |
| 测试 | 离线冒烟测试 | 合成行情，不依赖外部服务，改代码必跑 |

---

## 5. 核心抽象设计（每个模块的核心设计思路）

> 英文术语对照：Protocol=鸭子类型结构化接口（实现类无需显式继承）；ABC=抽象基类（需继承）；
> DIP=依赖倒置；LSP=里氏替换；SSOT=单一事实来源；upsert=存在则更新；因果计算=只用 ≤t 时刻数据计算 t 行。

### 5.1 值对象 `core/models.py` —— 不可变数据 + 双时间字段

全部 `@dataclass(frozen=True)`（不可变值对象，杜绝意外篡改）。

- **`Bar` 双时间字段是多频率兼容的命门**：`dt`（bar 时刻，分钟级含时分，用于排序/指标）+ `trade_date`（归属交易日，用于 T+1/涨跌停/绩效周期）。日线时代"一根 bar=一个交易日"是隐含前提，分钟级必须显式解耦。`Bar.date` 是 v0.1 向后兼容别名，**新代码一律用 `dt`/`trade_date`**。
- **`Position.available` 实现 T+1**：当日买入的部分不计入可卖数量，引擎每交易日开始时调 `Portfolio.on_new_day()` 解禁。
- `Fill` 含佣金、滑点成本、成交时刻；`avg_cost` 含佣金摊薄。

### 5.2 接口层 `core/abstractions.py` —— 系统的"宪法"

| 接口 | 职责 | 当前实现 | 将来可换 |
|---|---|---|---|
| `MarketDataSource` | 外部行情数据源 | `BaostockSource`、`AkshareSource`、`WindSource` | TushareSource 等 |
| `DataRepository` | 本地行情仓储（含可选因子读写） | `MySQLBarRepo` | Parquet + DuckDB |
| `Strategy` (ABC) | 策略钩子：`on_init`/`on_bar`（必需）+ `on_new_day`（可选）；`required_factors` 声明因子依赖 | `DoubleMAStrategy`、`FactorMomentumStrategy` | 任何继承它的类 |
| `Factor` (ABC) | 因子契约：`name`/`min_periods`/`compute`（只允许因果计算） | 5 个内置因子 | 任何子类（注册即用） |
| `FactorAccessor` | 策略可见的因子只读视图（引擎逐 bar 切片构造，防未来） | 引擎内部实现 | —— |
| `Broker` | 撮合通道 | `SimBroker`（回测） | `QmtBroker`（实盘，引擎零改动） |
| `RiskRule` (ABC) | 风控规则（责任链节点） | 4 条内置规则 | 新增子类即可 |
| `OrderSink` / `PortfolioView` | 策略可见的窄接口（只下单/只读账户） | 引擎内部实现 | —— |

**`StrategyContext` 是策略看到的全部世界**：`history`（截至当前 bar 的收盘价序列）、`factor()`/`factor_history()`（因子只读访问）、`portfolio`（只读视图）、`buy()`/`sell()`（按现金/仓位比例下单，自动取整 100 股）。策略代码**频率无关**——这是整个抽象设计最直接的收益。

### 5.3 因子层 `factor/` —— SSOT + 声明式依赖 + 防未来内建（v0.3）

三原则：

| 原则 | 说明 |
|---|---|
| **SSOT（Single Source of Truth，单一事实来源）** | 因子计算逻辑只有 `Factor.compute` 一份；**回测内存即时计算**（与行情同帧同口径，永远是权威值），落库 `dwd_factor_value_i` 长表仅是物化缓存，供选股/因子分析场景——规避"库里旧口径 vs 回测新口径"漂移 |
| **声明式依赖** | 策略只写 `required_factors = ["momentum_20"]`，服务层自动解析依赖、多加载预热历史、计算后注入引擎 |
| **预热（lookback）** | 回测起点前多加载 `max(min_periods)` 的历史算因子，首日即有有效值（不是前 20 天 NaN 空转） |
| **长表存储** | `(code, date_time, freq, factor_name, value)` + 唯一键 upsert——加新因子零 DDL（不用改表结构），读侧 pivot 成宽表 |

### 5.4 回测层 `backtest/` —— 事件驱动 + A 股规则引擎

- **`engine.py` 主循环按 `trade_date` 分组驱动**：日界事件（T+1 解禁、日终净值快照）每个交易日只发生一次，分钟 bar 不会被误判为"新的一天"；一天内的 bar 按 `dt` 逐根推进。净值按日快照 → 年化基准恒为 252，不被 bar 数虚增。
- **`sim_broker.py` 撮合规则**：信号在次一根 bar 的开盘价成交（日线=次日开盘，分钟=5分钟后）；涨跌停基准用**日线昨收 `pre_close`**（分钟 bar 的上一根收盘不能作基准，否则 10:00 的 bar 会被误判涨停）。
- **`risk.py` 责任链**：停牌ST → 手数 → 现金 → T+1 可卖，顺序即优先级，任一拒绝即拦截并记录 `rejects`。
- **`metrics.py` 纯函数**：全部由净值曲线 + 成交记录推导，`periods_per_year` 参数化。

### 5.5 数据层 `data/` —— 适配器 + 仓储双隔离

- **`baostock_source.py`（适配器 Adapter）**：吸收 Baostock 的脏格式（17 位毫秒数字串时间戳等），对上输出统一列 DataFrame；
- **`akshare_source.py`（适配器 Adapter）**：AKShare 免费开源无需注册。吸收中文列名（`日期`→`dt`、`开盘`→`open`…）、代码格式（`sh.600000`↔`600000`）、复权标记（`2`→`qfq`）等差异；分钟线按 30 天分段请求拼接突破长度限制；3 次重试应对网络抖动；
- **`wind_source.py`（适配器 Adapter）**：Wind（万得）需本机安装并登录 Wind 金融终端，WindPy 随终端分发、无法 pip 安装（故**延迟导入**，未装终端不影响其它源）。吸收代码格式（`sh.600519`↔`600519.SH`）、字段名大小写（返回 `OPEN`→归一 `open`）、`trade_status` 为**中文描述**（`交易`/`停牌`）、`amt`/`amount` 别名等差异；全量字段请求失败时自动降级核心字段重试；出口对必需列强校验，杜绝"缺行情列"的坏数据静默入库；
- **`mysql_repo.py`（仓储 Repository）**：对上屏蔽数据库细节。freq 分表：`1d` → `ods_d_stock_quotation_i`，`5min` → `ods_mi_stock_quotation_i`；分钟读取时 LEFT JOIN 日线表回填日线口径的 `pre_close/trade_status/is_st`（日线缺失时优雅降级为不判涨跌停）；
- 数据库约定：唯一键（日表 `code+date+adjust_flag`，分钟表 `code+date_time+adjust_flag`），写入 upsert 天然幂等；`adjust_flag` 复权标记 1=后复权 2=前复权 3=不复权（默认前复权）；ODS（Operational Data Store，贴源层）= 原样落库的外部数据。

### 5.6 应用层 `app/service.py` —— 依赖注入装配地

CLI 与 Web 共用，避免业务逻辑散落。核心职责：因子依赖解析与预热加载、策略装配、回测执行、行情帧透传（供渲染）。

---

## 6. 数据流转及涉及的核心类和方法

以 `python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 --freq 5min --strategy factor_momentum` 为例，完整走读：

```
cli.py main() 解析参数
  → service.run_backtest(code, start, end, freq, strategy)
      │
      ├─ strategy.required_factors 非空？                    # 声明式因子依赖
      │    · 是：lookback = max(min_periods)×1.6 日历日提前加载行情
      │      → FactorEngine.compute(bars, names)             # 内存即时计算因子宽表
      │      → 裁剪掉预热区间（预热 bar 只算因子，不进主循环）
      │
      ├─ MySQLBarRepo.load_bars([code], start, end, freq, adjust="2")
      │    · SELECT ... FROM ods.ods_mi_stock_quotation_i
      │    · LEFT JOIN 日线表回填 pre_close/trade_status/is_st
      │    · timeutil.norm_dt 归一时间戳；trade_date = dt[:10]
      │    · end 边界自动放宽到 23:59:59（防字符串比较丢当日数据）
      │
      └─ BacktestEngine(bars, strategy, factor_frame).run()
           for 每个交易日 trade_date:              # groupby("trade_date")
               Portfolio.on_new_day()               # T+1 解禁（每天仅一次）
               for 每根 bar（dt 升序）:
                   ① SimBroker.settle(bar)          # 上一根的挂单，本 bar 开盘价±滑点成交
                   │    └→ Portfolio.apply_fill(fill)   # 更新现金/持仓/盈亏
                   ② 追加 history + FactorAccessor(iloc[:n] 切片) 构造 StrategyContext
                   ③ 首根 bar 时触发 strategy.on_new_day()（撮合后、信号前的日始钩子）
                   ④ strategy.on_bar(ctx, bar)      # 策略信号
                        └→ ctx.buy()/sell() → 引擎.submit(order)
                             → RiskChain.check（停牌ST→手数→现金→T+1，四道风控）
                                  └→ SimBroker._pending（挂到次一根 bar）
               Portfolio.snapshot(trade_date)       # 日终净值快照（每天一条）
           → metrics.compute_metrics(equity_curve, trades)  # 年化/回撤/夏普/卡玛/胜率
  → CLI 打印 / webapp server.py 渲染（K线 x 轴=dt；净值曲线 x 轴=交易日）
```

**防未来函数（Look-ahead Bias，"用了当时不可能知道的数据"的作弊）的四道保险**：

1. `ctx.history` 只含当前 bar（含）及以前；
2. `ctx.factor()`/`ctx.factor_history()` 由引擎 `iloc[:n]` 切片注入——策略结构上不可能读到当前 bar 之后的因子行；
3. 订单在**次一根 bar 的开盘价**成交（信号收盘产生，不可能当根成交）；
4. 分钟级下成交价 = 开盘价 ± 滑点（Slippage，模拟真实成交偏差的成本）。

**历史踩坑清单**（别再踩）：

1. SQL `BETWEEN` 对 None：`end` 未传时拼进 SQL 会查空——service 层默认补今天；
2. baostock 分钟时间戳是 17 位毫秒数字串：入库前 `norm_dt` 归一 + `repair-dt` 命令可就地修复历史脏行（幂等）；
3. 分钟查询 end 边界：`'2025-06-30 15:00:00' > '2025-06-30'`（字符串比较）会丢 end 当日全部数据——仓储层已自动放宽到 23:59:59；
4. Web benchmark 错位：分钟频率下基准曲线按 bar 数生成而净值 x 轴是交易日——已按"每日最后一根 close"重采样对齐；
5. `__pycache__` 残留：已设 `PYTHONDONTWRITEBYTECODE=1` 环境变量全局禁用。

---

## 7. 扩展方法（不改旧代码）

| 想做什么 | 怎么做 | 改动量 |
|---|---|---|
| **新策略** | `quant/strategy/` 继承 `Strategy`，实现 `on_bar`（可选重写 `on_new_day`），用 `ctx.buy()/sell()` | 1 个新文件 |
| **新因子** | `quant/factor/builtin.py` 写一个 `Factor` 子类（`name`/`min_periods`/`compute`，只允许 rolling/shift 因果计算）+ `register()` 一行，策略声明 `required_factors` 即用 | 1 个类 + 1 行 |
| **新数据源** | `quant/data/` 写一个类实现 `fetch_bars`（返回统一列 DataFrame），`service.py` 的 `get_source` 注册一行 | 1 个新文件 + 1 行注册 |
| **换存储** | 重写 `DataRepository` 实现（如 Parquet + DuckDB），`service.py` 装配处换一行 | 1 个文件 |
| **新频率**（15/30/60min） | `baostock_source._FREQ_MAP` + `mysql_repo.TABLES` 各加一行 + 建表 | 2 行 + DDL |
| **新风控** | `risk.py` 加一个 `RiskRule` 子类，注册到链上 | 1 个类 |
| **切实盘** | 实现 `Broker` 协议（如 miniQMT 通道的 QmtBroker）注入引擎——引擎与策略零改动（LSP 的直接兑现） | 1 个文件 |
| **ETF / 可转债** | 主数据加类型字段；SimBroker 数量规则参数化 | 微调 |

每一行"不改旧代码"，都是当初接口抽象换来的红利（OCP 的直接兑现）。

---

## 8. 未来可扩展和优化的方向（v0.4+ 候选）

**因子面**（v0.3 已落地：时序因子 + 注册表 + 长表存储 + 回测即时计算）

- 截面因子（Cross-sectional，跨标的排名/行业中性化）与组合选股策略；
- IC / IR 因子有效性分析脚本（IC=信息系数，因子值与下期收益的相关性；IR=信息比率）；
- 因子增量水位计算（目前 compute-factors 为全量重算，幂等可接受）。

**数据面**

- 后复权双份存储（`adjust=1`）：前复权历史价会随分红漂移，严格长周期回测需要；复权因子表 + 任意时点复权重算；
- 全市场批量抓取调度（交易日历对齐、断点续抓、并发限速）；
- ~~双数据源互备~~ ✅ 已落地：Baostock + AKShare + Wind，`ingest --source baostock|akshare|wind` 一键切换。

**回测面**

- `StrategyContext.history` 每根 bar `pd.concat`——单标的没问题，多标的大规模回测需改预载矩阵（numpy 二维 + 滚动窗口）；
- 组合回测（多标的同时跑、资金分配合约）；
- 参数寻优（网格/贝叶斯/遗传）+ 过拟合检验（样本外 Walk-forward 滚动前推验证）；
- 回测结果落库（`strategy_runs` 表：代码版本 hash + 参数 + 数据水位 → 结果 JSON），实现"可复现"。

**执行面（实盘路线）**

- `QmtBroker` 实现 `Broker` 协议接 miniQMT（迅投）通道；
- 实时行情 Feed（替代回测的批量 DataFrame；回测=历史回放、实盘=实时订阅，对引擎透明——LSP）；
- 模拟盘先行：实盘 Runner + SimBroker + LiveFeed 观察一周后再上小资金实盘；
- 仓位对账 / 断线重连 / 日报推送（企业微信机器人）。

**工程面**

- 单测补齐（pytest；撮合与风控 100% 覆盖；`golden/` 黄金回测——固定数据快照 diff 锁定结果，防改动引入回归）；
- CI + 代码质量门禁（ruff + mypy，strict 模式只对 core/）；
- Web 分钟 K 线聚合渲染（一年 5 分钟 bar 上万根，前端需按日聚合或增量加载/dataZoom 优化）；
- 向量化快筛模式（pandas 全矩阵秒级出结果，与事件驱动内核共用成本参数，口径一致）——研究初筛用。

**风险清单**：

| 风险 | 对策 |
|---|---|
| 未来函数（回测虚高） | 四道保险 + 单测逐点断言锁定 |
| 过拟合 | 样本外验证、参数敏感性分析、寻优结果打折评估 |
| 前复权历史漂移 | 切后复权双份存储（§数据面） |
| 数据源单点 | ~~适配器模式预留~~ → ✅ Baostock + AKShare + Wind 三源互备（`--source` 切换） |
| 文档腐化 | 每个大版本提交时同步更新本文档 |
