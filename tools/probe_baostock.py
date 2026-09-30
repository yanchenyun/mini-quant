"""baostock 连接真相探测：判定"裸 socket 超时、却能登录成功"的矛盾。

背景
----
tools/diagnose_network.py 在家里 / 公司两处跑出了同一个自相矛盾的结果：

    [2/5] TCP     www.baostock.com:10030   0/3  TimeoutError
    [5/5] 端到端  baostock : 登录成功 ✓  (0.1s)

同一个进程、前后只差几十秒，既"连不上 10030"又"登录成功"。矛盾的出口只有
两个，本脚本专门用来判定到底是哪一个：

    出口 A  连的确实是远端
            说明 10030 的阻断是"有条件"的（间歇性 / 分时段 / 分机器），
            6 秒超时的单次采样不足以据此断言"不可达"；
    出口 B  连的不是远端
            说明存在本地或网关侧的"接住"者（企业管控客户端、透明代理、
            WFP 重定向等），它替你把 10030 应答了，公网 10030 其实不通。

判定手段（三条互相独立的证据）
------------------------------
1. 对端直读。登录成功后读 baostock 的默认 socket 对象
   （baostock.common.context.default_socket），取 getpeername() 与
   getsockname()。这是最硬的一条：它直接告诉你"到底连了谁"。
2. 连接追踪。登录期间临时包装 socket.socket.connect / create_connection /
   getaddrinfo，记录每一次连接尝试的目标地址与耗时，看 baostock 是否
   绕开了 www.baostock.com:10030。
3. 交替对照。同一进程内循环 [裸 socket 探测 -> bs.login()] 多轮，看两者
   是"持续不一致"（恒定的路径差异）还是"时好时坏"（间歇性阻断）。

设计要点
--------
- 只读探测：不改任何系统配置、不写业务数据；socket 包装只在进程内有效，
  退出即还原。
- 依赖复用：环境指纹（出口 IP / 网卡 / 管控软件 / 防火墙）直接复用同目录
  的 diagnose_network 模块，避免两份实现漂移。
- 缺 baostock 时降级：只跑裸 socket 部分，不报错退出。

用法
----
    python tools/probe_baostock.py          # 默认 3 轮
    python tools/probe_baostock.py -n 5     # 5 轮，用于观察间歇性

注意：网络不通时，每轮的登录探测要等约 20 秒超时，多轮会比较慢。

依赖
----
标准库 + baostock（可选）。
"""
from __future__ import annotations

import argparse
import ast
import ipaddress
import os
import socket
import sys
import time

# ── 同目录工具复用（环境指纹采集），缺失则降级跳过 ──────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from diagnose_network import (egress_ip, firewall_summary,
                                  local_adapters, managed_agents)
    _HAS_ENV = True
except Exception:                       # noqa: BLE001 - 工具缺失属正常降级
    _HAS_ENV = False

# ── 被探测目标 ───────────────────────────────────────────────────────
BAOSTOCK_HOST, BAOSTOCK_PORT = "www.baostock.com", 10030
_TCP_TIMEOUT = 6.0                      # 与 diagnose_network 的采样超时保持一致


