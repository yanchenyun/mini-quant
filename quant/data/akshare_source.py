"""AKShare 数据源适配器（MarketDataSource 实现）。

AKShare 免费开源、无需注册，底层聚合东方财富等多个数据源。
适配器职责：吸收 AKShare 的中文列名、代码格式差异、分钟数据分段限制，
对上输出与 BaostockSource 完全一致的统一列 DataFrame —— OCP。

与 Baostock 的关键差异：
- 代码格式：AKShare 用纯数字 "600000"，本系统用 "sh.600000"
- 列名：AKShare 返回中文列名（"日期"/"开盘"…），需映射为英文
- 复权：AKShare 用 "qfq"/"hfq"/""，本系统用 "2"/"1"/"3"
- 分钟数据：AKShare 有长度限制（近期数据），长周期需分段请求拼接
- pre_close / trade_status / is_st：AKShare 不直接提供，需额外处理
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

from .timeutil import norm_dt

# ── 代码格式转换 ──────────────────────────────────────────────────────────
def _to_ak_symbol(code: str) -> str:
    """本系统 'sh.600000' → AKShare '600000'（纯数字）。"""
    return code.split(".")[-1]


def _from_ak_symbol(code: str) -> str:
    """AKShare '600000' → 本系统 'sh.600000'（带交易所前缀）。"""
    # 6/9 开头为沪市，其余为深市
    if code.startswith("6") or code.startswith("9"):
        return f"sh.{code}"
    return f"sz.{code}"


# ── 复权标记转换 ──────────────────────────────────────────────────────────
# 本系统：1=后复权 2=前复权 3=不复权
# AKShare：qfq=前复权 hfq=后复权 ""=不复权
_ADJUST_MAP = {"1": "hfq", "2": "qfq", "3": ""}

# ── 频率映射 ──────────────────────────────────────────────────────────────
# AKShare 分钟线 period 参数（分钟数）
_MIN_PERIOD_MAP = {"5min": "5", "15min": "15", "30min": "30", "60min": "60"}

# ── 中文列名 → 英文列名映射 ──────────────────────────────────────────────
_DAILY_RENAME = {
    "日期": "dt", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "volume",
    "成交额": "amount", "振幅": "amplitude", "涨跌幅": "pct_chg",
    "涨跌额": "change", "换手率": "turn",
}
_MIN_RENAME = {
    "时间": "dt", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "volume",
    "成交额": "amount", "最新价": "latest",
}


def _retry(fn, retries: int = 3, delay: float = 1.0):
    """简单重试包装器（AKShare 偶有网络抖动）。"""
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(delay * (i + 1))
    raise last_err


class AkshareSource:
    """AKShare 数据源适配器（MarketDataSource 实现）。"""
    name = "akshare"

    @staticmethod
    def fetch_bars(code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """按频率拉取行情，返回统一列 DataFrame（与 BaostockSource 输出格式一致）。

        Args:
            code: 证券代码，如 sh.600000 / sz.000001。
            start: 起始日期 'YYYY-MM-DD'。
            end: 结束日期 'YYYY-MM-DD'。
            freq: '1d' 日线；'5min' 5分钟线（可扩展 15/30/60min）。
            adjust: 复权标记，1=后复权 2=前复权 3=不复权（默认前复权）。
        """
        symbol = _to_ak_symbol(code)
        ak_adjust = _ADJUST_MAP.get(adjust, "qfq")

        if freq == "1d":
            df = AkshareSource._fetch_daily(symbol, start, end, ak_adjust)
        else:
            df = AkshareSource._fetch_minute(symbol, start, end, freq, ak_adjust)

        df["code"] = code
        df["adjust_flag"] = int(adjust)
        return df

    # ── 日线 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _fetch_daily(symbol: str, start: str, end: str,
                     ak_adjust: str) -> pd.DataFrame:
        """日线：stock_zh_a_hist，返回统一列。"""
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")

        df = _retry(lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_fmt, end_date=end_fmt,
            adjust=ak_adjust,
        ))
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.rename(columns=_DAILY_RENAME)

        # dt 已是 'YYYY-MM-DD' 格式字符串
        df["trade_date"] = df["dt"]

        # pre_close：用 shift(1) 从 close 计算（复权后可能不精确，但可用）
        df["pre_close"] = df["close"].shift(1).fillna(0.0)

        # trade_status / is_st：AKShare 日线不直接提供
        # 安全默认：可交易、非 ST（与 Baostock 分钟线处理一致）
        df["trade_status"] = 1
        df["is_st"] = 0

        # 数值列转换
        num_cols = ["open", "high", "low", "close", "pre_close",
                    "volume", "amount", "pct_chg", "turn"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        for col in ("trade_status", "is_st"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(
                1 if col == "trade_status" else 0).astype(int)

        return df

    # ── 分钟线 ────────────────────────────────────────────────────────────
    @staticmethod
    def _fetch_minute(symbol: str, start: str, end: str,
                      freq: str, ak_adjust: str) -> pd.DataFrame:
        """分钟线：stock_zh_a_hist_min_em，支持分段请求拼接。

        AKShare 分钟数据有长度限制（通常只保留近期数据），
        长周期需按日分段请求再拼接。
        """
        if freq not in _MIN_PERIOD_MAP:
            raise ValueError(f"不支持的频率: {freq}（可选: {list(_MIN_PERIOD_MAP)}）")
        period = _MIN_PERIOD_MAP[freq]

        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        # 按 30 天分段请求（AKShare 分钟数据单次约支持 1-2 个月）
        segment_days = 30
        segments = []
        seg_start = start_dt

        while seg_start <= end_dt:
            seg_end = min(seg_start + timedelta(days=segment_days - 1), end_dt)
            s_str = seg_start.strftime("%Y-%m-%d %H:%M:%S")
            e_str = seg_end.strftime("%Y-%m-%d %H:%M:%S")

            try:
                seg_df = _retry(lambda: ak.stock_zh_a_hist_min_em(
                    symbol=symbol, period=period,
                    start_date=s_str, end_date=e_str,
                    adjust=ak_adjust,
                ))
                if seg_df is not None and not seg_df.empty:
                    segments.append(seg_df)
            except Exception:
                # 某段无数据则跳过（历史太早可能无分钟数据）
                pass

            seg_start = seg_end + timedelta(days=1)

        if not segments:
            return pd.DataFrame()

        df = pd.concat(segments, ignore_index=True)
        df = df.rename(columns=_MIN_RENAME)

        # 时间戳归一（AKShare 分钟时间通常为 'YYYY-MM-DD HH:MM:SS'）
        df["dt"] = df["dt"].map(norm_dt)
        df["trade_date"] = df["dt"].str[:10]

        # pre_close：分钟线不提供，由仓储层 LEFT JOIN 日线表回填
        df["pre_close"] = 0.0
        # trade_status / is_st：同上，安全默认
        df["trade_status"] = 1
        df["is_st"] = 0

        # 数值列转换
        num_cols = ["open", "high", "low", "close", "volume", "amount", "pre_close"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        for col in ("trade_status", "is_st"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(
                1 if col == "trade_status" else 0).astype(int)

        # 去重 + 按时间排序
        df = df.drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return df
