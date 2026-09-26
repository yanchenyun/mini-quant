"""网络与环境自检工具（数据源连通性诊断）。

用途
----
当出现 "baostock 连不上 / akshare 拉不到数据" 时，运行本脚本一次性给出
分层诊断结论，快速区分是 **代码问题** 还是 **网络/代理问题**：

    第 0 层  环境指纹      —— 出口 IP / 网卡与 VPN 状态 / 管控软件 / 防火墙
    第 1 层  DNS 解析      —— 域名能不能解析出 IP
    第 2 层  TCP 连通性    —— 目标端口能不能建立 TCP 连接（对 baostock 最关键）
    第 3 层  HTTPS 请求    —— 走应用层协议能不能拿到 200（对 akshare 最关键）
    第 4 层  代理环境      —— 环境变量代理是否指向一个"坏代理"
    第 5 层  端到端        —— 真正调一次 baostock 登录 / akshare 拉数

第 0 层是为了**跨环境对比**设计的：在"家里 / 公司 / 手机热点"各跑一次，把两份
输出并排比对，就能判定问题出在"网络位置"还是"这台机器"。

设计要点
--------
- **只读诊断**：不修改任何配置、不写业务数据，纯探测。
- **对照法**：每个目标都配一个"已知正常"的对照目标，把"个别目标被墙"
  和"整体断网"区分开。
- **TCP 探测用裸 socket**：裸 socket 不受 HTTP_PROXY 影响，能真实反映
  网络层可达性；这正是 baostock（裸 TCP 协议）走的路径。

用法
----
    python tools/diagnose_network.py            # 全量诊断
    python tools/diagnose_network.py --quick    # 只跑 TCP 层（几秒出结果）

依赖
----
标准库为主；baostock / akshare / requests 为可选（缺失则跳过对应层）。
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

# ── 被诊断目标（业务相关）────────────────────────────────────────────
# baostock：裸 TCP 协议，端口 10030（这是它与其他数据源最本质的区别）
BAOSTOCK_HOST, BAOSTOCK_PORT = "www.baostock.com", 10030
# akshare 日线接口依赖的行情域名（东方财富 push2his）
AK_DAILY_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                "?secid=1.600519&klt=101&fqt=1&beg=20240101&end=20240110"
                "&fields1=f1&fields2=f51,f52")

# ── 对照目标（"已知应该正常"的参照物，用于区分"个别被墙" vs "整体断网"）
TCP_CONTROLS = [
    ("www.baostock.com", 443, "baostock 同主机 443（对照）"),
    ("qt.gtimg.cn", 443, "腾讯行情（对照/备用数据源）"),
    ("hq.sinajs.cn", 443, "新浪行情（对照/备用数据源）"),
    ("www.baidu.com", 443, "百度（对照/基础连通性）"),
]
HTTP_CONTROLS = [
    ("腾讯行情", "https://qt.gtimg.cn/q=sh600519", {}),
    ("新浪行情", "https://hq.sinajs.cn/list=sh600519",
     {"Referer": "https://finance.sina.com.cn"}),   # 新浪要求 Referer
    ("百度", "https://www.baidu.com/", {}),
]

_PROBE_TIMEOUT = 6.0        # 单次 TCP 探测超时（秒）
_PROBE_ROUNDS = 3           # 每个目标采样次数（>1 以识别"间歇性"）


@dataclass
class ProbeResult:
    """一次连通性探测的汇总结果。"""

    target: str
    ok: int                 # 成功次数
    total: int              # 采样次数
    detail: str             # 失败原因（全部成功时为 'ok'）

    @property
    def all_ok(self) -> bool:
        return self.ok == self.total

    @property
    def none_ok(self) -> bool:
        return self.ok == 0


# ══════════════════════════════════════════════════════════════════════
# 第 1 层：DNS
# ══════════════════════════════════════════════════════════════════════
def check_dns(host: str) -> str | None:
    """解析域名，返回 IP；失败返回 None。"""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


# ══════════════════════════════════════════════════════════════════════
# 第 2 层：TCP 连通性（裸 socket，绕过一切 HTTP 代理）
# ══════════════════════════════════════════════════════════════════════
def tcp_probe(host: str, port: int,
              rounds: int = _PROBE_ROUNDS,
              timeout: float = _PROBE_TIMEOUT) -> ProbeResult:
    """对 host:port 做 rounds 次 TCP 连接尝试，返回成功计数与失败类型。

    裸 socket 不经 HTTP_PROXY，因此其结果是"网络层"的真实可达性 ——
    baostock 走的正是这条路，本函数是诊断它的核心手段。
    """
    ok, err = 0, ""
    for _ in range(rounds):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            ok += 1
        except OSError as exc:
            err = type(exc).__name__
        finally:
            sock.close()
    return ProbeResult(f"{host}:{port}", ok, rounds, err or "ok")


# ══════════════════════════════════════════════════════════════════════
# 第 3 层：HTTPS 请求（走 requests，暴露应用层/SNI 层拦截）
# ══════════════════════════════════════════════════════════════════════
def https_probe(url: str, rounds: int = _PROBE_ROUNDS,
                headers: dict | None = None) -> ProbeResult:
    """对 URL 发起 rounds 次 HTTPS GET（显式忽略环境代理），统计 200 次数。

    与 TCP 层的差异很关键：常见现象是 **TCP 通（5/5）但 HTTPS 全失败**，
    这是 SNI/应用层拦截的特征，而非端口不通。
    """
    try:
        import requests
    except ImportError:
        return ProbeResult(url, 0, rounds, "requests 未安装，跳过")

    ok, err = 0, ""
    session = requests.Session()
    session.trust_env = False                 # 忽略 HTTP_PROXY 等环境变量
    for _ in range(rounds):
        try:
            resp = session.get(url, timeout=12, headers=headers or {})
            if resp.status_code == 200:
                ok += 1
            else:
                err = f"HTTP {resp.status_code}"
        except Exception as exc:              # noqa: BLE001 - 诊断需容错
            err = type(exc).__name__
    return ProbeResult(url.split("/")[2], ok, rounds, err or "ok")


def https_via_proxy(url: str, proxy: str, rounds: int = 2) -> ProbeResult:
    """经指定 HTTP 代理请求 URL，用于判断"代理能否绕过拦截"。"""
    try:
        import requests
    except ImportError:
        return ProbeResult(url.split("/")[2], 0, rounds, "requests 未安装，跳过")

    ok, err = 0, ""
    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy}
    for _ in range(rounds):
        try:
            resp = session.get(url, timeout=12, proxies=proxies)
            if resp.status_code == 200:
                ok += 1
            else:
                err = f"HTTP {resp.status_code}"
        except Exception as exc:              # noqa: BLE001
            err = type(exc).__name__
    return ProbeResult(f"{url.split('/')[2]} via {proxy}", ok, rounds, err or "ok")


# ══════════════════════════════════════════════════════════════════════
# 第 4 层：代理环境
# ══════════════════════════════════════════════════════════════════════
def proxy_env() -> dict[str, str]:
    """读取进程可见的代理环境变量（不含 NO_PROXY —— 它不是代理地址）。"""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def proxy_request_ok(proxy: str, url: str, timeout: float = 8.0) -> bool:
    """通过指定 HTTP 代理请求 url，能拿到 200 才算通。

    为什么不能只看端口：端口处于 LISTENING ≠ 代理转发可用。而且一个代理
    可能"能通百度、却通不了东财行情"（半死状态）——因此必须按目标分别验证。
    """
    try:
        import requests
    except ImportError:
        return False
    session = requests.Session()
    session.trust_env = False                 # 只测这一个代理，不叠加其他代理
    try:
        resp = session.get(url, timeout=timeout,
                           proxies={"http": proxy, "https": proxy})
        return resp.status_code == 200
    except Exception:                          # noqa: BLE001 - 诊断需容错
        return False


def proxy_alive(proxy: str) -> bool:
    """代理是否"基本可用"（以能否访问百度为判据）。"""
    return proxy_request_ok(proxy, "https://www.baidu.com/")


# ══════════════════════════════════════════════════════════════════════
# 第 5 层：端到端
# ══════════════════════════════════════════════════════════════════════
def baostock_login_probe() -> str:
    """真实调用一次 baostock.login()，返回结果描述。"""
    try:
        import baostock as bs
    except ImportError:
        return "baostock 未安装，跳过"
    started = time.time()
    try:
        lg = bs.login()
        if lg.error_code == "0":
            bs.logout()
            return f"登录成功 ✓  ({time.time() - started:.1f}s)"
        return (f"登录失败 ✗  error_code={lg.error_code} "
                f"error_msg={lg.error_msg}  ({time.time() - started:.1f}s)")
    except Exception as exc:                  # noqa: BLE001
        return f"登录异常 ✗  {type(exc).__name__}: {exc}"


# ══════════════════════════════════════════════════════════════════════
# 第 0 层：环境指纹（跨环境对比用）
# ══════════════════════════════════════════════════════════════════════
def _run(cmd: list[str], timeout: float = 10.0) -> str:
    """执行系统命令并返回文本；失败返回空串（诊断工具不应因命令失败中断）。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        raw = proc.stdout or b""
        for enc in ("gbk", "utf-8"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "ignore")
    except Exception:                          # noqa: BLE001
        return ""


