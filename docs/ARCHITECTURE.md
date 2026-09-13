# mini-quant · 架构总览

> 本文档是项目架构的"独立入口"。根目录 `ReadMe.md` 是合并版（§1~§8 全量）；
> 本文档聚焦"分层 + 接口契约 + 关键设计模式"三件事，便于新读者快速建立心智模型。

---

## 1. 分层架构

**六层、依赖只能朝下**（高层依赖低层，反之不允许）：

```
┌────────────────────────────────────────────────────────────┐
│ app / webapp   应用层（CLI / 业务装配 / Web）                 │
├────────────────────────────────────────────────────────────┤
│ strategy      策略层    双均线 / 动量 / 用户自定义             │
├────────────────────────────────────────────────────────────┤
│ factor        因子层    注册表 / 内置因子 / FactorEngine      │
├──────────────────────┬─────────────────────────────────────┤
│ backtest      回测层 │  事件驱动引擎 / 模拟撮合 / 风控链 / 绩效│
│                      │                                     │
├──────────────────────┴─────────────────────────────────────┤
│ data          数据层    数据源适配器 / 仓储 / 时间归一        │
├────────────────────────────────────────────────────────────┤
│ core          ★领域层   models / abstractions / portfolio  │
│               零 import 第三方                             │
└────────────────────────────────────────────────────────────┘
```

**依赖方向图（含跨层调用）**：

```
core ◀── data        （实现 MarketDataSource / DataRepository 协议）
core ◀── backtest    （使用 models，实现 Broker / RiskRule 协议）
core ◀── factor      （使用 abstractions.Factor）
core ◀── strategy    （继承 Strategy，使用 StrategyContext）

backtest/strategy/factor ◀── app    （装配、回测执行）
backtest/strategy/factor ◀── webapp （仅通过 app.service）
data ◀── app         （service 构造 repo / source）
```

### 三条铁律（比 SOLID 更具体的约束）

1. **core 零第三方依赖**——它定义 `Bar / Order / Fill / Position / Strategy / Broker` 等契约；
   其余层实现这些契约。依赖箭头永远指向抽象（依赖倒置 DIP）。
2. **webapp 只调 app.service**，不直接触碰 data / backtest / factor。
3. **策略只见 `StrategyContext`**（接口隔离 ISP + 防未来函数）。

---

## 2. 六层职责速查

| 层 | 目录 | 单一职责 | 不应做什么 |
|---|---|---|---|
| core | `quant/core/` | 定义值对象 + 接口契约 + 记账本 | import 任何第三方库 |
| data | `quant/data/` | 外部数据源适配 + 本地仓储读写 | 写业务逻辑 / 触发订单 |
| factor | `quant/factor/` | 行情 → 因子（纯计算） | 碰存储 / 知道策略存在 |
| strategy | `quant/strategy/` | 产生交易信号 | 访问数据库 / 直接撮合 |
| backtest | `quant/backtest/` | 事件驱动主循环 + A 股规则模拟 | 知道 CLI/Web 存在 |
| app | `quant/app/` | 业务编排（CLI + Web 共用） | 写撮合逻辑 / 写 SQL |
| webapp | `quant/webapp/` | HTTP 端点 + 前端渲染 | 触碰引擎 / 仓储细节 |

---

## 3. 接口契约矩阵（"宪法"一览）

| 接口 | 类型 | 职责 | 当前实现 | 可替换方向 |
|---|---|---|---|---|
| `MarketDataSource` | Protocol | 外部行情源 | `BaostockSource`、`AkshareSource` | Tushare / Wind / …… |
| `DataRepository` | Protocol | 本地行情仓储 | `MySQLBarRepo` | Parquet + DuckDB |
| `Strategy` | ABC | 策略钩子 | `DoubleMAStrategy`、`FactorMomentumStrategy` | 任意子类 |
| `Factor` | ABC | 因子契约 | 5 个内置因子 | 任意子类 |
| `FactorAccessor` | class | 因子只读视图（防未来）| 引擎内部 | — |
| `Broker` | Protocol | 撮合通道 | `SimBroker`（回测）| `QmtBroker`（实盘）|
| `RiskRule` | ABC | 风控节点 | 4 条内置规则 | 新增子类即可 |
| `OrderSink` | Protocol | 订单入口 | 引擎实现（自身当 sink）| — |
| `PortfolioView` | Protocol | 账户只读视图 | 引擎内部 `_PortfolioViewImpl` | — |

**Protocol 与 ABC 的选择**：

- **Protocol**（鸭子类型）——实现类无需显式继承；适用于"数据源 / 仓储 / 撮合"
  这类**可能被第三方库包装**的外部依赖（轻侵入）。
- **ABC**（抽象基类）——子类须显式继承；适用于"策略 / 因子 / 风控"这类
  **需要明确"我是这个体系一员"**的扩展点（强约束）。

---

## 4. SOLID 落地对照