# ══════════════════════════════════════════════════════════════════════
# 连接追踪：记录登录期间每一次 socket 连接的目标
# ══════════════════════════════════════════════════════════════════════
class ConnectTrace:
    """进程内的 socket 连接记录器（上下文管理器，退出自动还原）。

    为什么用包装而不是抓包：包装看到的是"应用层请求连谁"，与 bs.login()
    内部代码走的是同一条路径，因此能直接暴露 baostock 是否绕开了我们在
    TCP 层探测的那个地址。
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        # 每项为 (属主对象, 属性名, 原实现, 该属性是否本来就定义在属主自身)
        self._patched: list[tuple[object, str, object, bool]] = []

    def __enter__(self) -> "ConnectTrace":
        self._patch(socket.socket, "connect", self._wrap_connect)
        self._patch(socket, "create_connection", self._wrap_create_connection)
        self._patch(socket, "getaddrinfo", self._wrap_getaddrinfo)
        return self

    def __exit__(self, *_exc: object) -> None:
        for owner, name, original, had_own in reversed(self._patched):
            if had_own:
                setattr(owner, name, original)
            else:
                try:                        # 原本是继承来的，删掉即恢复继承
                    delattr(owner, name)
                except AttributeError:
                    pass

    def _patch(self, owner: object, name: str, wrapper: object) -> None:
        """把 owner.name 换成 wrapper(original, ...) 形式，并登记还原信息。"""
        had_own = name in getattr(owner, "__dict__", {})
        original = getattr(owner, name)
        self._patched.append((owner, name, original, had_own))

        def wrapped(*args: object, **kwargs: object) -> object:
            return wrapper(original, *args, **kwargs)   # type: ignore[operator]

        setattr(owner, name, wrapped)

    def _wrap_connect(self, original: object, sock: object,
                      address: object) -> object:
        started = time.time()
        try:
            result = original(sock, address)            # type: ignore[operator]
        except Exception as exc:                        # noqa: BLE001
            self.events.append(f"connect {address}  "
                               f"{time.time() - started:.2f}s  失败"
                               f"({type(exc).__name__})")
            raise
        self.events.append(f"connect {address}  "
                           f"{time.time() - started:.2f}s  成功")
        return result

    def _wrap_create_connection(self, original: object, address: object,
                                *args: object, **kwargs: object) -> object:
        started = time.time()
        try:
            result = original(address, *args, **kwargs)  # type: ignore[operator]
        except Exception as exc:                         # noqa: BLE001
            self.events.append(f"create_connection {address}  "
                               f"{time.time() - started:.2f}s  失败"
                               f"({type(exc).__name__})")
            raise
        self.events.append(f"create_connection {address}  "
                           f"{time.time() - started:.2f}s  成功")
        return result

    def _wrap_getaddrinfo(self, original: object, host: object, port: object,
                          *args: object, **kwargs: object) -> object:
        infos = original(host, port, *args, **kwargs)    # type: ignore[operator]
        ips = sorted({item[4][0] for item in infos if item[4]})
        if ips:
            self.events.append(f"getaddrinfo {host}:{port} -> {', '.join(ips)}")
        return infos


# ══════════════════════════════════════════════════════════════════════
# 裸 socket 探测（与 diagnose_network 同一口径，便于交叉比对）
# ══════════════════════════════════════════════════════════════════════
def tcp_probe(host: str, port: int,
              timeout: float = _TCP_TIMEOUT) -> tuple[bool, float, str]:
    """单次裸 socket 探测，返回（是否成功, 耗时秒, 失败类型或 ok）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    started = time.time()
    try:
        sock.connect((host, port))
        return True, time.time() - started, "ok"
    except OSError as exc:
        return False, time.time() - started, type(exc).__name__
    finally:
        sock.close()


# ══════════════════════════════════════════════════════════════════════
# 登录探测：结果 + 真实对端 + 连接轨迹
# ══════════════════════════════════════════════════════════════════════
def inspect_login() -> dict[str, object]:
    """调用一次 bs.login()，并抓取真实对端与连接轨迹。"""
    info: dict[str, object] = {
        "available": False, "ok": False, "elapsed": 0.0,
        "error_code": "", "error_msg": "", "peer": "", "local": "",
        "trace": [],
    }
    try:
        import baostock as bs
        import baostock.common.context as context
    except ImportError as exc:
        info["error_msg"] = f"baostock 不可用（{exc}）"
        return info

    info["available"] = True
    trace = ConnectTrace()
    started = time.time()
    try:
        with trace:
            login_result = bs.login()
        info["error_code"] = str(login_result.error_code)
        info["error_msg"] = str(login_result.error_msg)
        info["ok"] = info["error_code"] == "0"
    except Exception as exc:                    # noqa: BLE001 - 诊断需容错
        info["error_msg"] = f"{type(exc).__name__}: {exc}"
    info["elapsed"] = time.time() - started
    info["trace"] = list(trace.events)

    # 关键一步：直接问 socket 对象它连的是谁
    default_socket = getattr(context, "default_socket", None)
    if default_socket is not None:
        for key, method in (("peer", "getpeername"), ("local", "getsockname")):
            try:
                info[key] = str(getattr(default_socket, method)())
            except OSError:
                info[key] = ""              # 连接失败时 socket 处于坏状态

    try:
        bs.logout()
    except Exception:                       # noqa: BLE001
        pass
    return info


