"""数据源与频率注册表（数据层的单一事实来源）。

这两份清单原先各散落多处：数据源名单同时写在服务层工厂的字典与 CLI 的
--source choices 里，频率名单同时写在仓储的表名映射、CLI 三个子命令的
choices 与 Web 端点的取值校验里。每加一个源或一个频率都要逐处同步，
漏改一处就会出现"CLI 能选、服务层不认"这类不一致。

本模块把它们收拢到唯一一处声明，其余位置一律从这里派生：

SOURCES 声明数据源：名称、一句话说明、适配器模块名与类名。这里只存
"模块名 + 类名"而不是直接导入实现类，是为了不把各数据源的第三方依赖
绑到数据层上——baostock 与 akshare 的适配器在模块顶层 import 各自的库，
若在本模块直接导入，未安装其中任意一家的环境连 CLI 都启动不了。真正的
导入推迟到 get_source 调用时进行。

FREQS 声明频率：名称、行情表名、一句话说明。仓储的表名映射（TABLES）
与 CLI 的 --freq choices、Web 端点的取值校验全部由它派生。

扩展方式
--------
新数据源 = 新增一个适配器文件 + 在 SOURCES 里加一行。CLI 的 --source
choices 与 Web 侧校验随之自动生效，无需改动入口代码。

新频率 = 在 FREQS 里加一行（表名随之进入仓储的表名映射），另需补齐
仓储的读写分派与建表 DDL——不同频率的列口径与取数 SQL 本就不同，
这部分无法由清单取代。

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
    """频率规格：名称、对应行情表名、说明。"""

    name: str
    table: str
    label: str


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("baostock", "免费稳定，日线与分钟线",
               "baostock_source", "BaostockSource"),
    SourceSpec("akshare", "免费聚合多源，日线与分钟线",
               "akshare_source", "AkshareSource"),
    SourceSpec("wind", "需本机安装并登录 Wind 终端",
               "wind_source", "WindSource"),
)

FREQS: tuple[FreqSpec, ...] = (
    FreqSpec("1d", "ods_d_stock_quotation_i", "日线"),
    FreqSpec("5min", "ods_mi_stock_quotation_i", "5分钟线"),
)

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

    实现模块在调用时才导入：只用 baostock 的环境不必安装 akshare，
    反之亦然。

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
