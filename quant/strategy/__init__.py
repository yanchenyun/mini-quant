"""策略层（Strategy Layer）—— 用户写策略信号的地方。

策略只依赖 ``core.abstractions.StrategyContext`` 窄接口（ISP）：
- ``ctx.history``             截至当前 bar（含）的收盘价序列
- ``ctx.factor(name)``        当前 bar 的因子值（NaN 表示预热区无值）
- ``ctx.factor_history(name)`` 截至当前 bar（含）的因子值序列
- ``ctx.portfolio``           账户只读视图（现金 / 持仓 / 净值）
- ``ctx.buy/sell``            按现金比例买入 / 按持仓比例卖出（T+1 仅 available）

策略**频率无关**——同一份代码在日线 / 5 分钟线上直接可用，rolling 指标
天然按 bar 计算（详见 ``strategy.double_ma.DoubleMAStrategy``）。

当前内置策略
------------
- ``double_ma``        双均线：快线上穿慢线买入，下穿卖出（全仓）。
- ``factor_momentum``  动量因子：声明 ``required_factors = ["momentum_N"]``，
                       服务层自动解析依赖、预热计算并注入，策略只关心信号语义。

新策略步骤
----------
1. ``quant/strategy/`` 下新建一个文件，继承 ``Strategy``；
2. 实现 ``on_init``（必需）+ ``on_bar``（必需）+ 可选重写 ``on_new_day``；
3. 如需因子，类属性声明 ``required_factors = ["xxx"]``；
4. 在 ``app/cli.py`` 与 ``webapp/server.py`` 的策略选择处注册（详见
   ``docs/EXTENSION_GUIDE.md``）。

防未来函数（4 道保险之策略侧）
-----------------------------
- ``ctx.history`` 只到当前 bar（含），无法读未来；
- ``ctx.factor()`` 由引擎 ``iloc[:n]`` 切片注入，结构上排除未来行；
- 策略在当前 bar 收盘产生信号 → ``ctx.buy/sell`` → 订单挂在 SimBroker
  → 次一 bar 开盘价成交（信号当根不可能成交）。

参见
----
- 架构总览：``docs/ARCHITECTURE.md``
- 扩展指南（如何加新策略）：``docs/EXTENSION_GUIDE.md``
"""