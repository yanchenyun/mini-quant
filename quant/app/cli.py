"""CLI 入口：python -m quant.app.cli <command>

    python -m quant.app.cli init-schema
    python -m quant.app.cli ingest  --code sh.600000 --start 2020-01-01
    python -m quant.app.cli ingest  --code sh.600000 --start 2025-01-01 --freq 5min
    python -m quant.app.cli compute-factors --code sh.600000 --start 2024-01-01
    python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --end 2024-12-31
    python -m quant.app.cli backtest --code sh.600000 --start 2021-01-01 --strategy factor_momentum --window 60
    python -m quant.app.cli backtest --code sh.600000 --start 2025-01-01 --freq 5min --fast 8 --slow 48
    python -m quant.app.cli serve [--port 8000]
"""
from __future__ import annotations

import argparse
import json

from ..app.service import compute_factors, ingest_bars, run_backtest
from ..factor import available as available_factors


def main() -> None:
    parser = argparse.ArgumentParser(prog="mini-quant", description="简易量化平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-schema", help="初始化 MySQL 库表（幂等：行情日表/分钟表/因子表）")

    p_in = sub.add_parser("ingest", help="Baostock 行情增量入库")
    p_in.add_argument("--code", required=True, help="证券代码，如 sh.600000 / sz.000001")
    p_in.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p_in.add_argument("--end", default=None, help="结束日期，默认今天")
    p_in.add_argument("--adjust", default="2", choices=["1", "2", "3"],
                      help="复权：1后复权 2前复权 3不复权，默认2")
    p_in.add_argument("--freq", default="1d", choices=["1d", "5min"],
                      help="频率：1d 日线 5min 5分钟线，默认1d")

    p_cf = sub.add_parser("compute-factors",
                          help="计算因子并存入 MySQL（幂等 upsert；不传 --factors 则计算全部内置因子）")
    p_cf.add_argument("--code", required=True)
    p_cf.add_argument("--start", required=True)
    p_cf.add_argument("--end", default=None, help="结束日期，默认今天")
    p_cf.add_argument("--freq", default="1d", choices=["1d", "5min"])
    p_cf.add_argument("--factors", default=None,
                      help=f"逗号分隔因子名，可选: {','.join(available_factors())}")

    p_bt = sub.add_parser("backtest", help="运行回测并打印绩效")
    p_bt.add_argument("--code", required=True)
    p_bt.add_argument("--start", required=True)
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--freq", default="1d", choices=["1d", "5min"],
                      help="频率：1d 日线 5min 5分钟线，默认1d")
    p_bt.add_argument("--strategy", default="double_ma",
                      choices=["double_ma", "factor_momentum"],
                      help="策略：double_ma 双均线 factor_momentum 动量因子（v0.3）")
    p_bt.add_argument("--fast", type=int, default=5,
                      help="快线周期（double_ma，按 bar 计）")
    p_bt.add_argument("--slow", type=int, default=20, help="慢线周期（double_ma）")
    p_bt.add_argument("--window", type=int, default=20,
                      help="动量窗口（factor_momentum，如 20/60）")
    p_bt.add_argument("--json", action="store_true", help="输出完整 JSON")

    p_sv = sub.add_parser("serve", help="启动 Web 控制台")
    p_sv.add_argument("--host", default="127.0.0.1")
    p_sv.add_argument("--port", type=int, default=8000)

    p_rp = sub.add_parser("repair-dt", help="修复分钟表历史脏时间戳（17位数字串→标准格式，幂等）")

    args = parser.parse_args()

    if args.command == "init-schema":
        from ..app.service import get_repo
        get_repo().init_schema()
        print("库表初始化完成（行情日/分钟表 + 因子表 dwd_factor_value_i）")

    elif args.command == "ingest":
        ingest_bars(args.code, args.start, args.end, args.adjust, freq=args.freq)

    elif args.command == "compute-factors":
        names = args.factors.split(",") if args.factors else None
        compute_factors(args.code, args.start, args.end, freq=args.freq, names=names)

    elif args.command == "backtest":
        if args.strategy == "factor_momentum":
            from ..strategy.factor_momentum import FactorMomentumStrategy
            strategy = FactorMomentumStrategy(args.window)
            label = f"momentum_{args.window}"
        else:
            strategy = None   # run_backtest 默认构造 DoubleMAStrategy(fast, slow)
            label = f"MA{args.fast}/{args.slow}"
        result, _ = run_backtest(args.code, args.start, args.end,
                                 strategy=strategy, fast=args.fast,
                                 slow=args.slow, freq=args.freq)
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
