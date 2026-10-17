> 本文档描述如何部署 SQMusic（搜歌下载入库）+ Navidrome（本地曲库播放），
> 与 App 的「在线音乐」互为补充：在线听用 App，想存本地走这套。

# 音乐服务（SQMusic + Navidrome）

家庭音乐方案由两个服务组成，各司其职：

| 服务 | 作用 | 地址 |
|---|---|---|
| **SQMusic** | 搜歌、选码率、下载到本地 | http://127.0.0.1:8996 |
| **Navidrome** | 曲库管理 + 播放（电脑网页 / 手机 App） | http://192.168.1.100:4534 |

```
SQMusic 搜歌 → 下载 FLAC/MP3 → 落盘 /vol1/1000/音乐 → Navidrome 自动扫描 → 手机/电脑播放
```

## 为什么是两个服务

Navidrome 只播放**你已有的文件**，它不提供音源。SQMusic 负责把歌拿下来。
所以视频那套「一个 LibreTV 全搞定」在音乐这里不成立 —— 音乐没有 maccms 那种公开采集标准。

## 在线音乐：直接听全网（2026-10-17）

打开 `/music` 默认就是「在线」：

- **首页**：热门歌手（华语男/女、港台、欧美、日韩，共 50 位）+ 12 个主题
- **点歌手** → 全网搜索结果（如「周杰伦」3600 首），底部可「加载更多」
- **点歌** → 直接播放，不下载、不占硬盘
- 顶部搜索框可搜任意歌名 / 歌手 / 专辑

```
web.py(8091/8443) --JWT--> SQMusic(8996) --> 酷我
```

SQMusic 的后端本身就是音乐资源池（各平台的签名与鉴权它已处理好），
web.py 只做一层代理，不复刻平台协议。
播放拿到的是酷我 CDN 直链，实测匿名可访问且支持 Range，浏览器 `<audio>` 直接播。

接口（`indexer/music_online.py`）：

| 路径 | 说明 |
|---|---|
| `/api/online/search?kw=&page=` | 搜索，每页 30 条 |
| `/api/online/url?id=` | 解析播放直链 |
| `/api/online/tips?kw=` | 搜索联想 |
| `/api/online/lyric?id=` | 歌词（LRC） |
| `/api/online/img?u=` | 封面代理（只放行 kuwo.cn） |
| `/api/online/download?id=&br=` | 下载到本地（默认优先 FLAC） |

**下载**：每条结果右侧有 `↓` 按钮，点了交给 SQMusic 下载
（上游是 `POST /api/download/downloadSong`，请求体同样要补 `brType` 与 `downloadFormat`）。
它会写好标签 / 封面 / 歌词并按 `歌手/专辑/歌名 - 歌手.flac` 归档，
落进 `/vol1/1000/音乐`，随后出现在「本地」tab 并被 Navidrome 收录。
下载是异步的，按钮上的 ✓ 只代表"已加入下载"，不是"已完成"。

**音质可选**：搜索框右侧有下拉（无损 / 320k / 128k），选择记在 localStorage，
刷新后仍生效。每条结果右侧显示的码率就是按当前选择实际会下的档位；
若该曲没有所选档位，自动降级（无损 → 320 → 128，降不动再往上兜底）。

实测参数（ffprobe）：

| 档位 | 编码 | 采样率 | 位深 | 码率 | 约 5 分钟体积 |
|---|---|---|---|---|---|
| 无损 | FLAC | 44.1 kHz | 16 bit | ~961 kbps | 35.9 MB |
| 320k | MP3 | 44.1 kHz | — | 320 kbps | 9.1 MB |
| 128k | MP3 | 44.1 kHz | — | 128 kbps | ~3.6 MB |

注意这是 **CD 级无损（16bit / 44.1kHz）**，不是 Hi-Res（24bit / 96kHz）——
酷我最高只提供到这一档，选择器里也没有更高选项。

**三条实测结论**：

1. `getDownloadUrl` 的请求体除搜索结果外**必须补 `brType` 与 `downloadFormat`**，
   否则返回 `code:500`。这是逐字段比对浏览器真实请求才发现的。
