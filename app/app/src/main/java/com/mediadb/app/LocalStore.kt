package com.mediadb.app

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.File
import java.io.InputStream
import java.util.zip.GZIPInputStream

/**
 * 本地片库：让 APK 完全离线可用，不依赖家里的 NAS。
 *
 * 数据从哪来
 * ----------
 * `assets/library.db.gz`（25 MB，含 197490 条）。首次启动解压到私有目录，
 * 之后直接打开 —— 解压是一次性的，约 2~4 秒。
 *
 * 为什么索引必须内置
 * ----------------
 * 采集站的 CMS 详情接口已返回 404（域名/接口变动），
 * 播放时无法按 id 回查剧集地址。剧集地址（episodes，71.9 MB）
 * 只能随索引一起打包，否则点了播不了。
 *
 * 播放不走这里
 * -----------
 * 播放地址直接交给 ExoPlayer 连 CDN，App 只负责查地址。
 * 封面同理，由浏览器直连图床（图床自带 30 天缓存）。
 */
object LocalStore {

    private const val TAG = "LocalStore"
    private const val DB_NAME = "library.db"

    /**
     * 内置片库的资源名。
     *
     * ⚠️ AGP 在 mergeDebugAssets 阶段会把 assets 里的 `.gz` **自动解压**，
     * 所以打进 APK 的名字是 `library.db` 而不是 `library.db.gz`
     * （实测：源目录 26MB 的 library.db.gz → APK 内 132MB 的 library.db）。
     * 两个名字都试，并对内容做 gzip 魔数判断，避免依赖这个隐式行为。
     */
    private val ASSET_NAMES = listOf("library.db", "library.db.gz")

    @Volatile
    private var db: SQLiteDatabase? = null

    fun isReady(): Boolean = db != null

    /**
     * 首次启动解压内置片库，之后直接打开。
     * 注意：解压 132 MB 需要几秒，调用方应在后台线程执行并显示进度。
     */
    fun init(ctx: Context) {
        if (db != null) return
        val f = File(ctx.filesDir, DB_NAME)
        if (!f.exists() || f.length() < 1024) {
            val t0 = System.currentTimeMillis()
            f.parentFile?.mkdirs()
            // 先写临时文件再改名：避免解压中途被杀进程留下半个库
            val tmp = File(ctx.filesDir, "$DB_NAME.tmp")
            openLibrary(ctx).use { ins ->
                tmp.outputStream().use { out -> ins.copyTo(out, 1 shl 16) }
            }
            if (f.exists()) f.delete()
            tmp.renameTo(f)
            Log.i(TAG, "片库解压完成 ${f.length() / 1048576}MB，耗时 ${System.currentTimeMillis() - t0}ms")
        }
        db = SQLiteDatabase.openDatabase(f.path, null, SQLiteDatabase.OPEN_READWRITE)
        // 观看进度存在同一个库里：App 私有，别处不共享
        db?.execSQL(
            """CREATE TABLE IF NOT EXISTS progress(
                   src TEXT NOT NULL, vod_id INTEGER NOT NULL,
                   episode INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                   finished INTEGER DEFAULT 0, pos INTEGER DEFAULT 0,
                   dur INTEGER DEFAULT 0, ts INTEGER DEFAULT 0,
                   PRIMARY KEY(src, vod_id))"""
        )
        db?.execSQL("CREATE INDEX IF NOT EXISTS i_prog ON progress(ts)")
        // (src, vod_id) 唯一索引：增量同步靠它做幂等写入（INSERT OR REPLACE）。
        // 首次建索引约 1 秒；已存在时 IF NOT EXISTS 几乎零开销。
        try {
            db?.execSQL("CREATE UNIQUE INDEX IF NOT EXISTS u_vid ON videos(src, vod_id)")
        } catch (e: Exception) {
            // 库里若已有重复行，建索引会失败 —— 不影响使用，
            // mergeEntries 会退回「先删后插」的写法。
            Log.w(TAG, "唯一索引建立失败（将用先删后插）: ${e.message}")
        }
    }

    // ── 增量同步 ──────────────────────────────────────────────

