"""应用层（Application Layer）—— CLI 与 Web 共用的业务编排地。

依赖注入与装配发生在此层：service 负责解析策略的因子依赖、加载预热行情、
计算因子、构造回测引擎并执行；CLI 与 Web 都只调 service 的公共函数，不直接
触碰 data / backtest——业务逻辑只有一份。

模块划分
--------
本层两个模块：service 业务编排，cli 命令行入口。两者的函数签名与子命令清单
不在此重复枚举——以文件内 docstring 与开发文档 §7 为准，避免增删函数后腐化。

本层的三条约定
--------------
- strategy 由调用方经策略注册表构造后传入，本层不感知任何具体策略；
- 数据源 / 策略 / 因子三者一律显式传参，不设默认值；
- backtest 的策略选项与各策略参数由注册表动态生成（cli.py 的 _add_strategy_args
  聚合），新增策略零改动本层代码。

因子依赖解析
------------
run_backtest 自动读取 strategy.required_factors：
1. 取 max(get_factor(n).min_periods for n in names) 作为 lookback；
2. 多加载 lookback × 1.6 + 10 日历日的行情（1.6 倍裕量应对非交易日）；
3. FactorEngine.compute 内存即时算因子；
4. 裁剪掉预热区间（预热 bar 只参与因子计算，不进入主循环）。

参见
----
- 开发文档（service 函数 / Web API 参考）：docs/DEVELOPMENT.md §7
- 操作文档（CLI 命令参考）：根目录 ReadMe.md §5
"""