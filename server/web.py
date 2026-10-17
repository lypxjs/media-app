"""
索引浏览页：封面墙 + 分类导航 + 筛选排序 + 本地进度。

单独提供 UI，不依赖 LibreTV。理由见 README 的 SSRF 说明——
LibreTV 拒绝内网源，而这个页面能完整承载「翻找列表」的核心诉求。

用法：
    python web.py            # 启动 http://0.0.0.0:8091
环境变量：
    INDEX_DB  索引库路径（默认 /data/index.db）
    PORT      端口（默认 8091）
    TRACK_DB  观看进度库（默认 /data/progress.db）
"""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import db
import api as apimod
import music
import music_online
import collections
import hashlib
import threading
import time
import urllib.request

INDEX_DB = os.environ.get("INDEX_DB", "/data/index.db")
# 注意用独立的 WEB_PORT：api.py 与 web.py 同容器并行时都读 PORT 会抢占端口
PORT = int(os.environ.get("WEB_PORT", "8091"))
PROG_DB = os.environ.get("TRACK_DB", "/data/progress.db")
# LibreTV 播放页地址：浏览页只做发现，播放交给它
LIBRETV_BASE = os.environ.get("LIBRETV_BASE", "http://127.0.0.1:8080").rstrip("/")

# 封面缓存目录（NAS 磁盘充裕，缓存命中后不再访问不稳定的图床）
COVER_DIR = os.environ.get("COVER_DIR", "/data/covers")
COVER_TIMEOUT = int(os.environ.get("COVER_TIMEOUT", "4"))
_py_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ── 封面缓存架构 ──────────────────────────────────────────────
# 实测：图床 tyyswimg2.com 按**出口 IP 限流**——本机与 NAS 同出口，
# 绝大多数请求被直接 reset（约 0.2s 即拒绝），仅偶发放行；
# 换个请求头、换解析出的 IP、加 Range 都无效，属 IP 维度封锁。
#
# 由此确立两段式策略：
#   ① 前台：/cover **只查本地缓存**。命中即返回；
#      未命中**立即返回占位图**（绝不让用户等待），同时把 URL 投入待抓队列。
#   ② 后台：单个守护线程低速顺序抓取（间隔 COVER_WARM_INTERVAL），
#      成功即落盘。缓存持久化，覆盖率随时间单调上升。
#
# 收益：页面加载不再被图床拖慢（未命中是毫秒级返回），
# 封面则随后台预热逐步"长出来"。
# 预热节奏：自适应退避。图床放行率随我们近期请求量剧烈波动
# （实测同一个固定间隔下，成功率会在 5%~60% 之间跳），
# 故不用固定间隔，改为「连续成功则加速、连续失败则退避」，
# 让程序自行收敛到当前限流窗口允许的节奏。
COVER_WARM_START = float(os.environ.get("COVER_WARM_INTERVAL", "3.0"))
COVER_WARM_MIN = float(os.environ.get("COVER_WARM_MIN", "1.2"))
COVER_WARM_MAX = float(os.environ.get("COVER_WARM_MAX", "20"))
COVER_WARM_MAX_QUEUE = 5000

_warm_lock = threading.Lock()
_warm_queue = collections.deque()
_warm_seen = set()
_warm_running = [False]

# ── 封面缓存的读写与后台预热 ──────────────────────────────────

def _cover_path(url):
    """按 URL 的 md5 分片存放，避免单目录文件过多。"""
    key = hashlib.md5(url.encode("utf-8")).hexdigest()
    return os.path.join(COVER_DIR, key[:2], key + ".img")


