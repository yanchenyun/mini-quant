"""离线冒烟测试入口：按层组织的用例模块在此顺序执行。

用例按架构层分模块（见 tests 包内各 test_ 开头文件）：
- test_core：核心契约（风控链语义、策略元数据严格性）
- test_backtest：引擎与撮合全链路（日线 / 5 分钟 / 通道注入 /
  引擎生命周期 / 静默失败回归 / 无效价信号隔离 / 涨跌停权威价 /
  限价边界语义 / trade_date 归属）
- test_strategy_factor：策略与因子（因子正确性与防未来、海龟全链路、
  注册表机制）
- test_data：数据层（Wind 适配器离线回归、注册表单一来源与年化基准、
  派生频率重采样聚合）
- test_app：应用层（CLI 必填参数）

全部用例离线运行：不依赖 MySQL / Wind 终端与外网，
网络相关路径一律以 mock 或合成数据覆盖。执行方式不变——在仓库根
目录运行 python tests/smoke_test.py；各模块命名同时兼容 pytest
直接收集。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from tests.test_app import test_cli_required_args
from tests.test_backtest import (test_broker_injection, test_daily,
                                 test_engine_lifecycle,
                                 test_invalid_price_signal_isolation,
                                 test_minute, test_price_limit_authority,
                                 test_price_limit_edge_semantics,
                                 test_silent_failure_regressions,
                                 test_trade_date_preserved)
from tests.test_core import test_risk_chain
from tests.test_data import (test_data_registry, test_resample_aggregation,
                             test_wind_source)
from tests.test_strategy_factor import (test_factor, test_strategy_registry,
                                        test_trend)


def main() -> None:
    test_daily()
    test_minute()
    test_factor()
    test_trend()
    test_wind_source()
    test_strategy_registry()
    test_cli_required_args()
    test_broker_injection()
    test_risk_chain()
    test_data_registry()
    test_resample_aggregation()
    test_silent_failure_regressions()
    test_engine_lifecycle()
    test_invalid_price_signal_isolation()
    test_price_limit_authority()
    test_price_limit_edge_semantics()
    test_trade_date_preserved()
    print("\n全部断言通过 ✓")


if __name__ == "__main__":
    main()
