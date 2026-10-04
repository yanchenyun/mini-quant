"""数据源与频率注册表（数据层的单一事实来源）。

这两份清单原先各散落多处：数据源名单同时写在服务层的数据源字典与 CLI 的
--source choices 里，频率名单同时写在仓储的表名映射、CLI 三个子命令的
choices 与 Web 端点的取值校验里。每加一个源或一个频率都要逐处同步，
漏改一处就会出现"CLI 能选、服务层不认"这类不一致。

本模块把它们收拢到唯一一处声明，其余位置一律从这里派生：

SOURCES 声明数据源：名称、一句话说明、适配器模块名与类名。这里只存
"模块名 + 类名"而不是直接导入实现类，是为了不把数据源的第三方依赖
绑到数据层上——适配器在模块顶层 import 各自的第三方库（如 WindPy），
若在本模块直接导入，未装该库的环境连 CLI 都启动不了。真正的导入推迟到
get_source 调用时进行。

FREQS 声明频率：名称、行情表名、说明、以及每个交易日的 bar 数。
仓储的表名映射（TABLES）与 CLI 的 --freq choices、Web 端点的取值校验
全部由它派生。

bars_per_day 描述 bar 的时间粒度（日线 1，5 分钟线 48），供仓储层
判定"是否日线口径"（时间列名、是否 JOIN 日线回填），不再按频率名逐个
硬编码。

注意：绩效年化不使用 bar 粒度——回测引擎按"交易日"记录净值快照
（每交易日一条），故 annual_return 的基准恒为 252 个交易日，
与行情频率无关。这一点由 metrics.compute_metrics 的默认值保证。

扩展方式
--------
新数据源 = 新增一个适配器文件 + 在 SOURCES 里加一行。CLI 的 --source
choices 与 Web 侧校验随之自动生效，无需改动入口代码。

新频率 = 在 FREQS 里加一行。有自己独立表的频率（如日线）：建表 DDL 与
仓储分派自动派生；派生频率（derived_from 指向已有分钟频率）：零存储改动，
读取时自动聚合。无论哪种，另需确认对应适配器的取数分支已支持该频率——
适配器能力与本清单须保持一致。

参见
----
- 开发文档（架构 / 接口契约 / 新数据源步骤）：docs/DEVELOPMENT.md
- 操作文档（数据源选择 / 网络排错）：根目录 ReadMe.md
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    """数据源规格：名称、说明、适配器模块名与类名（模块名相对本包）。"""

    name: str
    label: str
    module: str
    cls: str


@dataclass(frozen=True)
class FreqSpec:
    """频率规格：名称、对应行情表名、说明、每个交易日的 bar 数、派生源。

    bars_per_day 为该频率每个交易日的 bar 数（日线为 1，5 分钟线为 48），
    描述的是 bar 的时间粒度，供仓储层判定"是否日线口径"——决定时间列名
    （date / date_time）与是否 JOIN 日线回填日线级字段。

    derived_from 非空表示派生频率：不建表、不落库，读取时从源频率的
    分钟表重采样聚合而来（细粒度事实表 + 聚合派生的数据工程惯例——
    原始数据只存最细粒度一份，更粗频率随取随聚合，零冗余）。派生频率
    的 table 与源频率相同（数据物理上就在那张表）。

    它不用于绩效年化：回测引擎按交易日记录净值快照，年化基准恒为 252 个
    交易日，与行情频率无关（见 metrics.compute_metrics）。
    """

    name: str
    table: str
    label: str
    bars_per_day: int
    derived_from: str = ""   # 派生源频率名（如 '5min'）；空 = 有自己表的频率

    @property
    def is_daily(self) -> bool:
        """是否日线频率（每日一根 bar）。

        仓储层据此决定时间列名（date / date_time）、是否 JOIN 日线
        回填日线口径字段——不再按 freq 名逐个硬编码，新增分钟频率
        零分派改动。
        """
        return self.bars_per_day == 1


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("wind", "需本机安装并登录 Wind 终端",
               "wind_source", "WindSource"),
)

# 每交易日 bar 数：A 股连续竞价 4 小时 = 240 分钟，
# 5 分钟 48 根、15 分钟 16 根、30 分钟 8 根、60 分钟 4 根。
# 原始分钟数据只存最细粒度的 5 分钟一张表（ODS 规范口径）；15/30/60min
# 为派生频率——读取时由仓储层从 5 分钟数据重采样聚合，不建表不落库。
FREQS: tuple[FreqSpec, ...] = (
    FreqSpec("1d", "ods_d_stock_quotation_i", "日线", 1),
    FreqSpec("5min", "ods_mi_stock_quotation_i", "5分钟线", 48),
    FreqSpec("15min", "ods_mi_stock_quotation_i", "15分钟线", 16,
             derived_from="5min"),
    FreqSpec("30min", "ods_mi_stock_quotation_i", "30分钟线", 8,
             derived_from="5min"),
    FreqSpec("60min", "ods_mi_stock_quotation_i", "60分钟线", 4,
             derived_from="5min"),
)

# A 股年均交易日数：绩效年化基准（引擎按交易日快照，见 periods_per_year）
_TRADING_DAYS_PER_YEAR = 252

_SOURCES_BY_NAME: dict[str, SourceSpec] = {s.name: s for s in SOURCES}
_FREQS_BY_NAME: dict[str, FreqSpec] = {f.name: f for f in FREQS}


def available_sources() -> list[str]:
    """已登记数据源名列表（CLI choices 与 Web 校验共用同一份）。"""
    return [s.name for s in SOURCES]


def source_help() -> str:
    """数据源帮助文案：由注册表逐条拼接，新增源无需再改文案。"""
    return "；".join(f"{s.name} {s.label}" for s in SOURCES)


def get_source(name: str):
    """按名称取数据源类（工厂）。返回类而非实例，因其方法均为静态方法。

    实现模块在调用时才导入，未装该数据源依赖的环境不受影响。

    Raises:
        ValueError: 名称未登记（错误信息附带全部可选值）。
        ImportError: 该数据源的第三方依赖未安装。
    """
    spec = _SOURCES_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"未知数据源: {name}（可选: {available_sources()}）")
    try:
        module = importlib.import_module("." + spec.module, __package__)
    except ImportError as e:
        raise ImportError(
            f"数据源 {name} 的依赖未安装: {e}"
            f"（装上对应依赖后重试，或改用 {available_sources()} 中的其它源）") from e
    return getattr(module, spec.cls)


def available_freqs() -> list[str]:
    """已登记频率名列表。"""
    return [f.name for f in FREQS]


def freq_help() -> str:
    """频率帮助文案：由注册表逐条拼接。"""
    return "频率：" + " / ".join(f"{f.name} {f.label}" for f in FREQS)


def get_freq(name: str) -> FreqSpec:
    """按名称取频率规格（含对应行情表名）。

    Raises:
        ValueError: 频率未登记（错误信息附带全部可选值）。
    """
    spec = _FREQS_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"不支持的频率: {name}（可选: {available_freqs()}）")
    return spec


def freq_tables() -> dict[str, str]:
    """频率名到行情表名的映射（供仓储层构造表名注册表）。"""
    return {f.name: f.table for f in FREQS}


def periods_per_year(freq: str) -> int:
    """绩效年化基准：恒为 252 个交易日（与行情频率无关）。

    回测引擎按"交易日"记录净值快照（每个交易日一条，分钟级亦然），
    因此 annual_return / sharpe / annual_volatility 的周期数就是交易日数。
    这个函数领取 freq 参数只是为了做频率合法性校验，并集中说明该口径，
    避免调用方误以为"分钟频率要用 252×48 年化"而引入外推失真。

    Raises:
        ValueError: 频率未登记。
    """
    get_freq(freq)          # 校验频率合法
    return _TRADING_DAYS_PER_YEAR


def bars_per_day(freq: str) -> int:
    """该频率每个交易日的 bar 数（日线为 1）。

    Raises:
        ValueError: 频率未登记。
    """
    return get_freq(freq).bars_per_day


def is_daily(freq: str) -> bool:
    """是否为日线频率（每日一根 bar）。

    仓储层据此决定时间列名（date / date_time）与增量续抓边界。

    Raises:
        ValueError: 频率未登记。
    """
    return bars_per_day(freq) == 1


def default_freq() -> str:
    """默认频率名。

    显式返回 '1d' 而非取 FREQS[0]——避免注册表条目重排时
    默认行为被静默改变。
    """
    return "1d"