def egress_ip() -> dict[str, str]:
    """探测本机出口公网 IP —— 换网络后这里会变，是最直观的"环境指纹"。"""
    result: dict[str, str] = {}
    try:
        import requests
    except ImportError:
        return {"出口": "requests 未安装，跳过"}
    session = requests.Session()
    session.trust_env = False                 # 直连探测，避免被代理改写
    for label, url in (("IPv4 出口", "http://ip.3322.net"),
                       ("IP 归属", "http://myip.ipip.net")):
        try:
            result[label] = session.get(url, timeout=8).text.strip()[:90]
        except Exception:                      # noqa: BLE001
            result[label] = "探测失败"
    return result


def local_adapters() -> list[str]:
    """列出当前有 IPv4 地址的网卡，用于识别 VPN/虚拟网卡是否处于连接态。"""
    text = _run(["ipconfig"])
    if not text:
        return []
    out: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and ("适配器" in stripped
                                       or "adapter" in stripped.lower()):
            current = stripped.rstrip(":")
            continue
        match = re.search(r"IPv4\s*(?:地址|Address).*?:\s*([\d.]+)", stripped)
        if match and current:
            out.append(f"{current} -> {match.group(1)}")
            current = None
    return out


def managed_agents() -> list[str]:
    """检测企业终端管控 / VPN 类软件（这类软件可能影响出网策略）。"""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    keywords = ("sangfor", "topsec", "ngvone", "easyconnect", "anyconnect",
                "globalprotect", "forticlient", "zscaler", "360", "huorong",
                "qqpcmgr")
    found: list[str] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Services")
    except OSError:
        return []
    i = 0
    while True:
        try:
            name = winreg.EnumKey(key, i)
            i += 1
        except OSError:
            break
        if any(tag in name.lower() for tag in keywords):
            found.append(name)
    return sorted(set(found))


