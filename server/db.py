"""
索引库：SQLite schema 与访问层。

设计要点
--------
1. **FTS5 承载片名搜索**：20 万条下 LIKE 全表扫描约 200ms，FTS5 倒排索引 <5ms。
   表结构用 `fts5(title, src UNINDEXED, vod_id UNINDEXED)`：
   title 进倒排索引，src/vod_id 标记为 UNINDEXED 仅作结果回表用——
   这样一次 MATCH 就能直接拿到主表坐标，不必再 JOIN。
   （不能用 content=''：该模式只存倒排索引不存原文，查询拿不到片名本身。）

2. **主表保持普通 rowid 表**：episodes 这类大字段若用 WITHOUT ROWID 聚簇存储，
   按 vod_id 取整行时会拖慢读取，而按 (type_id,vod_time) 等维度已有独立索引覆盖。

3. **episodes 存 JSON 文本**：只有详情响应时才 loads，翻页/搜索不解析。

4. **watermark 记录每源水位**：src + 已同步页码 + 时间水位，支持增量续跑与断点恢复。

5. **连接按线程复用**：HTTP 服务是多线程的，sqlite3 连接不能跨线程共享。
"""

import json
import os
import sqlite3
import threading

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS videos (
    src          TEXT    NOT NULL,       -- 源标识：ty / ws
    vod_id       INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    pic          TEXT,
    type_id      INTEGER,
    type_name    TEXT,
    year         TEXT,
    area         TEXT,
    remarks      TEXT,
    score        REAL    DEFAULT 0,
    hits         INTEGER DEFAULT 0,
    serial       TEXT,                   -- 已更集数，如 "14"
    total        TEXT,                   -- 总集数，如 "24"
    actor        TEXT,
    director     TEXT,
    plot         TEXT,
    play_from    TEXT,
    episodes     TEXT,                   -- JSON 数组：["m3u8url", ...]
    vod_time     TEXT,                   -- 源站入库时间（增量水位依据）
    updated_at   INTEGER,
    PRIMARY KEY (src, vod_id)
);

-- 覆盖常用筛选/排序维度
CREATE INDEX IF NOT EXISTS idx_type_time ON videos(type_id, vod_time DESC);
CREATE INDEX IF NOT EXISTS idx_year      ON videos(year);
CREATE INDEX IF NOT EXISTS idx_src_time  ON videos(src, vod_time DESC);

