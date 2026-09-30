"""CLI 入口：python -m quant.app.cli <command>

    python -m quant.app.cli init-schema
    python -m quant.app.cli ingest  --code sh.600000 --start 2020-01-01 --source baostock
    python -m quant.app.cli ingest  --code sh.600000 --start 2025-01-01 --source wind --freq 5min
    python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01 --factors momentum_20
    python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --strategy double_ma
    python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 --strategy turtle --entry 20 --exit 10
    python -m quant.app.cli backtest --help        # 查看全部已注册策略及其参数
    python -m quant.app.cli serve [--port 8000]

数据源（--source）、策略（--strategy）、因子（--factors）一律显式指定，不设
默认值——这三者决定"数据从哪来、怎么决策、算什么指标"，选错会静默产出错误
结论，故由调用方每次明确选择。

--strategy 的可选项与各策略参数均由策略注册表动态生成：新增策略在
quant/strategy/ 下自注册即可，本文件零改动。
"""
from __future__ import annotations

import argparse
import json

from ..app.service import compute_factors, ingest_bars, run_backtest
from ..factor import available as available_factors
from ..strategy import (available_strategies, build_strategy,
                        get_strategy)


def _add_strategy_args(parser: argparse.ArgumentParser) -> None:
    """把注册表里的全部策略名与策略参数聚合成 argparse 选项。

    --strategy 必填（不设默认策略：选错策略会静默产出错误结论）。
    参数名跨策略全局唯一时同名复用（首个注册策略的 help 前缀生效）；
    choices / 类型 / 默认值均来自 ParamSpec 单一声明。help 中的百分号
    在此转义（argparse 对帮助文案做百分号格式化，见 Python 文档）。
    """
    names = available_strategies()
    parser.add_argument("--strategy", required=True, choices=names,
                        help="策略（必填；可选项由策略注册表动态生成）")
    seen: set[str] = set()
    for name in names:
        spec = get_strategy(name)
        for p in spec.params:
            if p.name in seen:
                continue        # 同名参数已由更早注册的策略声明
            seen.add(p.name)
            parser.add_argument(f"--{p.name}", type=p.type, default=p.default,
                                choices=p.choices,
                                help=f"（{spec.label}）{p.help}".replace("%", "%%"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="mini-quant", description="简易量化平台")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-schema", help="初始化 MySQL 库表（幂等：行情日表/分钟表/因子表）")

    p_in = sub.add_parser("ingest", help="行情增量入库（数据源必填）")
    p_in.add_argument("--code", required=True, help="证券代码，如 sh.600000 / sz.000001")
    p_in.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p_in.add_argument("--end", default=None, help="结束日期，默认今天")
    p_in.add_argument("--adjust", default="2", choices=["1", "2", "3"],
                      help="复权：1后复权 2前复权 3不复权，默认2")
    p_in.add_argument("--freq", default="1d", choices=["1d", "5min"],
                      help="频率：1d 日线 5min 5分钟线，默认1d")
    p_in.add_argument("--source", required=True,
                      choices=["baostock", "akshare", "wind"],
                      help="数据源（必填）：baostock 免费稳定 / akshare 免费聚合多源"
                           "/ wind 需本机安装并登录 Wind 终端")

    p_cf = sub.add_parser("compute-factors",
                          help="计算因子并存入 MySQL（幂等 upsert；因子必填）")
    p_cf.add_argument("--code", required=True)
    p_cf.add_argument("--start", required=True)
    p_cf.add_argument("--end", default=None, help="结束日期，默认今天")
    p_cf.add_argument("--freq", default="1d", choices=["1d", "5min"])
    p_cf.add_argument("--factors", required=True,
                      help=f"因子名（必填，逗号分隔），可选: {','.join(available_factors())}")

    p_bt = sub.add_parser("backtest", help="运行回测并打印绩效")
    p_bt.add_argument("--code", required=True)
    p_bt.add_argument("--start", required=True)
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--freq", default="1d", choices=["1d", "5min"],
                      help="频率：1d 日线 5min 5分钟线，默认1d")
    _add_strategy_args(p_bt)
    p_bt.add_argument("--json", action="store_true", help="输出完整 JSON")

    p_sv = sub.add_parser("serve", help="启动 Web 控制台")
    p_sv.add_argument("--host", default="127.0.0.1")
    p_sv.add_argument("--port", type=int, default=8000)

    sub.add_parser("repair-dt", help="修复分钟表历史脏时间戳（17位数字串→标准格式，幂等）")

    args = parser.parse_args()

    if args.command == "init-schema":
        from ..app.service import get_repo
        get_repo().init_schema()
        print("库表初始化完成（行情日/分钟表 + 因子表 dwd_factor_value_i）")

    elif args.command == "ingest":
        ingest_bars(args.code, args.start, end=args.end, adjust=args.adjust,
                    freq=args.freq, source=args.source)

    elif args.command == "compute-factors":
        compute_factors(args.code, args.start, args.end, freq=args.freq,
                        names=args.factors.split(","))

    elif args.command == "backtest":
        spec = get_strategy(args.strategy)
        params = {p.name: getattr(args, p.name) for p in spec.params}
        strategy = build_strategy(args.strategy, params)
        label = spec.describe(params) if spec.describe else spec.label
        result, _ = run_backtest(args.code, args.start, args.end,
                                 strategy=strategy, freq=args.freq)
        if args.json:
            print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
        else:
            print(f"\n===== 回测结果 {args.code} [{args.freq}] ({label}) =====")
            for k, v in result.metrics.items():
                print(f"  {k:20s} {v}")
            print(f"  成交笔数: {len(result.trades)}  风控拒单: {len(result.rejects)}")

    elif args.command == "repair-dt":
        from ..app.service import get_repo
        n = get_repo().repair_date_time()
        print(f"分钟表 date_time 归一完成，修复 {n} 行（0 行说明已是标准格式）")

    elif args.command == "serve":
        import uvicorn
        from ..webapp.server import app
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