def firewall_summary() -> dict[str, object]:
    """统计 Windows 防火墙：总规则数 / 出站 Block 数 / 与 10030 相关的规则。

    出站 Block 规则是"静默丢包"的典型来源（丢包表现为连接超时，
    这正是 baostock 报错的样子），因此单独拎出来看。
    """
    info: dict[str, object] = {"total": 0, "out_block": 0, "port_rules": []}
    if sys.platform != "win32":
        return info
    try:
        import winreg
    except ImportError:
        return info
    paths = (
        r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"
        r"\FirewallPolicy\FirewallRules",
        r"SOFTWARE\Policies\Microsoft\WindowsFirewall\FirewallRules",
    )
    for path in paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        i = 0
        while True:
            try:
                _, value, _ = winreg.EnumValue(key, i)
                i += 1
            except OSError:
                break
            text = str(value)
            info["total"] = int(info["total"]) + 1
            low = text.lower()
            if "action=block" in low and "dir=out" in low:
                info["out_block"] = int(info["out_block"]) + 1
            if "10030" in low or "baostock" in low:
                cast = info["port_rules"]
                assert isinstance(cast, list)
                cast.append(text[:150])
    return info


# ══════════════════════════════════════════════════════════════════════
# 报告渲染
# ══════════════════════════════════════════════════════════════════════
def _mark(result: ProbeResult) -> str:
    if result.all_ok:
        return "✓"
    if result.none_ok:
        return "✗"
    return "△"     # 部分成功 = 间歇性


