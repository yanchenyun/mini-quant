# mini-quant · Web 端文档

> 本目录是 mini-quant 量化平台的 Web 接入层。FastAPI 提供 JSON API，
> 原生 HTML + ECharts 提供前端渲染（无构建工具）。

---

## 1. 文件清单

```
quant/webapp/
├── __init__.py     # 模块级说明（依赖方向 + 启动方式）
├── server.py       # FastAPI 后端（3 个端点）
└── index.html      # ECharts 前端（K线 / 买卖点 / 净值 / 指标卡片 / 成交明细）
```

---

## 2. 启动方式

```bash
python -m quant.app.cli serve [--host 127.0.0.1] [--port 8000]
```

浏览器打开 `http://127.0.0.1:8000`。

依赖（已在 `requirements.txt`）：`fastapi>=0.110` · `uvicorn>=0.29`。
前端用 CDN 引入 `echarts@5.5.0`，**不需要 npm / build**。

---

## 3. API 端点（详细见 `docs/API.md` §2）

| 端点 | 作用 |
|---|---|
| `GET /` | 返回 `index.html` |
| `GET /api/codes` | DB 标的代码列表（下拉框数据源） |
| `GET /api/backtest` | 运行回测 + 返回前端渲染所需的全部 JSON |

`/api/backtest` 的完整参数与返回结构见 `docs/API.md` §2.3。

---

## 4. 架构约束

**Web 层只调 `app.service`，不直接触碰 data / backtest / factor**：

```python
# quant/webapp/server.py
from ..app.service import get_repo, run_backtest   # ✅ 只调 service
from ..strategy.double_ma import DoubleMAStrategy

@app.get("/api/backtest")
def backtest(...):
    strat = DoubleMAStrategy(fast, slow)           # 策略 import 在此层
    result, bars = run_backtest(                   # 业务调 service
        code, start, end, strategy=strat, freq=freq
    )
    return {...}                                   # 数据加工返回前端
```

**为什么这样**：业务逻辑只有一份（`service.py`），CLI 改了 Web 自动同步；
若 Web 直调引擎 / 仓储，则会出现两份实现难以维护。

---

## 5. 前端约定（A 股惯例）

### 5.1 涨跌色

- **涨红 `#c02020`**、**跌绿 `#0a7a3d`**
- 与中国股市一致（区别于美 / 欧的红跌绿涨）
- CSS：`.v.pos { color: #c02020 }` · `.v.neg { color: #0a7a3d }` · `.buy { color: #c02020 }` · `.sell { color: #0a7a3d }`
- ECharts K 线：`itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN }`

### 5.2 x 轴口径

| 数据 | x 轴 | 说明 |
|---|---|---|
| K 线 | 日线 = 交易日（`YYYY-MM-DD`），分钟 = `bar.dt`（含时分）| `bars.dates` 字段 |
| 买卖点散点 | 同 K 线 x 轴（`trade.date` == `bar.dt`）| 自动对齐 |
| 净值曲线 | 交易日 | `equity_curve[].date`，每个交易日一条 |
| 基准曲线 | **bar 粒度**（分钟频率下与 K 线等长） | `data.benchmark` 按 `close` 逐点生成 |

**关键约定**：净值曲线（每天一条，~250 点）与基准曲线（按 bar，分钟频率可上万点）
x 轴长度**不等**——前端两个 chart 独立渲染，靠各自 x 轴自动对齐。

### 5.3 指标卡片

10 张卡片，固定顺序：

```
总收益率 · 年化收益 · 最大回撤 · 夏普比率 · 卡玛比率
胜率 · 盈亏比 · 平仓次数 · 期末权益 · 总佣金
```

数值类（收益率 / 回撤）按涨红跌绿着色（与 A 股惯例一致）。

---

## 6. 前端代码结构

`index.html` 是单文件应用（无外部 CSS / JS 资源，仅 CDN ECharts）：

```
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>mini-quant 回测控制台</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
  <style>...</style>
</head>
<body>
  <h1>...</h1>
  <div class="toolbar">...</div>     <!-- 工具栏 -->
  <div class="cards" id="cards"></div> <!-- 指标卡片 -->
  <div class="charts">
    <div id="kchart"></div>          <!-- K 线 + 均线 + 买卖点 -->
    <div id="echart"></div>          <!-- 净值 vs 基准 -->
  </div>
  <table id="trades">...</table>     <!-- 成交明细 -->
  <script>...</script>               <!-- 渲染逻辑 -->
</body>
</html>
```

### 6.1 渲染入口

```javascript
$('run').onclick = async () => {
  $('run').disabled = true; $('status').textContent = '回测运行中…';
  try {
    const p = new URLSearchParams({...});         // 收集工具栏参数
    const r = await fetch('/api/backtest?' + p);  // 调 API
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    render(await r.json());                        // 渲染 JSON 数据
    $('status').textContent = '';
  } catch (e) { $('status').textContent = '失败：' + e.message; }
  finally { $('run').disabled = false; }
};
```

### 6.2 K 线图 series

```javascript
series: [
  { name: 'K线', type: 'candlestick',
    data: dates.map((d, i) => [open[i], close[i], low[i], high[i]]),
    itemStyle: { color: UP, color0: DOWN, ... } },
  { name: 'MA' + fast, type: 'line', data: ma_fast, ... },
  { name: 'MA' + slow, type: 'line', data: ma_slow, ... },
  { name: '买入', type: 'scatter', data: buys.map(b => [date, price]),
    symbol: 'triangle', itemStyle: { color: UP } },
  { name: '卖出', type: 'scatter', data: sells.map(s => [date, price]),
    symbol: 'path://M0,0L10,0L5,8Z',     // 下三角
    itemStyle: { color: DOWN } },
]
```

---

## 7. 调试 / 排错

### 7.1 启动失败

- **`RuntimeError: 无数据：...`** —— DB 无此标的 / 区间，先跑
  ```bash
  python -m quant.app.cli ingest --code sh.600000 --start 2020-01-01
  ```
- **数据库连接失败** —— 检查 `config/settings.yaml` 的 host / port / password，
  或 `QUANT_DB_*` 环境变量是否覆盖

### 7.2 前端展示异常

- **基准曲线与 K 线错位** —— 通常是某次回测 bug 导致 `close` 长度异常，
  检查后端 `bars.sort_values(x_key)` 后长度
- **指标 NaN / `inf`** —— 净值曲线空 / 交易数为 0 / 极端值导致 std=0，
  检查 `metrics.compute_metrics` 边界条件

### 7.3 分钟频率卡顿

- 一年 5 分钟 bar ≈ 48000 根，前端 dataZoom 已配默认 `inside + slider`
  可缩放查看
- 大规模数据下可加按日聚合（v0.4+ 候选）

---

## 8. 扩展方向（前端）

- 加新策略：在 `index.html` 的 `<select id="strategy">` 加 option，
  后端 `server.py` 加 import + if 分支
- 加新图表：在 `<div class="charts">` 加容器 + 新 `echarts.init()`
  + `render()` 函数内加 `setOption`
- 加新指标：在 `_round_list` 或 `render()` 内加数据加工 + 在 `cards` div 内插卡片

详见 `docs/EXTENSION_GUIDE.md` §1（新策略步骤含 Web 注册）。