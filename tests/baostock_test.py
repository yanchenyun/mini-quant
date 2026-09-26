import baostock.common.contants as c
# 打印 baostock 内置的服务器地址与端口
print([x for x in dir(c) if not x.startswith('__')])
print(c.BAOSTOCK_SERVER_IP, c.BAOSTOCK_SERVER_PORT)

import socket

HOST, PORT = "www.baostock.com", 10030
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect((HOST, PORT))
    print("✅ 端口可达，网络没问题")
except Exception as e:
    print(f"❌ 连不上：{type(e).__name__} {e}")
finally:
    s.close()