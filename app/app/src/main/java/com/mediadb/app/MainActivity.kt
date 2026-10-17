package com.mediadb.app

import android.annotation.SuppressLint
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView

/**
 * 主界面：WebView 浏览页 + 原生 ExoPlayer 播放。
 *
 * 分工
 * ----
 * 浏览、筛选、选集：走 8091 浏览页（已有 19.7 万条索引与封面墙）
 * 播放：原生 ExoPlayer（HLS 硬解、后台、锁屏控制、画中画——WebView 给不了）
 *
 * 桥接
 * ----
 * 浏览页点集数时调 window.MediaDBBridge.play(url, title, index, total)，
 * 经 @JavascriptInterface 回到这里起播。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var root: FrameLayout
    private lateinit var web: WebView
    /** WebView 的宿主容器：系统栏避让做在它身上（WebView 自己会消费 insets） */
    private lateinit var webHost: FrameLayout
    private var playerView: PlayerView? = null
    private var player: ExoPlayer? = null
    private lateinit var reporter: ProgressReporter

    /**
     * 当前生效的服务地址。默认内网；内网连不上时自动切到家里宽带的公网 IPv6，
     * 因为 NAS 没有公网 IPv4，出门只能走 IPv6。
     */
    private var activeBase: String = BuildConfig.WEB_BASE_LAN

    /** 独立版标志：页面与数据都在 APK 内，不存在服务器地址概念 */
    private val isLocalMode = true

    /** 是否已经尝试过公网地址，避免反复切换（本地模式下恒为 false） */
    private var triedWan = false

    private val handler = Handler(Looper.getMainLooper())
    private val progressTick = object : Runnable {
        override fun run() {
            reportProgress()
            handler.postDelayed(this, 15_000)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        root = FrameLayout(this)
        setContentView(root)



        relaxTls()
        reporter = ProgressReporter()

        // ---- 浏览 WebView ----
        // 组装层级：root(全屏) → webHost(避让系统栏) → web(浏览页)
        // 播放器直接挂在 root 上，保持全屏不受系统栏内边距影响。
        webHost = FrameLayout(this)
        root.addView(webHost, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT))

        web = WebView(this)
        webHost.addView(web, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT))

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            // 强制每次回源：避免 Service Worker / HTTP 缓存导致加载到旧页面
            // （本次踩坑：页面更新后 APK 仍拿到有语法错误的旧版本）
            cacheMode = WebSettings.LOAD_NO_CACHE
            // 内网 HTTP 资源与自签证书需放开
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            userAgentString = userAgentString + " MediaDB/1.0"
            // 本地独立版：页面与片库都打包在 APK 内，用 file:// 加载。
            // 必须放开 file 源的跨域限制，否则页面对图床、视频 CDN 的
            // 请求会被当作跨域拦掉（这些资源本来就在公网，不涉及本地文件）。
            allowFileAccess = true
            allowContentAccess = true
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = true
            @Suppress("DEPRECATION")
            allowUniversalAccessFromFileURLs = true
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            // 内网自签证书站点，关闭安全浏览提示（Kotlin 里是方法而非属性）
            web.settings.setSafeBrowsingEnabled(false)
        }
        WebView.setWebContentsDebuggingEnabled(true)

        web.webViewClient = object : WebViewClient() {
            override fun onReceivedSslError(
                view: WebView, handler: SslErrorHandler, error: SslError
            ) {
                // 自签证书（NAS 本地生成），局域网内直接放行
                handler.proceed()
            }

            /**
             * 主框架加载失败时：先自动切公网 IPv6，再失败才让用户改地址。
             * 起因：NAS 无公网 IPv4，出门只能走 IPv6，而 APK 里地址是写死的。
             */
            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError
            ) {
                // 本地独立版：页面来自 assets，不存在「连不上服务器」的问题。
                // 主框架报错说明 APK 资源损坏，交给日志排查，不再切公网地址。
                if (isLocalMode) {
                    if (request.isForMainFrame) {
                        Log.e("MediaDB", "本地页面加载失败: ${error.description}")
                    }
                    return
                }
                if (!request.isForMainFrame) return
                if (!triedWan && activeBase == BuildConfig.WEB_BASE_LAN) {
                    triedWan = true
                    activeBase = BuildConfig.WEB_BASE_WAN
                    reporter.baseUrl = BuildConfig.WEB_BASE_WAN
                    Log.i("MediaDB", "内网不可达，改用公网: $activeBase")
                    loadApp()
                } else {
                    showServerDialog()
                }
            }
        }

        web.addJavascriptInterface(Bridge(), "MediaDBNative")
        setupBackHandler()

        // 状态栏/刘海避让：Android 15+ 强制 edge-to-edge，
        // 内容会顶到状态栏下面（实测标题被遮挡）。
        // 用 WindowInsets 监听（比读 status_bar_height 可靠，
        // 且能同时覆盖刘海、导航栏、以及旋转后的变化）。
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(webHost) { v, insets ->
            val bars = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars()
                    or androidx.core.view.WindowInsetsCompat.Type.displayCutout()
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        prepareLocalThenLoad()
    }

    /**
     * 启动时先花 1.5 秒探测内网，再决定加载哪个地址。
     *
     * 为什么不直接 loadUrl 内网再靠 onReceivedError 回退：
     * WebView 的连接超时较长（实测可达 30 秒），在外网时会白等很久。
     * 先做一次轻量探测（请求 /config），切地址是即时的。
     */
    private fun pickAndLoad() {
        Thread {
            val lanOk = quickProbe(BuildConfig.WEB_BASE_LAN)
            runOnUiThread {
                if (lanOk) {
                    activeBase = BuildConfig.WEB_BASE_LAN
                    reporter.baseUrl = BuildConfig.WEB_BASE_HTTP
                    triedWan = false
                } else if (!triedWan && BuildConfig.WEB_BASE_WAN.isNotEmpty()) {
                    activeBase = BuildConfig.WEB_BASE_WAN
                    reporter.baseUrl = BuildConfig.WEB_BASE_WAN
                    triedWan = true
                    Log.i("MediaDB", "内网不可达，改用公网: $activeBase")
                }
                loadApp()
            }
        }.start()
    }

    /** 轻量可达性探测；自签证书已在 relaxTls() 里放开 */
    private fun quickProbe(base: String): Boolean {
        return try {
            val conn = java.net.URL(base.trimEnd('/') + "/config")
                .openConnection() as java.net.HttpURLConnection
            conn.connectTimeout = 1500
            conn.readTimeout = 1500
            conn.requestMethod = "GET"
            val code = conn.responseCode
            conn.disconnect()
            code in 200..399
        } catch (e: Exception) {
            false
        }
    }

    /** 自签证书会让 HttpsURLConnection 直接抛异常，探测前先放开 */
    private fun relaxTls() {
        try {
            val ctx = javax.net.ssl.SSLContext.getInstance("TLS")
            val tm = object : javax.net.ssl.X509TrustManager {
                override fun checkClientTrusted(
                    chain: Array<java.security.cert.X509Certificate>, authType: String
                ) { }

                override fun checkServerTrusted(
                    chain: Array<java.security.cert.X509Certificate>, authType: String
                ) { }

                override fun getAcceptedIssuers(): Array<java.security.cert.X509Certificate> =
                    arrayOf()
            }
            ctx.init(null, arrayOf<javax.net.ssl.TrustManager>(tm), java.security.SecureRandom())
            javax.net.ssl.HttpsURLConnection.setDefaultSSLSocketFactory(ctx.socketFactory)
            javax.net.ssl.HttpsURLConnection.setDefaultHostnameVerifier { _, _ -> true }
        } catch (e: Exception) {
            Log.w("MediaDB", "TLS relax failed: ${e.message}")
        }
    }

    /** 加载应用入口。带上 nosw=1 让服务端不注册 Service Worker */
    private fun loadApp() {
        // 页面与索引都在 APK 内。视频流由 ExoPlayer 直连 CDN，
        // 封面由 WebView 直连图床，全程不经过家里的 NAS。
        web.loadUrl("file:///android_asset/www/index.html")
    }

    /**
     * 独立版启动：解压内置片库 → 加载页面。
     *
     * 不再探测内网/公网：这个 App 不连任何服务器。
     * 索引 19.7 万条（压缩 25MB / 解压 132MB），首次启动需几秒，
     * 期间显示进度页；之后每次启动直接打开。
     */
    private fun prepareLocalThenLoad() {
        showLoading()
        Thread {
            if (LocalStore.isReady()) {
                runOnUiThread { loadApp() }
                return@Thread
            }
            val t0 = System.currentTimeMillis()
            try {
                LocalStore.init(applicationContext)
                Log.i("MediaDB", "片库就绪 ${System.currentTimeMillis() - t0}ms " +
                        LocalStore.stats())
                runOnUiThread { loadApp() }
                // 页面起来后再补数据，避免和首屏渲染抢 IO。
                // 连不上 NAS（在外面）时 2.5 秒超时后静默跳过，不影响使用。
                SyncManager.syncAsync { msg ->
                    runOnUiThread {
                        android.widget.Toast.makeText(
                            this, msg, android.widget.Toast.LENGTH_SHORT
                        ).show()
                        if (msg.startsWith("已更新")) {
                            // 有新片，刷新列表让它们立刻出现
                            web.evaluateJavascript("try{go(1)}catch(e){}", null)
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e("MediaDB", "片库初始化失败", e)
                runOnUiThread { showLoadError(e.message ?: e.toString()) }
            }
        }.start()
    }

    /** 首次解压时的等待页 */
    private fun showLoading() {
        val html = """<!doctype html><meta charset="utf-8">
<style>
html,body{height:100%;margin:0;background:#0f1115;color:#e6e9ef;
  font-family:-apple-system,system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
  display:flex;align-items:center;justify-content:center}
.box{text-align:center}
.t{font-size:17px;letter-spacing:.5px}
.s{font-size:13px;color:#8b93a7;margin-top:10px}
.bar{width:180px;height:3px;background:#222835;border-radius:2px;margin:18px auto 0;overflow:hidden}
.bar i{display:block;width:40%;height:100%;background:#4c8dff;border-radius:2px;
  animation:mv 1.1s ease-in-out infinite}
@keyframes mv{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}
</style>
<div class=box><div class=t>正在准备片库</div>
<div class=s>首次启动需要解压 19.7 万条索引，请稍候</div>
<div class=bar><i></i></div></div>"""
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
    }

    private fun showLoadError(msg: String) {
        val html = """<!doctype html><meta charset="utf-8">
<style>
html,body{height:100%;margin:0;background:#0f1115;color:#e6e9ef;
  font-family:-apple-system,system-ui,"PingFang SC",sans-serif;
  display:flex;align-items:center;justify-content:center;padding:24px}
.box{max-width:340px;text-align:center}
.t{font-size:16px;color:#f09595}
.s{font-size:12px;color:#8b93a7;margin-top:12px;line-height:1.7;word-break:break-all}
</style>
<div class=box><div class=t>片库准备失败</div>
<div class=s>${msg}</div></div>"""
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
    }

    /** 两个地址都连不上时，让用户手动填（比如 IPv6 前缀变了） */
    private fun showServerDialog() {
        val prefs = getSharedPreferences("mediadb", MODE_PRIVATE)
        val input = android.widget.EditText(this).apply {
            setText(prefs.getString("manual_base", activeBase) ?: activeBase)
            setSingleLine(true)
            setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 14f)
        }
        val box = FrameLayout(this)
        val lp = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        )
        lp.setMargins(48, 24, 48, 0)
        box.addView(input, lp)

        android.app.AlertDialog.Builder(this)
            .setTitle("连不上服务器")
            .setMessage(
                "在家会自动走内网；在外面需要填家里的公网地址，形如：\n" +
                    "https://[2409:xxxx:...]:8443\n" +
                    "（NAS 没有公网 IPv4，只能走 IPv6）"
            )
            .setView(box)
            .setPositiveButton("保存并重连") { _, _ ->
                val v = input.text.toString().trim().trimEnd('/')
                if (v.isNotEmpty()) {
                    activeBase = v
                    reporter.baseUrl = v
                    triedWan = true
                    prefs.edit().putString("manual_base", v).apply()
                    loadApp()
                }
            }
            .setNeutralButton("恢复默认") { _, _ ->
                prefs.edit().remove("manual_base").apply()
                activeBase = BuildConfig.WEB_BASE_LAN
                reporter.baseUrl = BuildConfig.WEB_BASE_HTTP
                triedWan = false
                loadApp()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /** 注入浏览页的原生桥 */
    inner class Bridge {
        /** 浏览页请求原生起播 */
        @JavascriptInterface
        fun play(url: String, title: String, index: Int, total: Int, id: String) {
            runOnUiThread { startNativePlayback(url, title, index, total, id) }
        }

        // ── 本地数据接口：网页通过 fetch 拦截器调用，等价于原来的 HTTP 接口 ──

        @JavascriptInterface
        fun types(): String = LocalStore.types()

        @JavascriptInterface
        fun browse(typeId: Int, year: String, order: String, limit: Int, page: Int): String =
            LocalStore.browse(typeId, year, order, limit, page)

        @JavascriptInterface
        fun search(wd: String, limit: Int): String = LocalStore.search(wd, limit)

        @JavascriptInterface
        fun detail(ids: String): String = LocalStore.detail(ids)

        @JavascriptInterface
        fun getProgress(ids: String): String = LocalStore.getProgress(ids)

        @JavascriptInterface
        fun saveProgress(json: String) = LocalStore.saveProgress(json)

        @JavascriptInterface
        fun stats(): String = LocalStore.stats()

        // ── 在线音乐（酷我移动端接口，无需登录）──
        // 网络请求必须放原生侧：file:// 页面里 fetch 跨域会被拦，
        // 且 http 接口在 WebView 里也不稳。结果通过 __musCb 异步回传。

        @JavascriptInterface
        fun musicSearch(kw: String, page: Int, cb: String) {
            Thread {
                val r = try {
                    MusicApi.search(kw, page, 40)
                } catch (e: Exception) {
                    org.json.JSONObject().put("error", e.message ?: "搜索失败").toString()
                }
                runOnUiThread {
                    web.evaluateJavascript(
                        "window.__musCb && window.__musCb('$cb', $r)", null)
                }
            }.start()
        }

        @JavascriptInterface
        fun musicUrl(rid: String, br: String, cb: String) {
            Thread {
                val r = try {
                    MusicApi.playUrl(rid, br)
                } catch (e: Exception) {
                    org.json.JSONObject().put("error", e.message ?: "取地址失败").toString()
                }
                runOnUiThread {
                    web.evaluateJavascript(
                        "window.__musCb && window.__musCb('$cb', $r)", null)
                }
            }.start()
        }

        @JavascriptInterface
        fun log(msg: String) {
            Log.d("MediaDB", msg)
        }
    }

    private fun startNativePlayback(
        url: String, title: String, index: Int, total: Int, id: String
    ) {
        if (url.isBlank()) return
        reporter.currentId = id
        // 保存集数，供进度上报使用（此前未保存，导致进度恒为第1集）
        currentIndex = index
        currentTotal = total
        val label = "$title（第 ${index + 1}/$total 集）"
        ensurePlayer()
        val pv = playerView ?: return

        if (pv.parent == null) {
            root.addView(pv, FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT))
            pv.useController = true
            pv.controllerShowTimeoutMs = 3500
        }
        pv.visibility = View.VISIBLE
        webHost.visibility = View.GONE

        // ⚠️ 此前这里把高度误设为 0（想做「半屏播放」），
        // 结果视频被压成 0 高度 → 只有声音没有画面。
        // 播放时铺满窗口，保持宽高比。
        pv.resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
        pv.layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT)
        pv.setPadding(0, 0, 0, 0)
        pv.keepScreenOn = true          // 看剧时不息屏

        player?.let { p ->
            p.setMediaItem(MediaItem.fromUri(url))
            p.prepare()
            p.playWhenReady = true
        }
        Log.i("MediaDB", "开始播放: $label  url=$url")

        handler.removeCallbacks(progressTick)
        handler.postDelayed(progressTick, 3_000)
    }

    /** 退出播放，回到浏览页（不清空浏览状态） */
    private fun exitPlayback() {
        val p = player ?: return
        reportProgress()
        p.pause()
        playerView?.visibility = View.GONE
        webHost.visibility = View.VISIBLE
        requestedOrientation =
            android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
    }

    /**
     * 返回键：播放中先退回列表，否则交给系统。
     * 用 OnBackPressedDispatcher（Android 13+ 起 override onBackPressed 不再生效，
     * 实测直接 override 会被忽略、按返回直接退出应用）。
     */
    private fun setupBackHandler() {
        onBackPressedDispatcher.addCallback(this,
            object : androidx.activity.OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    val pv = playerView
                    if (pv != null && pv.visibility == View.VISIBLE) {
                        exitPlayback()
                    } else {
                        // 列表页：交回系统处理（正常退出）
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                        isEnabled = true
                    }
                }
            })
    }

    private fun ensurePlayer() {
        if (player != null) return
        // RenderersFactory：开启解码器回退。
        // 部分机型（实测 OnePlus/ColorOS）硬解 AAC 会抛
        // MediaCodec$CodecException(-2147483648) 导致整条播放失败；
        // 开启回退后会自动切到软件解码，避免黑屏。
        val renderers = androidx.media3.exoplayer.DefaultRenderersFactory(this)
            .setEnableDecoderFallback(true)
            .setExtensionRendererMode(
                androidx.media3.exoplayer.DefaultRenderersFactory.EXTENSION_RENDERER_MODE_PREFER)

        // 缓冲参数：第三方源响应慢且波动大，适当放宽以提升起播成功率。
        // 注意：Media3 的 ExoPlayer.Builder 没有 setBufferDurationsMs
        // （那是旧版 ExoPlayer 2.x 的 API），须经 DefaultLoadControl 配置。
        val loadControl = androidx.media3.exoplayer.DefaultLoadControl.Builder()
            .setBufferDurationsMs(
                5000,     // 最小缓冲 5s
                60000,    // 最大缓冲 60s
                2500,     // 起播前缓冲 2.5s
                5000      // 重缓冲后起播 5s
            )
            .build()

        player = ExoPlayer.Builder(this, renderers)
            .setLoadControl(loadControl)
            .build().apply {
            addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) {
                    // 播完自动进入下一集（由浏览页提供地址）
                    if (state == Player.STATE_ENDED) {
                        web.evaluateJavascript(
                            "window.MediaDBBridge && window.MediaDBBridge.next(window.__curEp)", null)
                    }
                }

                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    if (!isPlaying) reportProgress()   // 暂停即上报
                }

                override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                    // 第三方采集源的流质量参差：失败时给出可见提示，
                    // 而不是只留一块黑屏。同时把详情写进日志便于排查。
                    Log.e("MediaDB", "播放失败: ${error.errorCodeName} ${error.message}", error)
                    runOnUiThread {
                        android.widget.Toast.makeText(
                            this@MainActivity,
                            "播放失败（${error.errorCodeName}），返回列表重试或换一集",
                            android.widget.Toast.LENGTH_LONG
                        ).show()
                    }
                }
            })

            setHandleAudioBecomingNoisy(true)       // 拔耳机自动暂停
        }
        playerView = PlayerView(this).apply {
            this.player = this@MainActivity.player
            useController = true
        }
    }

    private fun reportProgress() {
        val p = player ?: return
        val idx = currentEpisodeIndex()
        // 本地版：进度写 App 自己的 SQLite，不再上报 NAS
        reporter.report(idx, currentTotal, p.currentPosition / 1000, p.duration / 1000)
    }

    private var currentIndex = 0
    private var currentTotal = 0

    private fun currentEpisodeIndex(): Int = currentIndex

    override fun onPause() {
        super.onPause()
        reportProgress()
        handler.removeCallbacks(progressTick)
        if (isFinishing) {
            player?.release()
            player = null
        }
    }

    override fun onDestroy() {
        handler.removeCallbacks(progressTick)
        reportProgress()
        player?.release()
        player = null
        web.destroy()
        super.onDestroy()
    }
}
