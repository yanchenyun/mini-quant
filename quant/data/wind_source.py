"""Wind 数据源适配器（MarketDataSource 实现）。

WindPy 是万得金融终端配套的 Python 接口，数据质量与覆盖度优于免费源，
但它有三个"脏差异"，正是本适配器要吸收的部分：

1. 运行前置条件：必须先安装并登录 Wind 金融终端。WindPy 随终端分发
   （典型路径 ``C:\\Program Files (x86)\\Wind\\Wind.NET.Client\\WindNET\\x64``），
   不能通过 pip 安装。因此本模块对 WindPy 一律延迟导入——没装终端的
   机器照样能 import 本文件（只是调用 ``fetch_bars`` 时才抛 ImportError）。
2. 代码格式：Wind 用 ``600519.SH``（数字.大写交易所后缀），本系统用
   ``sh.600519``（小写交易所前缀.数字），需要双向转换。
3. 返回结构：``WindData`` 把结果按"字段"分组存放——单标的查询时
   ``.Data[i]`` 是第 i 个字段的时间序列（不是行记录）；时间以
   ``datetime.date`` / ``datetime.datetime`` 对象给出（不是字符串），
   需要自行组装成 DataFrame。

与 Baostock / AKShare 的口径对齐（上层完全无感知）：

============  ==========================================  ==================
维度          本系统口径                                    Wind 口径
============  ==========================================  ==================
复权          1=后复权 2=前复权 3=不复权                    ``PriceAdj=B/F/(空)``
频率          ``1d`` / ``5min`` / ``15/30/60min``          ``wsd`` / ``wsi`` + ``BarSize``
代码          ``sh.600519``                                ``600519.SH``
输出列        code/dt/trade_date/open/high/low/close/      （组装后对齐左列）
              pre_close/volume/amount/trade_status/is_st
============  ==========================================  ==================

分钟线的 pre_close / trade_status / is_st：Wind 分钟序列不提供，
按既有约定给安全默认（可交易、非 ST、昨收 0），由仓储层 LEFT JOIN 日线表
回填真实昨收——与 BaostockSource / AkshareSource 处理完全一致。

字段降级：Wind 可请求的字段集合随终端版本与账号权限变化（例如指数没有
换手率 ``turn``）。因此把日线字段分成"全量"与"核心"两组：全量请求失败时
自动降级到核心字段重试，保证"宁可少几个附加字段，也不能拉不到行情"。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, datetime

import pandas as pd

from .timeutil import norm_dt

# ── 代码格式转换 ──────────────────────────────────────────────────────────
# 本系统交易所前缀（小写） -> Wind 交易所后缀（大写）
_SUFFIX_MAP = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def _to_wind_code(code: str) -> str:
    """本系统 'sh.600519' → Wind '600519.SH'。

    Args:
        code: 本系统证券代码，形如 ``sh.600519`` / ``sz.000001`` / ``bj.430047``。

    Returns:
        Wind 代码，形如 ``600519.SH``。

    Raises:
        ValueError: 代码格式非法（缺交易所前缀或未知前缀）。
    """
    parts = code.strip().split(".")
    if len(parts) != 2:
        raise ValueError(f"无法识别的证券代码: {code!r}（应形如 sh.600519）")
    prefix, number = parts[0].lower(), parts[1]
    suffix = _SUFFIX_MAP.get(prefix)
    if suffix is None:
        raise ValueError(f"未知交易所前缀: {prefix!r}（支持 {list(_SUFFIX_MAP)}）")
    return f"{number}.{suffix}"


def _from_wind_code(wind_code: str) -> str:
    """Wind '600519.SH' → 本系统 'sh.600519'（反向转换，供结果回填 code 列）。"""
    parts = wind_code.strip().split(".")
    if len(parts) != 2:
        return wind_code
    number, suffix = parts[0], parts[1].lower()
    return f"{suffix}.{number}"


# ── 复权标记转换 ──────────────────────────────────────────────────────────
# 本系统：1=后复权 2=前复权 3=不复权
# Wind ：B=后复权 F=前复权 空=不复权
_ADJUST_MAP = {"1": "B", "2": "F", "3": ""}

# ── 频率映射 ──────────────────────────────────────────────────────────────
# 分钟频率 -> Wind wsi 的 BarSize（单位：分钟）
_FREQ_MAP = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}

# ── 字段定义 ──────────────────────────────────────────────────────────────
# 日线"全量"字段（含换手率、涨跌幅、交易状态等附加信息）
_DAILY_FIELDS = ["open", "high", "low", "close", "pre_close", "volume",
                 "amt", "pct_chg", "turn", "trade_status"]
# 日线"核心"字段（全量请求失败时的降级集合，任何品种都应可用）
_DAILY_CORE = ["open", "high", "low", "close", "pre_close", "volume", "amt"]

# 分钟线字段（Wind 分钟序列不提供昨收 / 估值 / ST）
_MIN_FIELDS = ["open", "high", "low", "close", "volume", "amt"]
_MIN_CORE = ["open", "high", "low", "close", "volume"]

# Wind 字段 -> 统一列名
_RENAME = {"amt": "amount"}

# 需要转数值的列（缺失的自动补 0.0）
_NUM_COLS = ["open", "high", "low", "close", "pre_close",
             "volume", "amount", "pct_chg", "turn"]

# 流水线出口的必需列：全量路径与降级路径都必须具备，出口处强校验（防静默缺列）
_REQUIRED_COLS = ["open", "high", "low", "close", "volume"]


# ══════════════════════════════════════════════════════════════════════════
# 连接管理（延迟导入 + 幂等复用）
# ══════════════════════════════════════════════════════════════════════════
_lock = threading.Lock()
_started = False   # 模块级连接状态：WindPy 单进程只需 start 一次


def _load_wind():
    """延迟导入 WindPy 模块对象 ``w``（未安装 Wind 终端时给出明确指引）。"""
    try:
        from WindPy import w
    except ImportError as exc:   # pragma: no cover - 取决于本机是否装 Wind
        raise ImportError(
            "未找到 WindPy。WindPy 随 Wind 金融终端分发、无法用 pip 安装；"
            "请先安装并登录 Wind 金融终端，或改用 --source baostock / akshare。"
        ) from exc
    return w


@contextmanager
def _wind_session(wait_time: int = 120):
    """确保 WindPy 已连接，yield 出模块级 ``w``。

    幂等：已连接时直接复用（``w.start()`` 本身也不重复启动，但显式判断可
    避免无谓的 120s 等待）。线程安全：用模块锁包住首次启动。

    刻意不在此处 stop——WindPy 在进程退出时自动 stop，反复 start/stop
    只会拖慢多标的批量抓取。

    Args:
        wait_time: ``w.start()`` 的命令超时时间（秒），首次连接 Wind 终端
            可能需要较久。
    """
    global _started
    w = _load_wind()
    if not _started or not w.isconnected():
        with _lock:
            # 双检：并发进入时只让一个线程真正启动
            if not _started or not w.isconnected():
                res = w.start(waitTime=wait_time)
                if res.ErrorCode != 0:
                    detail = res.Data if isinstance(res.Data, list) else [res.Data]
                    raise ConnectionError(
                        f"Wind 连接失败（ErrorCode={res.ErrorCode}）: {detail}。"
                        "请确认：① Wind 金融终端已启动并登录；"
                        "② 当前账号具备 Python API 权限。"
                    )
                _started = True
    yield w


# ══════════════════════════════════════════════════════════════════════════
# WindData -> DataFrame 组装
# ══════════════════════════════════════════════════════════════════════════
def _to_dt_str(v) -> str:
    """Wind 时间元素 → 'YYYY-MM-DD'（日线）/ 'YYYY-MM-DD HH:MM:SS'（分钟）。

    WindPy 的 ``.Times`` 元素类型随接口而异：``wsd`` / ``tdays`` 为
    ``datetime.date``，``wsi`` 为 ``datetime.datetime``（含时分秒）。
    ``datetime`` 是 ``date`` 的子类，故必须先判 ``datetime``。
    """
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    # 兜底：字符串形态交给 norm_dt（兼容 '20240102' / '2024-01-02 09:35:00' 等）
    return norm_dt(str(v))


def _winddata_to_frame(out, code: str) -> pd.DataFrame:
    """把 WindData 组装成 'code + dt + 各字段列' 的 DataFrame。

    单标的查询时 Wind 的 ``.Data`` 按字段分组（``.Data[i]`` = 第 i 个字段的
    时间序列），故这里按 ``.Fields`` 的顺序逐列取回；长度不齐时按 ``.Times``
    对齐截断/补齐，避免 DataFrame 构造失败。

    注意字段名大小写：Wind 返回的 ``.Fields`` 是大写（请求 ``'open'``
    实际回 ``'OPEN'``，``'trade_status'`` 回 ``'TRADE_STATUS'``）。若直接拿它
    当列名，后续按小写字段名取值会全部落空——这里统一 ``lower()`` 归一，
    是小写口径与 Wind 大写口径之间的关键接缝。
    """
    times = list(out.Times)
    fields = list(out.Fields)
    data: dict[str, list] = {}

    for i in range(min(len(fields), len(out.Data))):
        name = str(fields[i]).strip().lower()   # OPEN -> open, TRADE_STATUS -> trade_status
        col = list(out.Data[i])
        if len(col) != len(times):
            # 防御：接口偶发长度不一致（补齐 None / 截断），保证可构造
            col = (col + [None] * len(times))[:len(times)]
        data[name] = col

    df = pd.DataFrame(data)
    df.insert(0, "dt", [_to_dt_str(t) for t in times])
    df.insert(0, "code", _from_wind_code(code))
    return df


def _finalize(df: pd.DataFrame, code: str, freq: str, adjust: str,
              minute: bool) -> pd.DataFrame:
    """统一列归一：重命名 → 补缺失列 → 数值化 → 类型对齐（与 Baostock 口径一致）。"""
    if df.empty:
        return df

    df = df.rename(columns=_RENAME)
    df["code"] = code
    df["adjust_flag"] = int(adjust)

    # trade_date：分钟线取 dt 的日期部分，日线即 dt 本身
    df["trade_date"] = df["dt"].str[:10]
    df["date"] = df["trade_date"]     # 向后兼容别名（与 AkshareSource 输出口径一致）

    if minute:
        # Wind 分钟序列不含昨收；由仓储层 JOIN 日线表回填
        df["pre_close"] = 0.0

    # trade_status：Wind 返回的是中文描述（'交易' / '停牌'，经实测确认），
    # 不是数字！本系统口径为 1=可交易 / 0=不可交易，故按"是否含停牌"归一。
    # 采用 fail-open 判据：不含"停牌"（含未知值、空值）一律视为可交易——
    # 万一 Wind 调整该字段的取值文案，宁可少标记一天停牌，也不能把整个回测
    # 静默堵死（全仓判为停牌将导致零成交，且不报任何错）。
    if "trade_status" in df.columns:
        desc = df["trade_status"].astype(str)
        df["trade_status"] = (~desc.str.contains("停牌", na=False)).astype(int)
    else:
        df["trade_status"] = 1        # 字段不可用（降级/分钟线）→ 安全默认：可交易

    # is_st：Wind 未取该字段，给安全默认（与 AkshareSource 处理一致）；
    # 仓储层如需精确 ST 标记，可在日线表侧统一补充
    df["is_st"] = 0

    for col in _NUM_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["trade_status"] = df["trade_status"].astype(int)
    df["is_st"] = df["is_st"].astype(int)

    # 出口校验：宁可显式报错，也不要产出"缺行情列"的坏数据静默入库。
    # （Wind 返回字段名的大小写/别名差异曾导致过这类静默错误，故在此设卡。）
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"Wind 返回数据缺少必需列 {missing}（实际列: {list(df.columns)}）。"
            "通常是 WindPy 版本字段命名变化所致，请核对 _DAILY_FIELDS / _MIN_FIELDS。"
        )

    return df


def _options(adjust: str, bar_size: int | None = None) -> str:
    """拼 Wind options 字符串，如 ``'BarSize=5;PriceAdj=F'``。"""
    opts: list[str] = []
    if bar_size is not None:
        opts.append(f"BarSize={bar_size}")
    adj = _ADJUST_MAP.get(adjust, "F")
    if adj:
        opts.append(f"PriceAdj={adj}")
    return ";".join(opts)


# ══════════════════════════════════════════════════════════════════════════
# 适配器主体
# ══════════════════════════════════════════════════════════════════════════
class WindSource:
    """Wind 数据源适配器（MarketDataSource 实现）。

    使用前提：本机已安装并登录 Wind 金融终端（WindPy 随终端分发）。
    """

    name = "wind"

    @staticmethod
    def fetch_bars(code: str, start: str, end: str,
                   freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        """按频率拉取行情，返回统一列 DataFrame（与 BaostockSource 输出格式一致）。

        Args:
            code: 本系统证券代码，如 ``sh.600519``。
            start: 起始日期 ``'YYYY-MM-DD'``。
            end: 结束日期 ``'YYYY-MM-DD'``。
            freq: ``'1d'`` 日线；``'5min'/'15min'/'30min'/'60min'`` 分钟线。
            adjust: 复权标记，1=后复权 2=前复权 3=不复权（默认前复权）。

        Returns:
            统一列 DataFrame：``code/dt/trade_date/date/open/high/low/close/
            pre_close/volume/amount/trade_status/is_st/adjust_flag``（附加列
            视 Wind 字段可用性，缺失则省略）。

        Raises:
            ImportError: 本机未安装 WindPy（Wind 终端）。
            ConnectionError: Wind 终端未登录或无 Python API 权限。
            RuntimeError: Wind 接口返回错误码（如代码不存在、无数据权限）。
        """
        wind_code = _to_wind_code(code)

        with _wind_session() as w:
            if freq == "1d":
                df = WindSource._fetch_daily(w, wind_code, start, end, adjust)
                minute = False
            else:
                df = WindSource._fetch_minute(w, wind_code, start, end, freq, adjust)
                minute = True

        return _finalize(df, code, freq, adjust, minute)

    # ── 日线：w.wsd（日期序列）────────────────────────────────────────────
    @staticmethod
    def _fetch_daily(w, wind_code: str, start: str, end: str,
                     adjust: str) -> pd.DataFrame:
        """日线：先请求全量字段，失败降级到核心字段（如指数无 ``turn``）。"""
        opts = _options(adjust)
        out = w.wsd(wind_code, ",".join(_DAILY_FIELDS), start, end, opts)
        if out.ErrorCode != 0:
            # 降级：终端版本 / 账号权限可能不支持部分附加字段
            out = w.wsd(wind_code, ",".join(_DAILY_CORE), start, end, opts)
        if out.ErrorCode != 0:
            raise RuntimeError(
                f"Wind wsd 拉取失败（{wind_code} {start}~{end}）: "
                f"ErrorCode={out.ErrorCode} {out.Data}"
            )
        return _winddata_to_frame(out, wind_code)

    # ── 分钟线：w.wsi（日内序列 + BarSize）───────────────────────────────
    @staticmethod
    def _fetch_minute(w, wind_code: str, start: str, end: str,
                      freq: str, adjust: str) -> pd.DataFrame:
        """分钟线：``w.wsi`` + ``BarSize``；时间区间补足到整日避免漏 bar。"""
        if freq not in _FREQ_MAP:
            raise ValueError(f"不支持的频率: {freq}（可选: {list(_FREQ_MAP)}）")

        begin = f"{start} 00:00:00"      # 补足到当日零点，避免漏掉早盘首根
        finish = f"{end} 23:59:59"       # 补足到当日最后一秒
        opts = _options(adjust, bar_size=_FREQ_MAP[freq])

        out = w.wsi(wind_code, ",".join(_MIN_FIELDS), begin, finish, opts)
        if out.ErrorCode != 0:
            out = w.wsi(wind_code, ",".join(_MIN_CORE), begin, finish, opts)
        if out.ErrorCode != 0:
            raise RuntimeError(
                f"Wind wsi 拉取失败（{wind_code} {begin}~{finish}）: "
                f"ErrorCode={out.ErrorCode} {out.Data}"
            )
        return _winddata_to_frame(out, wind_code)

    # ── 交易日历：w.tdays ────────────────────────────────────────────────
    @staticmethod
    def fetch_trade_dates(start: str, end: str) -> list[str]:
        """交易日历（用于增量校验），返回 ``['YYYY-MM-DD', ...]``。"""
        with _wind_session() as w:
            out = w.tdays(start, end, "Days=Trading")
        if out.ErrorCode != 0:
            raise RuntimeError(
                f"Wind tdays 拉取失败（{start}~{end}）: "
                f"ErrorCode={out.ErrorCode} {out.Data}"
            )
        return [_to_dt_str(t) for t in out.Times]
