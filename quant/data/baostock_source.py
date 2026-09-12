"""Baostock 数据源适配器（MarketDataSource 实现）。

新增数据源（Tushare/AKShare）时：复制本文件、改 fetch_bars 内部实现即可，
上层（仓储 / 引擎 / 策略）零改动 —— OCP。

适配器的职责正是"吸收数据源的脏格式"：baostock 分钟线的时间戳为 17 位纯数字串
'YYYYMMDDHHMMSSmmm'（含 3 位毫秒），这里统一归一为 'YYYY-MM-DD HH:MM:SS'；
分钟线缺失的 pre_close / tradestatus / isST 由仓储层用日线表回填。
"""
from __future__ import annotations

from contextlib import contextmanager

import baostock as bs
import pandas as pd

from .timeutil import norm_dt

# 日线字段（含估值、换手等附加信息）
_DAILY_FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,turn,"
                 "tradestatus,pctChg,isST,peTTM,psTTM,pcfNcfTTM,pbMRQ")
# 分钟线字段（baostock 分钟频率不提供 preclose/估值/ST 等）
_MIN_FIELDS = "time,code,open,high,low,close,volume,amount,adjustflag"

# freq（本系统口径） -> baostock frequency
_FREQ_MAP = {"1d": "d", "5min": "5", "15min": "15", "30min": "30", "60min": "60"}

# baostock 字段 -> ODS 表字段
_RENAME = {
    "preclose": "pre_close", "pctChg": "pct_chg", "tradestatus": "trade_status",
    "isST": "is_st", "peTTM": "pe_ttm", "psTTM": "ps_ttm",
    "pcfNcfTTM": "pcf_ncf_ttm", "pbMRQ": "pb_mrq",
}
_NUM_COLS = ["open", "high", "low", "close", "pre_close", "volume", "amount",
             "turn", "pct_chg", "pe_ttm", "ps_ttm", "pcf_ncf_ttm", "pb_mrq"]


@contextmanager
def _login():
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(
            f"baostock 登录失败: {lg.error_code} {lg.error_msg}"
        )
    try:
        yield
    finally:
        bs.logout()


class BaostockSource:
    name = "baostock"

    @staticmethod
    def fetch_bars(code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """按频率拉取行情，返回统一列（见 MarketDataSource 协议注释）。

        adjust: baostock 约定 1=后复权 2=前复权 3=不复权（默认前复权）。
        """
        if freq not in _FREQ_MAP:
            raise ValueError(f"不支持的频率: {freq}（可选: {list(_FREQ_MAP)}）")
        minute = freq != "1d"
        fields = _MIN_FIELDS if minute else _DAILY_FIELDS

        with _login():
            rs = bs.query_history_k_data_plus(
                code, fields, start_date=start, end_date=end,
                frequency=_FREQ_MAP[freq], adjustflag=adjust,
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 拉取失败: {rs.error_code} {rs.error_msg}")

        df = pd.DataFrame(rows, columns=fields.split(","))
        df["adjust_flag"] = int(adjust)

        if minute:
            # baostock 分钟线：时间列名为 time，无 preclose/tradestatus/isST
            df = df.rename(columns={"time": "dt"})
            df["dt"] = df["dt"].map(norm_dt)   # '20250102093500000' → '2025-01-02 09:35:00'
            df["trade_date"] = df["dt"].str[:10]
            # 缺失字段给安全默认：可交易、非ST；昨收由仓储关联日线表填充
            df["pre_close"] = 0.0
            df["trade_status"] = 1
            df["is_st"] = 0
            num_cols = ["open", "high", "low", "close", "volume", "amount",
                        "pre_close"]
        else:
            df = df.rename(columns=_RENAME)
            df = df.rename(columns={"date": "dt"})
            df["trade_date"] = df["dt"]
            num_cols = _NUM_COLS

        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        for col in ("trade_status", "is_st"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(
                    1 if col == "trade_status" else 0).astype(int)
        return df

    # ── 向后兼容别名 ─────────────────────────────────────
    @staticmethod
    def fetch_daily(code: str, start: str, end: str,
                    adjust: str = "2") -> pd.DataFrame:
        return BaostockSource.fetch_bars(code, start, end, freq="1d", adjust=adjust)

    @staticmethod
    def fetch_trade_dates(start: str, end: str) -> list[str]:
        """交易日历（用于增量校验）。"""
        with _login():
            rs = bs.query_trade_dates(start_date=start, end_date=end)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        return [r[0] for r in rows if r[1] == "1"]
