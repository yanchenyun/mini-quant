"""时间戳归一工具：吸收各数据源的时间格式脏差异。

baostock 分钟线实际返回 17 位纯数字串 'YYYYMMDDHHMMSSmmm'（含 3 位毫秒），
另有 'YYYY-MM-DDHH:MM:SS'（无空格）等形态；本工具统一归一为
'YYYY-MM-DD HH:MM:SS'（毫秒丢弃——分钟级回测用不到）。

数据源适配器入库前调用（保证库内干净），仓储读取时也调用
（兜底修复历史已入库的脏数据，无需重灌）。
"""
from __future__ import annotations


def norm_dt(s: str | None) -> str:
    """把任意已知形态的时间字符串归一为 'YYYY-MM-DD HH:MM:SS'；无法识别则原样返回。"""
    if s is None:
        return ""
    s = str(s).strip()
    if s.isdigit():
        if len(s) == 17:   # YYYYMMDDHHMMSSmmm（baostock 分钟线，含毫秒）
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
        if len(s) == 14:   # YYYYMMDDHHMMSS
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
        if len(s) == 8:    # YYYYMMDD（纯日期）
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 18 and s[10] != " ":   # 'YYYY-MM-DDHH:MM:SS'（无空格）
        return f"{s[:10]} {s[10:]}"
    return s
