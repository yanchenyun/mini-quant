# 变更日志（CHANGELOG）

> 本项目的所有重要变更都记录在此文件。
> 版本格式遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)（MAJOR.MINOR.PATCH）。

---

## [Unreleased]

### 计划中
- Web 分钟 K 线聚合渲染（前端按日聚合或增量加载优化）
- pytest 单测框架接入（撮合与风控 100% 覆盖）
- CI + 代码质量门禁（ruff + mypy strict 仅对 `core/`）
- 组合回测（多标的资金分配）
- 参数寻优（网格/贝叶斯）+ 样本外 Walk-forward 验证
- 回测结果落库（`strategy_runs` 表 + 数据版本 hash，实现"可复现"）

---

## [0.3.0] - 2026-09

> v0.3 主题：因子库。回测从此有"标准化特征工程"能力。

### Added（新增）

- **因子层 `quant/factor/`**（v0.3 全新）：
  - `factor/base.py` 因子注册表（`register` / `get` / `available`）
  - `factor/builtin.py` 内置 5 个因子：`momentum_20` / `momentum_60` /
    `volatility_20` / `bias_20` / `volume_ratio_5_20`
  - `factor/engine.py` `FactorEngine.compute(bars, names)` 行情 → 因子宽表
- **`Strategy.required_factors` 声明式因子依赖**：策略只写依赖名，服务层自动
  解析 + 多加载预热历史 + 计算并注入引擎
- **因子长表 `dwd_factor_value_i`**：`(code, date_time, freq, factor_name, value)`
  + 唯一键 upsert —— 加新因子零 DDL
- **因子策略示例** `quant/strategy/factor_momentum.py`：
  `FactorMomentumStrategy(window, threshold, buy_ratio)`
- **CLI 子命令 `compute-factors`**：计算因子并落库（物化缓存）
- **AKShare 数据源适配器** `quant/data/akshare_source.py`：免费、无需注册、
  聚合多源；中文列名归一 + 分钟分段请求拼接 + 3 次重试
- **`FactorAccessor`**（引擎内）：每 bar 用 `iloc[:n]` 切片构造只读视图，
  策略结构上不可能读未来因子行
- **Web 端策略切换**：新增 `strategy=factor_momentum` + `window` 参数

### Changed（变更）

- **数据源选择从单源改为可切换**：`service.get_source(name)` 工厂方法，
  `settings.yaml: data_source` 配置默认源
- **CLI `ingest` 新增 `--source {baostock, akshare}` 参数**
- **`Settings.data_source` 字段**：默认 `baostock`
- **`requirements.txt`**：新增 `akshare>=1.14`

### Fixed（修复）

- **历史踩坑 #2**：分钟表脏时间戳 → `repair-dt` 命令幂等修复 + 入库前
  `norm_dt` 归一（双保险）

### 文件清单（v0.3）

| 新增 | 位置 |
|---|---|
| 因子层 | `quant/factor/base.py` · `builtin.py` · `engine.py` |
| 因子策略 | `quant/strategy/factor_momentum.py` |
| AKShare 适配器 | `quant/data/akshare_source.py` |
| 因子长表 DDL | `quant/data/mysql_repo.py` `DDL` 字符串 |
| 文档 | `docs/ARCHITECTURE.md` · `docs/EXTENSION_GUIDE.md` · `docs/API.md` · `docs/DESIGN_DECISIONS.md` |

| 变更 | 位置 |
|---|---|
| 数据源工厂 | `quant/app/service.py:get_source` |
| CLI ingest `--source` | `quant/app/cli.py` |
| Web `/api/backtest` 策略切换 | `quant/webapp/server.py` |

### 迁移注意

- **首次使用**：执行 `python -m quant.app.cli init-schema` 新建因子长表
- **配置**：复制 `config/settings.example.yaml` → `config/settings.yaml`，可选填
  `data_source: akshare`
- **代码兼容**：v0.1 / v0.2 的旧调用全部保留别名
  （`MySQLDailyRepo = MySQLBarRepo`、`save_daily` / `load_daily` / `latest_date`）

---

## [0.2.0] - 2025-08

> v0.2 主题：多频率。5 分钟线落地，"频率无关"策略正式生效。

### Added（新增）

