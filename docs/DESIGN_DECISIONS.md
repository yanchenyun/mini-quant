# mini-quant · 设计决策记录（ADR）

> ADR（Architecture Decision Record）记录"为什么这么设计"。每一节包含：
> **状态 / 背景 / 决策 / 取舍 / 影响**。
>
> 新增重大设计时复制模板追加即可，状态由 `已采纳` → `已弃用` 时保留原文 + 标废弃。

---

## ADR-001 · Bar 双时间字段（dt + trade_date）

**状态**：已采纳（v0.2 引入）

**背景**

分钟级回测要求一根 bar 既要按"时刻"排序、用于指标计算（`MA / rolling / shift`），
又要按"归属交易日"驱动日界事件（T+1 解禁、涨跌停基准、绩效周期）。
日线时代"一根 bar = 一个交易日"是隐含前提；分钟级若不显式解耦，
10:00 那根 bar 会被误判为"新的一天"，触发错误的 T+1 解禁和净值快照。

**决策**

`Bar` 值对象同时持两个字段：

- `dt`（bar 时刻）—— 日线 `YYYY-MM-DD`；分钟 `YYYY-MM-DD HH:MM:SS`。
  用于排序、rolling 指标、引擎主循环推进。
- `trade_date`（归属交易日）—— 恒为 `YYYY-MM-DD`。
  用于 T+1 解禁、涨跌停基准、绩效快照周期、止损止盈的"日"语义。

另保留 `Bar.date` 为 v0.1 向后兼容别名（== `trade_date`），**新代码一律用 `dt`/`trade_date`**。

**取舍**

- 备选 A：只保留一个时间字段，用"是否是 09:30"判定日界。代价：日界判定规则随频率变化，
  多频率兼容性差，5 分钟线 / 15 分钟线 / 60 分钟线要分别写判定逻辑。
- 备选 B：分钟 bar 直接展开成 daily summary。代价：丢失日内信息，分钟策略写不出来。

**影响**

- 仓储 / 数据源 / 引擎 / 策略全部按 `dt` 排序、按 `trade_date` 分组驱动日界；
- `Portfolio.snapshot(trade_date)` 只在每个交易日内最后一根 bar 后调用一次
  → 净值曲线每天一条 → 年化基准恒为 252，不被 bar 数虚增。

---

## ADR-002 · Position 双字段 T+1 实现

**状态**：已采纳（v0.1 引入）

**背景**

A 股 T+1 规则：当日买入的仓位当日不可卖。朴素实现"卖出时检查 `quantity`"
会让当日买入当日卖出（违反规则）；在回测引擎的逐 bar 推进中，
Position 是可变状态对象，须在 `apply_fill` 时正确反映这一约束。

**决策**

`Position` 持两个数量字段：

```python
@dataclass
class Position:
    quantity: int   # 总持仓（含当日冻结）
    available: int  # 当日可卖数量（≤ quantity）
```

- 买入时：只 `quantity += qty`，**不动** `available`（当日冻结）；
- 卖出时：`quantity -= qty` 且 `available -= qty`；
- 次日 `Position.on_new_day()`：`available = quantity`（T+1 解禁）。

引擎每个交易日开始调 `Portfolio.on_new_day()` → 遍历所有 `Position.on_new_day()` 解禁。

**取舍**

- 备选 A：用一个 `quantity` + 每日重算。代价：当日冻结语义依赖时间判断，
  易出错；新增同日多次买入时边界 case 多。
- 备选 B：用冻结日期集合。代价：每次查询要 O(n) 遍历，性能差。

**影响**

- 引擎主循环：每个 `trade_date` 组首部先调 `on_new_day`；
- 风控 `AvailabilityRule` 同时扣减当日已委托卖出量（`_pending_sell` 追踪），防止
  同 bar 多笔卖出超限。

---

## ADR-003 · avg_cost 含买入佣金摊薄

**状态**：已采纳（v0.2 引入）

**背景**

`Position.avg_cost` 是卖出利润公式 `(sell_price - avg_cost) × qty - sell_commission` 的基准。
若 `avg_cost` 不含买入佣金摊薄，则卖出利润会被低估（买入侧的佣金"消失"了）。

**决策**

`Position.on_buy(qty, price_incl_cost)`：

```python
# price_incl_cost = filled_price + commission / qty（含佣金的买入单价）
self.avg_cost = (self.avg_cost * self.quantity + price_incl_cost * qty) / (self.quantity + qty)
self.quantity += qty   # 仅 quantity 增；available 由 T+1 机制管理
```

