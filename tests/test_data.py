"""数据层用例：数据源适配器离线回归与注册表单一来源。"""
from __future__ import annotations

import contextlib
import io
from datetime import date, datetime

import pandas as pd

from quant.data import registry
from quant.data.mysql_repo import TABLES
from quant.data.wind_source import (_from_wind_code, _finalize, _to_wind_code,
                                    _winddata_to_frame)


class FakeWindData:
    """模拟 WindPy 的 WindData 结构：Data 按字段分组（每个字段一条时间序列）。"""

    def __init__(self, fields, times, data, error_code=0):
        self.ErrorCode = error_code
        self.Fields = fields
        self.Times = times
        self.Data = data


def test_wind_source() -> None:
    """Wind 适配器离线用例：代码转换 / 组装 / 归一 / 出口校验（不需要 Wind 终端）。

    回归重点（均为实测踩过的坑，对应 wind_source.py 中的注释）：
    1. w.wsd 返回的字段名是大写（OPEN / AMT / TRADE_STATUS）；若不做
       小写归一，后续按小写字段名取列会全部落空 → 静默产出"缺行情列"的数据；
    2. trade_status 返回的是中文描述（'交易' / '停牌'）而非数字；用
       == 1 判断会把所有交易日误判为停牌 → 回测零成交且不报任何错；
    3. 出口必须强校验必需列，防止上述两类问题日后再次静默通过。
    """
    # 1) 代码格式双向转换（本系统 sh.600519 <-> Wind 600519.SH）
    assert _to_wind_code("sh.600519") == "600519.SH"
    assert _to_wind_code("sz.000001") == "000001.SZ"
    assert _to_wind_code("bj.430047") == "430047.BJ"
    assert _from_wind_code("600519.SH") == "sh.600519"

    # 1b) 指数类代码：本系统 si.801050 <-> Wind 801050.SI；
    # Wind 原生格式（数字在前）允许直接输入并原样通过
    assert _to_wind_code("si.801050") == "801050.SI"
    assert _to_wind_code("801050.SI") == "801050.SI"
    assert _to_wind_code("801050.si") == "801050.SI"      # 后缀大小写不敏感
    assert _from_wind_code("801050.SI") == "si.801050"

    # 2) 日线：按 wsd 真实形态构造（字段名大写 + trade_status 中文）
    out = FakeWindData(
        fields=["OPEN", "HIGH", "LOW", "CLOSE", "PRE_CLOSE", "VOLUME", "AMT",
                "PCT_CHG", "TURN", "TRADE_STATUS"],
        times=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        data=[[10.0, 10.2, 10.1], [10.5, 10.4, 10.3], [9.8, 10.0, 9.9],
              [10.3, 10.1, 9.95], [10.0, 10.3, 10.1], [1000, 1200, 900],
              [10300, 12120, 8955], [0.5, -1.94, -1.49], [1.2, 1.5, 1.1],
              ["交易", "交易", "停牌"]],
    )
    df = _finalize(_winddata_to_frame(out, "600519.SH"),
                   "sh.600519", "1d", "2", minute=False)
    assert df["dt"].tolist() == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert df["code"].eq("sh.600519").all(), "code 列应回填为本系统格式"
    assert df["trade_date"].eq(df["dt"]).all(), "日线 trade_date 应等于 dt"
    assert "amount" in df.columns and "AMT" not in df.columns, \
        "大写字段名（AMT）应归一为小写 amount"
    assert df["trade_status"].tolist() == [1, 1, 0], \
        "中文状态应归一：'交易'->1、'停牌'->0（直接 ==1 会全判为停牌）"
    assert df["close"].tolist() == [10.3, 10.1, 9.95]
    assert df["is_st"].eq(0).all() and df["adjust_flag"].eq(2).all()

    # 3) 分钟线：按 wsi 真实形态构造（字段名小写、请求 amt 返回 amount）
    out_m = FakeWindData(
        fields=["open", "high", "low", "close", "volume", "amount"],
        times=[datetime(2024, 1, 2, 9, 35), datetime(2024, 1, 2, 9, 40)],
        data=[[10.0, 10.05], [10.1, 10.12], [9.95, 10.0],
              [10.05, 10.1], [100, 200], [1005, 2020]],
    )
    dfm = _finalize(_winddata_to_frame(out_m, "600519.SH"),
                    "sh.600519", "5min", "2", minute=True)
    assert dfm["dt"].tolist() == ["2024-01-02 09:35:00", "2024-01-02 09:40:00"]
    assert dfm["trade_date"].tolist() == ["2024-01-02", "2024-01-02"]
    assert dfm["amount"].tolist() == [1005.0, 2020.0]
    assert dfm["pre_close"].eq(0.0).all(), "分钟线昨收应为 0（由仓储层 JOIN 日线回填）"
    assert dfm["trade_status"].eq(1).all(), "分钟线无该字段 → 安全默认可交易"

    # 4) 出口校验：缺必需列必须显式报错，而不是静默返回缺列数据
    try:
        _finalize(_winddata_to_frame(
            FakeWindData(["CLOSE", "VOLUME"], [date(2024, 1, 2)], [[10.0], [100]]),
            "600519.SH"), "sh.600519", "1d", "2", minute=False)
    except RuntimeError as e:
        assert "缺少必需列" in str(e), f"报错信息应指明缺列，实际: {e}"
    else:
        raise AssertionError("缺必需列时应抛 RuntimeError，而不是静默返回")

    print("Wind 适配器用例通过 ✓")