def classify_peer(peer: str) -> str:
    """把 getpeername() 的字符串分类：公网远端 / 内网 / 本机回环。"""
    if not peer:
        return "未知（拿不到对端）"
    try:
        host = ast.literal_eval(peer)[0]
    except (ValueError, SyntaxError, TypeError, IndexError):
        return "无法解析"
    try:
        address = ipaddress.ip_address(str(host))
    except ValueError:
        return f"域名 {host}"
    if address.is_loopback:
        return "本机回环  <- 存在本地中间人"
    if address.is_private:
        return "内网地址  <- 存在网关侧中间人"
    return "公网远端  <- 走的是真实链路"


# ══════════════════════════════════════════════════════════════════════
# 环境指纹与防火墙出站 Block 规则
# ══════════════════════════════════════════════════════════════════════
def list_outbound_blocks(limit: int = 12) -> list[str]:
    """列出 Windows 防火墙的出站 Block 规则，用于排查"静默丢包"来源。

    出站 Block 规则被丢包时不会报错，表现为连接超时，正是 baostock 报错
    的样子，因此单独列出来看。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    paths = (
        r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"
        r"\FirewallPolicy\FirewallRules",
        r"SOFTWARE\Policies\Microsoft\WindowsFirewall\FirewallRules",
    )
    found: list[str] = []
    for path in paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        index = 0
        while len(found) < limit:
            try:
                _, value, _ = winreg.EnumValue(key, index)
                index += 1
            except OSError:
                break
            text = str(value)
            low = text.lower()
            if "action=block" in low and "dir=out" in low:
                found.append(text[:170])
    return found


def _print_env() -> None:
    """打印环境指纹（与 diagnose_network 同源，便于两份输出并排比对）。"""
    print("\n[环境指纹] 与 diagnose_network.py 同源，便于两份输出并排比对")
    if not _HAS_ENV:
        print("  跳过：同目录未找到 diagnose_network.py")
        return
    for label, value in egress_ip().items():
        print(f"  {label:<10} {value}")
    adapters = local_adapters()
    print(f"  活动网卡    {'; '.join(adapters) if adapters else '未识别'}")
    agents = managed_agents()
    print(f"  管控软件    {', '.join(agents) if agents else '未检测到'}")
    fw = firewall_summary()
    print(f"  防火墙      共 {fw['total']} 条，出站 Block {fw['out_block']} 条")
    for rule in list_outbound_blocks():
        print(f"      出站Block: {rule}")


def _print_target() -> None:
    """打印探测目标、DNS 全量 A 记录与 baostock 内置服务器地址。"""
    print("\n[目标]")
    print(f"  探测目标      {BAOSTOCK_HOST}:{BAOSTOCK_PORT}")
    try:
        infos = socket.getaddrinfo(BAOSTOCK_HOST, BAOSTOCK_PORT,
                                   socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted({item[4][0] for item in infos})
        print(f"  DNS 全部 A 记录 {'; '.join(ips) if ips else '（空）'}")
    except OSError as exc:
        print(f"  DNS 解析失败  {type(exc).__name__}: {exc}")
    try:
        import baostock
        from baostock.common import contants as cons
        version = getattr(baostock, "__version__", "未声明")
        print(f"  baostock 版本   {version}")
        print(f"  内置服务器地址  {cons.BAOSTOCK_SERVER_IP}"
              f":{cons.BAOSTOCK_SERVER_PORT}")
    except ImportError:
        print("  baostock 未安装，只跑裸 socket 部分")


# ══════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════
def _print_login(info: dict[str, object]) -> None:
    if not info.get("available"):
        print(f"  bs.login() 跳过：{info.get('error_msg')}")
        return
    mark = "✓ 成功" if info["ok"] else "✗ 失败"
    print(f"  bs.login() {mark}  {float(info['elapsed']):.2f}s  "
          f"error_code={info['error_code']} {info['error_msg']}")
    # 只有登录成功时本端/对端才有意义：连接失败后 socket 未绑定，
    # getpeername()/getsockname() 会给出 ('0.0.0.0', 端口) 这类噪声值。
    if info["ok"] and info["local"]:
        print(f"      本端 : {info['local']}")
    if info["peer"]:
        print(f"      对端 : {info['peer']}   -> "
              f"{classify_peer(str(info['peer']))}")
    for line in info["trace"]:
        print(f"      轨迹 : {line}")


def _print_verdict(rounds: list[tuple[bool, dict[str, object]]]) -> None:
    """根据多轮结果给出判定，重点回答"登录到底连了谁"。"""
    print("\n" + "=" * 66)
    print("结论")
    print("=" * 66)
    total = len(rounds)
    tcp_fail = sum(1 for ok, _ in rounds if not ok)
    login_ok = sum(1 for _, info in rounds if info.get("ok"))
    print(f"  裸 socket 10030 失败 {tcp_fail}/{total} 轮；"
          f"bs.login() 成功 {login_ok}/{total} 轮")

    peers = {str(info.get("peer")) for _, info in rounds
             if info.get("ok") and info.get("peer")}
    for peer in sorted(peers):
        print(f"  登录成功时的对端 : {peer}   -> {classify_peer(peer)}")

    if login_ok == 0:
        print("• 登录全部失败、10030 也全部超时 —— 两条路一致，10030 确实不通。")
        print("  按 diagnose_network 的建议处理即可（换数据源或让裸 TCP 走代理）。")
        return
    if tcp_fail == total:
        print("• 出现矛盾：10030 裸 socket 全超时，但 bs.login() 有成功 —— 登录")
        print("  走的绝不是我们在 TCP 层探测的那条路。按上面的『对端』下判断：")
        print("    对端是公网远端  -> 阻断是间歇 / 有条件的，单次 6 秒采样")
        print("                       不能作为『不可达』的结论；")
        print("    对端是回环/内网  -> 存在本地或网关侧中间人，公网 10030 仍不通，")
        print("                       真正的连通性取决于那个中间人。")
        print("• 下一步：把本脚本拿到『能登录的那台机器』上再跑一次，比对两次")
        print("  的『对端』是否相同。这是整个问题唯一的分水岭。")
    else:
        print("• 10030 时通时不通（间歇性）—— 链路或策略存在抖动，")
        print("  建议用更大的 -n 多轮采样，并结合对端地址判断是否走了代理。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="baostock 连接真相探测（裸 socket 超时 vs 登录成功的矛盾判定）")
    parser.add_argument("-n", "--rounds", type=int, default=3,
                        help="交替对照轮数（默认 3；网络不通时每轮约 20 秒）")
    args = parser.parse_args()

    print("=" * 66)
    print("mini-quant · baostock 连接真相探测")
    print("=" * 66)
    _print_env()
    _print_target()

    rounds: list[tuple[bool, dict[str, object]]] = []
    for index in range(1, args.rounds + 1):
        print(f"\n[第 {index}/{args.rounds} 轮]")
        ok, elapsed, detail = tcp_probe(BAOSTOCK_HOST, BAOSTOCK_PORT)
        print(f"  裸 socket {BAOSTOCK_HOST}:{BAOSTOCK_PORT}  "
              f"{'✓' if ok else '✗'} {elapsed:.1f}s  {detail}")
        info = inspect_login()
        rounds.append((ok, info))
        _print_login(info)

    _print_verdict(rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
