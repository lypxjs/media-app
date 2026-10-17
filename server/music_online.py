"""在线音乐：代理 SQMusic 的检索与直链解析。

为什么这样接
------------
音乐没有 maccms 那种公开采集标准，无法像影视那样建全量索引。
但 SQMusic 的后端本身就是一个音乐资源池（搜「晴天」有 3500+ 条），
它把各平台的加密签名都处理好了，对外只是简单的 REST。
故这里不复刻平台协议，只做一层代理：

    web.py  --(带 sqmusic token)-->  SQMusic:8996  -->  酷我

SQMusic 的认证方式：POST /api/config/login 拿 JWT，
之后每个请求带 header `sqmusic: <token>`。token 有效期 7 天，这里缓存 6 天。

播放不需要下载：/api/music/getDownloadUrl 直接返回 CDN 直链
（实测匿名可访问、支持 Range），浏览器 <audio> 直接播。
注意它要求请求体除搜索结果外再补两个字段：brType 与 downloadFormat。
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

SQ_BASE = os.environ.get("SQ_BASE", "http://127.0.0.1:8996").rstrip("/")
SQ_USER = os.environ.get("SQ_USER", "admin")
SQ_PASS = os.environ.get("SQ_PASS", "admin")

TOKEN_TTL = 6 * 86400
_log = {}
_lock = threading.Lock()

# 搜索结果缓存：点播放时按 id 取回完整记录（getDownloadUrl 需要整个对象）
_records = {}
_records_order = []
RECORDS_MAX = 3000

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url, payload, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with _op.open(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with _op.open(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _token(force=False):
    with _lock:
        now = time.time()
        if not force and _log.get("t") and now - _log["at"] < TOKEN_TTL:
            return _log["t"]
        try:
            j = _post_json(SQ_BASE + "/api/config/login",
                           {"username": SQ_USER, "password": SQ_PASS, "device": "web"})
            tok = (j.get("data") or {}).get("tokenValue") or ""
            if tok:
                _log["t"] = tok
                _log["at"] = now
                print("[online] SQMusic 登录成功", flush=True)
            return tok
        except Exception as e:
            print(f"[online] 登录失败 {type(e).__name__}: {str(e)[:60]}", flush=True)
            return ""


def _sq_get(path):
    """带 token 调 GET，token 失效时重登一次。"""
    for attempt in range(2):
        tok = _token(force=attempt > 0)
        if not tok:
            return None
        try:
            return _get_json(SQ_BASE + path, {"sqmusic": tok})
        except Exception as e:
            print(f"[online] GET {path[:40]} 失败 {type(e).__name__}", flush=True)
    return None


def _sq_post(path, payload):
    for attempt in range(2):
        tok = _token(force=attempt > 0)
        if not tok:
            return None
        try:
            return _post_json(SQ_BASE + path, payload, {"sqmusic": tok})
        except Exception as e:
            print(f"[online] POST {path[:40]} 失败 {type(e).__name__}", flush=True)
    return None


def _remember(records):
    with _lock:
        for r in records:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            if rid not in _records:
                _records_order.append(rid)
            _records[rid] = r
        while len(_records_order) > RECORDS_MAX:
            old = _records_order.pop(0)
            _records.pop(old, None)


def _slim(r):
    """给前端的精简结构，省流量。"""
    return {
        "id": str(r.get("id") or ""),
        "name": r.get("name") or "",
        "artist": "/".join(r.get("artistName") or []),
        "album": r.get("albumName") or "",
        "pic": r.get("pic") or "",
        "duration": r.get("duration") or "",
        "br": r.get("brTypes") or [],
    }


def search(kw, page=1, size=30):
    q = urllib.parse.quote(kw or "")
    d = _sq_get(f"/api/music/searchSong?plugName=kw&keyword={q}"
                f"&pageSize={size}&pageIndex={page}")
    if not d:
        return {"error": "上游不可用"}
    data = d.get("data") or {}
    recs = data.get("records") or []
    _remember(recs)
    return {
        "total": data.get("searchTotal") or 0,
        "page": page,
        "size": size,
        "list": [_slim(r) for r in recs],
    }


def tips(kw):
    q = urllib.parse.quote(kw or "")
    d = _sq_get(f"/api/music/searchTips?plugName=kw&keyword={q}")
    if not d:
        return []
    v = d.get("data")
    return v if isinstance(v, list) else []


def play_url(sid, br="KW_MP3_320"):
    with _lock:
        rec = _records.get(str(sid))
    if not rec:
        return {"error": "记录已过期，请重新搜索"}
    payload = dict(rec)
    chosen = _pick_br(rec.get("brTypes") or [], br)
    payload["brType"] = chosen
    payload["downloadFormat"] = "flac" if "FLAC" in chosen else "mp3"
    d = _sq_post("/api/music/getDownloadUrl", payload)
    if not d or d.get("code") != 200:
        return {"error": "取播放地址失败"}
    url = (d.get("data") or {}).get("url") or ""
    if not url:
        return {"error": "该曲目无可用音源"}
    return {"url": url, "br": chosen}


def lyric(sid):
    d = _sq_post("/api/music/getLyric", {"id": str(sid), "plugName": "kw"})
    if not d:
        return ""
    v = d.get("data")
    return v if isinstance(v, str) else ""


def _pick_br(brs, want):
    """按用户偏好挑一个实际可用的码率。

    顺序是无损 > 320 > 128。用户选了某档但该曲没有时，
    优先往下降（保体积不保音质），降不动再往上找。
    """
    order = ["KW_FLAC_2000", "KW_MP3_320", "KW_MP3_128"]
    if not brs:
        return "KW_MP3_320"
    if want in brs:
        return want
    if want not in order:
        want = order[0]
    i = order.index(want)
    for cand in order[i:] + list(reversed(order[:i])):
        if cand in brs:
            return cand
    return brs[0]


def download(sid, br=""):
    """交给 SQMusic 下载：它会写好标签/封面/歌词并按 歌手/专辑/曲目 归档，
    落进 /vol1/1000/音乐，随后被本地曲库与 Navidrome 自动收录。"""
    with _lock:
        rec = _records.get(str(sid))
    if not rec:
        return {"error": "记录已过期，请重新搜索"}
    chosen = _pick_br(rec.get("brTypes") or [], br)
    payload = dict(rec)
    payload["brType"] = chosen
    payload["downloadFormat"] = "flac" if "FLAC" in chosen else "mp3"
    d = _sq_post("/api/download/downloadSong", payload)
    if not d or d.get("code") != 200:
        msg = (d or {}).get("msg") or "下载失败"
        return {"error": str(msg)}
    data = d.get("data") or {}
    return {
        "ok": True,
        "br": chosen,
        "file": data.get("downloadFile") or "",
        "time": data.get("downloadTime") or "",
    }


# ────────────────────────── HTTP 入口 ──────────────────────────

def handle(h, qs):
    """由 web.py 调用；返回 True 表示已处理。

    qs 是已解析的 query（dict of list）。
    """
    u = urllib.parse.urlparse(h.path)
    p = u.path

    def one(k, dft=""):
        v = qs.get(k)
        return (v[0] if v else dft)

    if p == "/api/online/search":
        kw = one("kw")
        try:
            page = max(1, int(one("page", "1")))
        except ValueError:
            page = 1
        if not kw.strip():
            h._json({"error": "缺少关键词"})
            return True
        h._json(search(kw.strip(), page))
        return True

    if p == "/api/online/tips":
        h._json({"tips": tips(one("kw"))})
        return True

    if p == "/api/online/url":
        h._json(play_url(one("id"), one("br", "KW_MP3_320")))
        return True

    if p == "/api/online/download":
        h._json(download(one("id"), one("br")))
        return True

    if p == "/api/online/lyric":
        h._json({"lrc": lyric(one("id"))})
        return True

    if p == "/api/online/img":
        return _proxy_img(h, one("u"))

    return False


def _proxy_img(h, raw):
    """封面代理：酷我图床对无 Referer 的请求偶有拒绝，这里统一加 Referer。"""
    u = urllib.parse.unquote(raw or "")
    if not u.startswith(("http://", "https://")):
        h._json({"error": "bad url"}, 400)
        return True
    host = urllib.parse.urlparse(u).netloc
    if not host.endswith("kuwo.cn"):
        h._json({"error": "host not allowed"}, 403)
        return True
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.kuwo.cn/",
        })
        with _op.open(req, timeout=15) as r:
            data = r.read()
            ct = r.headers.get("content-type") or "image/jpeg"
    except Exception as e:
        h._json({"error": str(e)[:60]}, 502)
        return True
    h.send_response(200)
    h.send_header("Content-Type", ct)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=86400")
    h.end_headers()
    try:
        h.wfile.write(data)
    except Exception:
        pass
    return True
