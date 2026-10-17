package com.mediadb.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 酷我音源（移动端接口，**不需要登录**）。
 *
 * 接口来源
 * --------
 * NAS 上 SQMusic 的插件配置 `application-kw.yml` 里写着 `islogin: false`，
 * 并列出了全部端点。所以 App 不必依赖 SQMusic、也不必依赖 NAS，
 * 直接调这几个公开的移动端接口即可：
 *
 *   搜索  http://search.kuwo.cn/r.s?client=kt&...&all=<关键词>
 *   直链  https://mobi.kuwo.cn/mobi.s?...&type=convert_url_with_sign&rid=<id>&br=<音质>
 *
 * 直链是服务端签好名的 CDN 地址，**可匿名访问且支持 Range**（实测 206），
 * 播放器可以直接连，不需要任何请求头。
 *
 * 注意：官网 API（www.kuwo.cn/api/...）需要 csrf token，走不通；
 * 上面这两个是给移动端用的，反而更宽松。
 */
object MusicApi {

    private const val TAG = "MusicApi"

    private const val UA =
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"

    private const val SEARCH = "http://search.kuwo.cn/r.s" +
            "?client=kt&encoding=utf8&rformat=json&mobi=1&vipver=1&correct=1"

    private const val SIGN_BASE = "https://mobi.kuwo.cn/mobi.s" +
            "?f=web&user=0&source=kwplayer_ar_5.0.0.0_B_jiakong_vh.apk" +
            "&type=convert_url_with_sign"

    /** 音质档位：前端展示用 */
    val QUALITIES = listOf("2000kflac", "320kmp3", "128kmp3")

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    /**
     * 搜索歌曲。
     * @return `{"total":N,"list":[{rid,name,artist,album,dur}]}`
     */
    fun search(kw: String, page: Int = 1, rn: Int = 40): String {
        val k = kw.trim()
        if (k.isEmpty()) return """{"total":0,"list":[]}"""
        val url = "$SEARCH&pn=${maxOf(1, page)}&rn=${rn.coerceIn(1, 100)}" +
                "&all=${enc(k)}"
        val body = get(url) ?: return """{"error":"网络不通"}"""

        val root = try {
            JSONObject(body)
        } catch (e: Exception) {
            Log.w(TAG, "搜索响应解析失败: ${body.take(120)}")
            return """{"error":"解析失败"}"""
        }

        val arr = root.optJSONArray("abslist") ?: JSONArray()
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            // MUSICRID 形如 "MUSIC_10286955"，取数字部分
            val rid = o.optString("MUSICRID").substringAfterLast('_')
            if (rid.isBlank() || rid.toLongOrNull() == null) continue
            val name = o.optString("SONGNAME").ifBlank { o.optString("NAME") }
            if (name.isBlank()) continue

            out.put(
                JSONObject()
                    .put("rid", rid)
                    .put("name", name)
                    .put("artist", o.optString("ARTIST"))
                    .put("album", o.optString("ALBUM"))
                    .put("dur", o.optString("DURATION").toIntOrNull() ?: 0)
            )
        }
        val total = root.optString("TOTAL").toIntOrNull() ?: out.length()
        return JSONObject().put("total", total).put("list", out).toString()
    }

    /**
     * 取播放直链。
     * @param br 2000kflac / 320kmp3 / 128kmp3；失败时自动降一档
     * @return `{"url":"...","format":"mp3","br":"320kmp3","dur":269}`
     */
    fun playUrl(rid: String, br: String): String {
        var cur = br
        // 依次降级：flac → 320 → 128
        val order = when (br) {
            "2000kflac" -> listOf("2000kflac", "320kmp3", "128kmp3")
            "128kmp3" -> listOf("128kmp3", "320kmp3")
            else -> listOf("320kmp3", "128kmp3")
        }
        for (q in order) {
            cur = q
            val body = get("$SIGN_BASE&rid=$rid&br=$q") ?: continue
            val root = try {
                JSONObject(body)
            } catch (e: Exception) {
                continue
            }
            if (root.optInt("code") != 200) continue
            val d = root.optJSONObject("data") ?: continue
            val url = d.optString("url")
            if (url.isBlank()) continue
            return JSONObject()
                .put("url", url)
                .put("format", d.optString("format"))
                .put("br", cur)
                .put("dur", d.optInt("duration"))
                .toString()
        }
        return """{"error":"该曲无可用音源"}"""
    }

    // ── 内部 ────────────────────────────────────────────────

    private fun get(url: String): String? = try {
        val req = Request.Builder()
            .url(url)
            .header("User-Agent", UA)
            .header("Referer", "https://www.kuwo.cn/")
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                Log.w(TAG, "HTTP ${resp.code} ${url.take(80)}")
                null
            } else {
                resp.body?.string()
            }
        }
    } catch (e: Exception) {
        Log.w(TAG, "请求失败: ${e.javaClass.simpleName} ${e.message}")
        null
    }

    private fun enc(s: String): String =
        java.net.URLEncoder.encode(s, "UTF-8")
}