| 原则 | 落地方式 | 体现位置 |
|---|---|---|
| **S** 单一职责 | core / data / backtest / strategy / factor / app / webapp 各司其职，每模块只有一个变化原因 | 目录结构本身 |
| **O** 开闭 | 新数据源 / 新策略 / 新因子 / 新风控 = 新增一个实现类 + 注册一行，改 0 行旧代码 | 见 `docs/EXTENSION_GUIDE.md` |
| **L** 里氏替换 | `SimBroker` 与未来 `QmtBroker` 实现同一 `Broker` 协议且行为契约一致 → 回测代码 0 修改跑实盘 | `backtest/engine.py` 注入 `SimBroker` 处 |
| **I** 接口隔离 | 策略只见 `StrategyContext` 窄接口（history 只到当前 bar），不见引擎 / 数据库 | `core/abstractions.py:StrategyContext` |
| **D** 依赖倒置 | 引擎 / 策略依赖 core 抽象；MySQL / Baostock / AKShare 都是可替换插件 | `core/abstractions.py` 定义接口，其它层实现 |

---

## 5. 关键设计模式（项目用到的）

### 5.1 SSOT（Single Source of Truth）

**场景**：因子计算。

- 回测走"内存即时计算"（`FactorEngine.compute`，与行情同帧同口径，是权威值）；
- 落库 `dwd_factor_value_i` 长表仅是"物化缓存"，供选股 / 因子分析等非回测场景读取。

**反例**：库里旧口径 vs 回测新口径不一致 → 因子表现前后漂移无法解释。

### 5.2 防未来函数（Look-ahead Bias）

**4 道保险**（顺序即生效次序）：

| # | 保险 | 实现 |
|---|---|---|
| 1 | `ctx.history` 只到当前 bar（含）及以前 | 引擎 numpy 数组切片引用（零拷贝） |
| 2 | `ctx.factor()/factor_history()` 只看到 ≤ 当前 bar | 引擎 `_attach_factors` + 每 bar `iloc[:n]` 切片 |
| 3 | 订单次一 bar 开盘价成交 | `SimBroker.settle`（信号当根不可能成交） |
| 4 | 成交价 ± 滑点（千 2）模拟真实成交偏差 | `SimBroker._try_fill` |

### 5.3 T+1 实现

- `Position.available` 当日买入不增加，只 `quantity` 增加；
- 引擎每个交易日开始调 `Portfolio.on_new_day()` → `available = quantity` 解禁；
- `AvailabilityRule` 校验卖出委托 ≤ available（含同日已委托卖出扣减）。

### 5.4 适配器模式（Adapter）

数据源各自脏格式（中文列名 / 17 位毫秒时间戳 / 复权标记差异） → 适配器吸收，
对上输出**统一列 DataFrame**。

```python
# 仓储 / 引擎 / 策略 只认统一列
["code", "dt", "trade_date", "date", "open", "high", "low", "close",
 "pre_close", "volume", "amount", "trade_status", "is_st"]
```

### 5.5 责任链模式（Chain of Responsibility）

风控 4 条规则串行执行，任一拒绝即拦截：

```
TradabilityRule  →  LotSizeRule  →  CashSufficiencyRule  →  AvailabilityRule
   (停牌 ST)         (100 整手)       (现金足额)              (T+1 可卖)
```

顺序即优先级——先排除"不能做的"，再校验"能不能做"。

### 5.6 工厂 + 注册表

- 数据源：`service.get_source(name)` 字典查找（`'baostock'` / `'akshare'`）；
- 因子：`factor.register(Factor())` 全局字典，按 `name` 唯一索引；
- 策略：CLI 参数 `--strategy {double_ma, factor_momentum}` 显式 import（简洁优先）。

---

## 6. 目录结构（按依赖层组织）

```
mini-quant/
├── config/                  # 配置（settings.yaml 已 gitignore 防密码入库）
├── docs/                    # ★设计文档（本目录）
│   ├── ARCHITECTURE.md      # 本文件
│   ├── EXTENSION_GUIDE.md   # 如何加新策略/因子/数据源/存储/实盘
│   ├── API.md               # CLI + Web API + service 函数手册
│   └── DESIGN_DECISIONS.md  # 关键设计决策 ADR
├── quant/
│   ├── core/                # 领域层（★宪法，零第三方依赖）
│   ├── data/                # 数据层（实现 core 抽象）
│   ├── factor/              # 因子层（v0.3）
│   ├── strategy/            # 策略层
│   ├── backtest/            # 回测层
│   ├── app/                 # 应用层（CLI + 业务装配）
│   └── webapp/              # Web 层（FastAPI + ECharts）
├── tests/                   # 离线冒烟测试（不依赖外部服务）
├── webapp/README.md         # Web 端文档
├── tests/README.md          # 测试文档
├── CHANGELOG.md             # 版本演进日志
├── ReadMe.md                # 合并版主文档（§1~§8 全量）
├── requirements.txt
└── .gitignore
```

---

## 7. 相关文档导航

| 你想知道…… | 看…… |
|---|---|
| 怎么用（环境、五步流程） | 根 `ReadMe.md` §1 |
| 每份文件干什么 | 根 `ReadMe.md` §3 |
| 技术栈选型理由 | 根 `ReadMe.md` §4.3 |
| 关键设计模式详解 | 本文档 §5 |
| 每个抽象的核心设计思路 | 根 `ReadMe.md` §5 |
| 完整数据流转走读 | 根 `ReadMe.md` §6 |
| 如何加新策略 / 因子 / 数据源 | `docs/EXTENSION_GUIDE.md` |
| CLI / Web API 详细参数 | `docs/API.md` |
| 为什么这么设计（决策记录）| `docs/DESIGN_DECISIONS.md` |
| v0.1 → v0.3 演进历史 | `CHANGELOG.md` |
| Web 端文档 | `webapp/README.md` |
| 测试文档 | `tests/README.md` |