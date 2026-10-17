package com.mediadb.app

import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * 片库增量同步。
 *
 * 定位
 * ----
 * App 本身完全独立（内置 19.7 万条，不连服务器）。这里只做一件事：
 * **回家连 WiFi 时，把 NAS 新抓到的片子补进本地库**，免得每次都要重新打包 APK。
 *
 * 为什么只走局域网
 * --------------
 * 家里没有公网 IPv4；IPv6 的入站被 OpenWrt 的 wan 区域拦住（实测 ICMP 通、TCP 全不通）。
 * 所以这个同步只在家里的 WiFi 下会命中，在外网会 2.5 秒超时后静默跳过 ——
 * 不拖慢启动，也不影响任何功能。
 *
 * 幂等性
 * -----
 * 以 `vod_time` 为水位，只拉比本地更新的条目；写入用 INSERT OR REPLACE，
 * 重复拉取不会产生重复记录。
 */
object SyncManager {

    private const val TAG = "SyncManager"

    /** NAS 局域网地址。只用于补数据，App 不依赖它也能完整使用。 */
    private const val LAN_BASE = "http://127.0.0.1:8091"

    private const val CONNECT_TIMEOUT = 2500
    private const val READ_TIMEOUT = 15000
    private const val PAGE = 1500
    private const val MAX_ROUNDS = 8

    /** 最近一次同步的结论，供界面显示 */
    @Volatile
    var lastMessage: String = ""
        private set

    /**
     * 后台同步。有结果（新增了片子，或确实连上了但失败）才回调，
     * 连不上就直接静默 —— 绝大多数时间用户在外面，不该被打扰。
     */
    fun syncAsync(onDone: (String) -> Unit) {
        Thread {
            val msg = try {
                doSync()
            } catch (e: Exception) {
                Log.w(TAG, "同步异常: ${e.message}")
                ""
            }
            lastMessage = msg
            if (msg.isNotEmpty()) {
                Log.i(TAG, msg)
                onDone(msg)
            }
        }.start()
    }

    private fun doSync(): String {
        if (!LocalStore.isReady()) return ""
        var after = LocalStore.maxVodTime()
        if (after.isBlank()) return ""

        var added = 0
        var rounds = 0
        var touched = false

        while (rounds < MAX_ROUNDS) {
            val url = "$LAN_BASE/api/app/delta?after=${enc(after)}&limit=$PAGE"
            val body = httpGet(url)
            if (body == null) {
                // 连不上 = 不在家里网络，静默退出
                return if (added > 0) "已更新 $added 部新片" else ""
            }
            touched = true
            val o = JSONObject(body)
            val list = o.optJSONArray("list") ?: break
            if (list.length() == 0) break

            added += LocalStore.mergeEntries(list)
            val next = o.optString("max_time", "")
            if (next.isBlank() || next == after) break   // 水位没推进，防死循环
            after = next
            rounds++
            if (list.length() < PAGE) break
        }

        return when {
            added > 0 -> "已更新 $added 部新片"
            touched -> "片库已是最新"
            else -> ""
        }
    }

    private fun httpGet(urlStr: String): String? {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
                connectTimeout = CONNECT_TIMEOUT
                readTimeout = READ_TIMEOUT
                requestMethod = "GET"
                setRequestProperty("Accept", "application/json")
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                Log.w(TAG, "HTTP $code $urlStr")
                return null
            }
            conn.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
        } catch (e: Exception) {
            // 不在局域网时这里是常态（连接超时 / 拒绝），不打印堆栈
            Log.d(TAG, "不可达: ${e.javaClass.simpleName}")
            null
        } finally {
            try {
                conn?.disconnect()
            } catch (e: Exception) {
            }
        }
    }

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")
}
