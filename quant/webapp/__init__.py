"""Web 接入层（Web Layer）—— FastAPI 后端 + ECharts 前端。

Web 层只调 ``app.service``，不直接触碰 data / backtest / factor——
依赖方向不倒置（Web → App → Backtest/Strategy/Factor → Core ← Data）。

模块划分
--------
- ``server``    FastAPI 应用（``app = FastAPI(...)``）：两个端点
                - ``GET /``：返回 ``index.html`` 静态页
                - ``GET /api/codes``：DB 标的列表（用于前端下拉框）
                - ``GET /api/backtest``：调 ``service.run_backtest``，返回
                  params / metrics / kline / equity_curve / benchmark /
                  buys / sells / rejects 共 8 段数据供前端 ECharts 渲染。
- ``index.html`` 原生 HTML + ECharts 5.5（CDN 引入，无构建工具）。布局：
                ① 工具栏（代码下拉 / 频率 / 策略 / 日期 / 快慢线周期 / 动量窗口）
                ② 10 张绩效卡片（总收益 / 年化 / 最大回撤 / 夏普 / 卡玛 / 胜率 /
                   盈亏比 / 平仓次数 / 期末权益 / 总佣金）
                ③ 两张 ECharts 图（K线 + 买卖点散点 + 双均线 / 净值曲线 vs 基准）
                ④ 成交明细表（买卖逐行展示，含本轮盈亏）

A 股惯例
--------
- 涨红 ``#c02020``、跌绿 ``#0a7a3d``（与中国股市一致，区别于美 / 欧的红跌绿涨）。
- K 线 x 轴：日线 = 交易日，分钟 = ``bar.dt``（含时分）；买卖点散点 x 轴自动对齐。
- 基准曲线 = 期初全仓买入持有，按 bar 粒度生成（分钟频率下与 K 线等长）。

启动方式
--------
``python -m quant.app.cli serve [--port 8000]``（命令详见根目录 ``ReadMe.md`` §5）。

参见
----
- 开发文档（Web API / 前端约定 / 扩展）：``docs/DEVELOPMENT.md`` §7 §10
- 操作文档（启动 / 运维 / 排错）：根目录 ``ReadMe.md``
"""