- **多频率支持**：回测支持日线 + 5/15/30/60 分钟线，同一套策略代码天然适用
- **Bar 双时间字段** `dt` + `trade_date`（多频率兼容的命门）
- **freq 分表**：日表 `ods_d_stock_quotation_i` + 分钟表 `ods_mi_stock_quotation_i`
- **分钟表 LEFT JOIN 日线表**：回填 `pre_close` / `trade_status` / `is_st`
- **金额时间戳归一工具 `timeutil.norm_dt`**：识别 5 种脏形态统一归一
- **A股规则全内置**：佣金万 2.5 + 最低 5 元 + 印花税卖出万 5 + 滑点千 2 + 涨跌停一字板拒单 + 停牌 / ST 拒单 + T+1
- **Web 控制台** `quant/webapp/`：FastAPI + ECharts，K 线 + 买卖点 + 净值对比 + 指标卡片
- **冒烟测试 `tests/smoke_test.py`**：合成行情验证全链路（不依赖 MySQL / Baostock）

### Changed（变更）

- **引擎主循环**：按 `trade_date` 分组驱动日界，一天内按 `dt` 逐 bar 推进
- **净值快照**：每个交易日仅一条（年化基准恒为 252）
- **防未来函数保险扩到 4 道**：history 切片 + 因子切片 + 次 bar 撮合 + 滑点

### Fixed（修复）

- **历史踩坑 #3**：分钟查询 `end` 边界字符串比较丢当日数据 → 仓储层自动放宽到 23:59:59
- **历史踩坑 #4**：Web benchmark 错位 → 按 bar 粒度生成

### 文件清单（v0.2）

| 新增 | 位置 |
|---|---|
| 多频率 + 防未来保险 | `quant/backtest/engine.py` |
| A 股规则 | `quant/backtest/sim_broker.py` · `risk.py` |
| 数据源 freq 支持 | `quant/data/baostock_source.py` `_FREQ_MAP` |
| freq 分表 + LEFT JOIN | `quant/data/mysql_repo.py` |
| 时间归一 | `quant/data/timeutil.py` |
| Web | `quant/webapp/server.py` + `index.html` |
| 冒烟测试 | `tests/smoke_test.py` |

### 迁移注意

- **首次使用**：执行 `python -m quant.app.cli init-schema` 新建分钟表
- **代码兼容**：v0.1 的 `save_daily` / `load_daily` / `latest_date` 保留为
  `save_bars(freq="1d")` 的别名
- **数据兼容**：v0.1 入库的日表数据无需迁移

---

## [0.1.0] - 2025-03

> v0.1 主题：日线回测核心跑通。麻雀虽小五脏俱全。

### Added（新增）

- **六层架构 + core 零依赖**：DIP / ISP / OCP 全部落地
- **领域层** `quant/core/`：models（值对象）+ abstractions（接口契约）+ portfolio（记账本）
- **数据层** `quant/data/`：Baostock 适配器 + MySQL 仓储 + 时间归一
- **策略层** `quant/strategy/double_ma.py`：双均线策略
- **回测层** `quant/backtest/`：引擎 + 模拟撮合 + 风控链 + 绩效
- **应用层** `quant/app/`：CLI（init-schema / ingest / backtest）+ 业务装配
- **基础 A 股规则**：T+1（Position.available）+ 100 股整手 + 涨跌停近似

### 设计铁律（从一开始就贯彻）

1. core 零第三方依赖
2. webapp 只调 app.service
3. 策略只见 StrategyContext

### 文件清单（v0.1）

`quant/core/{models,abstractions,portfolio}.py` ·
`quant/data/{baostock_source,mysql_repo,timeutil}.py` ·
`quant/strategy/double_ma.py` ·
`quant/backtest/{engine,sim_broker,risk,metrics}.py` ·
`quant/app/{service,cli}.py` · `config/settings.example.yaml` · `requirements.txt` · `ReadMe.md`

---

## 版本对照速查

| 版本 | 主线能力 | 关键里程碑 |
|---|---|---|
| v0.3.0 | 因子库 + AKShare 双源 | 因子 SSOT 内存即时算 + 落库仅物化缓存 |
| v0.2.0 | 多频率 + A 股规则 | Bar 双时间字段 + freq 分表 + 5 分钟回测 |
| v0.1.0 | 日线回测核心 | 六层架构 + 双均线 + CLI |

---

## 引用

- 版本格式：[Semantic Versioning 2.0.0](https://semver.org/lang/zh-CN/)
- 变更日志规范：[Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)