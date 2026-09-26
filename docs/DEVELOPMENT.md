# mini-quant 开发文档

> 面向开发者：设计思路、架构说明、接口说明、扩展指南、测试体系。
> 安装、启动、运维等日常操作见根目录 `ReadMe.md`（操作文档）。
> 合规提示：本平台仅限个人研究自用，不对外提供服务、不构成投资建议。
>
> 版本演进：v0.1 日线回测 → v0.2 多频率（5 分钟）+ Web → v0.3 因子库 + 双源
> → v0.4（Unreleased）Wind 数据源。详细变更历史见 `git log`。

---

## 目录

1. [总体架构](#1-总体架构)
2. [目录结构与模块职责](#2-目录结构与模块职责)
3. [核心抽象与接口契约](#3-核心抽象与接口契约)
4. [关键设计决策](#4-关键设计决策)
5. [数据流转走读](#5-数据流转走读)
6. [A股规则与防未来函数](#6-a股规则与防未来函数)
7. [接口参考：Web API 与 service 函数](#7-接口参考web-api-与-service-函数)
8. [扩展指南](#8-扩展指南)
9. [测试体系](#9-测试体系)
10. [Web 端](#10-web-端)
11. [历史踩坑清单](#11-历史踩坑清单)
12. [路线图（v0.4+ 候选）](#12-路线图v04-候选)

---

## 1. 总体架构

**六层分层架构，依赖只能朝下**：

```
┌────────────────────────────────────────────────────────────┐
│ app / webapp   应用层（CLI / 业务装配 / Web）                 │
├────────────────────────────────────────────────────────────┤
│ strategy      策略层    双均线 / 动量 / 用户自定义             │
├────────────────────────────────────────────────────────────┤
│ factor        因子层    注册表 / 内置因子 / FactorEngine      │
├────────────────────────────────────────────────────────────┤
│ backtest      回测层    事件驱动引擎 / 模拟撮合 / 风控链 / 绩效│
├────────────────────────────────────────────────────────────┤
│ data          数据层    数据源适配器 × 3 / 仓储 / 时间归一     │
├────────────────────────────────────────────────────────────┤
│ core          ★领域层   models / abstractions / portfolio  │
│               零 import 第三方（全系统"宪法"）                │
└────────────────────────────────────────────────────────────┘
```

**三条铁律**（比 SOLID 更具体的硬约束）：

1. **core 零第三方依赖**——core 定义 `Bar / Order / Fill / Position / Strategy /
   Broker` 等契约，其余层实现契约；依赖箭头永远指向抽象（DIP）。
2. **webapp 只调 app.service**，不直接触碰 data / backtest / factor。
3. **策略只见 `StrategyContext`**，看不见引擎、数据源、数据库（ISP + 防未来）。

**业务流水线**：

```
数据接入 → 因子加工 → 策略信号 → 风控审核 → 模拟撮合 → 记账 → 绩效分析 → 展示
```

**SOLID 落地速查**：

| 原则 | 落地方式 |
|---|---|
| **S** 单一职责 | core(契约)/data(取数)/backtest(撮合)/strategy(信号)/factor(计算)/webapp(展示) 各司其职 |
| **O** 开闭 | 新数据源 / 新策略 / 新因子 / 新风控 = 新增实现类 + 注册一行，改 0 行旧代码 |
| **L** 里氏替换 | `SimBroker` 与未来 `QmtBroker` 实现同一 `Broker` 协议 → 回测代码 0 修改跑实盘 |
| **I** 接口隔离 | 策略只见 `StrategyContext` 窄接口（history 只到当前 bar） |
| **D** 依赖倒置 | 引擎 / 策略依赖 core 抽象；MySQL / Baostock / AKShare / Wind 都是可替换插件 |

---

## 2. 目录结构与模块职责

```
mini-quant/
├── config/
│   ├── settings.yaml            # 实际配置（含密码，已 gitignore）
│   └── settings.example.yaml    # 配置模板（入库，新环境复制它）
├── docs/
│   └── DEVELOPMENT.md           # 本文档（开发文档）
├── quant/                       # 主包，六层，依赖只能朝下
│   ├── core/          ★ 领域层（零第三方依赖）
│   │   ├── models.py       值对象：Bar（dt + trade_date 双时间字段）/ Order / Fill / Position / TradeRecord
│   │   ├── abstractions.py 接口契约：MarketDataSource / DataRepository / Strategy /
│   │   │                   Factor / FactorAccessor / Broker / RiskRule 等
│   │   └── portfolio.py    记账本：现金 / 持仓 / 净值曲线（T+1、含费摊薄成本）
│   ├── data/            数据层（core 抽象的实现）
│   │   ├── baostock_source.py  Baostock 适配器（1d/5/15/30/60min）
│   │   ├── akshare_source.py   AKShare 适配器（免费聚合多源，分钟分段请求）
│   │   ├── wind_source.py      Wind 适配器（需本机 Wind 终端；延迟导入 + 字段降级）
│   │   ├── mysql_repo.py      仓储：freq 分表 ods_d + ods_mi + 因子长表
│   │   └── timeutil.py        时间戳归一（norm_dt：识别 17 位毫秒串等 5 种形态）
│   ├── factor/          因子层（纯计算不碰存储）
│   │   ├── base.py       注册表：register / get / available
│   │   ├── builtin.py    内置因子：momentum_20/60、volatility_20、bias_20、volume_ratio_5_20
│   │   └── engine.py     FactorEngine：行情宽表 → 因子宽表
│   ├── strategy/
│   │   ├── double_ma.py       双均线策略（日线/分钟同一份代码）
│   │   └── factor_momentum.py 因子策略示例（声明式依赖 + ctx.factor 取值）
│   ├── backtest/
│   │   ├── engine.py      事件驱动引擎（按 trade_date 驱动日界，bar 级推进）
│   │   ├── sim_broker.py  模拟撮合：滑点/佣金/印花税/涨跌停拒单 + 挂单 TTL
│   │   ├── risk.py        风控责任链：停牌ST → 手数 → 现金 → T+1 可卖
│   │   └── metrics.py     绩效：年化/回撤/夏普/卡玛/胜率/盈亏比
│   ├── app/
│   │   ├── service.py     服务层（CLI 与 Web 共用：依赖注入装配地）
│   │   └── cli.py         命令行入口
│   └── webapp/
│       ├── server.py      FastAPI（只调 service）
│       └── index.html     ECharts 前端
├── tests/
│   ├── smoke_test.py     离线冒烟测试（合成行情全链路，改完代码必跑）
│   ├── baostock_test.py  Baostock 端口连通性脚本（需网络）
│   └── wind_test.py      Wind 终端登录 + 真实拉取（需 Wind）
├── tools/
│   └── diagnose_network.py  数据源网络自检（DNS/TCP/HTTPS/代理/端到端五层）
├── ReadMe.md             操作文档（启动 / 运维 / 技术栈）
└── requirements.txt
```

**模块职责速查**：

| 文件 | 一句话职责 | 关键点 |
|---|---|---|
| `core/models.py` | 全系统值对象（不可变 dataclass） | `Bar.dt` + `Bar.trade_date` 双字段是多频率兼容的命门；`Position.available` 实现 T+1 |
| `core/abstractions.py` | 接口契约（"宪法"） | Protocol（鸭子类型）与 ABC（需继承）混用；`Strategy.required_factors` 声明因子依赖 |
| `core/portfolio.py` | 记账本 | `apply_fill` 更新现金/持仓并结算盈亏；买入现金不足返回 False（防现金变负）；`snapshot` 停牌日用最近收盘价估值 |
| `data/*_source.py` | 数据源适配器（Adapter） | 吸收各源脏格式，对上输出统一列 DataFrame（见 §3.1） |
| `data/mysql_repo.py` | 仓储（Repository） | freq 分表 + upsert 幂等；分钟读取 LEFT JOIN 日线表回填涨跌停基准 |
| `factor/base.py` | 因子注册表 | `register/get/available`；新因子 = 一个子类 + 一行注册 |
| `factor/engine.py` | FactorEngine 纯计算 | 行情宽表 → 因子宽表 `[code, dt, <因子列…>]`；因子名与行情列冲突显式报错 |
| `strategy/double_ma.py` | 双均线策略 | 频率无关；容差交叉检测（eps=1e-8）防浮点精度丢信号 |
| `backtest/engine.py` | 事件驱动引擎 | 按 `trade_date` 分组驱动日界；预构建 numpy 数组消除 O(n²)；`submit` 时序异常记 rejects 不静默吞单 |
| `backtest/sim_broker.py` | 模拟撮合 | 信号次一 bar 开盘价成交；一字板拒单；限价单触及成交；挂单 TTL 默认 1（当日有效） |
| `backtest/risk.py` | 风控责任链 | 顺序即优先级；`_pending_sell` 同日卖出防重 |
| `backtest/metrics.py` | 绩效纯函数 | 全部由净值曲线 + 成交记录推导；`periods_per_year` 参数化 |
| `app/service.py` | 服务层 | CLI 与 Web 共用装配地；因子依赖解析 + 预热加载 |
| `webapp/server.py` | Web 后端 | 只调 service；基准曲线按 bar 粒度生成与 K 线 x 轴等长 |

---

## 3. 核心抽象与接口契约

> 术语：Protocol = 鸭子类型结构化接口（实现类无需显式继承，适合可能被第三方库
> 包装的外部依赖）；ABC = 抽象基类（需显式继承，适合需要明确"我是体系一员"的
> 扩展点）；SSOT = 单一事实来源；upsert = 存在则更新；因果计算 = t 行只依赖 ≤t 数据。

### 3.1 统一列（数据面的通用语言）

所有数据源适配器与仓储的输出都是**统一列 DataFrame**，仓储 / 引擎 / 策略只认它：

```python
["code", "dt", "trade_date", "open", "high", "low", "close",
 "pre_close", "volume", "amount", "trade_status", "is_st"]
```

- `code`：本系统口径 `sh.600000` / `sz.000001`（Baostock 风格，带交易所前缀）。
- `dt`：bar 时刻。日线 `YYYY-MM-DD`；分钟 `YYYY-MM-DD HH:MM:SS`。
- `trade_date`：归属交易日，恒为 `YYYY-MM-DD`。
- `adjust_flag`：复权标记 1=后复权 2=前复权（默认） 3=不复权。
- `trade_status`：1 正常交易 0 停牌；`is_st`：1 ST 0 正常。

### 3.2 接口契约矩阵

| 接口 | 类型 | 职责 | 当前实现 | 可替换方向 |
|---|---|---|---|---|
| `MarketDataSource` | Protocol | 外部行情源 | `BaostockSource` / `AkshareSource` / `WindSource` | Tushare 等 |
| `DataRepository` | Protocol | 本地行情仓储（四方法必备契约） | `MySQLBarRepo` | Parquet + DuckDB |
| `Strategy` | ABC | 策略钩子 `on_init`/`on_bar`（必需）+ `on_new_day`（可选） | `DoubleMAStrategy`、`FactorMomentumStrategy` | 任意子类 |
| `Factor` | ABC | 因子契约 `name`/`min_periods`/`compute` | 5 个内置因子 | 任意子类（注册即用） |
| `FactorAccessor` | class | 因子只读视图（引擎逐 bar `iloc[:n]` 切片，防未来） | 引擎内部 | —— |
| `Broker` | Protocol | 撮合通道 `submit`/`settle` | `SimBroker`（回测） | `QmtBroker`（实盘） |
| `RiskRule` | ABC | 风控规则（责任链节点） | 4 条内置规则 | 新增子类即可 |
| `OrderSink` | Protocol | 订单入口（策略只见它，不认识引擎） | 引擎自身实现 | —— |
| `PortfolioView` | Protocol | 账户只读视图 | 引擎内部 `_PortfolioViewImpl` | —— |

### 3.3 StrategyContext —— 策略看到的全部世界

```
ctx.history          截至当前 bar（含）的收盘价 Series，index 为 dt
ctx.price            当前 bar 收盘价
ctx.portfolio        账户只读视图（cash / position / equity）
ctx.factor(name)     当前 bar 的因子值（NaN = 预热区）
ctx.factor_history(name)  截至当前 bar 的因子值序列
ctx.buy(code, cash_ratio=0.95)   按现金比例市价买入（100 股向下取整）
ctx.sell(code, ratio=1.0)        卖出可用仓位（仅 available，ratio>=1 直接清仓）
```

策略代码**频率无关**——这是整个抽象设计最直接的收益：rolling 指标天然按 bar 计算
（日线 MA20 = 月均线，5 分钟 MA20 = 日内 100 分钟均线）。

**数据源适配器契约**（`MarketDataSource.fetch_bars`）：

```python
@staticmethod
def fetch_bars(code, start, end, freq="1d", adjust="2") -> pd.DataFrame
```

返回 §3.1 统一列。三个内置源输出**完全同构**，上层引擎 / 策略 / 仓储零感知。
`wind_source.py` 提供两个可借鉴技巧：**延迟导入**第三方 SDK（未装终端的机器不受
影响）；**字段降级容错**（全量字段失败时降级核心字段重试）。

---

## 4. 关键设计决策

> 完整推理过程（背景/取舍/影响）已随代码注释与 git 历史沉淀，此处保留结论速查。
> 新增重大设计时在此表追加一行。

| # | 决策 | 一句话理由 |
|---|---|---|
| 1 | `Bar` 双时间字段（`dt` + `trade_date`） | bar 时刻管排序/指标，归属交易日管 T+1/涨跌停/绩效周期；分钟级必须显式解耦，否则 10:00 的 bar 被误判"新的一天" |
| 2 | `Position` 双数量字段（`quantity` + `available`）实现 T+1 | 买入只增 quantity；次日 `on_new_day()` 解禁。比日期集合 / 每日重算简单且 O(1) |
| 3 | `avg_cost` 含买入佣金摊薄 | 卖出利润公式 `(sell-avg_cost)*qty-sell_commission` 不重复扣减买入侧费用 |
| 4 | 因子 SSOT：回测内存即时算，落库仅物化缓存 | 因子逻辑只有 `Factor.compute` 一份，规避"库里旧口径 vs 回测新口径"漂移 |
| 5 | freq 分表（`ods_d` / `ods_mi` / `dwd_factor_value_i`） | 字段集/唯一键天然不同；分钟表 LEFT JOIN 日线回填涨跌停基准 |
| 6 | 订单次一 bar 开盘价撮合 | 防"先见收盘价后成交"的未来函数；限价单整根 bar 区间触及即成交 |
| 7 | 涨跌停基准 = 日线昨收 `pre_close`（±9.5% 近似） | 分钟 bar 的上一根收盘不能作基准，否则日内累计涨幅被误判涨停 |
| 8 | 风控责任链顺序：停牌ST → 手数 → 现金 → T+1 | 先排除"不能做的"再校验"能不能做"，fail-fast 且拒单日志可读 |
| 9 | 风控估算口径对齐撮合（`open × (1+滑点+佣金率)` 缓冲） | 跳空时估算不低估；撮合侧 `apply_fill` 现金不足再兜底拒单（双保险） |
| 10 | 同日卖出防重（`RiskChain._pending_sell` 追踪） | `available` 次日 bar 才扣减，同日多笔卖单须累计校验 |
| 11 | 因子预热 `lookback × 1.6 + 10` 日历日 | 日历日/交易日 ≈ 1.45，取 1.6 防节假日集中 + 10 天防极端长假 |
| 12 | Web 基准曲线按 bar 粒度生成 | 与 K 线 x 轴等长（分钟频率下自动对齐 dataZoom） |
| 13 | 挂单 TTL 默认 1（A 股当日有效） | 规避"信号 t 在 t+30 才成交"假象；过期撤单不入 rejects（预期行为） |

---

## 5. 数据流转走读

以 `backtest --code sh.600000 --start 2025-01-01 --freq 5min --strategy factor_momentum` 为例：

```
cli.py main() 解析参数
  → service.run_backtest(code, start, end, freq, strategy)
      │
      ├─ strategy.required_factors 非空？                    # 声明式因子依赖
      │    · 是：lookback = max(min_periods)×1.6+10 日历日提前加载行情
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
               RiskChain.reset_pending()            # 清空昨日委托追踪
               for 每根 bar（dt 升序）:
                   ① SimBroker.settle(bar)          # 上一根的挂单，本 bar 开盘价±滑点成交
                   │    └→ Portfolio.apply_fill()   # 更新现金/持仓/盈亏；现金不足则拒单
                   ② 追加 history + FactorAccessor(iloc[:n] 切片) 构造 StrategyContext
                   ③ 首根 bar 触发 strategy.on_new_day()（撮合后、信号前的日始钩子）
                   ④ strategy.on_bar(ctx, bar)      # 策略信号
                        └→ ctx.buy()/sell() → 引擎.submit(order)
                             → RiskChain.check（停牌ST→手数→现金→T+1，四道风控）
                                  └→ SimBroker._pending（挂到次一根 bar）
               Portfolio.snapshot(trade_date)       # 日终净值快照（每天一条）
           → metrics.compute_metrics(equity_curve, trades)  # 年化/回撤/夏普/卡玛/胜率
  → CLI 打印 / webapp server.py 渲染（K线 x 轴=dt；净值曲线 x 轴=交易日）
```

---

## 6. A股规则与防未来函数

### 6.1 A股交易规则内置清单（回测可信的根基）

| 规则 | 实现位置 |
|---|---|
| T+1（当日买入次日才能卖） | `Position.available` + 引擎日界 `Portfolio.on_new_day()` |
| 100 股整手 | `ctx.buy/sell` 取整 + `LotSizeRule` |
| 佣金 万2.5（最低 5 元） | `CostModel.commission` |
| 印花税（仅卖出）万5 | `CostModel.commission` |
| 滑点 0.2%（千2） | `SimBroker._try_fill` |
| 涨停一字板拒买 / 跌停一字板拒卖 | `SimBroker._try_fill`（基准 = 日线昨收，±9.5% 近似；非一字板交由限价单逻辑） |
| 停牌 / ST 拒单 | `TradabilityRule` + 分钟表 LEFT JOIN 日线回填状态 |
| 挂单当日有效 | `Order.ttl_bars=1`，到期自动撤（不入 rejects） |
| 信号次一 bar 开盘价成交 | `SimBroker.settle`（防未来函数） |

### 6.2 防未来函数的四道保险

| # | 保险 | 实现 |
|---|---|---|
| 1 | `ctx.history` 只含当前 bar（含）及以前 | 引擎 numpy 数组切片引用（零拷贝） |
| 2 | `ctx.factor()/factor_history()` 只看到 ≤ 当前 bar | 引擎 `_attach_factors` + 每 bar `iloc[:n]` 切片 |
| 3 | 订单次一 bar 开盘价成交 | `SimBroker.settle`（信号当根不可能成交） |
| 4 | 成交价 ± 滑点模拟真实偏差 | `SimBroker._try_fill` |

---

## 7. 接口参考：Web API 与 service 函数

> CLI 命令的完整参数表见操作文档（`ReadMe.md`）§5。

### 7.1 Web API 端点（`webapp/server.py`）

| 端点 | 作用 |
|---|---|
| `GET /` | 返回 `index.html` 静态页 |
| `GET /api/codes` | DB 标的代码列表（前端下拉框） |
| `GET /api/backtest` | 运行回测，返回前端渲染所需的全部 JSON |

`GET /api/backtest` Query 参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` / `start` / `end` | str | ✅必填 | 证券代码 / 起止日期 |
| `freq` | str | `1d` | `1d` / `5min` |
| `strategy` | str | `double_ma` | `double_ma` / `factor_momentum` |
| `fast` / `slow` | int | `5` / `20` | 双均线周期（double_ma） |
| `window` | int | `20` | 动量窗口（factor_momentum） |

校验：`freq`/`strategy` 非法 → `400`；`fast >= slow` → `400`；库内无数据 → `404`。

响应 8 段 JSON：`params`（回显参数）、`metrics`（绩效指标集）、`kline`（dates +
OHLC + ma_fast/ma_slow）、`equity_curve`（每交易日一条净值）、`benchmark`（期初
全仓买入持有，按 bar 粒度）、`buys`/`sells`（成交明细）、`rejects`（风控拒单数）。

### 7.2 `app/service` 公共函数（CLI 与 Web 共用）

新写脚本时优先复用这一层：

```python
from quant.app.service import get_repo, get_source, ingest_bars, compute_factors, run_backtest

repo = get_repo()                          # 仓储工厂 → MySQLBarRepo
SrcCls = get_source("akshare")             # 数据源工厂（返回类，方法均为 @staticmethod）
n = ingest_bars("sh.600000", "2020-01-01") # 增量抓取入库（幂等），返回入库行数
compute_factors("sh.600000", "2024-01-01",
                names=["momentum_20"])     # 因子计算落库（物化缓存，upsert 幂等）
result, bars = run_backtest(               # 取数 → 跑回测，返回 (结果, 行情帧)
    code="sh.600000", start="2021-01-01",
    strategy=FactorMomentumStrategy(60), freq="1d")
```

要点：

- `ingest_bars` 自动从库中最新 bar 的次日续抓；`ingest_daily` 为 v0.1 兼容别名。
- `run_backtest` 的**因子预热**：`strategy.required_factors` 非空时自动多加载
  `max(min_periods) × 1.6 + 10` 日历日行情算因子，再裁剪回测区间注入引擎。
- 配置优先级：`config/settings.yaml` < `QUANT_DB_*` 环境变量（详见操作文档 §3）。

---

## 8. 扩展指南

> OCP 的兑现清单。共同铁律：**扩展不修改任何旧代码**（仅新增文件 + 注册处加一行）。

### 8.0 扩展点速查

| 想做什么 | 改哪些文件 | 改动量 |
|---|---|---|
| 新策略 | `quant/strategy/<新文件>.py` + `cli.py` + `server.py` | 1 新文件 + 2 行 |
| 新因子 | `quant/factor/builtin.py` 追加类 + register | 1 类 + 1 行 |
| 新数据源 | `quant/data/<新源>.py` + `service.get_source` + `cli.py` choices | 1 新文件 + 2 行 |
| 换存储 | 重写 `DataRepository` 实现 + `service.get_repo` | 1 文件 + 1 行 |
| 新频率 | `baostock_source._FREQ_MAP` + `mysql_repo.TABLES` + DDL | 2 行 + DDL |
| 新风控 | `risk.py` 追加子类 + `RiskChain.__init__` | 1 类 + 1 行 |
| 切实盘 | 新建 `QmtBroker` 实现 `Broker` 协议，替换引擎注入 | 1 新文件 |

### 8.1 新策略

```python
# quant/strategy/your_strategy.py
from ..core.abstractions import Strategy, StrategyContext
from ..core.models import Bar

class YourStrategy(Strategy):
    def __init__(self, param1: int = 20):
        self.param1 = param1
        self.params = {"param1": param1}          # ⚠️ 类属性陷阱：实例级重新赋值
        self.required_factors = []                # 如需因子：["momentum_20"]

    def on_init(self, ctx: StrategyContext) -> None: ...
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        hist = ctx.history                        # 截至当前 bar 的收盘价序列
        if len(hist) < self.param1:
            return
        if hist.rolling(self.param1).mean().iloc[-1] > hist.iloc[-1]:
            ctx.buy(bar.code)                     # 信号 → 次一 bar 开盘成交
    # on_new_day 可选重写（每交易日首根 bar，撮合后、on_bar 前）
```

注册两处：`cli.py` 的 `--strategy` choices + 分支构造；`server.py` 的
`/api/backtest` 同样加 import + 分支（前端 `index.html` 的 `<select>` 加 option）。

### 8.2 新因子

```python
# quant/factor/builtin.py 末尾追加
class TurnoverVolatility(Factor):
    """换手率波动率：N 期换手率标准差。"""
    def __init__(self, window: int):
        self.name = f"turnover_vol_{window}"
        self.min_periods = window + 1
        self.window = window
    def compute(self, bars: pd.DataFrame) -> pd.Series:
        return bars["turn"].astype(float).rolling(self.window).std()

register(TurnoverVolatility(20))          # ← 注册一行
```

契约（必须遵守）：**只允许因果计算**（rolling/shift，t 行只依赖 ≤t 数据）；
历史不足输出 **NaN** 不抛错；`min_periods` 准确（引擎据此算预热窗口）。
参数化因子直接注册多个实例，名字即规格（如 `momentum_20` / `momentum_60`）。

### 8.3 新数据源

```python
# quant/data/your_source.py
class YourSource:
    name = "your_source"

    @staticmethod
    def fetch_bars(code, start, end, freq="1d", adjust="2") -> pd.DataFrame:
        raw = ...                                    # ① 调外部 API
        df = raw.rename(columns={...})               # ② 归一为统一列（§3.1）
        df["trade_date"] = df["dt"].str[:10]
        return df                                    # 缺省字段安全兜底：
                                                     # pre_close=0 / trade_status=1 / is_st=0
```

注册：`service.get_source` 字典加一行 + `cli.py` 的 `--source` choices 加一项。
可参考 `wind_source.py` 的延迟导入、字段降级、出口必需列强校验三个防御技巧。

### 8.4 换存储 / 新频率 / 新风控 / 切实盘

- **换存储**（如 Parquet + DuckDB）：实现 `DataRepository` 四方法
  （`save_bars` / `load_bars` / `latest_bar_time` / `list_codes`），
  `service.get_repo` 按环境变量分发。
- **新频率**（15/30/60min）：`baostock_source._FREQ_MAP` 加一行 + `mysql_repo.TABLES`
  加一行 + DDL 追加建表，跑 `init-schema` 幂等建表。
- **新风控**：继承 `RiskRule` 实现 `check`（返回 None 放行，否则返回拒绝原因），
  加入 `RiskChain.rules` 链尾；或调用方通过 `BacktestEngine(risk_rules=[...])` 注入。
- **切实盘**：实现 `Broker` 协议（`submit` + `settle`），如 miniQMT 通道的
  `QmtBroker`；引擎与所有策略代码 0 改动（LSP 的直接兑现）。

### 8.5 扩展后验证清单

```bash
python tests/smoke_test.py            # 离线冒烟（不依赖外部服务，必跑）
python -m quant.app.cli backtest ...  # 真实数据回归
python -m quant.app.cli serve         # Web 端冒烟
```

新增因子 / 策略时在 `tests/smoke_test.py` 加一组合成行情用例（见 §9.2）。

---

## 9. 测试体系

### 9.1 覆盖矩阵（tests/smoke_test.py，离线、无外部依赖）

| 用例 | 验证点 | 不可替代特性 |
|---|---|---|
| `test_daily` | 日线回归：双均线、净值/整手/T+1 逐轮配对断言 | v0.1 兼容基线 |
| `test_minute` | 5 分钟频率：日界解禁每天仅一次、日内 T+1、净值按交易日 | 多频率兼容核心 |
| `test_factor` | 因子计算正确性（手算对照）/ 预热语义（前 N 行 NaN）/ 防未来（factor_history 长度逐 bar 增长）/ 因子策略全链路 | SSOT + 防未来核心 |
| `test_wind_source` | Wind 适配器（mock WindData，不连终端）：代码转换、组装归一、出口校验 | 适配器接缝回归（两个实测踩坑点） |

另有两个需外部依赖的脚本：`baostock_test.py`（端口连通性）、`wind_test.py`
（Wind 终端登录 + 真实拉取）。**改完任何代码都先跑一遍 smoke_test。**

### 9.2 合成数据生成器（smoke_test 内置）

- `make_bars(code, days, start_price)`：合成日线（随机游走 + 季节项
  `0.15·sin(t/25)`），跳过首日（无 pre_close）。
- `make_minute_bars(code, days, bars_per_day, start_price)`：合成分钟线，
  `pre_close` = 前一交易日收盘价。

### 9.3 加新测试的套路

新因子：① 计算正确性（手算预期值对照）② 预热语义（前 N 行 NaN，N+1 起有效）
③ 防未来（`FactorProbe` 全链路，逐 bar 对照因果口径）。
新策略：`final_equity > 0` + 整手断言 + T+1 逐轮配对（参照 `test_daily`）。
新风控：构造触发场景（如 99% 仓位超限），断言 `len(result.rejects) > 0`。

### 9.4 测试失败的常见原因

| 现象 | 可能原因 |
|---|---|
| T+1 断言失败 | `Position.available` 逻辑改坏；或引擎 `on_new_day` 时机错 |
| `final_equity <= 0` | 撮合现金变负；`apply_fill` 二次校验未生效 |
| 净值曲线长度 ≠ 交易日数 | 引擎按 `dt` 而非 `trade_date` 驱动日界 |
| 因子值与手算不一致 | 因子 `compute` 非因果（用了未来数据） |
| `factor_history` 长度不逐 bar 增长 | 引擎 `iloc[:n]` 切片逻辑改坏（防未来破洞） |

调试技巧：`on_bar` 内 print `bar.dt` 与 `ctx.history.iloc[-1]`；
`engine.portfolio.fills[-5:]` 看最近成交；`engine.risk.rejects` 看拒单分类。

---

## 10. Web 端

**架构约束**：`webapp` 只调 `app.service`——业务逻辑只有一份，CLI 改了 Web 自动
同步。启动方式见操作文档 §4。

**前端约定（A股惯例）**：

- 涨红 `#c02020`、跌绿 `#0a7a3d`（与欧美相反）。
- x 轴口径：K 线与买卖点散点 = `bar.dt`；净值曲线 = 交易日（每天一条）；
  基准曲线 = bar 粒度（分钟频率下与 K 线等长）。净值与基准长度不等，
  两个 chart 独立渲染、各自 x 轴自动对齐。
- 指标卡片固定 10 张：总收益率 · 年化收益 · 最大回撤 · 夏普 · 卡玛 · 胜率 ·
  盈亏比 · 平仓次数 · 期末权益 · 总佣金。

**前端扩展**：新策略 → `<select>` 加 option + 后端分支；新图表 → 加容器 +
`echarts.init()` + `render()` 内 `setOption`；新指标 → `_round_list` / `render()`
加数据加工 + 插卡片。`index.html` 是单文件应用，仅 CDN 引入 ECharts 5.5，
无构建工具。

---

## 11. 历史踩坑清单（别再踩）

| # | 坑 | 现状 / 对策 |
|---|---|---|
| 1 | SQL `BETWEEN` 对 None：`end` 未传时查空 | service 层默认补今天 |
| 2 | baostock 分钟时间戳是 17 位毫秒数字串 | 入库前 `norm_dt` 归一 + `repair-dt` 命令幂等修复历史脏行（双保险） |
| 3 | 分钟查询 end 边界字符串比较丢当日数据 | 仓储层自动放宽到 23:59:59 |
| 4 | Web benchmark 与 K 线 x 轴错位 | 基准按 bar 粒度生成（设计决策 #12） |
| 5 | Wind `w.wsd` 返回**大写**字段名（请求 `open` 回 `OPEN`，ErrorCode 仍为 0）+ `trade_status` 返回**中文**（`交易`/`停牌`）而非数字 | 适配器统一 `lower()` 归一 + 中文状态按"是否含停牌"归一（fail-open：未知值视为可交易）+ 出口必需列强校验（防坏数据静默入库） |
| 6 | `__pycache__` 残留导致改动不生效 | 已设 `PYTHONDONTWRITEBYTECODE=1` 全局禁用 |

---

## 12. 路线图（v0.4+ 候选）

**因子面**：截面因子（跨标的排名/行业中性化）与组合选股；IC/IR 因子有效性分析；
因子增量水位计算（当前全量重算，幂等可接受）。

**数据面**：后复权双份存储（前复权历史价随分红漂移，严格长周期回测需要）；
全市场批量抓取调度（交易日历对齐、断点续抓、并发限速）。

**回测面**：`StrategyContext.history` 改预载矩阵（多标的大规模回测）；
组合回测（多标的资金分配）；参数寻优 + 样本外 Walk-forward 验证；
回测结果落库（`strategy_runs` 表 + 数据版本 hash，实现"可复现"）。

**执行面（实盘路线）**：`QmtBroker` 接 miniQMT 通道；实时行情 Feed（回测=历史回放、
实盘=实时订阅，对引擎透明——LSP）；模拟盘先行一周再上小资金实盘；
仓位对账 / 断线重连 / 日报推送。

**工程面**：pytest 框架 + golden 黄金回测（固定数据快照 diff 锁定结果）；
CI + 质量门禁（ruff + mypy strict 仅对 core/）；Web 分钟 K 线按日聚合渲染；
向量化快筛模式（与事件驱动内核共用成本参数）。

**风险清单**：

| 风险 | 对策 |
|---|---|
| 未来函数（回测虚高） | 四道保险 + 冒烟测试逐点断言锁定 |
| 过拟合 | 样本外验证、参数敏感性分析、寻优结果打折评估 |
| 前复权历史漂移 | 切后复权双份存储 |
| 数据源单点 | 三源互备（baostock / akshare / wind），`--source` 切换 |
| 文档腐化 | 大版本提交时同步更新两份文档 |