def _print_fingerprint(include_ip: bool) -> None:
    """打印环境指纹：跨环境对比的关键证据。"""
    print("\n[环境指纹] 换网络后会变，用于对比'家里 / 公司 / 热点'")
    if include_ip:
        for label, value in egress_ip().items():
            print(f"  {label:<10} {value}")
    else:
        print("  出口 IP     （--quick 模式跳过探测）")
    adapters = local_adapters()
    print(f"  活动网卡    {'; '.join(adapters) if adapters else '未识别'}")
    agents = managed_agents()
    print(f"  管控软件    {', '.join(agents) if agents else '未检测到'}")
    fw = firewall_summary()
    print(f"  防火墙      共 {fw['total']} 条，出站 Block {fw['out_block']} 条")
    rules = fw["port_rules"]
    assert isinstance(rules, list)
    for rule in rules:
        print(f"      10030 相关: {rule}")


def report(quick: bool = False) -> None:
    """执行全部诊断层并打印结论。"""
    print("=" * 66)
    print("mini-quant · 数据源网络自检")
    print("=" * 66)

    _print_fingerprint(include_ip=not quick)

    # ── 第 1 层：DNS ──────────────────────────────────────────
    print("\n[1/5] DNS 解析")
    baostock_ip = check_dns(BAOSTOCK_HOST)
    print(f"  {BAOSTOCK_HOST:<30} -> {baostock_ip or '解析失败 ✗'}")

    # ── 第 2 层：TCP ─────────────────────────────────────────
    print(f"\n[2/5] TCP 连通性（裸 socket，各采样 {_PROBE_ROUNDS} 次）")
    baostock_tcp = tcp_probe(BAOSTOCK_HOST, BAOSTOCK_PORT)
    print(f"  {_mark(baostock_tcp)} {BAOSTOCK_HOST}:{BAOSTOCK_PORT}"
          f"  {baostock_tcp.ok}/{baostock_tcp.total}  ({baostock_tcp.detail})"
          f"   <- baostock 服务端口")
    for host, port, label in TCP_CONTROLS:
        res = tcp_probe(host, port)
        print(f"  {_mark(res)} {host}:{port:<5} {res.ok}/{res.total}"
              f"  ({res.detail})   <- {label}")

    if quick:
        _summarize_tcp(baostock_tcp)
        return

    # ── 第 3 层：HTTPS ──────────────────────────────────────
    print(f"\n[3/5] HTTPS 请求（显式绕过环境代理，各采样 {_PROBE_ROUNDS} 次）")
    ak_http = https_probe(AK_DAILY_URL)
    print(f"  {_mark(ak_http)} push2his.eastmoney.com(akshare 日线接口)"
          f"  {ak_http.ok}/{ak_http.total}  ({ak_http.detail})")
    for label, url, headers in HTTP_CONTROLS:
        res = https_probe(url, headers=headers)
        print(f"  {_mark(res)} {label:<16} {res.ok}/{res.total}"
              f"  ({res.detail})")

    # ── 第 4 层：代理 ───────────────────────────────────────
    print("\n[4/5] 代理环境")
    env = proxy_env()
    if env:
        # 按"唯一代理地址"聚合，避免同一个地址被 4 个变量引用时报 4 遍
        by_proxy: dict[str, list[str]] = {}
        for key, value in env.items():
            by_proxy.setdefault(value, []).append(key)
        for value, keys in by_proxy.items():
            base = proxy_alive(value)
            biz = proxy_request_ok(value, AK_DAILY_URL)
            print(f"  代理 {value}   （被 {', '.join(keys)} 引用）")
            print(f"      基础连通性 : {'✓' if base else '✗'}")
            print(f"      行情目标   : {'✓' if biz else '✗'}"
                  f"{'' if biz else '   <- 依赖它的库（如 akshare）会失败'}")
    else:
        print("  （未设置代理环境变量）")

    # ── 第 5 层：端到端 ─────────────────────────────────────
    print("\n[5/5] 端到端")
    print(f"  baostock : {baostock_login_probe()}")
    print(f"  akshare  : 日线接口 {_mark(ak_http)} "
          f"({'可用' if ak_http.all_ok else '不可用'})")

    _summarize(baostock_tcp, ak_http, env)


