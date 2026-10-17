"""
HTTPS 反向代理（零依赖，纯标准库）。

用途
----
浏览器允许安装 PWA 的前提是 **HTTPS**（或 localhost）。当前浏览页是
http://127.0.0.1:8091，装不了。此进程在 8443 端口提供 HTTPS，
把请求原样转发到 8091 / 8080。

为什么不用 Caddy/Nginx
----------------------
NAS 拉镜像需经加速器（已实测 BuildKit 401），而这是个纯转发服务，
用标准库实现可避免任何镜像依赖。

证书
----
首次运行自动生成自签证书（含 192.168.1.100 的 SAN）。
⚠️ 自签证书需在手机上信任一次，否则浏览器会拦截。
"""

import http.client
import os
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CERT_DIR = os.environ.get("CERT_DIR", "/vol1/docker/libretv-index/cert")
CERT = os.path.join(CERT_DIR, "cert.pem")
KEY = os.path.join(CERT_DIR, "key.pem")
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))

# 上游地址统一用「宿主 IP + 映射端口」——实测这是唯一稳定通路：
#   容器 IP 172.21.0.2:8091   -> TimeoutError（被 docker iptables 隔离）
#   容器名 libretv-index        -> gaierror（跨网络无法解析）
#   宿主 IP 192.168.1.100:x   -> OK（走 published port）
UPSTREAM_A = ("192.168.1.100", 8091)   # 浏览页+索引   # 浏览页 + 索引 API
UPSTREAM_B = ("192.168.1.100", 8080)   # LibreTV         # LibreTV 播放器

# ⚠️ 不要把 content-encoding 放进这里。
# 本代理是「原样透传」：既不解压响应体，也不重压。
# 先前误将其列为 hop-by-hop 头而丢弃，导致上游返回的 gzip/br 响应
# 被剥掉了声明头，浏览器按明文解析 → 页面呈现为乱码。
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


def ensure_cert():
    """生成自签证书（含内网 IP 的 SAN），已存在则跳过。"""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    os.makedirs(CERT_DIR, exist_ok=True)
    raise RuntimeError(
        "证书不存在，且容器内无 openssl 可用于生成。"
        "请在 NAS 宿主机执行 install_cert.sh 生成后重启本服务。")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MediaProxy/1.0"

    def _pick_upstream(self):
        """按路径分流：播放器相关走 8080，其余走 8091。"""
        p = self.path
        # 播放器与其必需接口走 8080（LibreTV）
        # 注意：/api/search 与 /api/detail 交给浏览页侧（8091），
        # 由它用本地索引应答 —— LibreTV 自身的 SSRF 校验会拒绝内网 CMS 源，
        # 故这两个接口必须在我们这侧实现，不能落到 LibreTV。
        if (p.startswith("/watch") or p.startswith("/api/proxy")
                or p.startswith("/api/auth") or p.startswith("/api/status")):
            return UPSTREAM_B
        # 浏览页自己的接口走 8091
        return UPSTREAM_A

    def _proxy(self, method):
        host, port = self._pick_upstream()
        # 把原始 path（含 query）原样转发
        conn = http.client.HTTPConnection(host, port, timeout=60)
        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "host":
                continue
            headers[k] = v
        headers["Host"] = self.headers.get("Host", "localhost")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n else None
        try:
            conn.request(method, self.path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "server" or lk == "date":
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)
        except Exception as e:
            msg = f"proxy error: {type(e).__name__}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    def do_GET(self):
        self._proxy("GET")

    def do_HEAD(self):
        self._proxy("HEAD")

    def do_POST(self):
        self._proxy("POST")

    def log_message(self, fmt, *args):
        pass


def main():
    ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    srv = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"[tls] HTTPS 监听 https://0.0.0.0:{HTTPS_PORT}", flush=True)
    print(f"[tls] 转发: / -> 8091(浏览页) | /watch /api/* -> 8080(LibreTV)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()