2. 认证是 `POST /api/config/login` → `{tokenName:"sqmusic", tokenValue:"JWT"}`，
   之后每个请求带 header **`sqmusic: <token>`**。有效期 7 天，这里缓存 6 天。
3. SQMusic **没有榜单接口**（只有单曲 / 专辑 / 歌手三种搜索）。所以首页入口
   用的是预设歌手与主题名单，点进去等价于一次搜索 —— 不存在"排行榜"这类数据源。

## 本地曲库：第二个 tab（按需下载的歌）

```
http://127.0.0.1:8091/music
https://192.168.1.100:8443/music      ← 手机建议用这个
```

影视库右上角也有「音乐」入口，两边互通。

**为什么不接 Navidrome 的 API**：Navidrome 的密码是 **AES 加密落库**（不是 bcrypt 等标准 hash），
无法手工生成一个已知密码的账号给页面用。而 SQMusic 的目录结构
`歌手/专辑/歌名 - 歌手.flac` 本身已经携带元数据，封面也放在目录里，
所以直接扫 `/vol1/1000/音乐` 即可 —— 零第三方依赖（容器是 python:3.13-alpine 纯净环境，
装不了也不需要 mutagen）。

功能：

- **封面墙**：按专辑聚合，封面取目录内的 `cover.jpg` / `folder.jpg` / `album.jpg`，缺了回退到歌手目录
- **点专辑** → 曲目列表 → 点曲目播放
- **底部播放条**：进度可拖、上一首/下一首、播完自动下一首
- **顶部筛选框**：本地过滤专辑名与歌手，不发请求
- **无封面**：用色相取自专辑名的渐变占位，不会出现破图

接口（实现见 `indexer/music.py`）：

| 路径 | 说明 |
|---|---|
| `/music` | 页面 |
| `/api/music/albums` | 专辑列表（15 秒缓存） |
| `/api/music/album?id=` | 该专辑曲目 |
| `/api/music/cover?p=` | 封面图 |
| `/api/music/stream?p=` | 音频流，支持 Range，可拖动进度 |

下载新歌后最多等 15 秒再刷新页面就会出现。

容器挂载：`/vol1/1000/音乐:/music:ro`（只读，页面不会改动你的文件）。

## 登录信息

**SQMusic**：`admin` / `admin`（首次登录后请到「设置」改密码）
**Navidrome**：`admin` / 你当初注册时设的密码（我无法读取，已加密存储）

## 容器与配置

| 容器 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| sqmusic_mysql | mysql:5.7 | 内部 | 数据库，密码 `（你在 compose 里设置的密码）` |
| sqmusic_main | aliyun/sqdockler/simple_sq_music_plus:latest | 内部 8099 | Java 后端，1.57GB |
| sqmusic_web | aliyun/sqdockler/simple_sq_music_plus_web:latest | **8996** | 前端 |
| navidrome | deluan/navidrome:latest | **4534** | 原有服务，未改动 |

配置文件：`/vol1/docker/sqmusic/docker-compose.yml`
数据目录：`/vol1/docker/sqmusic/mysql_data`
音乐目录：`/vol1/1000/音乐`（与 Navidrome 共用）

```bash
cd /vol1/docker/sqmusic
DOCKER_BUILDKIT=0 docker compose up -d
```

## 下载后的目录结构

```
/vol1/1000/音乐/周杰伦/叶惠美/晴天 - 周杰伦.flac
/vol1/1000/音乐/周杰伦/叶惠美/晴天 - 周杰伦.lrc
/vol1/1000/音乐/周杰伦/魔杰座/稻香 - 周杰伦.mp3
```

`歌手/专辑/歌名 - 歌手` 是 Emby / Jellyfin / Subsonic 通用规范，标签、封面、歌词自动写好。

## 坑一：文件权限是 000

SQMusic 生成的文件和目录权限是 **000**（`----------`），且不带 ACL。

Navidrome 容器以 root 运行，能读；但 **SMB 共享或其他客户端读不到**。

