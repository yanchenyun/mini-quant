"""回测层（Backtest Layer）—— 事件驱动内核 + A 股交易规则模拟。

主循环按 ``trade_date`` 分组驱动日界（T+1 解禁 / 日终净值快照每交易日仅一次），
一天内按 ``dt`` 逐 bar 推进。订单在次一 bar 开盘价成交（防未来函数）。
净值按日快照 → 年化基准恒为 252，不被 bar 数虚增。

模块划分
--------
- ``engine``      事件驱动回测引擎 ``BacktestEngine.run()``：按 ``trade_date``
                  分组、按 ``dt`` 逐 bar 推进；预分配 numpy 数组累计 history
                  避免主循环中构造 ``pd.Series`` 的 O(n²) 开销；因子通过
                  ``FactorAccessor.iloc[:n]`` 切片注入。
- ``sim_broker``  ``SimBroker``（实现 ``Broker`` 协议）模拟撮合：A 股交易规则
                  （佣金万 2.5 + 最低 5 元 + 印花税卖出万 5 + 滑点千 2 +
                  涨跌停一字板拒单 + 停牌 / ST 拒单 + 限价单跳空未成交保留）。
                  将来 ``QmtBroker`` 实现同一协议即可切实盘（引擎零改动 LSP）。
- ``risk``        风控责任链 ``RiskChain``：内置 4 条规则（停牌 ST → 手数 →
                  现金 → T+1 可卖）；订单通过全部规则才入 Broker 挂起队列；
                  同日内卖出防重通过 ``_pending_sell`` 追踪。
- ``metrics``     绩效纯函数 ``compute_metrics(equity_curve, trades)``：
                  总收益 / 年化（默认 252 基准）/ 最大回撤（含起止区间）/
                  夏普 / 卡玛 / 年化波动率 / 胜率 / 盈亏比 / 平均盈亏 / 总佣金。

A 股规则实现位置速查
--------------------
- T+1：``core.Position.available`` + ``engine.run`` 日界
- 100 股整手：``core.StrategyContext.buy/sell`` + ``risk.LotSizeRule``
- 佣金 / 印花税 / 滑点：``sim_broker.CostModel``
- 涨跌停一字板拒单：``sim_broker.SimBroker._try_fill``
- 停牌 / ST：``risk.TradabilityRule``
- 信号次 bar 撮合：``sim_broker.SimBroker.settle``

参见
----
- 开发文档（架构 / A股规则 / 设计决策 / 扩展）：``docs/DEVELOPMENT.md``
- 操作文档（启动 / 运维 / 排错）：根目录 ``ReadMe.md``
"""