    /** 本地最新的 `vod_time`，作为增量同步的水位 */
    fun maxVodTime(): String {
        val d = db ?: return ""
        return d.rawQuery("SELECT MAX(vod_time) FROM videos", null).use {
            if (it.moveToFirst()) it.getString(0) ?: "" else ""
        }
    }

    /**
     * 合并 NAS 增量条目，返回写入条数。
     * 依赖 (src, vod_id) 唯一索引；没有唯一约束时先删后插，保证不产生重复。
     */
    fun mergeEntries(arr: JSONArray): Int {
        val d = db ?: return 0
        if (arr.length() == 0) return 0
        var n = 0
        d.beginTransaction()
        try {
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val src = o.optString("src")
                val vid = o.optInt("vod_id")
                if (src.isBlank() || vid <= 0) continue
                val eps = o.optJSONArray("episodes")?.toString() ?: "[]"
                try {
                    d.execSQL(
                        """INSERT OR REPLACE INTO videos
                           (src,vod_id,name,pic,type_id,type_name,year,area,
                            remarks,score,hits,serial,total,episodes,vod_time)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        arrayOf<Any?>(
                            src, vid, o.optString("name"), o.optString("pic"),
                            o.optInt("type_id"), o.optString("type_name"),
                            o.optString("year"), o.optString("area"),
                            o.optString("remarks"), o.optDouble("score", 0.0),
                            o.optInt("hits"), o.optString("serial"),
                            o.optString("total"), eps, o.optString("vod_time")
                        )
                    )
                } catch (e: Exception) {
                    // 没有唯一约束时会插入重复，这里兜底：先删再插
                    d.execSQL("DELETE FROM videos WHERE src=? AND vod_id=?",
                        arrayOf<Any?>(src, vid))
                    d.execSQL(
                        """INSERT INTO videos
                           (src,vod_id,name,pic,type_id,type_name,year,area,
                            remarks,score,hits,serial,total,episodes,vod_time)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        arrayOf<Any?>(
                            src, vid, o.optString("name"), o.optString("pic"),
                            o.optInt("type_id"), o.optString("type_name"),
                            o.optString("year"), o.optString("area"),
                            o.optString("remarks"), o.optDouble("score", 0.0),
                            o.optInt("hits"), o.optString("serial"),
                            o.optString("total"), eps, o.optString("vod_time")
                        )
                    )
                }
                n++
            }
            d.setTransactionSuccessful()
        } finally {
            d.endTransaction()
        }
        return n
    }

    /** 打开内置片库流，自动识别是否仍是 gzip */
    private fun openLibrary(ctx: Context): InputStream {
        var last: Exception? = null
        for (name in ASSET_NAMES) {
            try {
                val raw = BufferedInputStream(ctx.assets.open(name), 1 shl 16)
                raw.mark(2)
                val b1 = raw.read()
                val b2 = raw.read()
                raw.reset()
                return if (b1 == 0x1f && b2 == 0x8b) {
                    Log.i(TAG, "片库资源 $name 为 gzip，边解压边写入")
                    GZIPInputStream(raw, 1 shl 16)
                } else {
                    Log.i(TAG, "片库资源 $name 已解压，直接复制")
                    raw
                }
            } catch (e: Exception) {
                last = e
            }
        }
        throw IllegalStateException("内置片库缺失：${ASSET_NAMES.joinToString()} (${last?.message})")
    }

    fun stats(): String {
        val d = db ?: return "{}"
        val n = d.rawQuery("SELECT COUNT(*) FROM videos", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        val types = d.rawQuery("SELECT COUNT(DISTINCT type_id) FROM videos", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        return JSONObject().put("total", n).put("types", types).toString()
    }

    // ── 分类 ────────────────────────────────────────────────

    fun types(): String {
        val d = db ?: return empty()
        val arr = JSONArray()
        d.rawQuery(
            """SELECT type_id, type_name, COUNT(*) c FROM videos
               WHERE type_id IS NOT NULL
               GROUP BY type_id, type_name ORDER BY c DESC""", null
        ).use { c ->
            while (c.moveToNext()) {
                arr.put(
                    JSONObject()
                        .put("type_id", c.getInt(0))
                        .put("type_name", c.getString(1) ?: "")
                        .put("count", c.getInt(2))
                )
            }
        }
        return JSONObject().put("class", arr).toString()
    }

    // ── 列表浏览 ─────────────────────────────────────────────

    fun browse(typeId: Int, year: String, order: String, limit: Int, page: Int): String {
        val d = db ?: return emptyList()
        val where = ArrayList<String>()
        val args = ArrayList<String>()
        if (typeId > 0) {
            where.add("type_id = ?"); args.add(typeId.toString())
        }
        if (year.isNotBlank()) {
            where.add("year = ?"); args.add(year.trim())
        }
        val clause = if (where.isEmpty()) "" else " WHERE " + where.joinToString(" AND ")
        val orderSql = when (order) {
            "hits" -> "hits DESC, vod_time DESC"
            "score" -> "score DESC, vod_time DESC"
            else -> "vod_time DESC, vod_id DESC"
        }
        val total = d.rawQuery("SELECT COUNT(*) FROM videos$clause", args.toTypedArray()).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        val pageNo = maxOf(1, page)
        val size = limit.coerceIn(1, 120)
        val off = (pageNo - 1) * size
        val list = JSONArray()
        d.rawQuery(
            "SELECT * FROM videos$clause ORDER BY $orderSql LIMIT ? OFFSET ?",
            (args + size.toString() + off.toString()).toTypedArray()
        ).use { c ->
            while (c.moveToNext()) list.put(card(c))
        }
        return JSONObject().put("list", list).put("total", total).toString()
    }

    // ── 搜索 ────────────────────────────────────────────────

    /**
     * 片名搜索。
     *
     * 服务端用的是 bigram 倒排表，这里没导出那张表（省体积），
     * 改用 LIKE 全表扫描 —— 19.7 万条实测百毫秒级，翻页无感。
     */
    fun search(wd: String, limit: Int): String {
        val d = db ?: return emptyList()
        val k = wd.trim()
        if (k.isEmpty()) return emptyList()
        val esc = k.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        val list = JSONArray()
        d.rawQuery(
            "SELECT * FROM videos WHERE name LIKE ? ESCAPE '\\' ORDER BY vod_time DESC LIMIT ?",
            arrayOf("%$esc%", limit.coerceIn(1, 200).toString())
        ).use { c ->
            while (c.moveToNext()) list.put(card(c))
        }
        return JSONObject().put("list", list).toString()
    }

    // ── 详情（剧集地址）─────────────────────────────────────

    fun detail(ids: String): String {
        val d = db ?: return emptyList()
        val out = JSONArray()
        for (ident in ids.split(",")) {
            val kv = parseId(ident) ?: continue
            d.rawQuery(
                "SELECT * FROM videos WHERE src=? AND vod_id=?",
                arrayOf(kv.first, kv.second.toString())
            ).use { c ->
                if (!c.moveToFirst()) return@use
                val epsJson = c.getString(c.getColumnIndexOrThrow("episodes")) ?: "[]"
                val arr = try {
                    JSONArray(epsJson)
                } catch (e: Exception) {
                    JSONArray()
                }
                val sb = StringBuilder()
                for (i in 0 until arr.length()) {
                    if (i > 0) sb.append('#')
                    sb.append("第").append(i + 1).append("集$").append(arr.optString(i))
                }
                out.put(
                    JSONObject()
                        .put("vod_id", kv.second)
                        .put("vod_name", c.getString(c.getColumnIndexOrThrow("name")) ?: "")
                        .put("vod_pic", c.getString(c.getColumnIndexOrThrow("pic")) ?: "")
                        .put("vod_year", c.getString(c.getColumnIndexOrThrow("year")) ?: "")
                        .put("vod_area", c.getString(c.getColumnIndexOrThrow("type_name")) ?: "")
                        .put("vod_remarks", c.getString(c.getColumnIndexOrThrow("remarks")) ?: "")
                        .put("vod_play_url", sb.toString())
                )
            }
        }
        return JSONObject().put("list", out).toString()
    }

    // ── 观看进度 ─────────────────────────────────────────────

    fun getProgress(ids: String): String {
        val d = db ?: return "{}"
        val obj = JSONObject()
        for (ident in ids.split(",")) {
            val kv = parseId(ident) ?: continue
            d.rawQuery(
                "SELECT episode,total,finished,pos,dur FROM progress WHERE src=? AND vod_id=?",
                arrayOf(kv.first, kv.second.toString())
            ).use { c ->
                if (c.moveToFirst()) {
                    obj.put(
                        ident.trim(), JSONObject()
                            .put("episode", c.getInt(0))
                            .put("episodes", c.getInt(1))
                            .put("finished", c.getInt(2) == 1)
                            .put("pos", c.getInt(3))
                            .put("dur", c.getInt(4))
                    )
                }
            }
        }
        return obj.toString()
    }

    fun saveProgress(json: String) {
        val d = db ?: return
        try {
            val o = JSONObject(json)
            val kv = parseId(o.optString("id")) ?: return
            val ep = o.optInt("episode", 0)
            val total = o.optInt("episodes", 0)
            val finished = if (o.has("finished")) o.optBoolean("finished") else ep + 1 >= total && total > 0
            val pos = o.optInt("position", 0)
            val dur = o.optInt("duration", 0)
            d.execSQL(
                """INSERT OR REPLACE INTO progress
                   (src,vod_id,episode,total,finished,pos,dur,ts)
                   VALUES(?,?,?,?,?,?,?,?)""",
                arrayOf<Any>(
                    kv.first, kv.second, ep, total,
                    if (finished) 1 else 0, pos, dur, System.currentTimeMillis()
                )
            )
        } catch (e: Exception) {
            Log.w(TAG, "保存进度失败: ${e.message}")
        }
    }

    // ── 内部 ────────────────────────────────────────────────

    /** "1:72186" → ("ty", 72186)。编码与 web.py 的 _card 一致：ty=1 / ws=2。 */
    private fun parseId(ident: String?): Pair<String, Int>? {
        val s = ident?.trim() ?: return null
        val i = s.indexOf(':')
        if (i <= 0) return null
        val code = s.substring(0, i).toIntOrNull() ?: return null
        val vid = s.substring(i + 1).toIntOrNull() ?: return null
        val src = when (code) {
            1 -> "ty"
            2 -> "ws"
            else -> return null
        }
        return src to vid
    }

    private fun card(c: android.database.Cursor): JSONObject {
        val src = c.getString(c.getColumnIndexOrThrow("src")) ?: ""
        val vid = c.getInt(c.getColumnIndexOrThrow("vod_id"))
        val code = when (src) {
            "ty" -> 1
            "ws" -> 2
            else -> 9
        }
        val eps = c.getString(c.getColumnIndexOrThrow("episodes")) ?: "[]"
        val epCount = try {
            JSONArray(eps).length()
        } catch (e: Exception) {
            0
        }
        return JSONObject()
            .put("id", "$code:$vid")
            .put("name", c.getString(c.getColumnIndexOrThrow("name")) ?: "")
            .put("pic", c.getString(c.getColumnIndexOrThrow("pic")) ?: "")
            .put("type_name", c.getString(c.getColumnIndexOrThrow("type_name")) ?: "")
            .put("year", c.getString(c.getColumnIndexOrThrow("year")) ?: "")
            .put("remarks", c.getString(c.getColumnIndexOrThrow("remarks")) ?: "")
            .put("serial", c.getString(c.getColumnIndexOrThrow("serial")) ?: "")
            .put("total", c.getString(c.getColumnIndexOrThrow("total")) ?: epCount.toString())
            .put("score", c.getDouble(c.getColumnIndexOrThrow("score")))
            .put("episodes", epCount)
            .put("vod_time", c.getString(c.getColumnIndexOrThrow("vod_time")) ?: "")
    }

    private fun empty() = JSONObject().put("class", JSONArray()).toString()
    private fun emptyList() = JSONObject().put("list", JSONArray()).toString()
}
