"""
索引 API：对外伪装成苹果 CMS 采集接口，供 LibreTV 当作普通点播源使用。

为什么需要这层
--------------
两个源站主动封闭了搜索（`?wd=` 返回纯文本「暂不支持搜索」），
但它们的列表接口能按页倒序翻完 19.9 万条，且详情接口能拿到完整播放地址。
既然不能按关键词搜，就把全量数据落到本地库，自己实现搜索——
并且顺手补上源站**不支持**的分类/年份/排序能力（实测 type_id / order / year 参数全被忽略）。

对外接口（苹果 CMS 协议）
------------------------
GET  /api.php/provide/vod?wd=关键词&ac=videolist&pg=N   搜索 + 分页
GET  /api.php/provide/vod?ac=list&pg=N[&type_id=][&year=][&order=]  翻页 + 筛选 + 排序
GET  /api.php/provide/vod?ac=detail&ids=1,2,3            批量详情（逗号分隔）
GET  /api/php/provide/types                分类列表（分类导航墙用）
GET  /healthz                     存活 + 索引统计

附加（LibreTV 不消费，供自维护界面用）
GET  /browse?type_id=&year=&order=&pg=   增强翻页：附带 has_episodes 标记
GET  /stats                          索引统计
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db

DB_PATH = os.environ.get("INDEX_DB", "/data/index.db")
PORT = int(os.environ.get("PORT", "8090"))
SRC_NAME = {"ty": "天涯影视", "ws": "无水印影视"}
# 源标识 → 单源编号（苹果 CMS 期望 vod_id 是纯数字，这里用 1=ty, 2=ws 编码 src）
SRC_CODE = {"ty": 1, "ws": 2}
CODE_SRC = {v: k for k, v in SRC_CODE.items()}

# 各源的图片代理前缀：LibreTV 拉外站图常失败，这里直接回落到原图，
# 真正的图片加速交给 LibreTV 自己的图片代理设置。
IMG_BASE = {"ty": "", "ws": ""}


def _enc_id(src: str, vod_id: int) -> int:
    """(src, vod_id) → 对外的数字 id。

    苹果 CMS 协议里 vod_id 是数字，而本地库用 (src, vod_id) 复合主键，
    需要压成单一整数。用 10^6 为界：源内 id 实测最大约 15 万，留足余量。
    """
    return SRC_CODE.get(src, 9) * 1_000_000 + (vod_id or 0)


def _dec_id(x: int) -> tuple[str | None, int]:
    """对外数字 id → (src, vod_id)。返回 (None, vid) 表示未带源前缀，需遍历。"""
    code, vid = divmod(int(x), 1_000_000)
    src = CODE_SRC.get(code)
    return src, vid


def _row_cms(r) -> dict:
    """数据库行 → 苹果 CMS list 元素格式。"""
    eps = r["episodes"]
    if isinstance(eps, str):
        try:
            eps = json.loads(eps or "[]")
        except Exception:
            eps = []
    return {
        "vod_id": _enc_id(r["src"], r["vod_id"]),
        "vod_name": r["name"],
        "vod_pic": r["pic"] or "",
        "type_name": r["type_name"] or "",
        "vod_year": r["year"] or "",
        "vod_area": r["area"] or "",
        "vod_remarks": r["remarks"] or "",
        "vod_score": r["score"] or 0,
        "vod_hits": r["hits"] or 0,
        "vod_serial": r["serial"] or "",
        "vod_total": r["total"] or "",
        "vod_actor": r["actor"] or "",
        "vod_director": r["director"] or "",
        "vod_plot": r["plot"] or "",
        "vod_time": r["vod_time"] or "",
    }


def _resp(code=1, msg="数据列表", page=1, pagecount=1, limit=20, total=0, lst=None):
    return {
        "code": code, "msg": msg, "page": page, "pagecount": pagecount,
        "limit": str(limit), "total": total, "list": lst or [],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # —— 输出 ——
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 静默访问日志（LibreTV 会高频轮询），仅记录异常
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        try:
            conn = db.connect(DB_PATH)

            # ── 健康检查 ──
            if u.path == "/healthz":
                st = db.stats(conn)
                return self._send({
                    "ok": True, "version": "1.0", "total": st["total"],
                    "per_source": st["per_source"], "type_count": st["type_count"],
                })

            if u.path == "/stats":
                return self._send(db.stats(conn))

            # ── 分类列表（分类导航墙）──
            if u.path in ("/api/php/provide/types", "/api.php/provide/types/"):
                rows = db.types(conn)
                groups = {}
                for r in rows:
                    name = r["type_name"] or "未分类"
                    g = groups.setdefault(name, {"type_name": name,
                                                "type_id": r["type_id"], "count": 0})
                    g["count"] += r["cnt"]
                return self._send({
                    "code": 1, "msg": "数据列表",
                    "class": sorted(groups.values(), key=lambda x: -x["count"]),
                })

            # ── 增强翻页（自维护界面用，非 CMS 协议）──
            if u.path == "/browse":
                return self._browse(conn, q, one)

            # ── CMS 主接口 ──
            if u.path.startswith("/api.php/provide/vod"):
                ac = one("ac", "list")
                if ac == "detail":
                    return self._detail(conn, one("ids", ""))
                if one("wd"):
                    return self._search(conn, one("wd"), one("pg", "1"))
                return self._list(conn, one("pg", "1"), q, one)

            return self._send({"error": "not found", "path": u.path}, 404)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send({"code": 0, "msg": f"{type(e).__name__}: {e}"}, 500)

    # ─────────────── 各接口实现 ───────────────

    def _search(self, conn, wd, pg):
        page_no = max(1, int(pg or 1))
        limit = 100
        rows = db.search(conn, wd, limit=page_no * limit)
        # 命中总数用一次 COUNT 近似（bigram 去重统计代价高，够用即可）
        all_rows = db.search(conn, wd, limit=1000)
        total = len(all_rows)
        pagecount = max(1, (total + limit - 1) // limit)
        slice_ = rows[(page_no - 1) * limit: page_no * limit]
        return self._send(_resp(page=page_no, pagecount=pagecount, limit=limit,
                                total=total, lst=[_row_cms(r) for r in slice_]))

    def _list(self, conn, pg, q, one):
        page_no = max(1, int(pg or 1))
        limit = int(one("limit", "20") or 20)
        type_id = one("type_id")
        year = one("year")
        order = one("order", "time")
        try:
            type_id = int(type_id) if type_id else None
        except ValueError:
            type_id = None
        rows, total = db.page(conn, type_id=type_id, year=year, order=order,
                              page_no=page_no, page_size=limit)
        pagecount = max(1, (total + limit - 1) // limit)
        return self._send(_resp(page=page_no, pagecount=pagecount, limit=limit,
                                total=total, lst=[_row_cms(r) for r in rows]))

    def _detail(self, conn, ids):
        if not ids:
            return self._send(_resp(lst=[]))
        out = []
        for token in str(ids).split(","):
            token = token.strip()
            if not token:
                continue
            row = None
            if ":" in token:
                # 显式格式 "1:72152"
                code, vid = token.split(":", 1)
                src = CODE_SRC.get(int(code))
                try:
                    row = db.detail(conn, src, int(vid)) if src else None
                except ValueError:
                    row = None
            else:
                try:
                    src, vid = _dec_id(int(token))
                except ValueError:
                    continue
                if src:
                    row = db.detail(conn, src, vid)
                else:
                    # 未带源前缀（裸源内 id）：跨源找
                    for s in SRC_CODE:
                        row = db.detail(conn, s, vid)
                        if row:
                            break
            if not row:
                continue
            d = db.row_to_dict(row)
            item = _row_cms(d)
            eps = d["episodes"] or []
            # 剧集串：名称$url#名称$url。名称统一用「第N集」——
            # 原站名称多为「第01集」「20221101期」，统一后前端展示更整齐
            item["vod_play_from"] = d["play_from"] or "index"
            item["vod_play_url"] = "#".join(
                f"第{i+1}集${u}" for i, u in enumerate(eps))
            out.append(item)
        return self._send(_resp(total=len(out), lst=out))

    def _browse(self, conn, q, one):
        """增强翻页：附带 has_episodes / src / vid，方便前端判断与直连。"""
        page_no = max(1, int(one("pg", "1") or 1))
        limit = min(100, int(one("limit", "30") or 30))
        try:
            type_id = int(one("type_id")) if one("type_id") else None
        except ValueError:
            type_id = None
        rows, total = db.page(conn, type_id=type_id, year=one("year"),
                              order=one("order", "time"),
                              page_no=page_no, page_size=limit)
        items = []
        for r in rows:
            eps = json.loads(r["episodes"] or "[]")
            items.append({
                "id": _enc_id(r["src"], r["vod_id"]),
                "src": SRC_NAME.get(r["src"], r["src"]),
                "name": r["name"], "pic": r["pic"] or "",
                "type_name": r["type_name"] or "", "year": r["year"] or "",
                "remarks": r["remarks"] or "", "serial": r["serial"] or "",
                "total": r["total"] or "", "score": r["score"] or 0,
                "has_episodes": bool(eps), "episodes": len(eps),
                "vod_time": r["vod_time"] or "",
            })
        return self._send({"code": 1, "page": page_no, "total": total,
                           "limit": limit, "list": items})


def main():
    conn = db.connect(DB_PATH)
    st = db.stats(conn)
    print(f"[index-api] DB={DB_PATH} 已索引 {st['total']} 条 / {st['type_count']} 个分类",
          flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[index-api] 监听 http://0.0.0.0:{PORT}", flush=True)
    print(f"[index-api] CMS 源地址: http://<host>:{PORT}/api.php/provide/vod", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
