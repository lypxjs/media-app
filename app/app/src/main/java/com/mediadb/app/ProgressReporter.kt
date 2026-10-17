package com.mediadb.app

import android.util.Log
import org.json.JSONObject

/**
 * 观看进度：写入 App 自带的 SQLite（`progress` 表）。
 *
 * 独立版不再上报到 NAS —— 进度属于本机数据，多设备之间本来就该各自独立
 * （在客厅电视看到第 5 集，不代表地铁上手机也要从第 5 集开始）。
 *
 * 上报时机：每 15 秒 + 退出播放时各一次，见 MainActivity 的 progressTick。
 */
class ProgressReporter {

    /** 当前播放的片源标识（形如 "1:72152"），由浏览页在起播时传入 */
    @Volatile
    var currentId: String? = null

    /**
     * 兼容保留：早期版本用它指向 NAS 地址，独立版已不再上报。
     * MainActivity 里仍有几处赋值（切换地址的旧分支），保留此字段免得改面过大；
     * 本地模式下那些分支不会执行。
     */
    @Volatile
    var baseUrl: String = ""

    fun report(episodeIndex: Int, total: Int, positionSec: Long, durationSec: Long) {
        val id = currentId ?: return
        val body = JSONObject().apply {
            put("id", id)
            put("episode", episodeIndex)
            put("episodes", total)
            // 只有播完最后一集才算「已看完」；否则记成"看到第几集"
            put("finished", total > 0 && episodeIndex + 1 >= total)
            put("position", positionSec)
            put("duration", durationSec)
        }.toString()

        try {
            LocalStore.saveProgress(body)
        } catch (e: Exception) {
            Log.w("ProgressReporter", "保存进度失败: ${e.message}")
        }
    }
}