已加定时任务自动修复：

```bash
# /vol1/docker/sqmusic/fix-perms.sh
find /vol1/1000/音乐 -mmin -20 -type f -exec chmod a+r {} + 2>/dev/null
find /vol1/1000/音乐 -mmin -20 -type d -exec chmod a+rx {} + 2>/dev/null
```

```
# crontab -l
* * * * * /vol1/docker/sqmusic/fix-perms.sh
```

只处理最近 20 分钟变动的条目，曲库变大后也不会有性能问题。

## 坑二：镜像拉取极慢

阿里云仓库拉到 0.1 MB/s，1.57GB 的 main 镜像花了约 40 分钟。
`ghcr.io/59799517/simple_sq_music_plus` 快约 6 倍（0.6 MB/s），可作为备选源。

**不要并发 pull 同一个镜像** —— 多个进程会互相抢带宽、重复下载。

## 坑三：Navidrome 增量扫描不发现新目录

`fullScan=false` 的初始扫描只处理已知文件夹，新建的子目录会被跳过。

解法：动一下目录 mtime 触发 watcher 的 selective scan

```bash
touch /vol1/1000/音乐 /vol1/1000/音乐/歌手目录 /vol1/1000/音乐/歌手目录/专辑目录
```

正常情况下不用手动做 —— SQMusic 建目录时 watcher 就会捕获，且 `fix-perms.sh` 的 chmod 也会更新 ctime。

## 音源现状

当前可用：**某我**（酷我）。
日志里的 `QQvip未开启插件 / 酷狗未开启插件` 是正常提示，不影响酷我。

支持码率：`KW_FLAC_2000`（无损）/ `KW_MP3_320` / `KW_MP3_128`。
逐首点码率按钮即下载；同一行可重复点不同码率。

## 手机客户端

Navidrome 兼容 Subsonic API，任意 Subsonic 客户端都能连：

| App | 平台 | 说明 |
|---|---|---|
| **音流** | Android / iOS | 中文界面，支持内网/外网双线路、离线缓存、滚动歌词 |
| Substreamer | Android / iOS | 轻量稳定 |
| Symfonium | Android | 功能最全（付费） |
| Feishin | 桌面端 | 界面精致，适合电脑 |

连接信息：服务器 `http://192.168.1.100:4534`，账号 `admin`，密码同 Navidrome。

> 局域网内直接可用。NAS 无公网 IP，外网访问需要额外组网（如已有的 StarVPN）。

## 验证记录（2026-10-17）

| 项 | 结果 |
|---|---|
| SQMusic 搜索「晴天」 | 11 条结果，含 FLAC 无损 |
| 下载 FLAC | 53MB，标签/封面/歌词齐全 |
| 下载 MP3-320 | 9MB |
| 权限自动修复 | cron 生效，`-r-xr-xr-x` |
| Navidrome 自动扫描 | watcher 捕获，无需手动触发 |
| 曲库统计 | 5 首 → **7 首** |
| 音乐展示页 `/music` | 3 张专辑 / 7 首，封面墙渲染正常 |
| 页面播放 | FLAC 时长 4:30，进度正常推进，JS 零错误 |
| 音频 Range | 206 Partial Content，`Content-Range: bytes 0-1023/55503711` |
| 筛选框 | 输入「魔杰」→ 1 张；清空 → 3 张 |
| 目录穿越防护 | `p=../../../etc/passwd` → 404 |
| 在线搜索「周杰伦」 | 3600 首，首屏 30 条 |
| 在线播放 | 酷我 FLAC 直链，4:30 进度正常推进 |
| 双 tab | 在线 ↔ 本地切换，播放不中断 |
| 在线下载 | 点 ↓ → 35MB FLAC + LRC 落盘 `周杰伦/七里香/`，按钮变 ✓ 且不误触发播放 |
| 音质切换 | 无损/320k/128k 切换后每行码率标签同步变化；刷新后选择保留 |
| 音质实测 | FLAC 961kbps/16bit、MP3 320kbps（ffprobe 确认） |
