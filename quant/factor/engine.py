"""因子计算引擎：行情宽表 → 因子宽表（纯函数，不碰存储）。

编排（加载行情/落库）由 app/service 做，本类保持纯函数、可离线单测。
"""
from __future__ import annotations

import pandas as pd

from ..core.abstractions import Factor
from .base import get


class FactorEngine:
    """compute(bars, names) → 宽表 [code, dt, <因子列...>]。

    bars：仓储统一列口径（code/dt/open/close/volume/...），可多标的；
    行序保留输入顺序（引擎按 code+dt 合并回行情，天然对齐）。
    """

    def compute(self, bars: pd.DataFrame, names: list[str]) -> pd.DataFrame:
        if not names:
            return pd.DataFrame(columns=["code", "dt"])
        if bars.empty:
            return pd.DataFrame(columns=["code", "dt", *names])

        # 因子名不得与行情列重名（引擎按列名左连接注入，冲突会污染行情列）
        clash = [n for n in names if n in bars.columns]
        if clash:
            raise ValueError(f"因子名与行情列冲突: {clash}")

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
