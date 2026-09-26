"""源码注释风格自检：检出 Python 源码注释里的 markdown 语法与 emoji。

背景
----
docstring 会被 IDE 悬浮提示、help()、Sphinx 等程序消费，而这些环境不渲染
markdown——加粗标记会原样显示成裸符号，管道表格会变成一行竖线。因此源码
注释统一采用 PEP 257 + RST（reStructuredText），即 Python 官方文档字符串
方言。完整约定见 docs/DEVELOPMENT.md 第 8.6 节。

检出的四类违规
--------------
1. markdown 加粗      两个星号包裹的强调文本
2. markdown 表格      以管道符开头并结尾的独立行（RST 无此写法）
3. markdown 代码块    三个反引号围栏
4. emoji              图形化 emoji 字符（终端状态符号 ✓ 与 ✗ 除外）

实现要点
--------
先用 tokenize 切出注释与字符串 token，再在 token 文本上匹配规则。这样
代码里的幂运算与字典解包天然不会被误报，且规则只作用于注释与字符串——
正是这套约定要约束的范围。

用法
----
    python tools/check_style.py                # 扫描 quant / tests / tools
    python tools/check_style.py quant tests    # 只扫指定路径
    python tools/check_style.py --quiet        # 仅输出汇总

退出码
------
0 = 无违规；1 = 存在违规（可直接用作 CI 门禁）。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import tokenize
from pathlib import Path

# ── 规则构造 ──────────────────────────────────────────────────────────────
# 用 chr() 拼接而非字面量，使本文件自身也符合它所检查的规范（自举）。
_STAR = chr(42)                     # 星号
_TICK = chr(96)                     # 反引号
_ESC = re.escape(_STAR)             # 正则里的字面星号

# markdown 加粗：两端不紧邻标识符/括号（避开与代码混排），内容不含星号且不跨行
_BOLD = re.compile(
    r"(?<![A-Za-z0-9_)\]}*{])"
    + _ESC + _ESC + r"(?!\s)[^*\n]+?" + _ESC + _ESC
    + r"(?![A-Za-z0-9_(\[{])"
)
# markdown 表格：整行以管道符开头并结尾（允许行首 # 注释前缀）。
# 必须带 re.MULTILINE——docstring 是多行字符串，无此标志时 ^$ 只匹配首尾。
_MD_TABLE = re.compile(r"^\s*(?:#\s*)?\|.*\|\s*$", re.M)
# markdown 代码块：三个反引号围栏
_MD_FENCE = re.compile(_TICK * 3)
# emoji：图形化字符；✓(U+2713) 与 ✗(U+2717) 属普通符号，刻意不在其中
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"          # 图标类
    "\u2699\u26A0\u26D4\u2705\u274C"  # 齿轮 / 警告 / 禁止 / 对勾按钮 / 叉按钮
    "\u2757\u2B50\u2714\u2716"        # 叹号 / 星 / 重对勾 / 重叉
    "]"
    "|[\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF]\uFE0F"   # 符号 + 变体选择符 = emoji 呈现
)

_RULES = (
    ("markdown 加粗", _BOLD),
    ("markdown 表格", _MD_TABLE),
    ("markdown 代码块", _MD_FENCE),
    ("emoji", _EMOJI),
)

DEFAULT_TARGETS = ("quant", "tests", "tools")


def scan_source(text: str) -> list[tuple[int, str, str]]:
    """扫描源码文本，返回 (行号, 规则名, 片段) 列表。

    只检查注释与字符串 token——代码里的幂运算、字典解包因此不会被误报，
    这既避免了噪音，也正好对齐规范约束的范围（注释与文档字符串）。

    多行字符串（docstring 是一个整体 token）按行展开后再匹配，这样能报出
    精确行号，也不会因某一行已命中而漏掉同一 token 内的其它行。
    """
    hits: list[tuple[int, str, str]] = []
    try:
        stream = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in stream:
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            for offset, line in enumerate(tok.string.splitlines()):
                for rule, pattern in _RULES:
                    if pattern.search(line):
                        hits.append((tok.start[0] + offset, rule, line.strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 语法不完整的文件跳过即可，交由 py_compile / 测试去报错
        pass
    return hits


def collect_files(targets: list[str]) -> list[Path]:
    """收集待扫描的 .py 文件（目录递归，文件直接采用）。"""
    found: list[Path] = []
    for item in targets:
        path = Path(item)
        if path.is_file() and path.suffix == ".py":
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.rglob("*.py")))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="源码注释风格自检：检出 markdown 语法与 emoji。")
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_TARGETS),
                        help="扫描目标（默认 quant / tests / tools）")
    parser.add_argument("--quiet", action="store_true",
                        help="只输出汇总，不逐条列出")
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    if not files:
        print("未找到可扫描的 .py 文件")
        return 0

    total = 0
    for path in files:
        hits = scan_source(path.read_text(encoding="utf-8", errors="replace"))
        if not hits:
            continue
        total += len(hits)
        if not args.quiet:
            print()
            print(path)
            for lineno, rule, snippet in hits:
                print(f"  {lineno:>4}  [{rule}]  {snippet[:72]}")

    print()
    print(f"扫描 {len(files)} 个文件，发现 {total} 处违规")
    if total:
        print("规范见 docs/DEVELOPMENT.md 第 8.6 节")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
