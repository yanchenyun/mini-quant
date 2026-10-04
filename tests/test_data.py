"""数据层用例：数据源适配器离线回归与注册表单一来源。"""
from __future__ import annotations

import contextlib
import io
from datetime import date, datetime

import pandas as pd

from quant.data import registry
from quant.data.mysql_repo import TABLES, _aggregate_bars
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

    # 2) 日线：按 wsd 真实形态构造（字段名大写 + trade_status 中文 +
    #    涨跌停价 UP_HGA / DOWN_HGA——各板块幅度不同的权威值）
    out = FakeWindData(
        fields=["OPEN", "HIGH", "LOW", "CLOSE", "PRE_CLOSE", "VOLUME", "AMT",
                "PCT_CHG", "TURN", "TRADE_STATUS", "UP_HGA", "DOWN_HGA"],
        times=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        data=[[10.0, 10.2, 10.1], [10.5, 10.4, 10.3], [9.8, 10.0, 9.9],
              [10.3, 10.1, 9.95], [10.0, 10.3, 10.1], [1000, 1200, 900],
              [10300, 12120, 8955], [0.5, -1.94, -1.49], [1.2, 1.5, 1.1],
              ["交易", "交易", "停牌"],
              [11.0, 11.33, 11.11], [9.0, 9.27, 9.09]],
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
    assert df["upper_limit"].tolist() == [11.0, 11.33, 11.11], \
        "Wind 涨停价（UP_HGA）应重命名为 upper_limit 列"
    assert df["lower_limit"].tolist() == [9.0, 9.27, 9.09], \
        "Wind 跌停价（DOWN_HGA）应重命名为 lower_limit 列"

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
    assert dfm["upper_limit"].eq(0.0).all() and dfm["lower_limit"].eq(0.0).all(), \
        "分钟线涨跌停应为 0（由仓储层 JOIN 日线回填权威值）"
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
    assert sources == ["wind"], sources
    assert freqs[0] == "1d" and "5min" in freqs, freqs
    # 分钟频率已与适配器能力对齐（曾出现"适配器支持而入口不认"的不一致）
    for f in ("15min", "30min", "60min"):
        assert f in freqs, f"registry 应登记分钟频率 {f}: {freqs}"

    # 1) 仓储表名映射由注册表派生（不再是第二份清单）
    assert TABLES == registry.freq_tables(), "仓储表名应与注册表同源"
    for f in registry.FREQS:
        assert registry.get_freq(f.name).table == f.table

    # 1b) 绩效年化基准恒为 252 个交易日——引擎按交易日记录净值快照，
    #     分钟级亦然。若误按"每年 bar 数"（如 5min 用 252×48）年化，
    #     会把 N 个交易日当成 N 个 bar 外推，亏损样本直接算成 -100%。
    from quant.backtest.metrics import compute_metrics
    assert registry.periods_per_year("1d") == 252
    assert registry.periods_per_year("5min") == 252, \
        "分钟频率的年化基准仍应是 252（快照按交易日）"
    assert registry.bars_per_day("1d") == 1
    assert registry.bars_per_day("5min") == 48
    curve = [{"date": f"2024-01-{d:02d}", "equity": 1_000_000 * (0.98 ** i)}
             for i, d in enumerate(range(1, 21))]
    m = compute_metrics(curve, [], periods_per_year=registry.periods_per_year("5min"))
    assert -100 < m["annual_return"] < 0, \
        f"20 个交易日小幅回撤不应年化成 -100%，实际 {m['annual_return']}"

    # 1c) 默认频率是显式常量，不依赖注册表条目顺序
    assert registry.default_freq() == "1d"
    assert registry.is_daily("1d") and not registry.is_daily("5min")

    # 1d) Bar 提供涨跌停价查询（权威值优先，缺失才按幅度估算）
    import quant.core.models as _cm
    assert hasattr(_cm.Bar, "price_limits"), "Bar 应提供涨跌停价查询"

    # 1e) 日线表 DDL 与仓储输出含涨跌停权威价两列（B 方案链路）：
    #     Wind up_hga/down_hga -> upper_limit/lower_limit 列入库，
    #     分钟表无此列、加载时 JOIN 日线回填
    from quant.data.mysql_repo import DDL, _D_COLS, _OUT_COLS
    assert "upper_limit" in DDL and "lower_limit" in DDL, \
        "日线建表 DDL 应含涨跌停两列"
    assert "upper_limit" in _D_COLS and "upper_limit" in _OUT_COLS, \
        "日线读写列与仓储输出列应含涨跌停"

    # 1f) 派生频率（15/30/60min）：不建表不落库，读取时从 5min 聚合。
    #     与源频率同表（数据物理上就在 5min 表），DDL 不重复建
    for name, bpd in (("15min", 16), ("30min", 8), ("60min", 4)):
        spec = registry.get_freq(name)
        assert spec.derived_from == "5min" and spec.bars_per_day == bpd, spec
        assert spec.table == registry.get_freq("5min").table, \
            f"{name} 应与源频率 5min 共用分钟表（派生频率不建表）"
    assert "ods_mi15" not in DDL, "派生频率不应出现在建表 DDL 中"

    # 1g) 派生频率落库必须被拒（写入只会以粗粒度 bar 污染 5min 细粒度数据）
    from quant.config import DBSettings
    from quant.data.mysql_repo import MySQLBarRepo
    try:
        MySQLBarRepo(DBSettings()).save_bars(
            pd.DataFrame({"code": ["sh.600000"], "dt": ["2024-01-02 09:45:00"]}),
            freq="15min")
    except ValueError as e:
        assert "派生频率" in str(e) and "5min" in str(e), e
    else:
        raise AssertionError("save_bars 应拒绝派生频率入库")

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


def _full_day_5min(bars: int = 48) -> pd.DataFrame:
    """构造一个完整交易日的 5 分钟 bar（上午 09:35~11:30、下午 13:05~15:00）。

    行情值用序号填充（open=i / close=i+1 / high=i+2 / low=i-1 / volume=1），
    使聚合断言可以精确到"首根 / 末根 / 极值 / 求和"各口径。
    """
    morning = pd.date_range("2024-01-02 09:35", "2024-01-02 11:30", freq="5min")
    afternoon = pd.date_range("2024-01-02 13:05", "2024-01-02 15:00", freq="5min")
    times = list(morning) + list(afternoon)
    assert len(times) == bars
    idx = range(len(times))
    return pd.DataFrame({
        "code": "sh.600000",
        "dt": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
        "trade_date": "2024-01-02", "date": "2024-01-02",
        "open": list(idx), "high": [i + 2 for i in idx],
        "low": [i - 1 for i in idx], "close": [i + 1 for i in idx],
        "pre_close": 100.0, "volume": [1.0] * len(times),
        "amount": [1.0] * len(times), "trade_status": 1, "is_st": 0,
        "upper_limit": 110.0, "lower_limit": 90.0,
    })


def test_resample_aggregation() -> None:
    """派生频率重采样：钟表槽位切分 + OHLCV 聚合口径（离线纯函数）。

    回归锁定三个关键行为：60 分钟切分是 09:30-10:30 / 10:30-11:30 /
    13:00-14:00 / 14:00-15:00（A 股惯例，钟表整点切分会把上午错切成
    30+60+30 三段）；午休两侧的 bar 不得串槽；缺 bar 时槽边界不漂移
    （钟表法只认时刻，序号法会把 09:45 误归首槽）。
    """
    src = _full_day_5min()

    # 1) 60min：48 根聚 4 根，槽位为 A 股惯例的时段边界
    out = _aggregate_bars(src, registry.get_freq("60min"))
    assert out["dt"].tolist() == ["2024-01-02 10:30:00",
                                  "2024-01-02 11:30:00",
                                  "2024-01-02 14:00:00",
                                  "2024-01-02 15:00:00"], out["dt"].tolist()
    first = out.iloc[0]
    assert first["open"] == 0 and first["close"] == 12, "open 首根 / close 末根"
    assert first["high"] == 13 and first["low"] == -1, "high/low 取槽内极值"
    assert first["volume"] == 12, "volume 为槽内求和（当根口径）"
    assert first["pre_close"] == 100.0 and first["upper_limit"] == 110.0, \
        "日线级字段取槽内首根"
    assert out["trade_date"].eq("2024-01-02").all()
    assert "date" in out.columns, "输出列应与仓储统一列口径一致"

    # 2) 15min：48 根聚 16 根；午休两侧不串槽（上午末槽 11:30、下午首槽 13:15）
    out15 = _aggregate_bars(src, registry.get_freq("15min"))
    assert len(out15) == 16, len(out15)
    dts15 = out15["dt"].tolist()
    assert dts15[7] == "2024-01-02 11:30:00" and dts15[8] == "2024-01-02 13:15:00", \
        "午休两侧的 bar 不得跨槽合并"
    assert dts15[0] == "2024-01-02 09:45:00" and dts15[-1] == "2024-01-02 15:00:00"
    assert out15["volume"].eq(3).all(), "完整数据下每 15 分钟槽恰含 3 根"

    # 3) 缺 bar 鲁棒性：抽掉 09:40 后首槽只含 09:35/09:45，dt 仍为 09:45，
    #    09:50 不被误吸入首槽（钟表槽位 vs 当日序号的关键差异）
    gap = src.drop(index=1).reset_index(drop=True)
    out_gap = _aggregate_bars(gap, registry.get_freq("15min"))
    assert out_gap.iloc[0]["dt"] == "2024-01-02 09:45:00"
    assert out_gap.iloc[0]["volume"] == 2
    assert out_gap.iloc[1]["dt"] == "2024-01-02 10:00:00", \
        "09:50 应归属第二槽而非填补首槽"
    print("派生频率重采样用例通过 ✓")
