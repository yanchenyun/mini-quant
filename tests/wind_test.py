"""Wind 数据源冒烟测试：验证 WindSource 能连通、拉数、并输出统一列。

覆盖三段：
1. **代码格式转换**（纯函数，不需要 Wind 连接）：``sh.600519`` ↔ ``600519.SH``；
2. **日线**：``w.wsd`` 拉取，断言统一列齐全、时间升序、价格为正；
3. **分钟线**：``w.wsi`` 拉取，断言 dt 含时分秒、trade_date 为日期部分。

前置条件
--------
本机已安装并登录 **Wind 金融终端**（WindPy 随终端分发，不能 pip 安装）。
Wind 终端未登录时，脚本会给出明确提示而不是堆栈。

用法
----
    D:/anaconda3/envs/env-3_12_7/python.exe tests/wind_test.py
    D:/anaconda3/envs/env-3_12_7/python.exe tests/wind_test.py --code sh.600519 --start 2024-01-01 --end 2024-01-15
    D:/anaconda3/envs/env-3_12_7/python.exe tests/wind_test.py --skip-minute   # 只测日线（更快）
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

sys.path.insert(0, ".")

from quant.data.wind_source import (WindSource, _from_wind_code,  # noqa: E402
                                    _to_wind_code)

# 统一列契约（见 core.abstractions.MarketDataSource；附加列视字段可用性）
REQUIRED_COLS = ["code", "dt", "trade_date", "open", "high", "low", "close",
                 "pre_close", "volume", "amount", "trade_status", "is_st"]


def check_code_mapping() -> None:
    """代码格式双向转换（不依赖 Wind 连接）。"""
    cases = [("sh.600519", "600519.SH"),
             ("sz.000001", "000001.SZ"),
             ("bj.430047", "430047.BJ")]
    for local, wind in cases:
        got = _to_wind_code(local)
        assert got == wind, f"{local} -> {got}，期望 {wind}"
        assert _from_wind_code(wind) == local, f"反向转换失败: {wind} -> {_from_wind_code(wind)}"
    print("✓ 代码格式转换  sh.600519 <-> 600519.SH")


def check_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """日线：w.wsd。"""
    df = WindSource.fetch_bars(code, start, end, freq="1d", adjust="2")
    assert not df.empty, f"日线无数据：{code} {start}~{end}（检查代码/日期区间）"

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    assert not missing, f"日线缺少统一列: {missing}"

    assert df["dt"].is_monotonic_increasing, "日线 dt 应升序"
    assert df["dt"].str.len().eq(10).all(), "日线 dt 应为 'YYYY-MM-DD'"
    assert (df["trade_date"] == df["dt"]).all(), "日线 trade_date 应等于 dt"
    assert df["code"].eq(code).all(), f"code 列应恒为 {code}"
    assert (df["close"] > 0).all(), "收盘价应全为正"
    assert df["adjust_flag"].eq(2).all(), "adjust_flag 应记录前复权=2"

    print(f"✓ 日线  {code} {start}~{end}  {len(df)} 行")
    print(f"    列: {list(df.columns)}")
    with pd.option_context("display.width", 160, "display.max_columns", 30):
        print(df[["dt", "code", "open", "high", "low", "close",
                  "pre_close", "volume", "trade_status"]].head(3).to_string(index=False))
    return df


def check_minute(code: str, start: str, end: str) -> pd.DataFrame:
    """分钟线：w.wsi + BarSize=5。"""
    df = WindSource.fetch_bars(code, start, end, freq="5min", adjust="2")
    assert not df.empty, f"5 分钟线无数据：{code} {start}~{end}"

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    assert not missing, f"分钟线缺少统一列: {missing}"

    assert df["dt"].str.len().eq(19).all(), "分钟线 dt 应为 'YYYY-MM-DD HH:MM:SS'"
    assert (df["trade_date"] == df["dt"].str[:10]).all(), "trade_date 应为 dt 的日期部分"
    assert df["dt"].is_monotonic_increasing, "分钟线 dt 应升序"
    n_days = df["trade_date"].nunique()
    print(f"✓ 5分钟 {code} {start}~{end}  {len(df)} 行 / {n_days} 个交易日")
    with pd.option_context("display.width", 160, "display.max_columns", 30):
        print(df[["dt", "code", "open", "high", "low", "close",
                  "volume", "amount"]].head(3).to_string(index=False))
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="Wind 数据源冒烟测试")
    ap.add_argument("--code", default="sh.600519", help="证券代码，默认 sh.600519（贵州茅台）")
    ap.add_argument("--start", default="2024-01-02", help="起始日期，默认 2024-01-02")
    ap.add_argument("--end", default="2024-01-15", help="结束日期，默认 2024-01-15")
    ap.add_argument("--skip-minute", action="store_true", help="跳过分钟线（更快）")
    args = ap.parse_args()

    # 1) 纯函数部分先跑——即使 Wind 未登录也能验证转换逻辑
    check_code_mapping()

    try:
        check_daily(args.code, args.start, args.end)
        if not args.skip_minute:
            check_minute(args.code, args.start, args.end)
    except ImportError as exc:
        print(f"\n✗ {exc}")
        return 2
    except ConnectionError as exc:
        print(f"\n✗ Wind 连接失败：{exc}")
        print("  → 请先启动并登录 Wind 金融终端，再重跑本脚本。")
        return 3
    except RuntimeError as exc:
        print(f"\n✗ Wind 接口报错：{exc}")
        return 4

    print("\nWind 数据源冒烟测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