`Portfolio.apply_fill` 买入路径：

```python
pos.on_buy(fill.filled_qty, fill.filled_price + fill.commission / fill.filled_qty)
```

**取舍**

- 备选 A：佣金不入 avg_cost，单独累计。代价：利润计算公式更复杂，卖出时还要
  摊回历史买入佣金。
- 备选 B：用 FIFO / LIFO 队列管理批次。代价：过设计，对单标的简单策略无价值。

**影响**

- 清仓时 `avg_cost` 必须归零（`on_sell` 中 `self.quantity <= 0` 分支），
  避免残留脏数据影响下次买入；
- 卖出盈亏公式 `(sell_price - avg_cost) × qty - sell_commission` 不再重复扣减买入侧费用。

---

## ADR-004 · 因子 SSOT（内存即时算 + 落库仅物化缓存）

**状态**：已采纳（v0.3 引入）

**背景**

因子计算逻辑放在多处会出现"口径漂移"：
库里旧口径 vs 回测新口径不一致 → 同一标的同一时刻出现两个不同因子值。
研究阶段最常见也最难诊断的 bug。

**决策**

因子计算逻辑**全平台只有 `Factor.compute` 一份**：

- **回测**：内存即时计算（`FactorEngine.compute`，与行情同帧同口径，永远是权威值）；
- **落库**：`dwd_factor_value_i` 长表仅是"物化缓存"，供选股 / 因子分析等
  **非回测场景**读取；
- 因子逻辑修正后重算落库即覆盖（upsert 幂等），无版本管理负担。

**取舍**

- 备选 A：因子全部落库，回测读库。代价：库表依赖、数据延迟、口径漂移风险；
- 备选 B：因子全部不落库。代价：跨标的截面分析、IC/IR 计算每次重算成本高。

**影响**

- `run_backtest` 自动多加载 `max(min_periods) × 1.6 + 10` 日历日历史算因子；
- 预热区间裁剪：算完因子再剔除预热 bar，bar 只参与因子计算不进入主循环；
- `compute-factors` 子命令独立存在，专门用于非回测场景的"物化缓存"写入。

---

## ADR-005 · freq 分表（ods_d / ods_mi / dwd_factor_value_i）

**状态**：已采纳（v0.2 引入）

**背景**

日线与 5 分钟线存在天然差异：

- 日线 = 交易日，缺失 pre_close / trade_status / is_st 时无法判涨跌停；
- 分钟线 = 每日 48 根，含时分，时间戳字段类型不同（`date` vs `date_time`）；
- 字段集不同（分钟线不带 PE / PB / 换手率等估值指标）；
- 若同一张表共存，唯一键设计复杂（`code + date_time + freq`），索引效率差。

**决策**

按频率分表，仓储注册表驱动：

```python
TABLES: dict[str, str] = {
    "1d": "ods_d_stock_quotation_i",
    "5min": "ods_mi_stock_quotation_i",
}
```

- 日表 `ods_d_stock_quotation_i`：完整字段（含估值、换手率、涨跌停状态）；
- 分钟表 `ods_mi_stock_quotation_i`：精简字段（行情 + 量价），pre_close / trade_status
  / is_st 通过 LEFT JOIN 日线表**回填**（日线缺失时优雅降级为不判涨跌停）；
- 因子表 `dwd_factor_value_i`：长表 `(code, date_time, freq, factor_name, value)`，
  加新因子零 DDL。

**取舍**

- 备选 A：单表 + freq 列。代价：字段 nullable 多、唯一键复杂、索引效率差。
- 备选 B：完全分离读写表。代价：同步链路长，对个人平台过度工程。

**影响**

- 新频率 = 加一行注册 + 建表 DDL + 加 `_FREQ_MAP` 行（数据源处），共 3 处；
- 分钟读取 LEFT JOIN 日线表是关键性能点（已加 `code + date + adjust_flag` 索引）。

---

## ADR-006 · 订单次一 bar 开盘价撮合（防未来函数）

**状态**：已采纳（v0.1 引入）

**背景**

回测中若订单"当根 bar 收盘价成交"，策略就能看到未来价格（先有收盘价后成交）——
这是回测虚高的头号原因（Look-ahead Bias）。

**决策**

`SimBroker.settle(bar)` 以**本 bar 开盘价**撮合上一根挂起的订单：

