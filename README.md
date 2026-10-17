<div align="center">

# Media App · 影视库

**一个完全独立的 Android 应用：离线影视索引库 + 在线音乐，不依赖任何自建服务器。**

19.7 万条影视索引打包进 APK · ExoPlayer 直连播放 · 酷我音源在线听 · 回家自动补新片

`Kotlin` · `Media3 / ExoPlayer` · `WebView` · `SQLite` · `Python`

</div>

---

## ⚠️ 免责声明

- 本项目**不存储、不上传、不分发任何影视或音乐文件**。
- 视频数据来自公开的 maccms 采集站接口（`/api.php/provide/vod`），音乐来自第三方客户端公开接口，全部内容的版权归原权利方所有。
- 项目仅用于**个人学习 Android 开发与数据架构**，请于 24 小时内删除，并支持正版。
- 请勿用于商业用途。使用本项目产生的任何问题由使用者自行承担。

## 为什么做这个

家里 NAS 上跑着一个影视索引（约 20 万条），最初的手机客户端需要连回 NAS 才能用。
后来想明白了一件事：**多台设备全都回源到家里这个单点，架构上不成立**——
出门没网不能用，每台手机都要打通回程链路。

于是把架构反过来：

```
NAS = 索引工厂（离线生成精简库，gzip 后 25MB）
  ↓ 打包进 APK（或回家 WiFi 增量同步）
手机本地 SQLite → 翻列表 / 搜索（零延迟、零依赖）
  ↓ 点播放
ExoPlayer 直连内容 CDN（不经过家里）
```

每台设备各自持有一份索引，互不影响；NAS 只在"补新片"时才用得上。

## 功能

**视频**
- 19.7 万条索引内置，首启解压约 0.5s，之后秒开
- 分类 / 年份 / 排序 / 搜索（采集站不支持服务端筛选，全靠本地库实现）
- ExoPlayer 硬解播放 m3u8，支持倍速 / 快捷键 / 画中画
- 观看进度记录在本地，多设备各自独立
- 回家连 WiFi 自动增量同步新片，在外面 2.5s 超时静默跳过

**音乐**
- 酷我移动端接口（无需登录），搜歌、FLAC 无损直链、在线播放
- 取不到无损自动降级 320k → 128k
- 歌手快捷入口、底部播放条、自动连播

## 构建

要求：JDK 17+、Android SDK、Gradle 8.7（未带 wrapper，需自行安装）

```bash
# 1. 生成本地索引库（见「索引数据」一节，需要先跑一次服务端抓取）
python tools/export_full.py          # 在有 index.db 的机器上执行

# 2. 把产出的 app_index.db.gz 放到 assets
cp /tmp/app_index.db.gz app/app/src/main/assets/library.db.gz

# 3. 生成浏览页（从 server/web.py 提取模板并注入本地数据层）
python tools/make_www.py

# 4. 构建
cd app
gradle assembleDebug
```

同步服务器地址在 `app/build.gradle.kts` 的 `SYNC_HOST`，默认 `http://192.168.1.100:8091`，改成你自己 NAS 的地址。

## 服务端（NAS）

服务端只负责三件事：**抓取索引、生成增量、（可选）代理封面**。

```bash
cd server
docker build -t media-index .
docker run -d --name media-index \
  -p 8091:8090 -p 8090:8091 \
  -v ./data:/data \
  -e WEB_PORT=8090 \
  media-index
```

每天定时增量抓取（Linux crontab）：

```
0 3 * * * docker exec media-index python crawler.py --mode incremental
```

App 侧增量接口：`GET /api/app/delta?after=<vod_time>&limit=1500`

## 索引数据

`tools/export_full.py` 会从服务端的 `index.db` 导出精简库：

| 处理 | 原因 |
|---|---|
| 去掉 `plot`（29MB） | 浏览页不显示简介 |
| 去掉 `actor`（8MB） | 同上 |
| **保留 `episodes`（72MB）** | 采集站详情接口不稳定，播放时无法按 id 回查剧集 |
| 搜索不用倒排表 | 19.7 万条 `LIKE` 全表扫仅百毫秒级，省 10MB+ |

**`episodes` 字段不能省**——没有它，点开一部剧就只能播第一集。

## 目录结构

```
├── app/          Android 工程（Kotlin + WebView + Media3）
│   └── app/src/main/java/com/mediadb/app/
│       ├── MainActivity.kt     WebView 宿主 + ExoPlayer + 本地桥
│       ├── LocalStore.kt       内置片库（SQLite 查询 / 增量合并 / 进度）
│       ├── MusicApi.kt         酷我接口封装
│       └── SyncManager.kt      回家自动同步
├── server/       NAS 端（Python 标准库，无第三方依赖）
│   ├── web.py            浏览页 + 索引 API + 增量接口
│   ├── music.py          网页版音乐页（本地曲库封面墙）
│   ├── music_online.py   网页版在线音乐（代理 SQMusic）
│   ├── crawler.py        采集站抓取（全量 / 增量 / 断点续传）
│   ├── db.py             查询层
│   ├── tls_proxy.py      零依赖 HTTPS 反代
│   └── sqmusic-compose.example.yml  音乐下载入库（可选）
├── docs/         sqmusic.md 下载入库方案部署说明
└── tools/        数据导出、页面生成、验证脚本
```

## 电脑上也能用（网页版）

同一套 `server/` 除了给 App 提供接口，本身就是完整的网页应用：

```
http://<NAS>:8091/        影视封面墙（分类 / 搜索 / 播放）
http://<NAS>:8091/music   音乐页（本地曲库 + 在线搜索）
```

电脑浏览器直接打开即可；手机浏览器打开 `https://<NAS>:8443`（PWA）也是同一套。
App 与网页版共用索引和进度，区别只是 App 把索引带在了身上、出门也能用。

## 音乐下载入库（可选）

App 里的「在线音乐」只播不存。想把歌真正存下来的话，配套一套
SQMusic + Navidrome（见 `docs/sqmusic.md` 与 `server/sqmusic-compose.example.yml`）：

- SQMusic 搜歌 → 按 `歌手/专辑/歌名` 落盘，标签封面歌词自动写好
- Navidrome 自动扫描入库，电脑网页 / 手机 Subsonic 客户端都能放

## 几个踩过的坑（详见代码注释）

- **AGP 会自动解压 assets 里的 `.gz`**：放 `library.db.gz`（26MB），打进 APK 变成 `library.db`（132MB）。代码要兼容两种名字
- **`.dhead` 必须加 `position:relative`**：封面占位层是 `position:absolute;inset:0`，父元素不是定位祖先时会盖住整个弹窗，表现为「封面挂了就点不了集数」
- **WebView 会自己消费 insets**：Android 15+ edge-to-edge 下，系统栏避让要加在 WebView 的外层容器上
- **`let` 声明的变量不会挂到 `window`**：前端混用两种访问方式会拿到 `undefined`

## License

[MIT](LICENSE)