-- 片名检索：自建 bigram 倒排表。
-- 为什么不用 FTS5：实测 unicode61 与 trigram 分词器对中文**短查询（2 字）全部失效**
-- （搜「山野」「三体」「流浪」命中 0，只有 ≥3 字的「山野藏金」能中）。
-- 中文片名大量是 2-3 字（庆余年/三体/药神），FTS5 直接不可用，故自建 bigram。
-- gram 取「相邻两字」；查询时按命中 gram 数降序——精确片名包含更多 gram，自然排最前。
CREATE TABLE IF NOT EXISTS grams (
    g       TEXT NOT NULL,
    src     TEXT NOT NULL,
    vod_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gram     ON grams(g, src, vod_id);
CREATE INDEX IF NOT EXISTS idx_gram_src ON grams(src, vod_id);

-- 每源同步状态
CREATE TABLE IF NOT EXISTS watermark (
    src        TEXT PRIMARY KEY,
    pages      INTEGER DEFAULT 1,        -- 已完成的列表页数
    last_time  TEXT,                     -- 已同步到的时间水位
    total      INTEGER DEFAULT 0,        -- 源站声明总数
    done       INTEGER DEFAULT 0,        -- 1 = 全量完成
    updated_at INTEGER
);
"""

_local = threading.local()


def connect(path: str) -> sqlite3.Connection:
    """线程内复用连接；首次调用时建表。"""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    if path != ":memory:":
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    _local.conn = conn
    _local.path = path
    return conn


# ───────────────────────── 写入 ─────────────────────────

def _bigrams(name: str) -> set[str]:
    """取片名的相邻二字组合。跳过空白与标点，避免「我的」「：」这类无意义 gram。"""
    s = "".join(ch for ch in name if not ch.isspace() and ch not in "：:，,。.、-—·！!？?【】[]（）()\"'")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """批量写入/更新视频，并同步维护 bigram 倒排。返回写入条数。

    gram 表按 (src,vod_id) 先删后插，保证改名后旧 gram 不残留。
    """
    cur = conn.cursor()
    now = int(_now())
    n = 0
    for r in rows:
        ep = r.get("episodes") or []
        cur.execute(
            """INSERT INTO videos
               (src,vod_id,name,pic,type_id,type_name,year,area,remarks,
                score,hits,serial,total,actor,director,plot,play_from,
                episodes,vod_time,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(src,vod_id) DO UPDATE SET
                 name=excluded.name, pic=excluded.pic,
                 type_id=excluded.type_id, type_name=excluded.type_name,
                 year=excluded.year, area=excluded.area, remarks=excluded.remarks,
                 score=excluded.score, hits=excluded.hits,
                 serial=excluded.serial, total=excluded.total,
                 actor=excluded.actor, director=excluded.director,
                 plot=excluded.plot, play_from=excluded.play_from,
                 episodes=excluded.episodes, vod_time=excluded.vod_time,
                 updated_at=excluded.updated_at""",
            (
                r["src"], r["vod_id"], r["name"], r.get("pic"),
                r.get("type_id"), r.get("type_name"), r.get("year"), r.get("area"),
                r.get("remarks"), r.get("score") or 0.0, r.get("hits") or 0,
                r.get("serial"), r.get("total"), r.get("actor"), r.get("director"),
                r.get("plot"), r.get("play_from"),
                json.dumps(ep, ensure_ascii=False) if ep else None,
                r.get("vod_time"), r.get("updated_at") or now,
            ),
        )
        cur.execute("DELETE FROM grams WHERE src=? AND vod_id=?", (r["src"], r["vod_id"]))
        for g in _bigrams(r["name"]):
            cur.execute("INSERT INTO grams (g,src,vod_id) VALUES (?,?,?)",
                        (g, r["src"], r["vod_id"]))
        n += 1
    conn.commit()
    return n


# ───────────────────────── 查询 ─────────────────────────

def search(conn, keyword: str, limit: int = 100) -> list[sqlite3.Row]:
    """片名检索（bigram 倒排）。

    排序策略：命中 gram 数降序→ 片名完全相等 → 更新时问倒序。
    「山野藏金」相对「山野生金：我的乡村创业路」包含更多 gram（3 vs 1），自然排前。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    s = "".join(ch for ch in kw if not ch.isspace())
    grams = sorted({s[i:i + 2] for i in range(len(s) - 1)}) or [s]
    ph = ",".join("?" * len(grams))
    return conn.execute(
        f"""SELECT v.*, COUNT(DISTINCT g.g) AS hits_cnt
            FROM grams g JOIN videos v ON v.src=g.src AND v.vod_id=g.vod_id
            WHERE g.g IN ({ph})
            GROUP BY v.src, v.vod_id
            ORDER BY hits_cnt DESC, (v.name = ?) DESC, v.vod_time DESC
            LIMIT ?""",
        grams + [kw, limit],
    ).fetchall()


def page(conn, *, type_id=None, year=None, order="time", page_no=1,
         page_size=20, src=None) -> tuple[list[sqlite3.Row], int]:
    """翻页/筛选/排序。返回 (当页行, 总条数)。

    order: time(最新) / hits(热门) / score(评分)
    源站不支持这些筛选，全靠本地库实现。
    """
    where, args = [], []
    if type_id is not None:
        where.append("type_id = ?"); args.append(type_id)
    if year:
        where.append("year = ?"); args.append(str(year))
    if src:
        where.append("src = ?"); args.append(src)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    order_sql = {
        "time": "vod_time DESC, vod_id DESC",
        "hits": "hits DESC, vod_time DESC",
        "score": "score DESC, vod_time DESC",
    }.get(order, "vod_time DESC, vod_id DESC")

    total = conn.execute(f"SELECT COUNT(*) FROM videos{clause}", args).fetchone()[0]
    off = (max(1, page_no) - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM videos{clause} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        args + [page_size, off],
    ).fetchall()
    return rows, total


def detail(conn, src: str, vod_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM videos WHERE src=? AND vod_id=?", (src, vod_id)
    ).fetchone()


def types(conn) -> list[sqlite3.Row]:
    """分类列表（含片数），按片数倒序——用于生成分类导航墙。"""
    return conn.execute(
        """SELECT type_id, type_name, COUNT(*) AS cnt,
                  MAX(vod_time) AS latest
           FROM videos WHERE type_id IS NOT NULL
           GROUP BY type_id, type_name
           ORDER BY cnt DESC"""
    ).fetchall()


def stats(conn) -> dict:
    t = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    per = conn.execute(
        "SELECT src, COUNT(*) c FROM videos GROUP BY src").fetchall()
    wm = conn.execute("SELECT * FROM watermark").fetchall()
    return {
        "total": t,
        "per_source": {r["src"]: r["c"] for r in per},
        "type_count": conn.execute(
            "SELECT COUNT(DISTINCT type_id) FROM videos").fetchone()[0],
        "watermarks": [dict(r) for r in wm],
    }


# ───────────────────────── 水位 ─────────────────────────

def get_wm(conn, src: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM watermark WHERE src=?", (src,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO watermark (src, pages, done) VALUES (?,1,0)",
            (src,))
        conn.commit()
        row = conn.execute("SELECT * FROM watermark WHERE src=?", (src,)).fetchone()
    return row


def set_wm(conn, src: str, **kw) -> None:
    if not kw:
        return
    kw["updated_at"] = int(_now())
    cols = ", ".join(f"{k}=?" for k in kw)
    conn.execute(
        f"UPDATE watermark SET {cols} WHERE src=?",
        list(kw.values()) + [src])
    conn.commit()


def _now() -> float:
    import time
    return time.time()


def row_to_dict(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    try:
        d["episodes"] = json.loads(d.get("episodes") or "[]")
    except Exception:
        d["episodes"] = []
    return d
