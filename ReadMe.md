# mini-quant 操作文档

> 个人量化回测平台（A 股）：数据接入 → 因子加工 → 策略回测 → 绩效分析 → Web 可视化。>   
> 合规提示：仅限个人研究自用，不对外提供服务、不构成投资建议。
>
> **文档导航**（全项目仅两份文档）：
>
> | 文档                    | 内容                             | 读者       |
> | --------------------- | ------------------------------ | -------- |
> | `ReadMe.md`（本文）       | 环境准备、启动流程、命令参考、数据源选择、日常运维、常见故障 | 使用者 / 运维 |
> | `docs/DEVELOPMENT.md` | 架构说明、接口契约、设计决策、扩展指南、测试体系       | 开发者      |

---

## 目录

1. [技术栈与版本](#1-技术栈与版本)
2. [版本演进](#2-版本演进)
3. [环境准备与配置](#3-环境准备与配置)
4. [标准使用流程](#4-标准使用流程)
5. [CLI 命令参考](#5-cli-命令参考)
6. [数据源选择与网络问题](#6-数据源选择与网络问题)
7. [日常运维](#7-日常运维)
8. [常见问题排错](#8-常见问题排错)
9. [目录结构一览](#9-目录结构一览)

---

## 1. 技术栈与版本

| 维度   | 选型                          | 版本                           | 说明                                         |
| ---- | --------------------------- | ---------------------------- | ------------------------------------------ |
| 语言   | Python                      | 3.12                         | 量化生态无可替代                                   |
| 数据处理 | pandas                      | ≥ 2.0                        | DataFrame 贯穿全链路                            |
| 数据源  | Baostock / AKShare / Wind   | ≥ 0.8.8 / ≥ 1.14 / 随终端       | 三源互备，`--source` 一键切换，输出列完全同构               |
| 存储   | MySQL 8（PyMySQL）            | ≥ 1.1                        | 本机或局域网均可；仓储接口隔离，换 Parquet+DuckDB 只动 data 层 |
| 回测   | 自研事件驱动内核                    | —                            | 每一行都懂、可控；比 vn.py 轻、比 backtrader 透明         |
| Web  | FastAPI + uvicorn + ECharts | ≥ 0.110 / ≥ 0.29 / 5.5 (CDN) | 无构建工具，原生 HTML/JS                           |
| 测试   | 离线冒烟测试                      | —                            | 合成行情，不依赖外部服务                               |

**依赖安装**：

```bash
pip install -r requirements.txt
```

> ⚠️ **WindPy 例外**：随 Wind 金融终端分发、**不能 pip 安装**。只在本机装了>   
> Wind 终端的环境里可用（`import WindPy` 能找到即已就绪）；未装终端不影响>   
> baostock / akshare 两个源的使用。



---

## 2. 版本演进

| 版本                | 主线能力        | 关键里程碑                                  |
| ----------------- | ----------- | -------------------------------------- |
| v0.1              | 日线回测核心      | 六层架构 + 双均线 + CLI                       |
| v0.2              | 多频率 + A 股规则 | Bar 双时间字段 + freq 分表 + 5 分钟回测 + Web 控制台 |
| v0.3              | 因子库 + 双数据源  | 因子 SSOT 内存即时算 + 声明式依赖 + 长表存储           |
| v0.4 (Unreleased) | Wind 数据源    | 适配器三源互备 + 网络自检工具                       |



> 详细变更历史与提交记录：`git log`。

---

## 3. 环境准备与配置

### 3.1 环境要求

- Python 3.12（建议独立 venv / conda 环境）
- MySQL 8（本机或局域网）
- 可选：Wind 金融终端（仅 `--source wind` 需要，须先登录终端）

### 3.2 初始化

```bash
cd mini-quant
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### 3.3 配置文件

复制模板 `config/settings.example.yaml` → `config/settings.yaml`，填入 MySQL  
连接与回测成本参数：

```yaml
db:
  host: 127.0.0.1
  port: 3306
  user: root
  password: "your-password"
  name: ods

data_source: baostock          # 默认数据源：baostock / akshare / wind

backtest:
  init_cash: 1000000           # 初始资金
  commission_rate: 0.00025     # 佣金 万2.5
  slippage: 0.002              # 滑点 千2
```

> ⚠️ `settings.yaml` 含密码，已被 `.gitignore` 排除，**不要提交入库**；>   
> 新环境一律从模板复制。

**环境变量覆盖**（优先级高于配置文件，适合临时覆盖）：

| 环境变量                                                | 默认                            |
| --------------------------------------------------- | ----------------------------- |
| `QUANT_DB_HOST` / `QUANT_DB_PORT` / `QUANT_DB_USER` | `127.0.0.1` / `3306` / `root` |
| `QUANT_DB_PASSWORD` / `QUANT_DB_NAME`               | `""` / `ods`                  |

### 3.4 证券代码格式

本系统口径（Baostock 风格，带交易所前缀）：沪市 `sh.600000`、深市 `sz.000001`。  
各数据源适配器自动转换，CLI / Web 层只认这个口径。

---

## 4. 标准使用流程

五步：**建库 → 抓数 → 回测 → （可选）因子落库 → Web 展示**。

```bash
# ① 初始化库表（幂等：日线表 + 分钟表 + 因子表，重复执行无副作用）
python -m quant.app.cli init-schema

# ② 抓取日线入库（默认前复权；增量——自动从库里最新日期的次日开始续抓）
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01

# ②b 换数据源抓日线（免费聚合多源 / 需本机登录 Wind 终端）
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 --source akshare
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 --source wind

# ②c 抓取 5 分钟线（建议同时保留日线：分钟表的涨跌停基准依赖日线昨收关联）
python -m quant.app.cli ingest --code sh.600000 --start 2025-01-01 --freq 5min

# ③ 命令行回测（日线/分钟、双均线/因子策略，同一套代码）
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --end 2025-12-31 --fast 5 --slow 20
python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 --freq 5min --fast 8 --slow 48
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --strategy factor_momentum --window 60

# ④ 因子计算落库（物化缓存，供选股/因子分析；回测不走库——内存即时算，口径权威）
python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01
python -m quant.app.cli compute-factors --code sh.600000 --start 2025-01-01 --freq 5min --factors momentum_20,volatility_20

# ⑤ Web 控制台（浏览器打开 http://127.0.0.1:8000）
python -m quant.app.cli serve --port 8000
```

**使用注意**：

- 回测 `--end` 不传默认到今天；数据必须先 `ingest`（否则报"无数据"）；
- 分钟级回测交易频率高，最低佣金 5 元/笔对小资金侵蚀显著，关注 `total_commission` 指标；
- 前复权（默认）历史价会随后续分红除权漂移，严格长周期回测建议关注后复权存储（路线图项）。

---

## 5. CLI 命令参考

入口：`python -m quant.app.cli <command>`

### 5.1 子命令速查

| 子命令               | 作用                                  | 是否需联网 |
| ----------------- | ----------------------------------- | ----- |
| `init-schema`     | 幂等建库建表（行情日表/分钟表/因子表）                | 否     |
| `ingest`          | 增量抓取行情入库（baostock / akshare / wind） | 是     |
| `compute-factors` | 计算因子并落库（物化缓存，upsert 幂等）             | 否     |
| `backtest`        | 运行回测并打印绩效                           | 否     |
| `repair-dt`       | 修复分钟表历史脏时间戳（幂等）                     | 否     |
| `serve`           | 启动 Web 控制台                          | 否     |

### 5.2 `ingest` 参数

```bash
python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01 [options]
```

| 参数         | 必填 | 默认         | 说明                                  |
| ---------- | -- | ---------- | ----------------------------------- |
| `--code`   | ✅  | —          | 证券代码（`sh.600000` / `sz.000001`）     |
| `--start`  | ✅  | —          | 起始日期 `YYYY-MM-DD`                   |
| `--end`    | —  | 今天         | 结束日期                                |
| `--adjust` | —  | `2`        | 复权：`1` 后复权 / `2` 前复权 / `3` 不复权      |
| `--freq`   | —  | `1d`       | 频率：`1d` 日线 / `5min` 5 分钟线           |
| `--source` | —  | `baostock` | 数据源：`baostock` / `akshare` / `wind` |

自动增量：从库中已有最新 bar 的次日开始续抓（库为空时用 `--start`）。

### 5.3 `backtest` 参数

```bash
python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 [options]
```

| 参数                   | 必填 | 默认          | 说明                              |
| -------------------- | -- | ----------- | ------------------------------- |
| `--code` / `--start` | ✅  | —           | 证券代码 / 起始日期                     |
| `--end`              | —  | 今天          | 结束日期                            |
| `--freq`             | —  | `1d`        | `1d` / `5min`                   |
| `--strategy`         | —  | `double_ma` | `double_ma` / `factor_momentum` |
| `--fast` / `--slow`  | —  | `5` / `20`  | 双均线快/慢线周期（按 bar 计）              |
| `--window`           | —  | `20`        | 动量窗口（factor_momentum）           |
| `--json`             | —  | `False`     | 输出完整 JSON 而非人类可读格式              |

### 5.4 `compute-factors` 参数

| 参数                   | 必填 | 默认   | 说明                                                                         |
| -------------------- | -- | ---- | -------------------------------------------------------------------------- |
| `--code` / `--start` | ✅  | —    | 证券代码 / 起始日期                                                                |
| `--end`              | —  | 今天   | 结束日期                                                                       |
| `--freq`             | —  | `1d` | 频率                                                                         |
| `--factors`          | —  | 全部内置 | 逗号分隔因子名（`momentum_20,momentum_60,volatility_20,bias_20,volume_ratio_5_20`） |

### 5.5 `repair-dt` / `serve`

```bash
python -m quant.app.cli repair-dt          # 分钟表 17 位数字串时间戳 → 标准格式，幂等
python -m quant.app.cli serve [--host 127.0.0.1] [--port 8000]
```

`serve` 启动 FastAPI 服务，浏览器打开 `http://127.0.0.1:8000`：  
K 线 + 均线 + 买卖点 + 净值对比 + 指标卡片 + 成交明细，支持日线/5 分钟、  
双均线/动量因子策略切换。

---

## 6. 数据源选择与网络问题

### 6.1 三源对比

| 数据源            | 费用        | 前置条件                  | 特点                                              |
| -------------- | --------- | --------------------- | ----------------------------------------------- |
| `baostock`（默认） | 免费        | 无                     | 稳定；走**裸 TCP 直连** `www.baostock.com:10030`，不支持代理 |
| `akshare`      | 免费        | 无                     | 聚合多源（东财等 HTTP 接口）；分钟数据仅保留近期                     |
| `wind`         | 需 Wind 账号 | 本机安装并**登录 Wind 金融终端** | 数据质量与覆盖度最优；走本地终端，完全不碰公网                         |

三者输出列完全同构，上层零感知，随时用 `--source` 切换。

### 6.2 网络自检工具

数据源连不上时，先跑五层自检（DNS → TCP → HTTPS → 代理 → 端到端），  
自带对照目标与多次采样，跑完直接给结论：

```bash
python tools/diagnose_network.py            # 全量诊断
python tools/diagnose_network.py --quick    # 只跑 TCP 层，几秒出结果
```

脚本还会打印环境指纹（出口 IP / 活动网卡 / 管控软件 / 防火墙），换网络环境  
排查时对比两份输出即可定位是"机器"还是"网络"的问题。

### 6.3 常见网络故障

| 现象                                                | 原因                                             | 处理                                                          |
| ------------------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------- |
| baostock 报 `10002007 网络接收错误` / `WinError 10057`   | 10030 端口从当前网络不可达（baostock 客户端会吞掉连接异常，报错信息是假线索） | 换网络（如公司专线）试试；或开代理 TUN 模式接管裸 TCP；或直接改用 `--source wind`（本地终端） |
| akshare 报 `ProxyError`                            | 环境变量 `HTTP_PROXY/HTTPS_PROXY` 指向失效代理           | 清掉代理变量，或改为可用代理地址                                            |
| akshare 拉东财接口超时/RST                               | 东财行情接口在当前网络被 SNI 层拦截                           | 走代理，或改用 `--source wind`                                     |
| `ModuleNotFoundError: No module named 'baostock'` | 当前 Python 环境没装                                 | `pip install -r requirements.txt`；确认没有用错解释器                 |
| `import WindPy` 失败                                | 本机未装 Wind 终端，或当前环境不在 Wind 安装路径                 | 用装了 WindPy 的环境；或改用 baostock / akshare                       |

---

## 7. 日常运维

### 7.1 测试（改完任何代码必跑）

```bash
python tests/smoke_test.py     # 离线冒烟：合成行情全链路，不依赖 MySQL / 网络
python tools/check_style.py    # 源码注释风格自检（markdown 语法 / emoji）
```

预期输出末尾：四组用例通过 + `全部断言通过 ✓`。  
测试体系与加新测试的说明见开发文档 §9；注释风格约定见开发文档 §8.6。

### 7.2 数据维护

| 任务       | 命令                             |
| -------- | ------------------------------ |
| 新环境初始化库表 | `init-schema`（幂等，重复执行无副作用）     |
| 日常增量更新   | `ingest`（自动从库里最新日期续抓）          |
| 重抓修数     | 直接重复 `ingest`（upsert 幂等，覆盖旧值）  |
| 修复历史脏时间戳 | `repair-dt`（幂等，只影响 17 位数字串的脏行） |

### 7.3 数据库表

| 表                              | 内容                  | 唯一键                                     |
| ------------------------------ | ------------------- | --------------------------------------- |
| `ods.ods_d_stock_quotation_i`  | A 股日线（含估值/换手率/交易状态） | `code + date + adjust_flag`             |
| `ods.ods_mi_stock_quotation_i` | A 股 5 分钟线（行情+量价）    | `code + date_time + adjust_flag`        |
| `ods.dwd_factor_value_i`       | 因子长表（加新因子零 DDL）     | `code + date_time + freq + factor_name` |

### 7.4 性能提示

- 分钟级回测一年约 12000 根 bar；Web 端已配 dataZoom 可缩放查看，    
  大区间前端渲染慢属正常（按日聚合渲染在路线图）。
- 批量抓取多标的时，Wind 源内置连接复用（不重复登录）。

---

## 8. 常见问题排错

| 现象                      | 原因                         | 处理                                                    |
| ----------------------- | -------------------------- | ----------------------------------------------------- |
| 回测报 `无数据：sh.600000 ...` | 库内无该标的 / 区间数据              | 先 `ingest`；确认 `--start/--end` 落在已抓取区间内                |
| MySQL 连接失败              | MySQL 未启动 / 配置错            | 检查 `settings.yaml` 或 `QUANT_DB_*` 环境变量；确认 MySQL 服务已启动 |
| 回测 `--end` 当日数据缺失       | 分钟查询边界                     | 已由仓储层自动放宽到 23:59:59；若仍缺，确认当日已 `ingest`                |
| 净值/指标出现 NaN、`inf`       | 净值曲线空 / 交易数为 0 / 极端值 std=0 | 检查策略在给定区间是否产生交易；预热期不足时延长 `--start` 前置历史               |
| 改了代码不生效                 | `__pycache__` 残留           | 已全局禁用（`PYTHONDONTWRITEBYTECODE=1`）；仍异常时手动清缓存目录        |
| Web 页面指标卡片空白            | `/api/backtest` 404（无数据）   | 看 `失败：...` 状态栏提示；先 ingest 再回测                         |

更多开发侧问题（T+1 断言失败、因子值不一致等）见开发文档 §9.4。

---

## 9. 目录结构一览

```
mini-quant/
├── config/                  # 配置（settings.yaml 已 gitignore 防密码入库）
├── docs/
│   └── DEVELOPMENT.md       # 开发文档（架构 / 接口 / 扩展 / 测试）
├── quant/                   # 主包，六层（core/data/factor/strategy/backtest/app/webapp）
├── tests/                   # 离线冒烟测试 + 数据源连通性脚本
├── tools/
│   ├── diagnose_network.py  # 数据源网络自检
│   └── check_style.py       # 源码注释风格自检
├── ReadMe.md                # 本文档（操作文档）
└── requirements.txt
```

各层职责、每个模块的关键设计、如何加新策略/因子/数据源——全部见 `docs/DEVELOPMENT.md`。
