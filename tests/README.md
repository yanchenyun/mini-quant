# mini-quant · 测试文档

> 本目录只有一份离线冒烟测试 `smoke_test.py`，不依赖任何外部服务（MySQL /
> Baostock），用合成行情验证引擎 + 策略 + 风控 + 撮合 + 绩效 全链路。
>
> 本文档说明覆盖矩阵、加新测试的步骤、踩过的坑。

---

## 1. 覆盖矩阵

| 用例 | 验证点 | 不可替代特性 |
|---|---|---|
| `test_daily` | 日线回归（v0.1 行为不变）：双均线 MA5/20、MA10/60 | v0.1 兼容基线 |
| `test_minute` | 5 分钟频率（v0.2 新能力）：日界解禁、T+1、净值按交易日 | **多频率兼容** 核心 |
| `test_factor` | 因子库（v0.3 新能力）：计算正确性、预热、防未来、因子策略全链路 | **SSOT** + **防未来** 核心 |

### 1.1 `test_daily` 详细断言

```python
assert len(result.equity_curve) == len(bars), "净值曲线天数应等于交易日数"
assert m["final_equity"] > 0
assert all(t["quantity"] % 100 == 0 for t in result.trades), "成交量须为100整数倍"

# T+1 验证（逐轮配对：每笔卖出必须晚于其对应轮次的买入）
for t in result.trades:
    day = t["date"][:10]
    if t["side"] == "buy":
        open_buy_day = day
    else:
        assert day > open_buy_day, f"当日买({open_buy_day})当日卖({day})，违反T+1"
        open_buy_day = None
```

### 1.2 `test_minute` 详细断言

```python
# 1) 探针策略：日界 / T+1 / 次日可卖
probe = IntradayT1Probe()
result = engine.run()

assert len(probe.new_day_events) == days  # 日界按交易日触发
assert probe.new_day_events == sorted(set(bars["trade_date"]))  # 顺序一致
assert sell_day > buy_day  # 当日买次日卖
assert buy_day in probe.sell_attempt_days  # 当日存在被 T+1 拦截的卖出尝试

# 2) 双均线在 5 分钟序列上直接可用（策略代码与日线完全一致）
engine2 = BacktestEngine(bars=bars, strategy=DoubleMAStrategy(4, 24), ...)
r2 = engine2.run()
assert len(r2.equity_curve) == days  # 净值快照按交易日，不按 bar
```

### 1.3 `test_factor` 详细断言

```python
# 1) 计算正确性 + 预热语义
frame = FactorEngine.compute(bars, names)
expected = close[idx] / close[idx - 20] - 1.0
assert abs(frame["momentum_20"].iloc[idx] - expected) < 1e-12
assert frame["momentum_20"].iloc[:20].isna().all()  # 预热区 NaN
assert frame["momentum_20"].iloc[20:].notna().all()  # 预热后全部有效

# 2) 防未来：探针逐 bar 对照因果口径
probe = FactorProbe()
engine = BacktestEngine(..., factor_frame=frame)
engine.run()
for i, (dt, v, n) in enumerate(probe.snapshots):
    assert n == i + 1  # factor_history 长度逐 bar 增长（防未来切片）
    assert abs(v - mom.iloc[i]) < 1e-9  # 因子值与因果计算一致

# 3) 因子策略全链路
engine2 = BacktestEngine(..., strategy=FactorMomentumStrategy(20), factor_frame=frame)
r = engine2.run()
assert len(r.trades) > 0  # 合成行情涨跌交替，动量必然变号 → 应有交易
```

---

## 2. 跑测试

```bash
python tests/smoke_test.py
```

预期输出末尾：

```
日线回归用例通过 ✓
5 分钟频率用例通过 ✓
因子库用例通过 ✓

全部断言通过 ✓
```

**不需要任何外部依赖**（MySQL / Baostock / 网络）—— 改完任何代码都先跑这一遍。

---

## 3. 合成数据生成器

`smoke_test.py` 内置两个生成器，覆盖日线 + 5 分钟线：

### 3.1 `make_bars(code, days, start_price)`

合成日线：

- `days` = 交易日数（默认 400）
- `start_price` = 起始价（默认 10.0）
- 走势：随机游走 + 季节项 `0.15 * sin(t/25)`
- 列：`code, date, dt, trade_date, open, high, low, close, volume, amount, trade_status=1, is_st=0`
- 跳过首日（无 pre_close）→ 实际 `days - 1` 行

### 3.2 `make_minute_bars(code, days, bars_per_day, start_price)`

合成 5 分钟线：

