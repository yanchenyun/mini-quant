"""领域层（Domain Layer）—— 全系统的"宪法"。

本包是整个项目的基石，**零第三方依赖**（仅使用标准库 dataclasses / enum / abc / typing），
其它所有层（data / factor / backtest / strategy / app / webapp）都依赖并实现本包定义的
抽象契约。

模块划分
--------
- ``models``       不可变值对象：Bar（含 dt + trade_date 双时间字段）/ Order / Fill /
                   Position（实现 T+1 的 available）/ TradeRecord / Side（买卖方向枚举）。
                   ``Bar`` 是引擎 / 策略 / 仓储 / Web 共用的统一数据类型。
- ``abstractions`` 系统扩展点的接口契约（Protocol 鸭子接口 + ABC 抽象基类混用）：
                   MarketDataSource / DataRepository / Strategy / StrategyContext /
                   OrderSink / PortfolioView / Broker / RiskRule / Factor /
                   FactorAccessor。新增数据源 / 策略 / 因子 / 风控 = 新增一个实现类即可。
- ``portfolio``    组合记账本：现金 / 持仓 / 净值曲线 / 成交记录。apply_fill 按成交
                   更新现金与持仓、卖出时结算本轮盈亏（含买入佣金摊薄的成本基准）。

三条铁律
--------
1. 本包零第三方依赖 —— 不允许 import pandas / numpy / pymysql 等；
2. 依赖箭头永远指向本包（其它层可以依赖本包，本包不能反向依赖任何上层）；
3. 策略只见 ``StrategyContext`` 窄接口，看不见引擎 / 数据库 / 数据源（接口隔离 ISP）。

参见
----
- 架构总览：``docs/ARCHITECTURE.md``
- 设计决策：``docs/DESIGN_DECISIONS.md``
"""