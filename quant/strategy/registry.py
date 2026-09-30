"""策略注册表：新策略 = 一个文件（策略类 + 文件尾注册一行），CLI 与 Web 自动发现。

与因子层 factor.base 的注册表同构（OCP 的第二个兑现点）。策略把自己
"叫什么、怎么构造、有哪些参数、K 线上画什么辅助线"全部声明在一个
StrategySpec 里注册进本模块；cli.py 的 --strategy 选项与各策略参数、
webapp 的策略下拉与参数输入框、GET /api/strategies 端点全部由注册表
动态生成。新增策略因此不再需要改动任何入口代码。

参数规格 ParamSpec
------------------
name 是参数名，同时是构造函数关键字参数名、CLI 选项名（--name）与
Web 查询参数名，三处共用一份声明；type 是参数类型（int 或 float），
CLI 用作 argparse 的 type，Web 用作字符串转型；default 是默认值，
CLI 未传时用它构造，Web 输入框以它为初值；choices 是可选的合法取值
集合，CLI 渲染成选项、Web 渲染成下拉框；help 是一句话说明，CLI 的
--help 与 Web 输入框标签共用。

choices 存在的典型理由：参数化因子的名字即规格（如 momentum_20 /
momentum_60），只允许已注册因子对应的取值进入，把"运行期才 KeyError"
提前到"构造期就 ValueError"。

策略规格 StrategySpec
--------------------
name 是 CLI / API 名称；cls 是 Strategy 子类，构造函数的关键字参数须
与 params 一一对应；label 是中文显示名（Web 下拉与结果标题）；
params 是参数规格元组；overlay 是可选展示钩子，签名
(行情宽表, 参数字典) -> {线名: Series}，声明要附着在 K 线上的辅助线
（均线、通道等），Web 端自动渲染，不需要就留 None；describe 是可选
钩子，(参数字典) -> str，定制结果标题里的策略描述串，缺省用 label。

校验的分工
----------
类型转型与 choices 校验由 build_strategy 统一做（本模块）；跨参数
校验（如 fast < slow、entry > exit）与取值范围校验（如 risk_pct 上限）
放策略类的 __init__ 里——构造即校验，CLI 与 Web 走同一条错误路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from ..core.abstractions import Strategy


@dataclass(frozen=True)
class ParamSpec:
    """策略参数规格：CLI 选项、Web 输入框、构造函数三处共用一份声明。"""

    name: str
    type: type = int
    default: Any = None
    choices: tuple | None = None
    help: str = ""


@dataclass(frozen=True)
class StrategySpec:
    """策略规格：名称、类、显示标签、参数与可选的展示钩子。"""

    name: str
    cls: type[Strategy]
    label: str
    params: tuple[ParamSpec, ...] = ()
    overlay: Callable[[pd.DataFrame, dict[str, Any]],
                      dict[str, pd.Series]] | None = None
    describe: Callable[[dict[str, Any]], str] | None = None


# 全局策略注册表：name -> StrategySpec，由各策略模块导入时填充
_REGISTRY: dict[str, StrategySpec] = {}


def register_strategy(spec: StrategySpec) -> StrategySpec:
    """注册策略规格。name 冲突时抛 ValueError（避免静默覆盖）。"""
    if spec.name in _REGISTRY:
        raise ValueError(f"策略重复注册: {spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def get_strategy(name: str) -> StrategySpec:
    """按名字查找策略规格；未注册时抛 KeyError 并列出可用策略。"""
    if name not in _REGISTRY:
        raise KeyError(f"未知策略: {name}（可用: {available_strategies()}）")
    return _REGISTRY[name]


def available_strategies() -> list[str]:
    """返回已注册策略名列表（按注册顺序）。策略一律由调用方显式指定，无默认项。"""
    return list(_REGISTRY)


def build_strategy(name: str, raw: dict[str, Any] | None = None) -> Strategy:
    """按注册表构造策略实例（CLI 与 Web 共用的唯一构造路径）。

    raw 是原始参数字典，值允许是字符串（Web 查询参数形态）：先按
    ParamSpec 转型，再校验 choices，最后交给策略类构造。未在规格中
    声明的键视为调用方拼写错误，显式报错而不是静默忽略。

    Args:
        name: 注册表中的策略名。
        raw: 原始参数；缺省项以规格 default 补齐。

    Returns:
        就绪的策略实例（构造校验已通过）。

    Raises:
        KeyError: 策略名未注册。
        ValueError: 参数未声明 / 类型转型失败 / 取值不在 choices 内 /
            策略类构造校验不通过（如 fast >= slow）。
    """
    spec = get_strategy(name)
    raw = dict(raw or {})
    unknown = set(raw) - {p.name for p in spec.params}
    if unknown:
        raise ValueError(f"策略 {name} 未声明的参数: {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for p in spec.params:
        value = raw.get(p.name, p.default)
        try:
            value = p.type(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"参数 {p.name} 无法转为 {p.type.__name__}: {value!r}") from e
        if p.choices is not None and value not in p.choices:
            raise ValueError(
                f"参数 {p.name} 仅支持 {list(p.choices)}，收到 {value!r}")
        kwargs[p.name] = value
    return spec.cls(**kwargs)
