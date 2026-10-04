"""因子计算引擎：行情宽表 → 因子宽表（纯计算，不碰存储）。

职责：接收统一列行情 DataFrame，按因子注册表计算指定因子，返回因子宽表。
编排（加载行情 / 落库）由 app/service 负责，本类可离线单测。
"""
from __future__ import annotations

import pandas as pd

from ..core.abstractions import Factor
from .base import get


class FactorEngine:
    """行情宽表 → 因子宽表。

    输入：bars 为仓储统一列口径（code/dt/open/close/volume/...），可含多标的。
    输出：[code, dt, <因子列...>]，行序与输入对齐（按 code+dt 合并回行情）。
    """

    @staticmethod
    def compute(bars: pd.DataFrame, names: list[str]) -> pd.DataFrame:
        """计算指定因子，返回因子宽表。

        价格列的非正值与 NaN（停牌日行情、脏数据）在计算前统一归一为
        NaN——无效样本沿 NaN 语义传播，rolling/shift 会自然跳过；0 价若
        混入输入，动量会算出 -100% 与 +inf、通道下轨会被砸穿为 0。归一
        只作用于本函数内的副本，调用方的行情帧不被改动；volume 不在此列
        （停牌日零成交是真实值，量比等因子应如实看到它）。

        Args:
            bars: 统一列行情 DataFrame（至少含 code/dt/close/volume）。
            names: 因子名列表（须在因子注册表中已注册）。

        Returns:
            因子宽表 [code, dt, <因子列...>]；names 为空时返回 [code, dt]。

        Raises:
            ValueError: 因子名与行情原始列重名（防止左连接注入时污染行情列）。
        """
        if not names:
            return pd.DataFrame(columns=["code", "dt"])
        if bars.empty:
            return pd.DataFrame(columns=["code", "dt", *names])

        # 因子名不得与行情原始列重名（引擎按列名左连接注入，冲突会覆盖行情列）
        clash = [n for n in names if n in bars.columns]
        if clash:
            raise ValueError(f"因子名与行情列冲突: {clash}")

        # 无效价归一（口径见 docstring）：非正价格统一置 NaN 后再交给因子
        bars = bars.copy()
        for col in ("open", "high", "low", "close", "pre_close"):
            if col in bars.columns:
                numeric = pd.to_numeric(bars[col], errors="coerce")
                bars[col] = numeric.where(numeric > 0)

        # 按标的分组计算（因子 compute 契约：输入单标的、按 dt 升序的行情帧）
        factors: list[Factor] = [get(n) for n in names]
        parts: list[pd.DataFrame] = []
        for code, sub in bars.groupby("code", sort=False):
            sub = sub.sort_values("dt", kind="stable")
            df = pd.DataFrame({"code": code, "dt": sub["dt"].to_numpy()},
                              index=sub.index)
            for f in factors:
                df[f.name] = f.compute(sub).to_numpy()
            parts.append(df)
        return pd.concat(parts, ignore_index=True)
