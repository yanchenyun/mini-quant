"""Web 接入层（Web Layer）—— FastAPI 后端 + ECharts 前端。

Web 层只调 app.service，不直接触碰 data / backtest / factor——
依赖方向不倒置（Web → App → Backtest/Strategy/Factor → Core ← Data）。

模块划分
--------
本层两个文件，端点与响应字段以 server.py 为准，这里不重复枚举（避免加字段
后此处腐化）：

- server    FastAPI 应用（app = FastAPI(...)）：静态页 + 数据接口。要点：
            /api/strategies 暴露策略注册表的全部规格，前端据此渲染策略下拉
            与参数输入框（新增策略零前端改动）；/api/backtest 只转发
            service.run_backtest，再把结果组装成前端渲染所需的 JSON。
- index.html 原生 HTML + ECharts 5.5（CDN 引入，无构建工具）。布局为
            工具栏（口径由 /api/strategies 驱动）+ 绩效指标卡片 + 图表
            （K 线含买卖点与策略辅助线；净值曲线 vs 基准）+ 成交明细表；
            前端约定见开发文档 §10。

A 股惯例
--------
- 涨红 #c02020、跌绿 #0a7a3d（与中国股市一致，区别于美 / 欧的红跌绿涨）。
- K 线 x 轴：日线 = 交易日，分钟 = bar.dt（含时分）；买卖点散点 x 轴自动对齐。
- 基准曲线 = 期初全仓买入持有，按 bar 粒度生成（分钟频率下与 K 线等长）。

启动方式
--------
python -m quant.app.cli serve [--port 8000]（命令详见根目录 ReadMe.md §5）。

参见
----
- 开发文档（Web API / 前端约定 / 扩展）：docs/DEVELOPMENT.md §7 §10
- 操作文档（启动 / 运维 / 排错）：根目录 ReadMe.md
"""