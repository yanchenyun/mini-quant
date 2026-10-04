"""MySQL 行情仓储（DataRepository 实现），表结构对齐 ODS 规范。

表清单由 data.registry 的频率注册表派生（freq_tables / FREQS），
当前登记：日线 ods_d_stock_quotation_i + 5 分钟线 ods_mi_stock_quotation_i
（唯一键均为 code + 时间 + adjust_flag）。15/30/60min 为派生频率——不建表
不落库，读取时从 5 分钟数据重采样聚合（细粒度事实表 + 聚合派生）。

freq 分发表读写：日线走日表，分钟频率共用分钟表。分钟行情加载时
LEFT JOIN 日线表回填 pre_close / trade_status / is_st / 涨跌停价——
涨跌停与停牌 ST 判定必须用"日线级"口径，而非上一根分钟 bar。

新增有独立表的频率只需在 data.registry 加一行（建表 DDL 与读写分派
自动派生）；派生频率加一行并指定 derived_from 即可，本文件零改动。

替换存储（如 Parquet + DuckDB）只需另写一个 DataRepository 实现，再到服务层
工厂装配处切换；本文件之外不需要改动 —— DIP/OCP。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

import pandas as pd
import pymysql

from ..config import DBSettings
from .registry import FREQS, FreqSpec, freq_tables, get_freq
from .timeutil import norm_dt

# ── 表名注册表：由 data.registry 的频率清单派生（新增频率在那里加一行）───
# 日线走 _D_COLS 口径，其余分钟频率共用 _M_COLS 口径（列结构相同，
# 仅表名与 bar 粒度不同），因此新增分钟频率无需再改本文件的读写分派。
TABLES: dict[str, str] = freq_tables()

# 分钟表的建表 DDL 模板：表名与注释由频率注册表逐条生成，
# 避免"加了频率忘记加表"。
_MINUTE_DDL = """
CREATE TABLE IF NOT EXISTS `ods`.`{table}` (
  `id` varchar(36) NOT NULL COMMENT '唯一主键',
  `code` varchar(20) NOT NULL COMMENT '证券代码',
  `date_time` varchar(30) NOT NULL COMMENT '行情时间',
  `adjust_flag` tinyint DEFAULT NULL COMMENT '复权状态',
  `open` decimal(10,4) DEFAULT NULL COMMENT '开盘价',
  `close` decimal(10,4) DEFAULT NULL COMMENT '收盘价',
  `low` decimal(10,4) DEFAULT NULL COMMENT '最低价',
  `high` decimal(10,4) DEFAULT NULL COMMENT '最高价',
  `volume` bigint DEFAULT NULL COMMENT '成交量（单位：股，当根口径——Wind 分钟序列为该 bar 内成交量，当日各根之和等于日线量）',
  `amount` decimal(18,4) DEFAULT NULL COMMENT '成交额',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据插入时间（自动填充当前时间）',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间（更新时自动刷新）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_date_time` (`code`,`date_time`,`adjust_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A股{label}行情增量表';
"""

_DDAILY_DDL = """
CREATE TABLE IF NOT EXISTS `ods`.`{table}` (
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
  `upper_limit` decimal(10,4) DEFAULT NULL COMMENT '涨停价（权威值；NULL/0 表示未知）',
  `lower_limit` decimal(10,4) DEFAULT NULL COMMENT '跌停价（权威值；NULL/0 表示未知）',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据插入时间',
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_code_date` (`code`,`date`,`adjust_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A股{label}行情增量表';
"""

_FACTOR_DDL = """
CREATE TABLE IF NOT EXISTS `ods`.`dwd_factor_value_i` (
  `id` varchar(36) NOT NULL COMMENT '唯一主键',
  `code` varchar(20) NOT NULL COMMENT '证券代码',
  `date_time` varchar(30) NOT NULL COMMENT '因子时间（日线=交易日，分钟含时分）',
  `freq` varchar(8) NOT NULL COMMENT '频率：见 data.registry 的频率注册表',
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


def _build_ddl() -> str:
    """由频率注册表派生全部建表 DDL（派生频率与源同表，跳过不重复建）。"""
    parts = ["""CREATE DATABASE IF NOT EXISTS `ods`
DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci;"""]
    for spec in FREQS:
        if spec.derived_from:
            continue   # 派生频率无独立表（读取时从源表聚合）
        tmpl = _DDAILY_DDL if spec.is_daily else _MINUTE_DDL
        parts.append(tmpl.format(table=spec.table, label=spec.label))
    parts.append(_FACTOR_DDL)
    return "\n".join(parts)


# 与 ODS 规范保持一致（幂等建库建表，方便新环境一键初始化）
DDL = _build_ddl()

# 日表列（db.txt 口径 + upper_limit/lower_limit 涨跌停权威价两列扩展）
_D_COLS = ["code", "date", "adjust_flag", "open", "close", "low", "high", "pre_close",
           "volume", "amount", "turn", "pct_chg", "pe_ttm", "ps_ttm", "pcf_ncf_ttm",
           "pb_mrq", "trade_status", "is_st", "upper_limit", "lower_limit"]
# 分钟表列（ods_mi_stock_quotation_i 实际只有行情+量价字段；
# pre_close/trade_status/is_st 不在库中，加载时从日线表 JOIN 回填）
_M_COLS = ["code", "date_time", "adjust_flag", "open", "close", "low", "high",
           "volume", "amount"]

_INSERT_D_SQL = """
INSERT INTO `{db}`.`{table}`
(id, code, date, adjust_flag, open, close, low, high, pre_close, volume, amount,
 turn, pct_chg, pe_ttm, ps_ttm, pcf_ncf_ttm, pb_mrq, trade_status, is_st,
 upper_limit, lower_limit)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
 open=VALUES(open), close=VALUES(close), low=VALUES(low), high=VALUES(high),
 pre_close=VALUES(pre_close), volume=VALUES(volume), amount=VALUES(amount),
 turn=VALUES(turn), pct_chg=VALUES(pct_chg), pe_ttm=VALUES(pe_ttm),
 ps_ttm=VALUES(ps_ttm), pcf_ncf_ttm=VALUES(pcf_ncf_ttm), pb_mrq=VALUES(pb_mrq),
 trade_status=VALUES(trade_status), is_st=VALUES(is_st),
 upper_limit=VALUES(upper_limit), lower_limit=VALUES(lower_limit)
"""

_INSERT_M_SQL = """
INSERT INTO `{db}`.`{table}`
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
             "pre_close", "volume", "amount", "trade_status", "is_st",
             "upper_limit", "lower_limit"]


def _aggregate_bars(df: pd.DataFrame, spec: FreqSpec) -> pd.DataFrame:
    """把源分钟 bar 重采样聚合为目标粒度（15/30/60min 派生频率用）。

    槽位按"钟表网格"划分而非当日 bar 序号——缺 bar 时序号法会让槽边界
    漂移（缺 09:40 则 09:45 会被误归首槽），钟表法只认时刻本身：上午
    09:30 起、下午 13:00 起各 120 分钟竞价，源 bar 的"时段内偏移分钟"
    整除目标跨度即为槽号。60min 由此切分为 09:30-10:30 / 10:30-11:30 /
    13:00-14:00 / 14:00-15:00（A 股惯例），而非钟表整点（09:00-10:00
    会把上午错切成 30+60+30 三段）。

    聚合口径：open 首根、high/low 极值、close 末根、volume/amount 求和
    （当根口径——若源 volume 实为当日累计口径需改末根差分，该口径为
    遗留待核对项）；pre_close / is_st / 涨跌停价取首根（日线级字段整日
    恒定）；trade_status 取槽内最小值（任一源 bar 停牌即整槽保守视为
    停牌）；dt 取槽内最后一根源 bar 时刻（源缺尾根时近似，可接受）。
    """
    src = get_freq(spec.derived_from)
    src_minutes = 240 // src.bars_per_day      # 源 bar 分钟跨度（5min→5）
    span = 240 // spec.bars_per_day            # 目标 bar 分钟跨度（60min→60）

    ts = pd.to_datetime(df["dt"])
    minutes = ts.dt.hour * 60 + ts.dt.minute
    # 时段内偏移分钟：上午自 09:30(570)；下午 13:00(780) 折算接上午 120 分钟
    # （午休不占偏移空间，13:05 的偏移是 125 而非 245）
    off = minutes.where(minutes < 780, minutes - 90) - 570
    slot = ((off - src_minutes) // span).clip(lower=0)

    out = (df.assign(_day=ts.dt.strftime("%Y-%m-%d"), _slot=slot)
             .sort_values(["code", "dt"])
             .groupby(["code", "_day", "_slot"], sort=True)
             .agg(dt=("dt", "last"), trade_date=("trade_date", "first"),
                  open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last"),
                  pre_close=("pre_close", "first"), volume=("volume", "sum"),
                  amount=("amount", "sum"), trade_status=("trade_status", "min"),
                  is_st=("is_st", "first"), upper_limit=("upper_limit", "first"),
                  lower_limit=("lower_limit", "first"))
             .reset_index())
    out["date"] = out["trade_date"]
    return out[_OUT_COLS]


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
        # 先不指定 database 连接，确保库不存在时也能创建
        conn_no_db = pymysql.connect(
            host=self._db.host, port=self._db.port, user=self._db.user,
            password=self._db.password, charset="utf8mb4", autocommit=True,
        )
        try:
            cur = conn_no_db.cursor()
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `{}` "
                "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci".format(
                    self._db.name)
            )
            cur.close()
        finally:
            conn_no_db.close()

        with self._conn() as conn:
            cur = conn.cursor()
            for stmt in DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.upper().startswith("CREATE DATABASE"):
                    cur.execute(stmt)
            self._migrate_daily_limit_cols(conn)

    def _migrate_daily_limit_cols(self, conn: pymysql.Connection) -> None:
        """旧日线表补涨跌停两列（幂等迁移）。

        CREATE TABLE IF NOT EXISTS 只对新环境生效，已存在的表不会自动
        加列，这里查 information_schema 后按需 ALTER。旧行该两列为
        NULL，读出时 fillna(0) 即"未知"，撮合层回退 pre_close 估算。
        注意：分钟行情的 JOIN 亦引用这两列，升级后应先执行一次
        init-schema（幂等）再读分钟数据。
        """
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (self._db.name, TABLES["1d"]),
        )
        existing = {row[0] for row in cur.fetchall()}
        for col in ("upper_limit", "lower_limit"):
            if col not in existing:
                cur.execute(
                    f"ALTER TABLE `{self._db.name}`.`{TABLES['1d']}` "
                    f"ADD COLUMN `{col}` decimal(10,4) DEFAULT NULL "
                    f"COMMENT '涨跌停价（权威值；NULL/0 表示未知）'"
                )

    # ── 写 ───────────────────────────────────────────────
    def save_bars(self, df: pd.DataFrame, freq: str = "1d") -> int:
        if df.empty:
            return 0
        spec = get_freq(freq)
        if spec.derived_from:
            # 派生频率的数据物理上就在源表，写入只会以粗粒度 bar 污染
            # 细粒度数据（upsert 按时间戳覆盖），必须拒绝
            raise ValueError(
                f"{freq} 为派生频率（读取时从 {spec.derived_from} 数据聚合），"
                f"不落库；请以源频率 {spec.derived_from} 入库")
        if spec.is_daily:
            return self._save_daily(df)
        return self._save_minute(df, spec)

    def _save_daily(self, df: pd.DataFrame) -> int:
        # fetch_bars 的日线输出含 dt/trade_date；写库前归一为 date 列
        if "date" not in df.columns:
            df = df.assign(date=df["trade_date"] if "trade_date" in df.columns
                           else df["dt"])
        rows = [(str(uuid.uuid4()),
                 *[r.get(c) for c in _D_COLS]) for r in df.to_dict("records")]
        sql = _INSERT_D_SQL.format(db=self._db.name, table=TABLES["1d"])
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                return cur.rowcount

    def _save_minute(self, df: pd.DataFrame, spec: FreqSpec) -> int:
        # 统一取 dt 作为 bar 时间写入 date_time 列（写入前归一，杜绝脏格式入库）
        rows = [(str(uuid.uuid4()),
                 r.get("code"),
                 norm_dt(r.get("dt") or r.get("trade_time") or r.get("date_time")),
                 int(r.get("adjust_flag") or 2),
                 r.get("open"), r.get("close"), r.get("low"), r.get("high"),
                 r.get("volume") or 0, r.get("amount") or 0.0)
                for r in df.to_dict("records")]
        sql = _INSERT_M_SQL.format(db=self._db.name, table=spec.table)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
                return cur.rowcount

    # ── 读 ───────────────────────────────────────────────
    def load_bars(self, codes: list[str], start: str, end: str,
                  freq: str = "1d", adjust: str = "2") -> pd.DataFrame:
        if not codes:
            return pd.DataFrame(columns=_OUT_COLS)
        spec = get_freq(freq)
        if spec.is_daily:
            return self._load_daily(codes, start, end, adjust)
        return self._load_minute(codes, start, end, adjust, spec)

    def _load_daily(self, codes: list[str], start: str, end: str,
                    adjust: str) -> pd.DataFrame:
        placeholders = ",".join(["%s"] * len(codes))
        sql = (f"SELECT {', '.join(_D_COLS)} FROM `{self._db.name}`.`{TABLES['1d']}` "
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
                    "amount", "turn", "pct_chg", "upper_limit", "lower_limit"):
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
                     adjust: str, spec: FreqSpec) -> pd.DataFrame:
        """分钟行情：LEFT JOIN 日线表回填日线口径的
        pre_close（涨跌停估算基准）/ trade_status / is_st /
        upper_limit / lower_limit（涨跌停权威价——分钟表本身没有这些列）。
        日线缺数据时优雅降级为不判涨跌停。

        派生频率（15/30/60min）：物理表与源频率相同（spec.table 即 5min
        表），读出细粒度数据后由 _aggregate_bars 重采样聚合。
        """
        placeholders = ",".join(["%s"] * len(codes))
        # end 为纯日期时补足当日边界：'2025-06-30' 与 '2025-06-30 15:00:00'
        # 的字符串比较会排除 end 当日全部分钟 bar，必须放宽到 23:59:59
        end_bound = end if len(end.strip()) > 10 else end.strip() + " 23:59:59"
        sql = f"""
        SELECT m.code, m.date_time, m.adjust_flag,
               m.open, m.close, m.low, m.high, m.volume, m.amount,
               COALESCE(d.pre_close, 0) AS pre_close,
               COALESCE(d.trade_status, 1) AS trade_status,
               COALESCE(d.is_st, 0) AS is_st,
               COALESCE(d.upper_limit, 0) AS upper_limit,
               COALESCE(d.lower_limit, 0) AS lower_limit
        FROM `{self._db.name}`.`{spec.table}` m
        LEFT JOIN `{self._db.name}`.`{TABLES['1d']}` d
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
                "volume", "amount", "pre_close", "trade_status", "is_st",
                "upper_limit", "lower_limit"]
        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            return df
        for col in ("open", "close", "low", "high", "volume", "amount",
                    "pre_close", "upper_limit", "lower_limit"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
        for col in ("trade_status", "is_st"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(
                1 if col == "trade_status" else 0).astype(int)
        # 统一输出：dt 含时分（读取时归一，兜底修复历史入库的脏格式，
        # 无需重灌数据）；trade_date / date 归属交易日
        df["dt"] = df["date_time"].map(norm_dt)
        df["trade_date"] = df["dt"].str[:10]
        df["date"] = df["trade_date"]
        if spec.derived_from:
            return _aggregate_bars(df, spec)
        return df

    def latest_bar_time(self, code: str, freq: str = "1d",
                        adjust: str = "2") -> str | None:
        """该标的在库中最新的 bar 时间（日线为日期，分钟含时分）。"""
        table = self._table(freq)
        time_col = "date" if get_freq(freq).is_daily else "date_time"
        sql = (f"SELECT MAX(`{time_col}`) FROM `{self._db.name}`.`{table}` "
               f"WHERE code=%s AND adjust_flag=%s")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (code, int(adjust)))
                row = cur.fetchone()
        val = row[0] if row is not None else None
        return norm_dt(val) if val is not None else None

    def repair_date_time(self) -> int:
        """一次性修复：把历史入库的 17 位纯数字时间戳（早期数据源的
        原始毫秒格式，库中可能仍有存量）就地 UPDATE 为标准
        'YYYY-MM-DD HH:MM:SS'。幂等，只影响脏行。

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