- `days` = 交易日数（默认 40）
- `bars_per_day` = 每日 bar 数（默认 8，时间点：09:35 / 09:40 / ... / 14:55）
- `pre_close` = 前一交易日收盘价（涨跌停基准 = 日线昨收）
- 列：与 `make_bars` 一致 + `dt` 含时分

---

## 4. 加新测试的步骤

### 4.1 新因子测试

```python
def test_new_factor() -> None:
    bars = make_bars()
    names = ["your_new_factor"]

    # 1) 计算正确性（手算预期值对照）
    frame = FactorEngine.compute(bars, names)
    expected = ...  # 手算公式
    assert abs(frame["your_new_factor"].iloc[100] - expected) < 1e-12

    # 2) 预热语义：前 N 行 NaN，N+1 起有效
    assert frame["your_new_factor"].iloc[:N].isna().all()
    assert frame["your_new_factor"].iloc[N:].notna().all()

    # 3) 防未来：用 FactorProbe 跑全链路
    probe = FactorProbe()
    probe.required_factors = ["your_new_factor"]
    engine = BacktestEngine(bars=bars, strategy=probe,
                            init_cash=1_000_000, cost=CostModel(),
                            factor_frame=frame)
    engine.run()
    # ... 验证 probe.snapshots 的因子值与全量因果计算一致

    print("新因子用例通过 ✓")
```

记得在 `main()` 中调用。

### 4.2 新策略测试

```python
def test_new_strategy() -> None:
    bars = make_bars()
    engine = BacktestEngine(
        bars=bars, strategy=YourStrategy(),
        init_cash=1_000_000, cost=CostModel(),
    )
    result = engine.run()
    m = result.metrics

    assert m["final_equity"] > 0
    assert all(t["quantity"] % 100 == 0 for t in result.trades), "成交量须为100整数倍"

    # T+1 逐轮配对（同 test_daily）
    open_buy_day = None
    for t in result.trades:
        day = t["date"][:10]
        if t["side"] == "buy":
            open_buy_day = day
        else:
            assert open_buy_day is not None and day > open_buy_day, "违反 T+1"
            open_buy_day = None

    print(f"\n== 新策略 ==")
    for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
              "trade_count", "win_rate", "final_equity"):
        print(f"  {k:18s} {m.get(k)}")
    print("新策略用例通过 ✓")
```

### 4.3 新风控测试

```python
def test_new_risk() -> None:
    bars = make_bars()

    # 构造一个会触发的场景（模拟超限）
    class _Probe(Strategy):
        def on_init(self, ctx): pass
        def on_bar(self, ctx, bar):
            ctx.buy(bar.code, 0.99)  # 99% 仓位，可能超 MaxPositionRule

    engine = BacktestEngine(
        bars=bars, strategy=_Probe(),
        init_cash=1_000_000, cost=CostModel(),
        risk_rules=[MaxPositionRule(max_ratio=0.3)],
    )
    result = engine.run()
    assert len(result.rejects) > 0, "MaxPositionRule 应至少拒一单"

    print("新风险用例通过 ✓")
```

---

## 5. 排错指引

### 5.1 测试失败的常见原因

| 现象 | 可能原因 |
|---|---|
| T+1 断言失败 | `Position.available` 逻辑改坏；或引擎 `on_new_day` 时机错 |
| `final_equity <= 0` | 撮合时现金变负；`apply_fill` 二次校验未生效 |
| 净值曲线长度不等于交易日数 | 引擎主循环按 `dt` 而非 `trade_date` 驱动日界 |
| 因子值与手算不一致 | 因子 `compute` 实现非因果（用了未来数据）|
| `factor_history` 长度不逐 bar 增长 | 引擎 `iloc[:n]` 切片逻辑改坏（防未来破洞）|

### 5.2 调试小技巧

- 在 `on_bar` 内 `print(bar.dt, ctx.history.iloc[-1])` 看实际数据；
- 用 `engine.portfolio.fills[-5:]` 看最近 5 笔成交细节；
- 用 `engine.risk.rejects` 看拒单原因分类统计。

---

## 6. 未来扩展（v0.4+ 候选）

- **pytest 框架**：当前用 `assert + main()` 手动调用，改 pytest 后可
  - 单条用例单独跑：`pytest tests/smoke_test.py::test_factor`
  - 加 `--cov` 看覆盖率
  - 加 `golden/` 黄金回测（固定数据快照 diff 锁定结果，防改动引入回归）
- **撮合与风控 100% 覆盖**：当前冒烟测试以"端到端"为主，可拆出 unit test
  直接测 `SimBroker._try_fill` 各分支（一字板拒单、限价单跳空未成交等）
- **CI 接入**：GitHub Actions 自动跑 `python tests/smoke_test.py` + ruff + mypy