def _summarize_tcp(baostock_tcp: ProbeResult) -> None:
    print("\n" + "-" * 66)
    if baostock_tcp.none_ok:
        print("结论：baostock 的 10030 端口 TCP 层不可达 —— 属于网络/代理问题，"
              "与项目代码无关。")
        print("建议：开启代理软件的 TUN 模式，或改用其他数据源（--source akshare）。")
    else:
        print("结论：TCP 层可达，问题不在此层。")


def _summarize(baostock_tcp: ProbeResult, ak_http: ProbeResult,
               env: dict[str, str]) -> None:
    """根据各层结果给出人话结论与建议。"""
    print("\n" + "=" * 66)
    print("结论与建议")
    print("=" * 66)

    if baostock_tcp.none_ok:
        print("• baostock：TCP 连不上 10030 端口 → 网络层阻断。baostock 走裸 TCP、")
        print("  不支持代理，因此只能靠『TUN 模式』或换数据源绕过。")
    elif baostock_tcp.all_ok:
        print("• baostock：TCP 可达。若仍登录失败，请核对账号/服务端状态。")

    if ak_http.none_ok:
        print("• akshare：行情接口被拦截（TCP 通但 HTTPS 失败 = SNI 层拦截）。")
    elif ak_http.all_ok:
        print("• akshare：接口正常可用。")

    if env:
        base_ok = sorted({v for v in env.values() if proxy_alive(v)})
        biz_ok = sorted({v for v in env.values() if proxy_request_ok(v, AK_DAILY_URL)})
        if base_ok and not biz_ok:
            print(f"• 代理：{base_ok} 能上普通网站、却拉不到行情接口。")
            print("  若该代理是 sandbox-cli.exe（AI 助手沙箱）—— 属正常现象，")
            print("  无需处理，但拉行情时请显式覆盖代理（如设为 7897 或清空）。")
            print("  若是其他程序残留 —— 建议从环境变量中移除或修正。")
        elif not base_ok:
            print(f"• 代理：检测到完全不可达的代理 {sorted(set(env.values()))} "
                  "—— 建议从环境变量中移除或修正。")
    print("\n建议动作（按优先级）：")
    print("  1) 开启代理软件的 TUN 模式（让裸 TCP 也走代理）后复跑本脚本；")
    print("  2) 修正/删除失效的代理环境变量；")
    print("  3) 若端口长期不可达，改用可用数据源（如 akshare / 腾讯 / 新浪）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="mini-quant 数据源网络自检")
    parser.add_argument("--quick", action="store_true",
                        help="只跑 TCP 层（几秒出结果）")
    args = parser.parse_args()
    report(quick=args.quick)
    return 0


if __name__ == "__main__":
    sys.exit(main())