def _cover_read(path):
    """读缓存；不存在或过小返回 None。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > 512:
            with open(path, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


def _cover_store(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _cover_fetch(url):
    """抓一张封面。带 Referer（部分站校验），4s 超时。返回 bytes 或 None。"""
    try:
        origin = urlparse(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": _py_ua,
            "Referer": f"{origin.scheme}://{origin.netloc}/",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=COVER_TIMEOUT) as r:
            data = r.read()
        return data if len(data) > 512 else None
    except Exception:
        return None


def _warm_enqueue(url):
    """把 URL 投入预热队列（去重，超上限则丢弃最旧的）。"""
    with _warm_lock:
        if url in _warm_seen:
            return
        if len(_warm_queue) >= COVER_WARM_MAX_QUEUE:
            try:
                old = _warm_queue.popleft()
                _warm_seen.discard(old)
            except IndexError:
                pass
        _warm_queue.append(url)
        _warm_seen.add(url)
        if len(_warm_seen) > 200000:      # 防止无限增长
            _warm_seen.clear()
        if not _warm_running[0]:
            _warm_running[0] = True
            threading.Thread(target=_warm_loop, daemon=True).start()


def _warm_bootstrap():
    """服务启动时把「最近更新的封面」投入预热队列。

    只入队尚未缓存的，避免重复劳动；后台线程随后以低速逐个抓取。
    数量由环境变量 COVER_WARM_BOOTSTRAP 控制（默认 1500，设 0 可关闭）。
    """
    try:
        limit = int(os.environ.get("COVER_WARM_BOOTSTRAP", "1500"))
    except ValueError:
        limit = 1500
    if limit <= 0:
        return
    try:
        conn = db.connect(INDEX_DB)
        rows = conn.execute(
            "SELECT pic FROM videos WHERE pic IS NOT NULL AND pic != '' "
            "ORDER BY vod_time DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
    except Exception as e:
        print(f"[cover-warm] 查询失败: {e}", flush=True)
        return

    n = 0
    for (u,) in rows:
        if u and u.startswith("http") and not os.path.exists(_cover_path(u)):
            _warm_enqueue(u)
            n += 1
    if n:
        print(f"[cover-warm] 已入队 {n} 张未缓存封面"
              f"（自适应间隔 {COVER_WARM_MIN}~{COVER_WARM_MAX}s），后台抓取中",
              flush=True)


def _warm_loop():
    """后台封面预热：低速顺序抓取，节奏自适应。

    自适应依据：连续成功 → 逐步加速；连续失败 → 逐步退避。
    这样既不浪费配额（失败时不硬冲），也不无谓拖延（顺利时提速）。
    """
    interval = COVER_WARM_START
    ok = 0
    fail = 0
    ok_streak = 0
    fail_streak = 0

    while True:
        with _warm_lock:
            if not _warm_queue:
                _warm_running[0] = False
                print(f"[cover-warm] 队列已空：成功 {ok} / 失败 {fail}", flush=True)
                break
            url = _warm_queue.popleft()
            _warm_seen.discard(url)
            remain = len(_warm_queue)

        path = _cover_path(url)
        data = None if _cover_read(path) is not None else _cover_fetch(url)
        if data and _cover_store(path, data):
            ok += 1
            ok_streak += 1
            fail_streak = 0
            if ok_streak >= 3:
                interval = max(COVER_WARM_MIN, interval * 0.75)
                ok_streak = 0
        else:
            if data is None and _cover_read(path) is not None:
                pass            # 已被抓过，不算失败
            else:
                fail += 1
            fail_streak += 1
            ok_streak = 0
            if fail_streak >= 3:
                interval = min(COVER_WARM_MAX, interval * 1.7)
                fail_streak = 0

        if (ok + fail) % 25 == 0 and (ok + fail) > 0:
            rate = ok / max(1, ok + fail) * 100
            print(f"[cover-warm] 成功 {ok} 失败 {fail}（{rate:.0f}%）"
                  f" 当前间隔 {interval:.1f}s 队列余 {remain}", flush=True)

        time.sleep(interval)
    _warm_running[0] = False


BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63fcffff3f030005fe02fea72d5a2c0000000049454e44ae426082")

# ── PWA ──
MANIFEST = {
    "name": "影视库",
    "short_name": "影视库",
    "description": "本地影视索引 · 翻找与观看",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1115",
    "theme_color": "#0f1115",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
}

# 只缓存页面外壳与静态接口；/browse /api/search 等动态请求不缓存，
# 避免索引更新后仍显示旧数据。视频流（m3u8/分片）完全不碰。
SW_JS = """
const CACHE = 'shell-v7';
const SHELL = ['/', '/config', '/manifest.webmanifest', '/icons/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // 动态接口与封面图走网络（封面域名是外部，不在此处理）
  if (url.pathname.startsWith('/browse') || url.pathname.startsWith('/api/')
      || url.pathname.startsWith('/progress') || url.pathname.startsWith('/play')) return;

  // 页面导航：网络优先，失败回退缓存（断网仍能打开）
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')));
    return;
  }
  // 其余静态资源：缓存优先
  e.respondWith(caches.match(req).then(hit =>
    hit || fetch(req).then(res => {
      if (res.ok && res.type === 'basic') {
        const cp = res.clone();
        caches.open(CACHE).then(c => c.put(req, cp));
      }
      return res;
    })
  ));
});
"""


def make_icon(size: int) -> bytes:
    """生成 PNG 图标：深色圆角底 + 白色播放三角。

    纯标准库实现（zlib + struct 手写 PNG），避免引入图片素材依赖。
    三角判定用「半平面符号一致性」（三个边同侧即为内部），
    这是标准的凸多边形内点测试，比重心法稳��。
    边缘做 2x2 超采样抗锯齿，避免锯齿感。
    """
    import struct
    import zlib

    W = H = size
    bg = (15, 17, 21)
    fg = (233, 236, 242)

    cx = cy = W / 2.0
    # 播放三角：底边竖直（朝右），几何上标准「播放」形
    bw = W * 0.20      # 底边半宽
    bh = W * 0.24# 从底边到尖端的高度
    x0 = cx - bw * 0.85   # 底边 x
    y0, y1 = cy - bh / 2, cy + bh / 2
    tx = x0 + bw * 1.70   # 尖端 x

    # 三角形三个顶点
    A = (x0, y0)
    B = (x0, y1)
    C = (tx, cy)

    def cross(ax, ay, bx, by):
        """向量 (b-a) x (p-a) 的 z 分量。"""
        return (bx - ax) * (by - ay) - (ax - ax) * (by - ay)

    def in_tri(px, py):
        """半平面法：三点相对三条边的符号必须一致。"""
        def side(P1, P2, Px, Py):
            return (P2[0] - P1[0]) * (Py - P1[1]) - (P2[1] - P1[1]) * (Px - P1[0])
        d1 = side(A, B, px, py)
        d2 = side(B, C, px, py)
        d3 = side(C, A, px, py)
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (neg and pos)

    r = W * 0.215      # 圆角半径

    def in_round_rect(px, py):
        cxr = min(max(px, r), W - r)
        cyr = min(max(py, r), H - r)
        return (px - cxr) ** 2 + (py - cyr) ** 2 <= r * r

    SS = 2   # 超采样倍数
    rows = []
    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            # 超采样求覆盖度
            hit_t = 0
            hit_o = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    py = y + (sy + 0.5) / SS
                    if in_round_rect(px, py):
                        hit_o += 1
                        if in_tri(px, py):
                            hit_t += 1
            total = SS * SS
            if hit_o == 0:
                row += bytes((0, 0, 0, 0))
                continue
            # 三角覆盖度（前景）→ 颜色
            a_t = hit_t / total           # 前景 alpha
            a_o = hit_o / total           # 整体不透明度
            # 先合成前景色到背景色，再乘整体 alpha
            c = tuple(int(fg[i] * a_t + bg[i] * (1 - a_t)) for i in range(3))
            row += bytes((c[0], c[1], c[2], int(round(a_o * 255))))
        rows.append(bytes(row))

    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


OFFLINE_HTML = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>影视库V2</title><style>
body{margin:0;background:#0f1115;color:#9aa3b2;font:14px/1.6 system-ui;
 display:flex;align-items:center;justify-content:center;height:100vh;text-align:center}
</style></head><body><div><p style="font-size:15px;color:#e6e9ef">连不上 NAS</p>
<p style="font-size:13px">请确认与家里的网络连接后重试</p></div></body></html>"""