- 策略在 t 日 bar 收盘产生信号 → 订单挂在 `SimBroker._pending`；
- t+1 日 bar 处理时 `settle(open_price)` 撮合；
- 成交价 = `bar.open × (1 + slippage)`（买入时上滑、卖出时下滑）；
- 若开盘触板但盘中打开（非一字板）→ 成交价 = 限价单逻辑处理（不会超过涨跌停）。

**取舍**

- 备选 A：当根 bar 收盘成交。代价：未来函数，回测虚高。
- 备选 B：随机延迟 N bar。代价：随机性引入额外噪声，对回测可信度是负面。

**影响**

- 防未来函数第 3 道保险（4 道保险之一）；
- T+1 验证（`smoke_test.test_daily` 逐轮配对断言）依赖此约定。

---

## ADR-007 · 涨跌停基准 = 日线昨收（pre_close）

**状态**：已采纳（v0.2 引入）

**背景**

分钟级回测中，5 分钟 bar 的"上一根收盘"绝不能作为涨跌停基准——
10:00 那根 bar 会被误判涨停（因为 09:35 ~ 10:00 累计涨跌幅可能跨过 10%）。

**决策**

涨跌停基准一律使用**日线昨收** `pre_close`：

```python
upper = bar.pre_close * (1 + 0.095)
lower = bar.pre_close * (1 - 0.095)
```

分钟表本身不存 `pre_close`，由仓储 LEFT JOIN 日线表回填；日线缺失时优雅降级
为不判涨跌停（`pre_close = 0` 时跳过判停）。

**取舍**

- 备选 A：用上一根 bar 的收盘。代价：分钟级误判涨停。
- 备选 B：用当日开盘价。代价：跳空开盘本身就是涨/跌停的极端情形，无法区分。

**影响**

- 分钟表 LEFT JOIN 日线表（`mysql_repo._load_minute`）；
- 仓储输出统一列含 `pre_close`，仓储层自动注入；
- `bar.pre_close = 0` 时跳过涨跌停判停（`SimBroker._try_fill` 中 `if bar.pre_close > 0`）。

---

## ADR-008 · 风控责任链顺序：Tradability → LotSize → Cash → Availability

**状态**：已采纳（v0.1 引入）

**背景**

风控规则多条并行时，顺序影响拒单原因的可读性与系统的稳定性。
先排除"不能做的"再校验"能不能做"，更符合交易员直觉，且能提前 fail-fast。

**决策**

内置规则按以下顺序串行检查，任一拒绝即拦截：

```
TradabilityRule  →  LotSizeRule  →  CashSufficiencyRule  →  AvailabilityRule
   (停牌 ST)         (100 整手)       (现金足额)              (T+1 可卖)
```

**取舍**

- 备选 A：并行检查一次性拒多原因。代价：拒单日志噪声大，调试不友好。
- 备选 B：随机顺序。代价：相同订单多次跑结果不一致，难以复现。

**影响**

- 拒单原因按优先级排序，常见错误（如停牌）先暴露；
- 用户扩展时按相同思路组织：新规则加在链尾或显式指定位置。

---

## ADR-009 · 风控估算口径对齐撮合（bar.open × 缓冲系数）

**状态**：已采纳（v0.2 引入）

**背景**

风控检查使用当前 bar 的 `bar.open` 估算，撮合时使用次 bar 的实际 `bar.open`。
两个 open 不一致时（跳空高开 / 低开），风控可能误判"现金足够"，实际撮合时却
现金不足 → 现金变负 → 资不抵债。

**决策**

`CashSufficiencyRule.check` 估算口径与 `SimBroker._try_fill` 对齐：

```python
# 风控估算
buffer = 1 + cost.slippage + cost.commission_rate
est_cost = bar.open * order.quantity * buffer
if est_cost > portfolio.cash:  # 拒单
```

缓冲系数 = `1 + slippage + commission_rate`，覆盖滑点 + 佣金上界。
撮合时实际花费若仍超现金，`Portfolio.apply_fill` 返回 False，记录到 `risk.rejects`
（"现金不足（撮合时实际花费超过可用现金）"）。

**取舍**

- 备选 A：用 `bar.close` 估算。代价：close ≈ open 时 OK，跳空时严重低估成本。
- 备选 B：不估，直接放行到撮合。代价：现金可能变负，账目破坏。

**影响**