def test_data_registry() -> None:
    """数据源 / 频率清单单一来源：CLI 选项、Web 校验与仓储表名同源派生。"""
    from fastapi import HTTPException

    from quant.app.cli import build_parser
    from quant.webapp.server import backtest as backtest_endpoint

    sources, freqs = registry.available_sources(), registry.available_freqs()
    assert sources == ["baostock", "akshare", "wind"], sources
    assert freqs[0] == "1d" and "5min" in freqs, freqs

    # 1) 仓储表名映射由注册表派生（不再是第二份清单）
    assert TABLES == registry.freq_tables(), "仓储表名应与注册表同源"
    for f in registry.FREQS:
        assert registry.get_freq(f.name).table == f.table

    # 2) CLI 的 choices 由注册表派生：非法取值时 argparse 列出全部合法值
    parser = build_parser()
    cases = ((["ingest", "--code", "sh.600000", "--start", "2020-01-01",
               "--source", "nope"], sources),
             (["backtest", "--code", "sh.600000", "--start", "2020-01-01",
               "--strategy", "double_ma", "--freq", "3min"], freqs))
    for argv, expected in cases:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                parser.parse_args(argv)
            except SystemExit:
                pass
        msg = err.getvalue()
        assert "invalid choice" in msg, msg
        for name in expected:
            assert name in msg, f"错误提示应列出注册表取值 {name}: {msg}"

    # 3) Web 端点的频率校验与注册表同源
    class FakeRequest:
        def __init__(self, q: dict):
            self.query_params = q

    try:
        backtest_endpoint(FakeRequest({"code": "sh.600000", "start": "2020-01-01",
                                       "strategy": "double_ma", "freq": "3min"}))
    except HTTPException as e:
        assert e.status_code == 400, e
        assert "1d" in str(e.detail) and "5min" in str(e.detail), e.detail
    else:
        raise AssertionError("非法 freq 应返回 400")

    # 4) 未知数据源 / 频率在注册表层显式报错并列出可选值
    for bad, fn in (("nope", registry.get_source), ("3min", registry.get_freq)):
        try:
            fn(bad)
        except ValueError as e:
            assert bad in str(e), e
        else:
            raise AssertionError(f"{bad} 应抛 ValueError")
    print("数据源/频率注册表用例通过 ✓")


def test_akshare_segment_errors() -> None:
    """AKShare 分钟分段：全部失败显式抛错（含原因），部分失败保留数据并告警。

    用 mock 替换底层接口，不发起任何网络请求；重试包装临时替换为单次
    尝试以缩短用例耗时（重试本身的正确性不是本用例的关注点）。
    """
    try:
        from quant.data import akshare_source as aks
    except Exception as e:      # akshare 未安装的环境直接跳过
        print(f"akshare 不可导入，跳过分段异常用例: {e}")
        return

    orig_fetch = aks.ak.stock_zh_a_hist_min_em
    orig_retry = aks._retry
    aks._retry = lambda fn, *a, **k: fn()      # 单次尝试，绕开退避等待

    calls = {"n": 0}

    def flaky_fetch(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("网络中断")
        return pd.DataFrame({
            "时间": ["2024-01-31 09:35:00", "2024-01-31 09:40:00"],
            "开盘": [10.0, 10.1], "收盘": [10.05, 10.12],
            "最高": [10.1, 10.15], "最低": [9.98, 10.0],
            "成交量": [100, 200], "成交额": [1005, 2024],
        })

    def boom(**kw):
        raise ConnectionError("网络中断")

    try:
        # 1) 部分失败：首段抛错、次段成功 → 保留成功数据 + stderr 告警留痕
        aks.ak.stock_zh_a_hist_min_em = flaky_fetch
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            df = aks.AkshareSource._fetch_minute("600000", "2024-01-01",
                                                 "2024-02-01", "5min", "qfq")
        assert len(df) == 2, f"成功分段的数据应保留，实际 {len(df)} 行"
        assert "分段拉取失败" in err.getvalue(), \
            f"部分失败应在 stderr 留痕: {err.getvalue()}"
        assert "网络中断" in err.getvalue()

        # 2) 全部失败：聚合各段原因抛 RuntimeError，不再静默返回空表
        aks.ak.stock_zh_a_hist_min_em = boom
        try:
            aks.AkshareSource._fetch_minute("600000", "2024-01-01",
                                             "2024-02-01", "5min", "qfq")
        except RuntimeError as e:
            assert "全部分段" in str(e) and "网络中断" in str(e), e
        else:
            raise AssertionError("全部分段失败应抛 RuntimeError 而非返回空表")
    finally:
        aks.ak.stock_zh_a_hist_min_em = orig_fetch
        aks._retry = orig_retry
    print("AKShare 分段异常用例通过 ✓")