PROG_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS progress (
    src     TEXT NOT NULL,
    vod_id  INTEGER NOT NULL,
    episode INTEGER DEFAULT 0,
    episodes INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0,
    updated_at INTEGER,
    PRIMARY KEY (src, vod_id)
);
"""

_pl = threading.local()


def prog():
    if getattr(_pl, "c", None) is None:
        c = sqlite3.connect(PROG_DB, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(PROG_SCHEMA)
        _pl.c = c
    return _pl.c


# ── 页面 ──

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<title>影视库V2</title>
<!-- PWA：支持「添加到主屏幕」，点开为独立全屏窗口 -->
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1115">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="影视库">
<link rel="icon" href="/icons/icon-192.png">
<link rel="apple-touch-icon" href="/icons/icon-192.png">
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --chip:#1f232c; --line:#272c37;
  --tx:#e6e9ef; --tx2:#9aa3b2; --tx3:#6b7280; --ac:#4f8cff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
  font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
/* PWA 独立窗口下顶栏要避开状态栏/刘海（刘海屏必须） */
/* 顶部间距：APK WebView 未开 edge-to-edge，系统已留状态栏空间，
   故这里不再叠加 env(safe-area-inset-top)，只做小幅上提让标题更透气 */
header{padding-top:14px;
  padding-left:calc(16px + env(safe-area-inset-left, 0px));
  padding-right:calc(16px + env(safe-area-inset-right, 0px))}
main{padding-bottom:calc(20px + env(safe-area-inset-bottom))}
/* 独立 PWA 窗口无地址栏，给标题栏留出紧凑高度 */
@media (display-mode: standalone){
  header{padding-top:calc(10px + env(safe-area-inset-top))}
  h1{font-size:16px;margin-bottom:10px}
}
/* 触摸设备：去掉 hover 位移避免粘滞态，改用点击反馈 */
@media (hover:none){
  .card:hover{transform:none;border-color:var(--line)}
  .card:active{transform:scale(.97)}
  .cat:hover{border-color:var(--line);color:var(--tx2)}
}
/* 独立 PWA 窗口下加大点击热区 */
@media (hover:none){
  .ep{min-height:42px;display:flex;align-items:center;justify-content:center}
  .cat{min-height:34px;display:flex;align-items:center}
}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:20;background:rgba(15,17,21,.94);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 12px;font-size:17px;font-weight:600}
.search{display:flex;gap:8px;margin-bottom:12px}
input{flex:1;padding:9px 12px;background:var(--chip);border:1px solid var(--line);
  border-radius:8px;color:var(--tx);font-size:14px;outline:none}
input:focus{border-color:var(--ac)}
button{padding:9px 16px;background:var(--ac);border:0;border-radius:8px;
  color:#fff;font-size:14px;cursor:pointer;font-weight:500}
button.ghost{background:var(--chip);color:var(--tx2);border:1px solid var(--line)}
button.ghost.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.cats{display:flex;flex-wrap:wrap;gap:6px}
/* 分类多达 54 个，手机上改为横向单行滑动，避免占满整屏把封面墙挤出视野 */
@media (max-width:700px){
  .cats{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;
    -webkit-overflow-scrolling:touch;scrollbar-width:none;
    padding:2px 0 6px}
  .cats::-webkit-scrollbar{display:none}
  .cat{flex-shrink:0}
  .grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:10px}
  .name{font-size:12px}
}
.cat{padding:5px 11px;background:var(--chip);border:1px solid var(--line);
  border-radius:14px;font-size:12px;color:var(--tx2);cursor:pointer;white-space:nowrap}
.cat:hover{border-color:var(--ac);color:var(--tx)}
.cat.on{background:var(--ac);border-color:var(--ac);color:#fff}
.cat small{opacity:.6;margin-left:3px}
main{padding:20px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
  margin-bottom:16px;color:var(--tx3);font-size:12px}
.bar select,.bar input{background:var(--chip);border:1px solid var(--line);
  color:var(--tx);border-radius:6px;padding:5px 8px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;cursor:pointer;transition:.15s}
.card:hover{transform:translateY(-3px);border-color:var(--ac)}
.thumb{position:relative;aspect-ratio:2/3;background:var(--chip);overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
/* 占位海报：图床失效时的替身，纯 CSS 不依赖外部服务 */
.ph{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:8px;padding:10px;text-align:center}
.ph-t{font-size:34px;font-weight:700;color:rgba(255,255,255,.82);line-height:1}
.ph-n{font-size:11px;color:rgba(255,255,255,.62);line-height:1.4;
  word-break:break-all;max-height:3.6em;overflow:hidden}
.badge{position:absolute;top:6px;right:6px;padding:2px 6px;border-radius:4px;
  font-size:10px;background:rgba(0,0,0,.75);color:#fff}
.badge.prog{background:var(--ac)}
.badge.done{background:#16a34a}
.meta{padding:8px 10px}
.name{font-size:13px;font-weight:500;margin-bottom:3px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sub{font-size:11px;color:var(--tx3);display:flex;gap:6px;flex-wrap:wrap}
.empty{text-align:center;padding:80px 20px;color:var(--tx3)}
.load{margin:26px 0;text-align:center}
.err{background:#3a1d22;color:#ffb4bc;padding:10px 14px;border-radius:8px;
  margin-bottom:14px;font-size:13px}
/* 内嵌播放层：在浏览页内全屏播放，避免新开标签页切窗口。
   播放本身仍由 LibreTV 渲染（同源 iframe），只是换了承载方式。 */
.play-mask{position:fixed;inset:0;z-index:200;background:#000;
  display:flex;flex-direction:column}
.play-mask[hidden]{display:none}
.play-bar{display:flex;align-items:center;gap:12px;padding:8px 14px;
  background:#14171c;border-bottom:1px solid var(--line);flex:0 0 auto}
.play-bar .pt{font-size:13px;color:var(--tx2);flex:1;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.play-bar button{padding:6px 14px;font-size:13px}
.play-mask iframe{flex:1 1 auto;width:100%;border:0;background:#000}

/* 弹窗遮罩（用普通 div 而非 <dialog>，规避 WebView 兼容问题） */
.dlg-mask{position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:100;
  display:flex;align-items:center;justify-content:center;padding:16px}
.dlg-mask[hidden]{display:none}
.dlg{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  width:min(880px,94vw);max-height:88vh;overflow:auto}
.dhead{display:flex;gap:16px;padding:18px;border-bottom:1px solid var(--line);position:relative}
/* ↑ position:relative 不可省：封面加载失败时会插入一个
   position:absolute;inset:0 的占位层，若这里不是定位祖先，
   占位层会逃到最外层遮罩上，把集数按钮全盖住 → 无法选集。 */
.dhead img{width:110px;aspect-ratio:2/3;object-fit:cover;border-radius:8px;flex-shrink:0}
.dinfo{flex:1;min-width:0}
.dinfo h2{margin:0 0 8px;font-size:18px}
.dinfo p{margin:4px 0;color:var(--tx2);font-size:13px}
.eps{display:flex;flex-wrap:wrap;gap:7px;padding:18px}
.ep{padding:8px 13px;background:var(--chip);border:1px solid var(--line);
  border-radius:7px;cursor:pointer;font-size:13px}
.ep:hover{border-color:var(--ac)}
.ep.cur{background:var(--ac);border-color:var(--ac);color:#fff}
.x{background:none;border:0;color:var(--tx3);font-size:22px;cursor:pointer;
  padding:0 4px;line-height:1}
</style></head><body>
<header>
  <h1>影视库 <span id="cnt" style="color:var(--tx3);font-weight:400;font-size:13px"></span>
    <a href="/music" style="float:right;font-size:13px;font-weight:400;color:var(--tx2);background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:5px 12px;margin-top:-3px">音乐</a>
  </h1>
  <div class="search">
    <input id="q" placeholder="搜索片名…" autocomplete="off">
    <button onclick="go(1)">搜索</button>
    <button class="ghost" onclick="reset()">重置</button>
  </div>
  <div class="cats" id="cats"></div>
</header>
<main>
  <div id="err"></div>
  <div class="bar">
    <span id="info"></span>
    <select id="order" onchange="go(1)">
      <option value="time">最新更新</option>
      <option value="hits">最多播放</option>
      <option value="score">最高评分</option>
    </select>
    <input id="year" placeholder="年份" style="width:88px" onkeydown="if(event.key==='Enter')go(1)">
    <button class="ghost" onclick="go(1)">筛选</button>
    <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
      <input type="checkbox" id="unwatched" style="width:auto"
             onchange="go(1)"> 只看没看完的
    </label>
  </div>
  <div class="grid" id="grid"></div>
  <div class="load"><button id="more" class="ghost" onclick="more()"
      style="display:none">加载更多</button></div>
</main>

<div id="dlg" class="dlg-mask" hidden>
 <div class="dlg">
  <div class="dhead" id="dhead">
    <img id="dimg" alt="">
    <div class="dinfo">
      <h2 id="dname"></h2>
      <p id="dmeta"></p>
      <p style="margin-top:12px">
        <button onclick="closeDlg()">关闭</button>
      </p>
    </div>
    <button class="x" onclick="closeDlg()">&times;</button>
  </div>
  <div class="eps" id="deps"></div>
 </div>
</div>
<div id="playmask" class="play-mask" hidden>
  <div class="play-bar">
    <span class="pt" id="playtitle"></span>
    <button onclick="closePlay()">关闭播放</button>
  </div>
  <iframe id="playframe" title="播放器"
          allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
          allowfullscreen></iframe>
</div>

<script>
let page=1, typeId="", watching=false, cur=null, eps=[];
// LibreTV 播放地址，由 /config 注入（服务端持有密码与地址，前端不硬编码）
// ⚠️ 必须挂到 window：此前用 let 声明、却以 window.LIBRETV_BASE 读取，
// let 不会成为 window 属性 → 恒为 undefined → 退化成相对路径 /watch，
// 请求打到浏览页自身的 8091 端口 → {"error":"not found"}。
window.LIBRETV_BASE = '';
fetch('/config').then(r=>r.json()).then(d=>{
  window.LIBRETV_BASE = (d && d.libretv) || '';
});

async function loadCats(){
  const r = await fetch('/api/php/provide/types');
  const d = await r.json();
  const all = d.class || [];
  const el = document.getElementById('cats');
  const totalCnt = all.reduce((s,c)=>s+c.count,0);
  // 用模板字符串拼接，避免单引号字符串里嵌套双引号导致提前闭合
  const head = `<div class="cat${typeId === '' ? ' on' : ''}" ` +
               `onclick="pickType('')">全部<small>${fmt(totalCnt)}</small></div>`;
  const rest = all.map(c =>
    `<div class="cat${typeId === c.type_id ? ' on' : ''}" ` +
    `onclick="pickType('${c.type_id}')">${c.type_name}` +
    `<small>${fmt(c.count)}</small></div>`).join('');
  el.innerHTML = head + rest;
}
function pickType(id){ typeId=id; loadCats(); go(1); }
function fmt(n){ return n>=10000 ? (n/10000).toFixed(1)+'万' : n; }

async function go(p){
  page = p;
  const q = document.getElementById('q').value.trim();
  const order = document.getElementById('order').value;
  const year = document.getElementById('year').value.trim();
  const unwatched = document.getElementById('unwatched').checked;
  document.getElementById('err').innerHTML = '';
  let url;
  if(q) url = '/api/search?wd='+encodeURIComponent(q)+'&limit=60';
  else    url = '/browse?limit=30&order='+order+
               (typeId?'&type_id='+typeId:'')+(year?'&year='+year:'');
  const r = await fetch(url);
  if(!r.ok){ document.getElementById('err').innerHTML='请求失败 '+r.status; return; }
  const d = await r.json();
  let list = d.list || [];
  // 附观看进度
  const ids = list.map(x=>x.id);
  let prog = {};
  if(ids.length){
    const pr = await fetch('/progress?ids='+ids.join(','));
    if(pr.ok) prog = await pr.json();
  }
  if(unwatched){
    list = list.filter(x=>{
      const p = prog[x.id];
      return !p || (!p.finished && p.episode < (p.episodes||9999));
    });
  }
  window._prog = prog;
  // 保存当前列表供事件委托取用；渲染时传下标
  window._list = list;
  document.getElementById('grid').innerHTML = list.map((x,i)=>card(x,i)).join('');
  document.getElementById('info').textContent =
    '共 '+fmt(d.total)+' 部' + (q? '（搜索：'+q+'）' : '');
  document.getElementById('cnt').textContent = fmt(d.total||0)+' 部在库';
  document.getElementById('more').style.display = (list.length>=30&&!q)?'inline-block':'none';
  window.scrollTo({top:0,behavior:'smooth'});
  coverRetry = 0;
  setTimeout(retryPlaceholders, 9000);   // 给后台一点抓取时间再重试
}

/* 封面渲染：三级降级
   1) 原始图直连（多数情况可用）
   2) onerror -> 用片名生成占位海报（不依赖外部服务，图床全挂时仍可辨识）
   占位色相取自片名哈希，同片名颜色稳定，便于快速区分。 */
function phColor(name){
  let h=0;
  for(let i=0;i<name.length;i++){ h=(h*31+name.charCodeAt(i))>>>0; }
  return h%360;
}
function placeholder(x, cls){
  const hue = phColor(x.name||'');
  const initial = (x.name||'?').trim().slice(0,1);
  return `<div class="${cls}" style="background:hsl(${hue} 42% 26%)">
    <div class="ph-t">${esc(initial)}</div>
    <div class="ph-n">${esc((x.name||'').slice(0,14))}</div></div>`;
}

/* 卡片渲染：只写 data-i 下标，不把数据塞进内联属性。
   原因：JSON.stringify 的结果含双引号，放进 onclick="..." 会提前闭合属性，
   导致点击失效且 WebView 报 SyntaxError（本次踩坑）。 */
function card(x, i){
  const p = (window._prog||{})[x.id] || {};
  let badge='';
  if(p.finished) badge='<span class="badge done">已看完</span>';
  else if(p.episode>0) badge='<span class="badge prog">看到'+(p.episode+1)+'集</span>';
  else if(x.remarks) badge='<span class="badge">'+esc(x.remarks.slice(0,6))+'</span>';
  // 封面策略：**直连图床优先，失败降级到服务端缓存**。
  // 缘由：图床按出口 IP 限流，NAS 所在出口已被硬封（实测 54 次请求全失败），
  // 但手机侧网络通常可正常访问。故先让浏览器直连（快，且浏览器会自行缓存），
  // 失败时再由 error 处理器回落到 /cover（命中 NAS 缓存则给出真图，否则占位）。
  const thumb = x.pic
    ? `<img loading="lazy" data-i="${i}" data-cov="1"
         data-orig="${esc(x.pic)}"
         src="${esc(x.pic)}">`
    : '';
  return `<div class="card" data-i="${i}">
    <div class="thumb">${thumb}${badge}</div>
    <div class="meta">
      <div class="name">${esc(x.name)}</div>
      <div class="sub">${x.type_name?`<span>${esc(x.type_name)}</span>`:''}
        ${x.year?`<span>${esc(x.year)}</span>`:''}
        ${x.total?`<span>${esc(x.serial||0)}/${esc(x.total)}集</span>`:''}</div>
    </div></div>`;
}

/* 封面加载失败 → 换成片名占位海报。
   error 事件不冒泡，故用捕获阶段监听。 */
document.addEventListener('error', e => {
  const img = e.target;
  if(!img || img.tagName !== 'IMG') return;
  if(!img.dataset || img.dataset.i === undefined) return;

  // 第一级降级：直连失败 → 改走服务端 /cover（可能命中 NAS 本地缓存）
  if(img.dataset.orig && !img.dataset.triedProxy){
    img.dataset.triedProxy = '1';
    img.src = '/cover?u=' + encodeURIComponent(img.dataset.orig) + '&coverv=2';
    return;
  }
  // 第二级降级：服务端也没有 → 换成片名占位海报
  if(img.dataset.ph) return;
  img.dataset.ph = '1';
  const x = (window._list||[])[+img.dataset.i];
  if(!x) return;
  const holder = img.parentNode;
  if(holder){
    const tmp = document.createElement('div');
    tmp.innerHTML = placeholder(x, 'ph');
    const ph = tmp.firstElementChild;
    ph.style.position = 'absolute';
    ph.style.inset = '0';
    holder.style.position = 'relative';
    holder.replaceChild(ph, img);
  }
}, true);

async function openDlg(x){
  cur = x;
  const set = (id, v) => { const e = document.getElementById(id); if(e) e.textContent = v; };

  // 封面（含降级，整体保护）
  try{
    const dimg = document.getElementById('dimg');
    if(dimg){
      const oldph = document.getElementById('dimgph');
      if(oldph) oldph.remove();
      if(x.pic){
        dimg.style.display = '';
        dimg.onerror = () => {
          dimg.style.display = 'none';
          if(document.getElementById('dimgph')) return;
          const d = document.createElement('div');
          d.id = 'dimgph'; d.className = 'ph';
          d.style.cssText = 'position:absolute;inset:0';
          d.innerHTML = '<div class="ph-t">'+esc((x.name||'?').slice(0,1))+
                        '</div><div class="ph-n">'+esc(x.name)+'</div>';
          const dh = document.getElementById('dhead');
          if(dh) dh.prepend(d);
        };
        dimg.src = x.pic;
      } else { dimg.style.display = 'none'; }
    }
  }catch(e){ console.warn('cover', e); }

  set('dname', x.name || '');
  set('dmeta', [x.type_name, x.year, x.remarks,
                 x.total ? ('共'+x.total+'集') : ''].filter(Boolean).join(' · '));

  const deps = document.getElementById('deps');
  deps.innerHTML = '<p style="color:var(--tx3);padding:18px">正在载入剧集…</p>';

  // 显示遮罩
  const dlg = document.getElementById('dlg');
  if(dlg) dlg.hidden = false;

  // 取详情
  try{
    const r = await fetch('/api/php/provide/vod?ac=detail&ids='+encodeURIComponent(x.id));
    const d = await r.json();
    const first = (d && d.list && d.list[0]) ? d.list[0] : null;
    const eps = first ? (first.vod_play_url || '').split('#').filter(Boolean) : [];
    window._eps = eps;
    const p = (window._prog||{})[x.id] || {};
    if(!eps.length){
      deps.innerHTML = '<p style="color:var(--tx3);padding:18px">该条目暂无播放地址</p>';
    } else {
      // 事件委托：按钮只带 data-idx，避免 inline onclick 的引号问题
      deps.innerHTML = eps.map((e,i)=>
        `<button class="ep${i===(p.episode||0)?' cur':''}" data-idx="${i}">${i+1}</button>`
      ).join('');
      // 预取首集：用户已打开详情，很可能要播。提前把 m3u8 与首个分片
      // 拉进浏览器缓存，点集数时可直接命中，缩短起播等待。
      prefetchStream(eps[0]);
    }
  }catch(err){
    deps.innerHTML = '<p style="color:#f09595;padding:18px">载入失败：'+
                     esc(err.message||err)+'</p>';
  }
}

/* 预取首个剧集的播放数据，缩短起播等待。
   HLS 起播必须「先取清单、再取首分片、然后解码首帧」，
   这一步约 2~3 秒。用户打开详情弹窗时意图明确，可提前拉取。 */
async function prefetchStream(url){
  if(!url) return;
  const P = u => '/api/proxy?url=' + encodeURIComponent(u);
  try{
    const r = await fetch(P(url));
    if(!r.ok) return;
    const txt = await r.text();
    if(!/^#EXTM3U/.test(txt.trim())) return;      // 不是清单，放弃
    const NL = String.fromCharCode(10), CR = String.fromCharCode(13);
    const lines = txt.split(NL).map(l=>l.split(CR).join('').trim())
      .filter(Boolean);
    const base = url.replace(/[^/]*$/, '');
    const seg = lines.find(l=>!l.startsWith('#'));
    if(!seg) return;
    const segUrl = seg.startsWith('http') ? seg : base + seg;
    if(segUrl.endsWith('.m3u8')){
      // master 清单：再取一层子清单
      const r2 = await fetch(P(segUrl));
      if(!r2.ok) return;
      const t2 = await r2.text();
      const l2 = t2.split(NL).map(l=>l.split(CR).join('').trim()).filter(Boolean);
      const s2 = l2.find(l=>!l.startsWith('#'));
      if(s2){
        const base2 = segUrl.replace(/[^/]*$/, '');
        const u2 = s2.startsWith('http') ? s2 : base2 + s2;
        fetch(P(u2)).catch(()=>{});
      }
    } else {
      fetch(P(segUrl)).catch(()=>{});
    }
  }catch(e){ /* 预取失败不影响主流程 */ }
}

/* 内嵌播放：在浏览页内全屏承载 LibreTV 播放页 */
function openPlay(url, title){
  const mask = document.getElementById('playmask');
  const frame = document.getElementById('playframe');
  const t = document.getElementById('playtitle');
  if(!mask || !frame){ window.open(url, '_blank'); return; }   // 兜底

  if(t) t.textContent = title || '';
  const dlg = document.getElementById('dlg');
  if(dlg) dlg.hidden = true;        // 关掉详情弹窗，避免两层叠着

  // 同源 iframe：加载完成后主动触发播放。
  // 否则用户点完集数还要在 iframe 里再点一次播放键，观感上像"没反应"。
  // 同源才能访问 contentDocument；跨域会抛异常，故整体 try/catch。
  frame.onload = () => {
    let tries = 0;
    const kick = () => {
      tries++;
      try {
        const doc = frame.contentDocument;
        const v = doc && doc.querySelector('video');
        if (v) {
          const pr = v.play();
          if (pr && pr.catch) pr.catch(() => {});
          if (!v.paused) return;         // 已在播放
        }
      } catch (e) { /* 跨域或尚未就绪 */ }
      if (tries < 20) setTimeout(kick, 400);   // 播放器初始化需要时间，重试
    };
    setTimeout(kick, 300);
  };

  frame.src = url;
  mask.hidden = false;
  document.body.style.overflow = 'hidden';
}

function closePlay(){
  const mask = document.getElementById('playmask');
  const frame = document.getElementById('playframe');
  if(mask) mask.hidden = true;
  if(frame) frame.src = 'about:blank';   // 置空：停止播放并释放解码器
  document.body.style.overflow = '';
  go(page);                              // 刷新列表，更新「已看完」等标记
}

function closeDlg(){
  const dlg = document.getElementById('dlg');
  if(dlg) dlg.hidden = true;
}

/* 集数点击：事件委托（绑一次，作用于所有按钮） */
/* 占位图自动重试：
   服务端未命中缓存时返回 1x1 透明图，此时 img.naturalWidth 为 1。
   后台预热抓到真图后，重新请求即可拿到（占位图响应带 no-store，不会被缓存）。
   每次只重试当前仍是占位的图，最多 4 轮，避免无谓请求。 */
let coverRetry = 0;
function retryPlaceholders(){
  if(coverRetry >= 3) return;
  coverRetry++;
  const imgs = document.querySelectorAll('img[data-cov]');
  let pending = 0;
  imgs.forEach(img => {
    // 仅重试「降级成占位」或「仍为空」的；直接成功的不管
    if(img.dataset.ph || (!img.complete && img.naturalWidth === 0)){
      pending++;
      img.dataset.triedProxy = '';
      img.dataset.ph = '';
      if(img.dataset.orig) img.src = img.dataset.orig + (img.dataset.orig.includes('?')?'&':'?') + '_r=' + coverRetry;
    }
  });
  if(pending) setTimeout(retryPlaceholders, 15000);
}

document.addEventListener('click', e => {
  if(!e.target.closest) return;
  // 集数按钮
  const epBtn = e.target.closest('.ep[data-idx]');
  if(epBtn){ play(parseInt(epBtn.dataset.idx, 10)); return; }
  // 卡片 → 打开详情
  const cardEl = e.target.closest('.card[data-i]');
  if(cardEl){
    const x = (window._list||[])[+cardEl.dataset.i];
    if(x) openDlg(x);
  }
});

/* 播放：优先原生（APK），否则跳 LibreTV 网页播放器 */
function play(ep){
  const eps = window._eps || [];
  const url = (eps[ep] || '').split('$').pop();
  if(!url){ alert('未取到播放地址'); return; }
  const total = eps.length;
  const title = (cur && cur.name ? cur.name : '') + (total>1 ? ' 第'+(ep+1)+'集' : '');
  fetch('/progress',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:cur.id, episode:ep, episodes:total,
                         finished:ep+1>=total})}).catch(()=>{});

  if(window.MediaDBNative && typeof window.MediaDBNative.play === 'function'){
    try{ window.MediaDBNative.play(url, title, ep, total, cur.id); return; }
    catch(err){ console.warn('native play failed', err); }
  }
  // 跳 LibreTV 播放页 —— 用它的**标准模式**（source + id），不用 url 直连模式。
  //
  // 为什么改用标准模式
  // ----------------
  // url 直连模式（?url=<m3u8>）只给一个地址，LibreTV 没有剧集列表，
  // 走的是「单集直连」分支；而它自己搜索进入时用的是 source+id，
  // 会先取详情拿到完整 episodes 再播放 —— 后者才是我方浏览页想要的：
  // 有剧集列表、可切集、自动连播、断点续播都走原生逻辑。
  // 我们的 /api/detail 已在反代层接管，能按 id 返回 episodes，故标准模式可用。
  //
  // 仍必须带 sourceUrl：LibreTV 的 resolveSource() 只在
  // 「source 命中已配置源」或「sourceUrl 非空」时返回源对象，否则报「点播源不存在」。
  const qs = new URLSearchParams({
    source: 'mediadb',
    id: cur.id,                       // 形如 "1:72186"
    index: String(ep),
    title: title,
    sourceUrl: location.origin + '/api/php/provide/vod'
  });

  // 必须走**同源相对路径**，不能直接跳 8080。
  // 原因：播放页要用 /api/detail 取剧集，而 LibreTV 自带的 /api/detail
  // 会被它自己的 SSRF 校验拒绝内网源（sourceUrl 指向本机 8091），
  // 表现为「获取剧集失败 / 视频加载失败」。
  // 只有同源（8091 或 8443）时，该请求才会落到我们的接管层，用本地索引应答。
  // 实测经反代的分片下载速度并不慢（见 bench_stream.py），故代理不是卡顿源。
  // 改为内嵌播放：不新开标签页、不切窗口，关闭即回列表。
  // （页面加载本身只占 159ms，慢在播放器初始化+首分片，内嵌能改善观感）
  openPlay('/watch?' + qs.toString(), title);
}

/* 供原生侧在播放结束时回调切下一集 */
window.MediaDBBridge = window.MediaDBBridge || {};
window.MediaDBBridge.next = function(currentIdx){
  const eps = window._eps || [];
  const n = (currentIdx|0) + 1;
  if(n < eps.length) play(n);
};

async function markWatched(){
  if(!cur) return;
  const r = await fetch('/api.php/provide/vod?ac=detail&ids='+cur.id);
  const d = await r.json();
  const eps = d.list&&d.list[0]?(d.list[0].vod_play_url||'').split('#').filter(Boolean):[];
  await fetch('/progress',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:cur.id, episode:0, episodes:eps.length, finished:true})});
  closeDlg(); go(page);
}

/* 注意：closeDlg 已在前面定义（弹窗改为 div 后不再有 .close() 方法）。
   此处曾残留一版 `document.getElementById('dlg').close()`，
   因函数声明提升会覆盖前面的实现 → 点「关闭」直接抛
   「dlg.close is not a function」。已删除。 */
function reset(){
  document.getElementById('q').value='';
  document.getElementById('year').value='';
  document.getElementById('unwatched').checked=false;
  typeId=''; loadCats(); go(1);
}
function more(){ go(page+1); }
function esc(s){ return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

loadCats(); go(1);

/* 注册 Service Worker：仅缓存页面外壳，不碰视频流（大文件）。
   作用是断网时仍能打开界面、并让「添加到主屏幕」完整可用。

   ⚠️ 带 ?nosw=1 时跳过注册（APK 用这个模式）。
   原因：SW 缓存会顽固地留住旧页面，导致服务端更新后 APK 仍加载到
   有语法错误的旧版本，表现为点卡片无反应且控制台报 SyntaxError。
   APK 端不需要离线能力，页面每次由服务端实时下发即可。 */
if('serviceWorker' in navigator && !/[?&]nosw=1/.test(location.search)){
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
} else if (/[?&]nosw=1/.test(location.search)) {
  // 清理可能残留的 SW 与缓存，确保拿到最新页面
  if (navigator.serviceWorker) {
    navigator.serviceWorker.getRegistrations().then(rs => rs.forEach(r => r.unregister()));
  }
  if (window.caches && caches.keys) {
    caches.keys().then(ks => ks.forEach(k => caches.delete(k)));
  }
}
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy_libretv(self, method="GET"):
        """把请求同源转发给 LibreTV。

        为什么必须同源
        --------------
        LibreTV 2.18+ 需要密码验证，登录态保存在 **localStorage**，
        而 localStorage 严格按 origin（scheme+host+port）隔离：
        浏览页在 8091、播放器在 8080 属不同 origin，登录态无法共享，
        于是播放时 LibreTV 认为「点播源不存在」。
        由本服务（8091）转发后，浏览与播放同源，登录一次即全程可用。
        """
        import http.client

        base = urlparse(LIBRETV_BASE)
        host = base.hostname or "127.0.0.1"
        port = base.port or (443 if base.scheme == "https" else 80)

        hop = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "content-length"}
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in hop}

        body = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            body = self.rfile.read(n) if n else None

        try:
            conn = http.client.HTTPConnection(host, port, timeout=60)
            conn.request(method, self.path, body=body, headers=fwd)
            resp = conn.getresponse()
            data = resp.read()
            hdrs = resp.getheaders()
            status = resp.status
        except Exception as e:
            return self._json({"error": f"libretv unreachable: {e}"}, 502)

        self.send_response(status)
        for k, v in hdrs:
            if k.lower() in hop:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _js(self, text):
        b = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def _cover(self, raw_url):
        """封面：**只读本地缓存**，未命中立即返回占位图并交后台预热。

        为什么不在这里同步下载：图床按出口 IP 限流（多数请求被 reset），
        同步下载会让首屏逐个等待（原实现最坏要十几秒）。
        改为「缓存命中即返回、未命中毫秒返回占位 + 后台慢慢抓」后，
        页面加载不再受图床影响，封面则随时间自然补齐。
        """
        from urllib.parse import unquote
        url = unquote(raw_url or "")
        if not url.startswith(("http://", "https://")):
            return self._png(BLANK_PNG, cache=False)

        data = _cover_read(_cover_path(url))
        if data is not None:
            return self._png(data)                  # 真图：长缓存

        _warm_enqueue(url)          # 交给后台，不阻塞当前请求
        return self._png(BLANK_PNG, cache=False)    # 占位：不缓存，便于稍后更新

    def _png(self, data, cache=True):
        """返回图片。

        cache=True  —— 真图，长缓存（浏览器不必再请求）
        cache=False —— 占位图，**禁止缓存**：否则浏览器会把占位图存 7 天，
                       等后台抓到真图也不会更新，用户永远看到占位。
        """
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _icon(self, path):
        """用纯 Python 生成 PNG 图标，避免引入图片素材依赖。
        画一个「播放按钮 + 胶片底色」的简单图形，尺寸按请求自适应。"""
        name = os.path.basename(path)
        size = 512 if "512" in name else 192
        try:
            png = make_icon(size)
        except Exception:
            self._json({"error": "icon"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(png)

    def _html(self, text):
        b = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        try:
            # 音乐：在线（代理 SQMusic）与本地曲库
            if u.path.startswith("/api/online/"):
                return music_online.handle(self, q)
            if u.path == "/music" or u.path.startswith("/api/music/"):
                return music.handle(self)

            conn = db.connect(INDEX_DB)

            if u.path in ("/", "/index.html"):
                return self._html(PAGE)

            # 观看进度查询
            if u.path == "/progress":
                ids = [i for i in (one("ids", "") or "").split(",") if i]
                if not ids:
                    return self._json({})
                ph = ",".join("?" * len(ids))
                rows = prog().execute(
                    f"SELECT * FROM progress WHERE "
                    f"(src || ':' || vod_id) IN ({ph})", ids).fetchall()
                out = {}
                for r in rows:
                    out[f"{r['src']}:{r['vod_id']}"] = {
                        "episode": r["episode"], "episodes": r["episodes"],
                        "finished": bool(r["finished"])}
                return self._json(out)

            # 取播放地址
            if u.path == "/config":
                return self._json({"libretv": LIBRETV_BASE})

            # LibreTV 详情：原样返回本地索引，避免其服务端 SSRF 拒绝内网源
            if u.path == "/api/detail":
                ident = one("id", "") or ""
                if ":" not in ident:
                    return self._json({"episodes": [], "videoInfo": {}})
                try:
                    code, vid = ident.split(":", 1)
                    src = _CODE_SRC.get(int(code))
                    row = db.detail(conn, src, int(vid)) if src else None
                except (ValueError, TypeError):
                    row = None
                if not row:
                    return self._json({"episodes": [], "videoInfo": {}})
                return self._json(_lt_detail(row))

            if u.path == "/cover":
                return self._cover(one("u", ""))

            if u.path == "/manifest.webmanifest":
                return self._json(MANIFEST)

            if u.path == "/sw.js":
                return self._js(SW_JS)

            if u.path.startswith("/icons/"):
                return self._icon(u.path)

            if u.path == "/offline":
                return self._html(OFFLINE_HTML)

            # CMS 详情：浏览页弹窗据此取集数列表（此前缺失导致弹窗打不开）
            if u.path == "/api/php/provide/vod":
                ac = one("ac", "")
                if ac == "detail":
                    ids = [i for i in (one("ids", "") or "").split(",") if i]
                    out = []
                    for ident in ids[:20]:
                        if ":" not in ident:
                            continue
                        code, vid = ident.split(":", 1)
                        src = apimod.CODE_SRC.get(int(code))
                        row = db.detail(conn, src, int(vid)) if src else None
                        if not row:
                            continue
                        d = db.row_to_dict(row)
                        eps = d.get("episodes") or []
                        play = "#".join(
                            f"第{i+1}集${u}" for i, u in enumerate(eps))
                        out.append({
                            "vod_id": vid,
                            "vod_name": d.get("name", ""),
                            "vod_pic": d.get("pic", ""),
                            "vod_year": d.get("year", ""),
                            "vod_area": d.get("type_name", ""),
                            "vod_remarks": d.get("remarks", ""),
                            "vod_play_url": play,
                        })
                    return self._json({"list": out})
                # 不带 ac 时按关键词搜索（CMS 兼容）
                kw = one("wd", "")
                rows = db.search(conn, kw, limit=int(one("limit", "60") or 60))
                return self._json({"list": [_cms_card(r) for r in rows],
                                   "total": len(rows)})

            if u.path == "/play":
                return self._play(conn, one("id", ""), int(one("ep", "0") or 0))

            if u.path == "/api/php/provide/types":
                rows = db.types(conn)
                groups = {}
                for r in rows:
                    g = groups.setdefault(r["type_name"] or "未分类",
                                         {"type_name": r["type_name"] or "未分类",
                                          "type_id": r["type_id"], "count": 0})
                    g["count"] += r["cnt"]
                return self._json({"class": sorted(groups.values(),
                                                   key=lambda x: -x["count"])})

            if u.path == "/api/search":
                kw = one("wd", "")
                rows = db.search(conn, kw, limit=int(one("limit", "60") or 60))
                return self._json({"list": [_card(r) for r in rows],
                                   "total": len(rows)})

            if u.path == "/browse":
                limit = min(60, int(one("limit", "30") or 30))
                try:
                    tid = int(one("type_id")) if one("type_id") else None
                except ValueError:
                    tid = None
                rows, total = db.page(conn, type_id=tid, year=one("year"),
                                      order=one("order", "time"),
                                      page_no=max(1, int(one("pg", "1") or 1)),
                                      page_size=limit)
                return self._json({"list": [_card(r) for r in rows], "total": total})

            # ── App 增量同步 ──────────────────────────────────
            # 独立版 APK 内置了一份索引快照，这里给它补「比本地更新」的条目。
            # 只能在局域网内用：外网被路由器拦（见项目备忘），所以同步是
            # 「回家连 WiFi 时自动补齐新片」，不是实时回源。
            if u.path == "/api/app/delta":
                after = (one("after", "") or "").strip()
                try:
                    limit = min(5000, max(1, int(one("limit", "1500") or 1500)))
                except ValueError:
                    limit = 1500
                rows = conn.execute(
                    "SELECT * FROM videos WHERE vod_time > ? "
                    "ORDER BY vod_time ASC LIMIT ?", (after, limit)).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["episodes"] = json.loads(d.get("episodes") or "[]")
                    except Exception:
                        d["episodes"] = []
                    out.append(d)
                server_total = conn.execute(
                    "SELECT COUNT(*) FROM videos").fetchone()[0]
                return self._json({
                    "list": out,
                    "count": len(out),
                    "max_time": rows[-1]["vod_time"] if rows else after,
                    "server_total": server_total,
                })

            # 未匹配的路径 → 同源转发给 LibreTV（播放页 / 静态资源 / 其 API）
            return self._proxy_libretv("GET")
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)

        # LibreTV 聚合搜索：直接查本地索引后按它的格式返回。
        # 这样 LibreTV 的界面能搜到我们 19.7 万条内容，且完全不触发它的 SSRF 校验。
        if u.path == "/api/search":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            wd = str(body.get("wd") or "").strip()
            items = []
            if wd:
                conn = db.connect(INDEX_DB)
                try:
                    for r in db.search(conn, wd, limit=60):
                        items.append(_lt_search_item(r))
                finally:
                    conn.close()
            return self._json({"list": items, "failures": []})

        if u.path != "/progress":
            # 其余 POST 交给 LibreTV（登录等），同源转发
            return self._proxy_libretv("POST")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or "{}")
            ident = str(d.get("id", ""))
            if ":" not in ident:
                return self._json({"error": "bad id"})
            src, vid = ident.split(":", 1)
            prog().execute(
                """INSERT INTO progress (src,vod_id,episode,episodes,finished,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(src,vod_id) DO UPDATE SET
                     episode=excluded.episode, episodes=excluded.episodes,
                     finished=excluded.finished, updated_at=excluded.updated_at""",
                (src, int(vid), int(d.get("episode", 0)),
                 int(d.get("episodes", 0)), 1 if d.get("finished") else 0,
                 int(time.time())))
            prog().commit()
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _play(self, conn, ident, ep):
        if ":" not in ident:
            return self._json({"error": "bad id"})
        code, vid = ident.split(":", 1)
        import api as apimod
        src = apimod.CODE_SRC.get(int(code))
        row = db.detail(conn, src, int(vid)) if src else None
        if not row:
            return self._json({"error": "not found"})
        eps = db.row_to_dict(row)["episodes"] or []
        if not eps:
            return self._json({"error": "无播放地址"})
        idx = max(0, min(ep, len(eps) - 1))
        return self._json({"url": eps[idx], "episode": idx + 1, "total": len(eps)})


def _cms_card(r) -> dict:
    """转成苹果 CMS 的 list 项格式（与 vod 列表兼容）。"""
    c = _card(r)
    eps = json.loads(r["episodes"] or "[]")
    return {
        "vod_id": c["id"].split(":", 1)[1],
        "vod_name": c["name"],
        "vod_pic": c["pic"],
        "vod_year": c["year"],
        "vod_area": c["type_name"],
        "vod_remarks": c["remarks"],
        "vod_play_url": "#".join(f"第{i+1}集${u}" for i, u in enumerate(eps)),
    }


def _card(r) -> dict:
    eps = json.loads(r["episodes"] or "[]")
    code = {"ty": 1, "ws": 2}.get(r["src"], 9)
    return {
        "id": f"{code}:{r['vod_id']}",
        "name": r["name"], "pic": r["pic"] or "",
        "type_name": r["type_name"] or "", "year": r["year"] or "",
        "remarks": r["remarks"] or "", "serial": r["serial"] or "",
        "total": r["total"] or "", "score": r["score"] or 0,
        "episodes": len(eps), "vod_time": r["vod_time"] or "",
    }


_CODE_SRC = {1: "ty", 2: "ws"}
_SRC_NAME = {"ty": "影视库", "ws": "影视库"}


def _lt_search_item(r) -> dict:
    """把索引行转成 LibreTV 的 SearchResultItem。"""
    c = _card(r)
    return {
        "sourceKey": "mediadb",          # 固定标识，反代层认得
        "sourceName": _SRC_NAME.get(r["src"], "影视库"),
        "vodId": c["id"],                # 形如 "1:72186"
        "name": c["name"],
        "pic": c["pic"],
        "typeName": c["type_name"],
        "year": c["year"],
        "area": "",
        "remarks": c["remarks"],
        "isAdult": False,
    }


def _lt_detail(row) -> dict:
    """把索引行转成 LibreTV 的 VideoDetail：{episodes, videoInfo}。"""
    c = _card(row)
    try:
        eps = json.loads(row["episodes"] or "[]")
    except Exception:
        eps = []
    return {
        "episodes": eps,
        "videoInfo": {
            "title": c["name"],
            "cover": c["pic"],
            "typeName": c["type_name"],
            "year": c["year"],
            "area": "",
            "desc": "",
            "remarks": c["remarks"],
        },
    }


def main():
    st = db.stats(db.connect(INDEX_DB))
    print(f"[web] {st['total']} 条 / {st['type_count']} 个分类 -> http://0.0.0.0:{PORT}", flush=True)
    # 启动预热：后台低速抓取封面，填充本地缓存（不阻塞服务）
    threading.Thread(target=_warm_bootstrap, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
