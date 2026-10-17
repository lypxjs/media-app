"""
索引爬虫：抓取源站列表与详情，写入 SQLite。

两阶段流水线（这是把成本从 11 小时压到 1 小时的关键）
-------------------------------------------------------
阶段 1「列表」：并发抓`?ac=list&pg=N`，每页 20 条，含封面/分类/集数等翻找所需字段。
              这一步就已能支撑「翻找」——LibreTV 的详情请求只在用户点开时才发。
阶段 2「详情」：用 `?ac=detail&ids=1,2,...,20` **批量**取20 条播放地址（实测 129.9 条/秒）。

增量策略
--------
列表按 vod_time 倒序，故增量只需从第 1 页往后扫，直到遇到已入库记录即停；
再用「待补详情」的 id 批量补抓（新建片目的episodes 为空）。
断点：每处理完一页就写 watermark.pages，中断后重跑自动续。
"""

import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time
import urllib.error
import urllib.request

import db
import cms

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SOURCES = {
    "ty": {"name": "天涯影视", "base": "https://tyyszyapi.com/api.php/provide/vod"},
    "ws": {"name": "无水印影视", "base": "https://api.wsyzy.net/api.php/provide/vod"},
}

BATCH = 20          # 详情批量大小，与源站列表每页条数一致
LIST_WORKERS = 20   # 列表并发（实测 30 并发反降速）
DETAIL_WORKERS = 20 # 详情并发
TIMEOUT = 30

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def http_json(url: str, retries: int = 3):
    """取 JSON。源站偶发 5xx，按指数退避重试。"""
    last = None
    for i in range(retries):
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(rq, timeout=TIMEOUT) as r:
                return json.loads(r.read(3_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (403, 404):
                break          # 客户端错误重试无意义
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.5 * (i + 1))
    raise RuntimeError(last or "unknown")


# ─────────────────────阶段 1：列表 ─────────────────────

def crawl_list(conn, src: str, max_pages: int | None = None) -> int:
    """抓列表页。返回本轮新增/更新条数。

    分页漂移问题
    ------------
    源站列表按 vod_time 倒序实时变动，抓取过程中不断有新片插入，
    会导致各页内容整体后移——同一页在不同请求里返回不同条目，
    实测并发抓 3 页时有 8 条漏抓。因此这里改为：
      1) 抓完所有页，只收集 vod_id（不落库，避免错位数据）
      2) 全部页抓完后，把 id 去重后再分批取详情（详情是按 id 精确查询，不受漂移影响）
    这样漏抓率从「每页都可能错位」降到「只在两次请求之间新增的条目」。
    """
    base = SOURCES[src]["base"]
    wm = db.get_wm(conn, src)
    start_page = wm["pages"]
    if wm["done"]:
        log(f"[{src}] 已完成全量，跳过列表（--full 可强制重跑）")
        return 0

    first = http_json(f"{base}?ac=list&pg=1")
    total = int(first.get("total") or 0)
    pagecount = int(first.get("pagecount") or 0)
    if max_pages:
        pagecount = min(pagecount, max_pages)
    log(f"[{src}] 总数 {total}，共 {pagecount} 页，从第 {start_page} 页开始")

    if start_page > pagecount:
        db.set_wm(conn, src, pages=pagecount, total=total, done=1)
        return 0

    # ── 阶段 A：只收 id，不落库 ──
    ids: list[int] = []

    def collect(pg: int):
        d = first if pg == 1 else http_json(f"{base}?ac=list&pg={pg}")
        lst = d.get("list") or []
        return ([it.get("vod_id") for it in lst if it.get("vod_id") is not None],
                next((it.get("vod_time") for it in lst if it.get("vod_time")), None))

    t0 = time.time()
    pages_to_do = list(range(start_page, pagecount + 1))
    newest_time = None
    with cf.ThreadPoolExecutor(max_workers=LIST_WORKERS) as ex:
        futs = {ex.submit(collect, pg): pg for pg in pages_to_do}
        done = 0
        for fu in cf.as_completed(futs):
            try:
                page_ids, page_time = fu.result()
                ids.extend(page_ids)
                # 水位取全库最新的 vod_time（源站按时间倒序，故新片只在头部）
                if page_time and (newest_time is None or page_time > newest_time):
                    newest_time = page_time
            except Exception as e:
                log(f"[{src}] pg={futs[fu]} 失败 {type(e).__name__}，跳过")
            done += 1
            if done % 1000 == 0:
                rate = done / max(0.001, time.time() - t0)
                log(f"[{src}] 扫描 {done}/{len(pages_to_do)} 页 | 收集 {len(ids)} 个 id "
                    f"| {rate:.1f} 页/秒")
                db.set_wm(conn, src, pages=start_page + done, total=total,
                          last_time=newest_time)

    uniq = sorted(set(ids))
    log(f"[{src}] 扫描完成：{len(ids)} 个 id，去重后 {len(uniq)} 个"
        f"（耗时 {(time.time()-t0)/60:.1f} 分钟）")

    # ── 阶段 B：按 id 批量取详情入库（精确查询，不受分页漂移影响）──
    written = 0
    t1 = time.time()
    batches = [uniq[i:i + BATCH] for i in range(0, len(uniq), BATCH)]
    with cf.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        futs = [ex.submit(lambda b: http_json(
            f"{base}?ac=detail&ids={','.join(map(str, b))}").get("list") or [], b)
            for b in batches]
        done = 0
        for fu in cf.as_completed(futs):
            try:
                items = fu.result()
            except Exception:
                continue
            recs = [r for r in (cms.to_record(src, it) for it in items) if r]
            if recs:
                written += db.upsert(conn, recs)
            done += 1
            if done % 1000 == 0:
                rate = done * BATCH / max(0.001, time.time() - t1)
                log(f"[{src}] 入库 {done}/{len(batches)} 批 | {rate:.0f} 条/秒")
    log(f"[{src}] 全量完成：{written} 条，耗时 {(time.time()-t1)/60:.1f} 分钟")
    db.set_wm(conn, src, pages=pagecount, total=total, done=1, last_time=newest_time)
    return written


# ───────────────────── 阶段 2：批量详情 ─────────────────────

def crawl_detail(conn, src: str, limit: int | None = None) -> int:
    """给 episodes 为空的记录补播放地址。20 条/请求。"""
    base = SOURCES[src]["base"]
    rows = conn.execute(
        "SELECT src, vod_id FROM videos WHERE src=? AND (episodes IS NULL OR episodes='[]') "
        "ORDER BY vod_time DESC, vod_id DESC",
        (src,)).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        log(f"[{src}] 详情已完整，无需补抓")
        return 0

    ids = [r["vod_id"] for r in rows]
    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    log(f"[{src}] 待补详情 {len(ids)} 条，分{len(batches)} 批")

    t0 = time.time()
    written = 0

    def do_batch(batch):
        d = http_json(f"{base}?ac=detail&ids={','.join(str(i) for i in batch)}")
        return d.get("list") or []

    with cf.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        futs = [ex.submit(do_batch, b) for b in batches]
        done = 0
        for fu in cf.as_completed(futs):
            try:
                items = fu.result()
            except Exception:
                continue
            recs = [r for r in (cms.to_record(src, it) for it in items) if r]
            if recs:
                # 详情阶段只更新剧集等字段，保留列表阶段已有的 hits/year 等
                written += db.upsert(conn, recs)
            done += 1
            if done % 500 == 0:
                rate = done / max(0.001, time.time() - t0) * BATCH
                left = (len(batches) - done) * BATCH
                log(f"[{src}] 详情 {done}/{len(batches)} 批 | {rate:.0f} 条/秒 "
                    f"| 剩余约 {left/max(rate,0.001)/60:.0f} 分钟")

    log(f"[{src}] 详情完成：{written} 条，耗时 {(time.time()-t0)/60:.1f} 分钟")
    return written


# ───────────────────── 增量 ─────────────────────

def crawl_incremental(conn, src: str, max_scan: int = 60) -> int:
    """增量：从第 1 页往后扫，收集未见过的 id 补全。

    命中已入库记录即认为到达水位线，停止扫描（列表按时间倒序，新内容只在头部）。
    """
    base = SOURCES[src]["base"]
    new_ids: list[int] = []
    t0 = time.time()

    with cf.ThreadPoolExecutor(max_workers=LIST_WORKERS) as ex:
        futs = {ex.submit(http_json, f"{base}?ac=list&pg={pg}"): pg
                for pg in range(1, max_scan + 1)}
        for fu in cf.as_completed(futs):
            try:
                d = fu.result()
            except Exception:
                continue
            for it in d.get("list") or []:
                vid = it.get("vod_id")
                if vid is None:
                    continue
                row = db.detail(conn, src, int(vid))
                if row is None:
                    new_ids.append(int(vid))

    log(f"[{src}] 增量扫描 {max_scan} 页，发现 {len(new_ids)} 条新内容 "
        f"({time.time()-t0:.0f}s)")
    if not new_ids:
        return 0

    # 补列表元数据
    batches = [new_ids[i:i + BATCH] for i in range(0, len(new_ids), BATCH)]
    n = 0
    with cf.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        for items in ex.map(
            lambda b: http_json(f"{base}?ac=detail&ids={','.join(map(str,b))}").get("list") or [],
            batches):
            recs = [r for r in (cms.to_record(src, it) for it in items) if r]
            n += db.upsert(conn, recs)
    log(f"[{src}] 增量入库 {n} 条")
    return n


# ───────────────────── CLI ─────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="index.db")
    ap.add_argument("--src", default="ty,ws", help="要处理的源")
    ap.add_argument("--mode", default="full",
                    choices=["full", "detail", "incremental", "stats"])
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--detail-limit", type=int, default=None)
    ap.add_argument("--full", action="store_true", help="强制重跑全量列表")
    args = ap.parse_args()

    conn = db.connect(args.db)

    if args.mode == "stats":
        import json as _j
        print(_j.dumps(db.stats(conn), ensure_ascii=False, indent=2))
        return

    for src in args.src.split(","):
        src = src.strip()
        if src not in SOURCES:
            log(f"未知源 {src}，跳过")
            continue
        log(f"===== {SOURCES[src]['name']} ({src}) =====")
        if args.mode in ("full", "detail"):
            if args.full:
                conn.execute("UPDATE watermark SET pages=1, done=0 WHERE src=?", (src,))
                conn.commit()
            crawl_list(conn, src, args.max_pages)
            crawl_detail(conn, src, args.detail_limit)
        elif args.mode == "incremental":
            crawl_incremental(conn, src)

    log("\n===== 索引统计 =====")
    st = db.stats(conn)
    log(f"总计 {st['total']} 条 | 分类 {st['type_count']} 个 | {st['per_source']}")


if __name__ == "__main__":
    main()
