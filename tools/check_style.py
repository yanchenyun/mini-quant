"""源码注释风格自检：检出 Python 源码注释里的 markdown 语法、反引号与 emoji。

背景
----
docstring 会被 IDE 悬浮提示、help()、Sphinx 等程序消费，而这些环境不渲染
markdown——加粗标记会原样显示成裸符号，管道表格会变成一行竖线，双反引号
也会带着符号一起显示。因此源码注释一律写纯文本，只保留 PEP 257 结构与
RST 章节下划线。完整约定见 docs/DEVELOPMENT.md 第 8.6 节。

检出的五类违规
--------------
1. markdown 加粗      两个星号包裹的强调文本
2. markdown 表格      以管道符开头并结尾的独立行（RST 无此写法）
3. markdown 代码块    三个反引号围栏
4. 反引号             单反引号与 RST 双反引号（程序消费端会原样显示符号）
5. emoji              图形化 emoji 字符（终端状态符号 ✓ 与 ✗ 除外）

实现要点
--------
先用 tokenize 切出注释与文档字符串 token，再在 token 文本上匹配规则。这样
代码里的幂运算、字典解包与 SQL 语句里的反引号天然不会被误报——规则只作用于
注释与文档字符串，正是这套约定要约束的范围。

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
# 反引号：单反引号与 RST 双反引号都属文档标记语法，源码注释里一律不写。
# 注册顺序放在围栏之后，围栏会先命中并给出更具体的规则名。
_BACKTICK = re.compile(_TICK)
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
    ("反引号", _BACKTICK),
    ("emoji", _EMOJI),
)

DEFAULT_TARGETS = ("quant", "tests", "tools")


def style_tokens(tokens):
    """筛出需要检查的 token：注释 + 文档字符串。

    文档字符串的判定标准是「作为独立表达式语句出现」——前一个有效 token 为
    NEWLINE / INDENT / DEDENT 或位于文件首，即模块 / 类 / 函数的第一条语句。
    数据字符串（SQL 语句、断言消息、拼接片段）因此不参与检查：它们里面的
    反引号是 MySQL 标识符引用，属代码而非注释排版，不应被误报。
    """
    prev = None
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            yield tok
            continue
        if tok.type == tokenize.STRING and prev in (None, tokenize.NEWLINE,
                                                    tokenize.INDENT,
                                                    tokenize.DEDENT):
            yield tok
        if tok.type not in (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING):
            prev = tok.type


def scan_source(text: str) -> list[tuple[int, str, str]]:
    """扫描源码文本，返回 (行号, 规则名, 片段) 列表。

    只检查注释与文档字符串 token——代码里的幂运算、字典解包、SQL 语句里的
    反引号因此不会被误报，这既避免噪音，也正好对齐规范约束的范围。

    多行字符串（docstring 是一个整体 token）按行展开后再匹配，这样能报出
    精确行号，也不会因某一行已命中而漏掉同一 token 内的其它行。
    """
    hits: list[tuple[int, str, str]] = []
    try:
        stream = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in style_tokens(stream):
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
