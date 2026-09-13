"""应用层（Application Layer）—— CLI 与 Web 共用的业务编排地。

依赖注入与装配发生在此层：``service.py`` 负责解析策略的因子依赖、加载预热
行情、计算因子、构造 BacktestEngine 并执行；CLI 与 Web 都只调 service 的
公共函数，**不直接触碰 data / backtest**——避免业务逻辑散落两份。

模块划分
--------
- ``service``  业务服务层（供 CLI 与 Web 共用）：
               - ``get_repo(settings)``：构造 ``MySQLBarRepo``（工厂）
               - ``get_source(name)``：构造数据源类（工厂）
               - ``ingest_bars(code, start, end, adjust, freq, source, ...)``：
                 增量抓取并入库（自动从库里最新日期的次日开始续抓，幂等）
               - ``compute_factors(code, start, end, freq, names, ...)``：
                 加载行情 → 算因子 → 落库（物化缓存，upsert 幂等）
               - ``run_backtest(code, start, end, strategy, fast, slow, freq, ...)``：
                 加载行情（含因子预热 lookback）→ BacktestEngine.run() → 返回
                 ``(BacktestResult, bars)`` 供 CLI / Web 渲染
- ``cli``      命令行入口（argparse）：``init-schema`` / ``ingest`` /
               ``compute-factors`` / ``backtest`` / ``repair-dt`` / ``serve``
               共 6 个子命令。

因子依赖解析
------------
``run_backtest`` 自动读取 ``strategy.required_factors``：
1. 取 ``max(get_factor(n).min_periods for n in names)`` 作为 lookback；
2. 多加载 ``lookback × 1.6 + 10`` 日历日的行情（1.6 倍裕量应对非交易日）；
3. ``FactorEngine.compute`` 内存即时算因子；
4. 裁剪掉预热区间（预热 bar 只参与因子计算，不进入主循环）。

参见
----
- API 手册：``docs/API.md``
- 架构总览：``docs/ARCHITECTURE.md``
"""