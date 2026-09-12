"""mini-quant：麻雀虽小五脏俱全的个人量化平台（简易版）。

分层（依赖箭头永远指向 core 的抽象）：
    webapp/app  ──▶  backtest / strategy  ──▶  core(抽象)  ◀──  data(实现)
"""
__version__ = "0.1.0"
