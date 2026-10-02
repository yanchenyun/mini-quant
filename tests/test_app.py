"""应用层用例：CLI 参数契约（必填项显式指定，禁止隐式默认）。"""
from __future__ import annotations

import contextlib
import io
import sys


def test_cli_required_args() -> None:
    """CLI 三项强制显式指定：缺 --source / --factors / --strategy 应退出。

    数据源 / 策略 / 因子不设默认值——选错会静默产出错误结论。这里按
    argparse 契约断言：缺必填参数时以退出码 2 结束（错误信息在 stderr）。
    """
    from quant.app import cli

    cases = [
        ["ingest", "--code", "sh.600000", "--start", "2020-01-01"],
        ["compute-factors", "--code", "sh.600000", "--start", "2024-01-01"],
        ["backtest", "--code", "sh.600000", "--start", "2021-01-01"],
    ]
    saved = sys.argv
    try:
        for argv in cases:
            sys.argv = ["mini-quant", *argv]
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    cli.main()
                except SystemExit as e:
                    assert e.code == 2, (argv, e.code)
                else:
                    raise AssertionError(f"缺少必填参数应退出: {argv}")
            assert "--" in err.getvalue(), (argv, err.getvalue())
    finally:
        sys.argv = saved
    print("CLI 必填参数用例通过 ✓")
