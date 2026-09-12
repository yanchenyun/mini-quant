"""MySQL 行情仓储（DataRepository 实现），表结构对齐 ODS 规范：

    ods.ods_d_stock_quotation_i   —— A股日线增量表（唯一键 code+date+adjust_flag）
    ods.ods_mi_stock_quotation_i  —— A股分钟线增量表（唯一键 code+date_time+adjust_flag）

v0.2 起 freq 分发表读写：'1d' 走日表，'5min' 走分钟表。分钟行情加载时
LEFT JOIN 日线表回填 pre_close / trade_status / is_st——涨跌停与停牌 ST 判定
必须用"日线级"口径，而非上一根分钟 bar。

替换存储（如 Parquet + DuckDB）时只需重写本类，其余零改动 —— DIP/OCP。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

import pandas as pd
import pymysql

from ..config import DBSettings
from .timeutil import norm_dt

# ── 表名注册表：新增频率 = 加一行 + 建表 DDL ─────────────────
TABLES: dict[str, str] = {
    "1d": "ods_d_stock_quotation_i",
    "5min": "ods_mi_stock_quotation_i",
}

# 与 ODS 规范保持一致（幂等建库建表，方便新环境一键初始化）
DDL = """
CREATE DATABASE IF NOT EXISTS `ods`
DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS `ods`.`ods_d_stock_quotation_i` (
  `id` varchar(36) NOT NULL COMMENT '唯一主键',
  `code` varchar(20) NOT NULL COMMENT '证券代码',
  `date` varchar(10) NOT NULL COMMENT '行情日期',
  `adjust_flag` tinyint DEFAULT NULL COMMENT '复权状态',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `pre_close` decimal(10,4) DEFAULT NULL COMMENT '昨日收盘价',
  `volume` bigint DEFAULT NULL COMMENT '成交数量（单位：股）',
  `amount` decimal(18,4) DEFAULT NULL COMMENT '成交金额',
  `turn` decimal(10,6) DEFAULT NULL COMMENT '换手率（单位：%）',
  `pct_chg` decimal(10,6) DEFAULT NULL COMMENT '涨跌幅（百分比）',
  `pe_ttm` decimal(10,6) DEFAULT NULL COMMENT '滚动市盈率',
  `ps_ttm` decimal(10,6) DEFAULT NULL COMMENT '滚动市销率',
  `pcf_ncf_ttm` decimal(10,6) DEFAULT NULL COMMENT '滚动市现率',
  `pb_mrq` decimal(10,6) DEFAULT NULL COMMENT '市净率',
  `trade_status` tinyint DEFAULT NULL COMMENT '交易状态（1：正常交易，0：停牌）',
  `is_st` tinyint DEFAULT NULL COMMENT '是否ST（1：是，0：否）',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据插入时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_date` (`code`,`date`,`adjust_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A股日线股票行情增量表';
CREATE TABLE IF NOT EXISTS `ods`.`ods_mi_stock_quotation_i` (
  `id` varchar(36) NOT NULL COMMENT '唯一主键',
  `code` varchar(20) NOT NULL COMMENT '证券代码',
  `date_time` varchar(30) NOT NULL COMMENT '行情时间',
  `adjust_flag` tinyint DEFAULT NULL COMMENT '复权状态',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `volume` bigint DEFAULT NULL COMMENT '成交量（单位：股，累计值）',
  `amount` decimal(18,4) DEFAULT NULL COMMENT '成交额',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据插入时间（自动填充当前时间）',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间（更新时自动刷新）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_date_time` (`code`,`date_time`,`adjust_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A股5分钟股票行情增量表';
CREATE TABLE IF NOT EXISTS `ods`.`dwd_factor_value_i` (
  `id` varchar(36) NOT NULL COMMENT '唯一主键',
  `code` varchar(20) NOT NULL COMMENT '证券代码',
  `date_time` varchar(30) NOT NULL COMMENT '因子时间（日线=交易日，分钟含时分）',
  `freq` varchar(8) NOT NULL COMMENT '频率：1d / 5min',
  `factor_name` varchar(64) NOT NULL COMMENT '因子名（如 momentum_20）',
  `value` double DEFAULT NULL COMMENT '因子值',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据插入时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_factor` (`code`,`date_time`,`freq`,`factor_name`),
  KEY `idx_name_freq_dt` (`factor_name`,`freq`,`date_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='因子值增量表（长表：加新因子零DDL，读侧pivot成宽表）';
"""

# 日表列（db.txt 口径）
_D_COLS = ["code", "date", "adjust_flag", "open", "close", "low", "high", "pre_close",
           "volume", "amount", "turn", "pct_chg", "pe_ttm", "ps_ttm", "pcf_ncf_ttm",
           "pb_mrq", "trade_status", "is_st"]
# 分钟表列（ods_mi_stock_quotation_i 实际只有行情+量价字段；
# pre_close/trade_status/is_st 不在库中，加载时从日线表 JOIN 回填）
_M_COLS = ["code", "date_time", "adjust_flag", "open", "close", "low", "high",
           "volume", "amount"]

_INSERT_D_SQL = """
INSERT INTO `{db}`.`ods_d_stock_quotation_i`
(id, code, date, adjust_flag, open, close, low, high, pre_close, volume, amount,
 turn, pct_chg, pe_ttm, ps_ttm, pcf_ncf_ttm, pb_mrq, trade_status, is_st)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
 open=VALUES(open), close=VALUES(close), low=VALUES(low), high=VALUES(high),
 pre_close=VALUES(pre_close), volume=VALUES(volume), amount=VALUES(amount),
 turn=VALUES(turn), pct_chg=VALUES(pct_chg), pe_ttm=VALUES(pe_ttm),
 ps_ttm=VALUES(ps_ttm), pcf_ncf_ttm=VALUES(pcf_ncf_ttm), pb_mrq=VALUES(pb_mrq),
 trade_status=VALUES(trade_status), is_st=VALUES(is_st)
"""

_INSERT_M_SQL = """
INSERT INTO `{db}`.`ods_mi_stock_quotation_i`
(id, code, date_time, adjust_flag, open, close, low, high, volume, amount)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
 open=VALUES(open), close=VALUES(close), low=VALUES(low), high=VALUES(high),
 volume=VALUES(volume), amount=VALUES(amount)
"""

# 因子表长行 upsert（存在则更新：因子逻辑修正后重算即覆盖——"物化缓存"语义）
_INSERT_F_SQL = """
INSERT INTO `{db}`.`dwd_factor_value_i`
(id, code, date_time, freq, factor_name, value)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE value=VALUES(value)
"""

# 仓储输出统一列（引擎 / Web 消费的口径）
_OUT_COLS = ["code", "dt", "trade_date", "date", "open", "high", "low", "close",
             "pre_close", "volume", "amount", "trade_status", "is_st"]


class MySQLBarRepo:
    """freq 感知的 MySQL 行情仓储。"""

    def __init__(self, settings: DBSettings):
        self._db = settings

    @contextmanager
    def _conn(self) -> Iterator[pymysql.Connection]:
        conn = pymysql.connect(
            host=self._db.host, port=self._db.port, user=self._db.user,
            password=self._db.password, database=self._db.name,
            charset="utf8mb4", autocommit=True,
        )
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _table(freq: str) -> str:
        if freq not in TABLES:
            raise ValueError(f"不支持的频率: {freq}（可选: {list(TABLES)}）")
        return TABLES[freq]

    # ── 初始化 ───────────────────────────────────────────
    def init_schema(self) -> None:
        """幂等建库建表（日表 + 分钟表）。"""
        with self._conn() as conn:
            cur = conn.cursor()
            for stmt in DDL.strip().split(";"):
                if stmt.strip():
                    cur.execute(stmt)

    # ── 写 ───────────────────────────────────────────────
    def save_bars(self, df: pd.DataFrame, freq: str = "1d") -> int:
        if df.empty:
            return 0
        if freq == "1d":
            return self._save_daily(df)
        if freq == "5min":
            return self._save_minute(df)
        raise ValueError(f"不支持的频率: {freq}")

    def _save_daily(self, df: pd.DataFrame) -> int:
        # fetch_bars 的日线输出含 dt/trade_date；写库前归一为 date 列
        if "date" not in df.columns:
            df = df.assign(date=df["trade_date"] if "trade_date" in df.columns
                           else df["dt"])
        rows = [(str(uuid.uuid4()),
                 *[r.get(c) for c in _D_COLS]) for r in df.to_dict("records")]
        sql = _INSERT_D_SQL.format(db=self._db.name)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                return cur.rowcount

    def _save_minute(self, df: pd.DataFrame) -> int:
        # 统一取 dt 作为 bar 时间写入 date_time 列（写入前归一，杜绝脏格式入库）
        rows = [(str(uuid.uuid4()),
                 r.get("code"),
                 norm_dt(r.get("dt") or r.get("trade_time") or r.get("date_time")),
                 int(r.get("adjust_flag") or 2),
                 r.get("open"), r.get("close"), r.get("low"), r.get("high"),
                 r.get("volume") or 0, r.get("amount") or 0.0)
                for r in df.to_dict("records")]
        sql = _INSERT_M_SQL.format(db=self._db.name)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                return cur.rowcount

    # ── 读 ───────────────────────────────────────────────
    def load_bars(self, codes: list[str], start: str, end: str,
                  freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        if not codes:
            return pd.DataFrame(columns=_OUT_COLS)
        if freq == "1d":
            return self._load_daily(codes, start, end, adjust)
        if freq == "5min":
            return self._load_minute(codes, start, end, adjust)
        raise ValueError(f"不支持的频率: {freq}")

    def _load_daily(self, codes: list[str], start: str, end: str,
                    adjust: str) -> pd.DataFrame:
        placeholders = ",".join(["%s"] * len(codes))
        sql = (f"SELECT {', '.join(_D_COLS)} FROM `{self._db.name}`.`ods_d_stock_quotation_i` "
               f"WHERE code IN ({placeholders}) AND date BETWEEN %s AND %s "
               f"AND adjust_flag = %s ORDER BY code, date")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (*codes, start, end, int(adjust)))
                rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=_D_COLS)
        if df.empty:
            return df
        for col in ("open", "close", "low", "high", "pre_close", "volume",
                    "amount", "turn", "pct_chg"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
        # NULL 兜底：表定义允许 NULL，手工/异构入库的旧行缺值时
        # astype(int) 会崩（None 不可转），先补安全默认（正常/非ST）
        for col in ("trade_status", "is_st"):
            df[col] = (pd.to_numeric(df[col], errors="coerce")
                       .fillna(1 if col == "trade_status" else 0).astype(int))
        # 统一输出：dt = 交易日；date 为向后兼容别名（Web 层消费）
        df["dt"] = df["date"].astype(str)
        df["trade_date"] = df["dt"]
        return df

    def _load_minute(self, codes: list[str], start: str, end: str,
                     adjust: str) -> pd.DataFrame:
        """分钟行情：LEFT JOIN 日线表回填日线口径的
        pre_close（涨跌停基准）/ trade_status / is_st（分钟表本身没有这三列）。
        日线缺数据时优雅降级为不判涨跌停。"""
        placeholders = ",".join(["%s"] * len(codes))
        # end 为纯日期时补足当日边界：'2025-06-30' 与 '2025-06-30 15:00:00'
        # 的字符串比较会排除 end 当日全部分钟 bar，必须放宽到 23:59:59
        end_bound = end if len(end.strip()) > 10 else end.strip() + " 23:59:59"
        sql = f"""
        SELECT m.code, m.date_time, m.adjust_flag,
               m.open, m.close, m.low, m.high, m.volume, m.amount,
               COALESCE(d.pre_close, 0) AS pre_close,
               COALESCE(d.trade_status, 1) AS trade_status,
               COALESCE(d.is_st, 0) AS is_st
        FROM `{self._db.name}`.`ods_mi_stock_quotation_i` m
        LEFT JOIN `{self._db.name}`.`ods_d_stock_quotation_i` d
          ON d.code = m.code
         AND d.date = SUBSTRING(m.date_time, 1, 10)
         AND d.adjust_flag = m.adjust_flag
        WHERE m.code IN ({placeholders})
          AND m.date_time BETWEEN %s AND %s
          AND m.adjust_flag = %s
        ORDER BY m.code, m.date_time
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (*codes, start, end_bound, int(adjust)))
                rows = cur.fetchall()
        cols = ["code", "date_time", "adjust_flag", "open", "close", "low", "high",
                "volume", "amount", "pre_close", "trade_status", "is_st"]
        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            return df
        for col in ("open", "close", "low", "high", "volume", "amount", "pre_close"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
        for col in ("trade_status", "is_st"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(
                1 if col == "trade_status" else 0).astype(int)
        # 统一输出：dt 含时分（读取时归一，兜底修复历史入库的脏格式，
        # 无需重灌数据）；trade_date / date 归属交易日
        df["dt"] = df["date_time"].map(norm_dt)
        df["trade_date"] = df["dt"].str[:10]
        df["date"] = df["trade_date"]
        return df

    def latest_bar_time(self, code: str, freq: str = "1d",
                        adjust: str = "2") -> str | None:
        """该标的在库中最新的 bar 时间（日线为日期，分钟含时分）。"""
        table = self._table(freq)
        time_col = "date" if freq == "1d" else "date_time"
        sql = (f"SELECT MAX(`{time_col}`) FROM `{self._db.name}`.`{table}` "
               f"WHERE code=%s AND adjust_flag=%s")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (code, int(adjust)))
                (val,) = cur.fetchone()
        return norm_dt(val) if val is not None else None

    def repair_date_time(self) -> int:
        """一次性修复：把历史入库的 17 位纯数字时间戳（baostock 原始格式）
        就地 UPDATE 为标准 'YYYY-MM-DD HH:MM:SS'。幂等，只影响脏行。

        修复的意义：脏格式会导致 BETWEEN 范围过滤与增量续抓游标（MAX(date_time)）
        的字符串比较错乱——'20250102...' 与 '2025-01-02...' 排序规则不一致。
        """
        sql = f"""
        UPDATE `{self._db.name}`.`ods_mi_stock_quotation_i`
        SET date_time = CONCAT(SUBSTRING(date_time, 1, 4), '-', SUBSTRING(date_time, 5, 2),
                               '-', SUBSTRING(date_time, 7, 2), ' ', SUBSTRING(date_time, 9, 2),
                               ':', SUBSTRING(date_time, 11, 2), ':', SUBSTRING(date_time, 13, 2))
        WHERE CHAR_LENGTH(date_time) = 17 AND date_time REGEXP '^[0-9]+$'
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.rowcount

    def list_codes(self, freq: str = "1d") -> list[str]:
        table = self._table(freq)
        sql = f"SELECT DISTINCT code FROM `{self._db.name}`.`{table}` ORDER BY code"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [r[0] for r in cur.fetchall()]

    # ── 因子读写（dwd_factor_value_i 长表）────────────────
    def save_factors(self, frame: pd.DataFrame, freq: str = "1d") -> int:
        """因子宽表（code/dt/因子列…）熔成长行 upsert。

        NaN 行跳过（预热区无意义不落库）；幂等——因子逻辑修正后
        重算覆盖旧值（"落库是物化缓存"的语义）。
        """
        if frame.empty:
            return 0
        factor_cols = [c for c in frame.columns if c not in ("code", "dt")]
        rows: list[tuple] = []
        for r in frame.to_dict("records"):
            for c in factor_cols:
                v = r.get(c)
                if pd.isna(v):
                    continue
                rows.append((str(uuid.uuid4()), r["code"], str(r["dt"]),
                             freq, c, float(v)))
        if not rows:
            return 0
        sql = _INSERT_F_SQL.format(db=self._db.name)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                return cur.rowcount

    def load_factors(self, codes: list[str], names: list[str], start: str,
                     end: str, freq: str = "1d") -> pd.DataFrame:
        """长表 → 宽表 [code, dt, <因子列…>]。供选股/因子分析场景读取
        （回测不走这里：回测内存即时计算，保证与行情同帧同口径）。"""
        if not codes or not names:
            return pd.DataFrame(columns=["code", "dt", *names])
        code_ph = ",".join(["%s"] * len(codes))
        name_ph = ",".join(["%s"] * len(names))
        # end 纯日期时补当日边界（同分钟行情查询：字符串比较会排除 end 当日）
        end_bound = end if len(end.strip()) > 10 else end.strip() + " 23:59:59"
        sql = (f"SELECT code, date_time, factor_name, value "
               f"FROM `{self._db.name}`.`dwd_factor_value_i` "
               f"WHERE code IN ({code_ph}) AND factor_name IN ({name_ph}) "
               f"AND freq = %s AND date_time BETWEEN %s AND %s "
               f"ORDER BY code, date_time")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (*codes, *names, freq, start, end_bound))
                rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=["code", "dt", *names])
        long_df = pd.DataFrame(rows, columns=["code", "date_time",
                                              "factor_name", "value"])
        wide = (long_df.pivot(index=["code", "date_time"], columns="factor_name",
                              values="value").reset_index()
                .rename(columns={"date_time": "dt"}))
        wide.columns.name = None
        # 保证请求的因子列都存在（库里没算过 → NaN 列）
        for n in names:
            if n not in wide.columns:
                wide[n] = float("nan")
        return wide[["code", "dt", *names]]

    # ── 向后兼容别名（v0.1 接口，委托到 freq 版本）─────────
    def save_daily(self, df: pd.DataFrame) -> int:
        return self.save_bars(df, freq="1d")

    def load_daily(self, codes: list[str], start: str, end: str,
                   adjust: str = "2") -> pd.DataFrame:
        return self.load_bars(codes, start, end, freq="1d", adjust=adjust)

    def latest_date(self, code: str, adjust: str = "2") -> str | None:
        return self.latest_bar_time(code, freq="1d", adjust=adjust)


# v0.1 类名别名：旧引用零改动
MySQLDailyRepo = MySQLBarRepo