- 风控侧拒绝更"严"，撮合侧兜底更"松"——配合 `apply_fill` 的二次校验形成双保险；
- 拒单日志分两段（风控拒 + 撮合拒），便于诊断现金管理问题。

---

## ADR-010 · 同日内卖出防重（_pending_sell 追踪）

**状态**：已采纳（v0.1 引入）

**背景**

策略在同日可能为同一标的提交多笔卖出订单（典型场景：一根 bar 内先卖一半再卖剩余）。
`Position.available` 要等到次 bar 结算时才扣减，所以同日多笔卖可能超限。

**决策**

`RiskChain` 内部维护 `_pending_sell: dict[str, int]`：

```python
# 通过全部规则后
if order.side == Side.SELL:
    self._pending_sell[order.code] = (
        self._pending_sell.get(order.code, 0) + order.quantity
    )
```

`AvailabilityRule.check` 检查 `effective = pos.available - pending_sell(code)`。

每个 `trade_date` 开始时 `RiskChain.reset_pending()` 清空。

**取舍**

- 备选 A：当日卖出订单按队列排队，每笔成交后扣减 available。代价：撮合逻辑与
  风控耦合，复杂度上升。
- 备选 B：要求策略不重单。代价：策略写法受限，无法写日内多次调仓策略。

**影响**

- `smoke_test.test_minute` 中 `IntradayT1Probe` 验证同日重复卖单被静默拦截；
- `engine.run` 在每日 `on_new_day` 前调 `risk.reset_pending()`。

---

## ADR-011 · Lookback × 1.6 + 10 日历日（因子预热裕量）

**状态**：已采纳（v0.3 引入）

**背景**

回测起点若等于策略起点，因子预热不足（前 N 根 bar NaN），策略会"空转"。
多加载历史算因子保证首日有效，但加载多少合适？

**决策**

```python
lookback = max(get_factor(n).min_periods for n in names)   # bar 数
load_start = (date.fromisoformat(start)
              - timedelta(days=int(lookback * 1.6) + 10)).isoformat()  # 日历日
```

- `1.6` 倍裕量：日历日 / 交易日 ≈ 365/252 ≈ 1.45，取 1.6 防节假日集中；
- `+10` 天：额外缓冲，应对极端长假（如春节）。

**取舍**

- 备选 A：直接 `lookback` 个日历日。代价：节假日集中时预热不足。
- 备选 B：lookback × 3 + 30 天。代价：过量加载，慢但安全过头。

**影响**

- `service.run_backtest` 多加载历史 → `FactorEngine.compute` → 裁剪回测区间；
- 裁剪逻辑：保留 `bars["trade_date"] >= start` 的部分，预热 bar 不进入主循环。

---

## ADR-012 · Web benchmark 按 bar 粒度生成

**状态**：已采纳（v0.2 引入）

**背景**

前端 K 线 x 轴：日线 = 交易日（~250 点），分钟线 = 每根 bar（每年 ~48000 点）。
基准曲线（期初全仓买入持有）若按日生成，分钟频率下与 K 线 x 轴不等长，
图表错位且无法联动 dataZoom。

**决策**

`webapp/server.py` 按 bar 粒度生成基准曲线：

```python
benchmark = [round(float(v) / first * result.metrics["init_cash"], 2) for v in close]
```

每根 bar 都有一个基准点，与 K 线 x 轴自动等长。

**取舍**

- 备选 A：基准按日生成，前端按日重采样。代价：前端逻辑复杂，与分钟数据双轴难对齐。
- 备选 B：不画基准。代价：无法直观对比策略 vs 买入持有。

**影响**

- 净值曲线 x 轴 = 交易日（每天一条 snapshot），基准曲线 x 轴 = bar 时刻
  ——两者长度不等。前端 `equity_curve` 用交易日（按 252 个交易日），
  `benchmark` 用 bar 时刻（按 bar 数），自动按 x 轴对齐。

---

## 模板（新增 ADR）

```markdown
## ADR-NNN · 标题

**状态**：已采纳 / 已弃用（YYYY-MM-DD）

**背景**

[问题陈述——为什么需要做决策]

**决策**

[选择的具体方案——给"是什么"]

**取舍**

- 备选 A：[方案] —— 代价：[为什么放弃]
- 备选 B：[方案] —— 代价：[为什么放弃]

**影响**

[决策带来的下游约束 / 必须遵守的约定 / 后续演进方向]
```