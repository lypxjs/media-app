"""音乐浏览页（在线 + 本地）。

设计取舍
--------
不依赖 Navidrome 的 API：它的密码用 AES 加密落库（不是 bcrypt 等标准 hash），
无法手工构造访问凭据。而 SQMusic 下载下来的目录结构本身就是
「歌手/专辑/歌名 - 歌手.flac」，元数据已经写在路径里，封面也在目录中，
所以直接扫目录即可，零第三方依赖（容器是 python:3.13-alpine 纯净环境）。

接口
----
GET /music              封面墙页面
GET /api/music/albums   专辑列表
GET /api/music/album    单张专辑的曲目（?id=相对路径）
GET /api/music/cover    封面图（?p=相对路径，无封面时返回 404 由前端画占位）
GET /api/music/stream   音频流（?p=相对路径，支持 Range，浏览器可拖动进度）
"""

import json
import mimetypes
import os
import time
from urllib.parse import parse_qs, quote, urlparse

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")

AUDIO_EXT = {
    ".flac", ".mp3", ".m4a", ".aac", ".ape", ".wav",
    ".ogg", ".opus", ".wma", ".aiff", ".alac",
}
COVER_NAMES = (
    "cover.jpg", "folder.jpg", "album.jpg", "front.jpg", "artist.jpg",
    "cover.jpeg", "folder.jpeg", "cover.png", "folder.png",
)

# 扫描结果缓存：目录数量少时开销可忽略，但每次翻页都扫一遍没必要
_cache = {"at": 0.0, "albums": None}
CACHE_TTL = 15.0


def _safe(rel):
    """把相对路径解析成绝对路径，并确保没有越出 MUSIC_DIR。"""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    full = os.path.realpath(os.path.join(MUSIC_DIR, rel))
    root = os.path.realpath(MUSIC_DIR)
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def _find_cover(folder, fallback_folder=None):
    """在目录里找封面文件，找不到再退到上一级（歌手头像）。"""
    for d in (folder, fallback_folder):
        if not d:
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        lower = {n.lower(): n for n in names}
        for want in COVER_NAMES:
            if want in lower:
                return os.path.join(d, lower[want])
    return None


def _tracks_of(folder):
    out = []
    try:
        entries = os.scandir(folder)
    except OSError:
        return out
    with entries:
        for e in entries:
            if not e.is_file():
                continue
            ext = os.path.splitext(e.name)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            stem = os.path.splitext(e.name)[0]
            # SQMusic 的命名是「歌名 - 歌手」，展示时去掉后缀更干净
            title = stem
            if " - " in stem:
                left, right = stem.rsplit(" - ", 1)
                if left.strip():
                    title = left.strip()
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            out.append({
                "name": e.name,
                "title": title,
                "size": size,
                "path": os.path.relpath(e.path, MUSIC_DIR),
            })
    out.sort(key=lambda x: x["name"])
    return out


def _scan():
    """遍历音乐目录，返回专辑列表。"""
    now = time.time()
    if _cache["albums"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["albums"]

    albums = []
    try:
        top = sorted(os.scandir(MUSIC_DIR), key=lambda e: e.name)
    except OSError:
        top = []

    loose = []
    for artist in top:
        if artist.name.startswith("."):
            continue
        if artist.is_file():
            if os.path.splitext(artist.name)[1].lower() in AUDIO_EXT:
                loose.append(artist)
            continue
        if not artist.is_dir():
            continue

        # 结构一：歌手/专辑/曲目
        subs = sorted(os.scandir(artist.path), key=lambda e: e.name)
        for alb in subs:
            if alb.is_dir():
                tracks = _tracks_of(alb.path)
                if not tracks:
                    continue
                albums.append({
                    "id": os.path.relpath(alb.path, MUSIC_DIR),
                    "artist": artist.name,
                    "album": alb.name,
                    "cover": _cover_rel(alb.path, artist.path),
                    "count": len(tracks),
                })
            elif alb.is_file() and os.path.splitext(alb.name)[1].lower() in AUDIO_EXT:
                # 结构二：歌手/散装曲目 —— 视为该歌手的一张专辑
                loose.append(alb)

    # 根目录散装文件（历史遗留，无目录结构）
    loose_tracks = []
    for f in loose:
        parent = os.path.dirname(f.path)
        artist = "" if parent == MUSIC_DIR else os.path.basename(parent)
        stem = os.path.splitext(f.name)[0]
        title = stem
        if " - " in stem:
            left, right = stem.rsplit(" - ", 1)
            if left.strip():
                title = left.strip()
        try:
            fsize = f.stat().st_size
        except OSError:
            fsize = 0
        loose_tracks.append({
            "name": f.name,
            "title": title,
            "size": fsize,
            "path": os.path.relpath(f.path, MUSIC_DIR),
            "artist": artist,
        })
    if loose_tracks:
        albums.append({
            "id": "__loose__",
            "artist": "未分类",
            "album": "散装曲目",
            "cover": None,
            "count": len(loose_tracks),
        })

    albums.sort(key=lambda a: (a["id"] == "__loose__", a["artist"], a["album"]))
    _cache["albums"] = albums
    _cache["at"] = now
    return albums


def _cover_rel(album_path, artist_path):
    p = _find_cover(album_path, artist_path)
    if not p:
        return None
    try:
        return os.path.relpath(p, MUSIC_DIR)
    except ValueError:
        return None


def _album_tracks(album_id):
    if album_id == "__loose__":
        # 复用扫描结果里的散装曲目
        _scan()
        out = []
        for e in sorted(os.scandir(MUSIC_DIR), key=lambda x: x.name):
            if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXT:
                stem = os.path.splitext(e.name)[0]
                title = stem.rsplit(" - ", 1)[0].strip() if " - " in stem else stem
                out.append({
                    "name": e.name, "title": title,
                    "size": e.stat().st_size,
                    "path": os.path.relpath(e.path, MUSIC_DIR),
                })
        return out
    folder = _safe(album_id)
    if not folder or not os.path.isdir(folder):
        return []
    return _tracks_of(folder)


def invalidate():
    _cache["albums"] = None


# ────────────────────────────── 页面 ──────────────────────────────

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>音乐</title>
<meta name="theme-color" content="#0f1115">
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --chip:#1f232c; --line:#272c37;
  --tx:#e6e9ef; --tx2:#9aa3b2; --tx3:#6b7280; --ac:#4f8cff;
  --bar:76px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
  font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  padding-bottom:calc(var(--bar) + env(safe-area-inset-bottom, 0px))}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:20;background:rgba(15,17,21,.96);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);
  padding:12px calc(16px + env(safe-area-inset-left,0px)) 10px
             calc(16px + env(safe-area-inset-right,0px))}
.hrow{display:flex;align-items:center;gap:12px}
h1{margin:0;font-size:17px;font-weight:600;flex:1}
h1 small{color:var(--tx3);font-weight:400;font-size:12px;margin-left:6px}
.navlink{font-size:13px;color:var(--tx2);padding:6px 12px;
  background:var(--chip);border:1px solid var(--line);border-radius:999px}
.tabs{display:flex;gap:6px;margin-top:10px}
.tab{flex:0 0 auto;padding:6px 16px;font-size:13px;border-radius:999px;
  background:var(--chip);border:1px solid var(--line);color:var(--tx2);cursor:pointer}
.tab.on{background:var(--ac);border-color:var(--ac);color:#fff}
.searchbar{margin-top:10px;display:flex;gap:8px}
.searchbar input{flex:1;min-width:0;padding:9px 12px;background:var(--chip);
  border:1px solid var(--line);border-radius:8px;color:var(--tx);
  font-size:14px;outline:none}
.searchbar input:focus{border-color:var(--ac)}
.searchbar select{flex:0 0 auto;padding:0 8px;background:var(--chip);
  border:1px solid var(--line);border-radius:8px;color:var(--tx2);
  font-size:13px;outline:none;cursor:pointer}
.searchbar[hidden]{display:none}
.wrap{padding:16px calc(16px + env(safe-area-inset-left,0px))
               16px calc(16px + env(safe-area-inset-right,0px))}

.sec{margin:18px 0 10px;font-size:13px;color:var(--tx3)}
.sec:first-child{margin-top:4px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:7px 14px;font-size:13px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line);
  color:var(--tx);cursor:pointer}
.chip:active{background:var(--ac);border-color:var(--ac);color:#fff}

.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}
@media (max-width:520px){.grid{grid-template-columns:repeat(2,1fr);gap:12px}}
.card{cursor:pointer}
.cover{position:relative;width:100%;aspect-ratio:1/1;border-radius:10px;
  overflow:hidden;background:var(--panel);border:1px solid var(--line)}
.cover img{width:100%;height:100%;object-fit:cover;display:block}
.cover .ph{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:26px;font-weight:600;color:#fff;
  letter-spacing:2px;text-align:center;padding:8px}
.card .t{margin-top:7px;font-size:13px;line-height:1.4;overflow:hidden;
  text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical}
.card .s{margin-top:2px;font-size:12px;color:var(--tx3);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.empty{padding:56px 20px;text-align:center;color:var(--tx3);line-height:2}
.empty b{display:block;color:var(--tx2);font-size:15px;margin-bottom:6px}
.spin{padding:48px 0;text-align:center;color:var(--tx3)}

.row{display:flex;align-items:center;gap:12px;padding:9px 2px;
  border-bottom:1px solid var(--line);cursor:pointer}
.row:active{background:var(--panel)}
.row .thumb{width:46px;height:46px;border-radius:6px;overflow:hidden;
  background:var(--chip);flex:0 0 46px;position:relative}
.row .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.row .thumb .ph{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:15px;color:#fff}
.row .meta{flex:1;min-width:0}
.row .meta b{display:block;font-weight:500;font-size:14px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.row .meta span{font-size:12px;color:var(--tx3);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;display:block}
.row.on .meta b{color:var(--ac)}
.row .tag{font-size:11px;color:var(--tx3);flex:0 0 auto}
.row .dl{width:30px;height:30px;flex:0 0 30px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  background:var(--chip);border:1px solid var(--line);
  color:var(--tx2);font-size:15px;cursor:pointer;user-select:none}
.row .dl:active{background:var(--ac);border-color:var(--ac);color:#fff}
.row .dl.busy{opacity:.55}
.row .dl.done{background:#1d9e75;border-color:#1d9e75;color:#fff}
.loadmore{display:block;width:100%;margin:16px 0 4px;padding:11px;
  background:var(--chip);border:1px solid var(--line);border-radius:9px;
  color:var(--tx2);font-size:13px;cursor:pointer}
.back{color:var(--tx2);font-size:13px;margin-bottom:12px;display:inline-block}
.head{display:flex;align-items:center;gap:14px;margin-bottom:14px}
.head .cover{width:104px;height:104px;flex:0 0 104px}
.head .info{min-width:0}
.head .info h2{margin:0 0 4px;font-size:16px;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.head .info p{margin:0;color:var(--tx3);font-size:13px}
.list{margin:0;padding:0;list-style:none}

.player{position:fixed;left:0;right:0;bottom:0;z-index:40;
  background:rgba(23,26,33,.97);backdrop-filter:blur(12px);
  border-top:1px solid var(--line);
  padding:8px calc(14px + env(safe-area-inset-left,0px))
           calc(8px + env(safe-area-inset-bottom,0px))
           calc(14px + env(safe-area-inset-right,0px))}
.player[hidden]{display:none}
.prow{display:flex;align-items:center;gap:12px}
.pcover{width:44px;height:44px;border-radius:6px;overflow:hidden;
  background:var(--chip);flex:0 0 44px}
.pcover img{width:100%;height:100%;object-fit:cover;display:block}
.pinfo{flex:1;min-width:0}
.pinfo b{display:block;font-size:13px;font-weight:500;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pinfo span{font-size:12px;color:var(--tx3);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;display:block}
.pbtn{width:38px;height:38px;border-radius:50%;border:0;flex:0 0 auto;
  background:var(--ac);color:#fff;font-size:15px;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.pbtn.ghost{background:transparent;color:var(--tx2);font-size:19px;width:32px}
.prog{display:flex;align-items:center;gap:8px;margin-top:6px}
.prog input{flex:1;-webkit-appearance:none;height:3px;border-radius:2px;
  background:var(--line);outline:none}
.prog input::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;
  border-radius:50%;background:var(--ac)}
.prog span{font-size:11px;color:var(--tx3);font-variant-numeric:tabular-nums;
  min-width:34px;text-align:right}
</style></head><body>

<header>
  <div class="hrow">
    <h1>音乐 <small id="cnt"></small></h1>
    <a class="navlink" href="/">影视</a>
  </div>
  <div class="tabs">
    <button class="tab on" data-tab="online">在线</button>
    <button class="tab" data-tab="local">本地</button>
  </div>
  <div class="searchbar" id="sbar">
    <input id="q" placeholder="搜歌名 / 歌手 / 专辑…" autocomplete="off">
    <select id="qsel" title="音质">
      <option value="KW_FLAC_2000">无损</option>
      <option value="KW_MP3_320">320k</option>
      <option value="KW_MP3_128">128k</option>
    </select>
  </div>
</header>

<div class="wrap" id="view"><div class="spin">加载中…</div></div>

<div class="player" id="player" hidden>
  <div class="prow">
    <div class="pcover"><img id="pcov" alt=""></div>
    <div class="pinfo"><b id="ptitle"></b><span id="partist"></span></div>
    <button class="pbtn ghost" id="prev">‹</button>
    <button class="pbtn" id="toggle">▶</button>
    <button class="pbtn ghost" id="next">›</button>
  </div>
  <div class="prog">
    <span id="cur">0:00</span>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span id="dur">0:00</span>
  </div>
</div>

<audio id="au" preload="metadata"></audio>

<script>
var ARTISTS = [
  ["华语男声", ["周杰伦", "陈奕迅", "林俊杰", "薛之谦", "李荣浩", "毛不易",
                "张学友", "刘德华", "汪苏泷", "许嵩"]],
  ["华语女声", ["邓紫棋", "孙燕姿", "梁静茹", "蔡依林", "张惠妹", "王菲",
                "那英", "田馥甄", "张碧晨", "单依纯"]],
  ["港台经典", ["张国荣", "梅艳芳", "谭咏麟", "Beyond", "杨千嬅", "容祖儿",
                "谢安琪", "陈慧琳", "林子祥", "Beyond 黄家驹"]],
  ["欧美", ["Taylor Swift", "Ed Sheeran", "Adele", "Coldplay", "Maroon 5",
            "Billie Eilish", "Bruno Mars", "The Weeknd"]],
  ["日韩", ["米津玄师", "YOASOBI", "久石让", "IU", "BLACKPINK", "Aimer"]]
];
var THEMES = ["华语流行", "粤语金曲", "经典老歌", "摇滚", "民谣", "纯音乐",
              "钢琴曲", "古风", "DJ舞曲", "说唱", "怀旧", "治愈"];

var mode = 'online';
var albums = [];
var queue = [];
var qIndex = -1;
var currentAlbum = null;
var kwText = '';
var lastKw = '';
var lastPage = 0;
var lastTotal = 0;
var onlineList = [];

var view = document.getElementById('view');
var au = document.getElementById('au');
var player = document.getElementById('player');
var sbar = document.getElementById('sbar');
var qEl = document.getElementById('q');
var qsel = document.getElementById('qsel');
var prefBr = 'KW_FLAC_2000';
try { prefBr = localStorage.getItem('musicBr') || prefBr; } catch(e){}
if(qsel) qsel.value = prefBr;
if(qsel){
  qsel.addEventListener('change', function(){
    prefBr = qsel.value;
    try { localStorage.setItem('musicBr', prefBr); } catch(e){}
    // 切换后重画当前结果，让每行的码率标签反映新的选择
    if(mode === 'online' && onlineList.length && lastKw){
      var h = '<div class="sec">「' + lastKw + '」共 ' + lastTotal + ' 首</div>' +
        onlineRows(onlineList, 0) +
        '<button class="loadmore" id="more">加载更多</button>';
      view.innerHTML = h;
      var m = document.getElementById('more');
      if(m){
        if(onlineList.length >= lastTotal) m.style.display = 'none';
        m.onclick = function(){ doSearch(lastKw, lastPage + 1); };
      }
      markRow();
    }
  });
}

function enc(s){ return encodeURIComponent(s); }
function fmt(sec){
  if(!isFinite(sec) || sec < 0) return '0:00';
  var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m + ':' + (s < 10 ? '0' + s : s);
}
function hue(str){
  var h = 0;
  for(var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  return h;
}
function phStyle(name){
  var h = hue(name);
  return 'background:linear-gradient(135deg,hsl(' + h + ',42%,26%),hsl(' +
    ((h + 48) % 360) + ',38%,17%))';
}
function coverHTML(item, extraCls){
  var cls = extraCls || '';
  if(item.cover){
    return '<div class="cover ' + cls + '"><img loading="lazy" alt="" src="/api/music/cover?p=' +
      enc(item.cover) + '"></div>';
  }
  var label = (item.album || item.title || '?').slice(0, 4);
  return '<div class="cover ' + cls + '"><div class="ph" style="' + phStyle(label) + '">' +
    label + '</div></div>';
}
function thumbHTML(name, pic){
  if(pic){
    return '<span class="thumb"><img loading="lazy" alt="" src="' + pic +
      '" onerror="this.style.display=&#39;none&#39;"></span>';
  }
  var label = (name || '?').slice(0, 1);
  return '<span class="thumb"><span class="ph" style="' + phStyle(name || '?') + '">' +
    label + '</span></span>';
}

function setTab(m){
  mode = m;
  var tabs = document.querySelectorAll('.tab');
  for(var i = 0; i < tabs.length; i++){
    if(tabs[i].getAttribute('data-tab') === m) tabs[i].classList.add('on');
    else tabs[i].classList.remove('on');
  }
  sbar.hidden = false;
  qEl.value = '';
  kwText = '';
  document.getElementById('cnt').textContent = '';
  if(m === 'online'){ renderOnlineHome(); }
  else { loadLocal(); }
}

function renderOnlineHome(){
  var h = '<div class="sec">热门歌手</div>';
  for(var i = 0; i < ARTISTS.length; i++){
    h += '<div class="sec" style="margin:14px 0 8px;font-size:12px">' +
      ARTISTS[i][0] + '</div><div class="chips">';
    var arr = ARTISTS[i][1];
    for(var j = 0; j < arr.length; j++){
      h += '<span class="chip" data-kw="' + arr[j] + '">' + arr[j] + '</span>';
    }
    h += '</div>';
  }
  h += '<div class="sec" style="margin-top:20px">按主题找</div><div class="chips">';
  for(var k = 0; k < THEMES.length; k++){
    h += '<span class="chip" data-kw="' + THEMES[k] + '">' + THEMES[k] + '</span>';
  }
  h += '</div>';
  view.innerHTML = h;
}

function pickBr(brs, want){
  var order = ['KW_FLAC_2000', 'KW_MP3_320', 'KW_MP3_128'];
  if(!brs || !brs.length) return 'KW_MP3_320';
  if(brs.indexOf(want) >= 0) return want;
  var i = order.indexOf(want);
  if(i < 0) i = 0;
  var seq = order.slice(i).concat(order.slice(0, i).reverse());
  for(var k = 0; k < seq.length; k++){
    if(brs.indexOf(seq[k]) >= 0) return seq[k];
  }
  return brs[0];
}

function brLabel(code){
  return code.replace('KW_', '').replace('_', ' ');
}

function onlineRows(list, offset){
  offset = offset || 0;
  var h = '<div class="res">';
  for(var i = 0; i < list.length; i++){
    var x = list[i];
    var n = offset + i;
    var br = brLabel(pickBr(x.br, prefBr));
    h += '<div class="row" data-o="' + n + '">' + thumbHTML(x.name, x.pic) +
      '<span class="meta"><b>' + x.name + '</b><span>' + x.artist +
      (x.album ? ' · ' + x.album : '') + '</span></span>' +
      '<span class="tag">' + br + '</span>' +
      '<span class="dl" data-dl="' + n + '" title="下载到本地">↓</span></div>';
  }
  h += '</div>';
  return h;
}

function doDownload(el){
  var i = parseInt(el.getAttribute('data-dl'), 10);
  var x = onlineList[i];
  if(!x) return;
  if(el.dataset.busy) return;
  el.dataset.busy = '1';
  el.textContent = '…';
  el.classList.add('busy');
  fetch('/api/online/download?id=' + enc(x.id) + '&br=' + enc(prefBr))
    .then(function(r){ return r.json(); })
    .then(function(d){
      el.classList.remove('busy');
      delete el.dataset.busy;
      if(d.ok){
        el.textContent = '✓';
        el.classList.add('done');
        el.title = '已加入下载：' + (d.file || x.name);
      } else {
        el.textContent = '!';
        el.title = d.error || '下载失败';
      }
      setTimeout(function(){
        el.textContent = '↓';
        el.classList.remove('done');
      }, 2600);
    })
    .catch(function(){
      el.classList.remove('busy');
      delete el.dataset.busy;
      el.textContent = '!';
      setTimeout(function(){ el.textContent = '↓'; }, 2600);
    });
}

function doSearch(kw, page){
  kw = (kw || '').trim();
  if(!kw) return;
  lastKw = kw;
  lastPage = page;
  if(page === 1){
    view.innerHTML = '<div class="spin">搜索中…</div>';
    onlineList = [];
  }
  fetch('/api/online/search?kw=' + enc(kw) + '&page=' + page)
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.error){ view.innerHTML = '<div class="empty"><b>搜索失败</b>' + d.error + '</div>'; return; }
      var list = d.list || [];
      lastTotal = d.total || 0;
      if(page === 1){
        onlineList = list;
        var h = '<div class="sec">「' + kw + '」共 ' + lastTotal + ' 首</div>' +
          onlineRows(list, 0);
        h += '<button class="loadmore" id="more">加载更多</button>';
        view.innerHTML = h;
      } else {
        var base = onlineList.length;
        for(var i = 0; i < list.length; i++) onlineList.push(list[i]);
        var box = document.querySelector('.res');
        if(box){
          var chunk = onlineRows(list, base);
          chunk = chunk.slice('<div class="res">'.length, chunk.length - '</div>'.length);
          box.insertAdjacentHTML('beforeend', chunk);
        }
        var more = document.getElementById('more');
        if(more && onlineList.length >= lastTotal) more.style.display = 'none';
      }
      document.getElementById('cnt').textContent = onlineList.length + ' / ' + lastTotal;
      var m = document.getElementById('more');
      if(m) m.onclick = function(){ doSearch(kw, lastPage + 1); };
    })
    .catch(function(){ view.innerHTML = '<div class="empty"><b>网络错误</b></div>'; });
}

function loadLocal(){
  view.innerHTML = '<div class="spin">加载曲库…</div>';
  fetch('/api/music/albums')
    .then(function(r){ return r.json(); })
    .then(function(d){ albums = d.albums || []; renderAlbums(); })
    .catch(function(){ view.innerHTML = '<div class="empty"><b>加载失败</b></div>'; });
}

function renderAlbums(){
  currentAlbum = null;
  if(!albums.length){
    view.innerHTML = '<div class="empty"><b>本地曲库是空的</b>' +
      '去 SQMusic（8996）下载，或直接用「在线」听</div>';
    return;
  }
  var total = 0, shown = [];
  for(var i = 0; i < albums.length; i++){
    total += albums[i].count;
    var a = albums[i];
    if(!kwText || a.album.indexOf(kwText) >= 0 || a.artist.indexOf(kwText) >= 0){
      shown.push(i);
    }
  }
  document.getElementById('cnt').textContent = albums.length + ' 张 · ' + total + ' 首';
  if(!shown.length){
    view.innerHTML = '<div class="empty"><b>没有匹配</b>换个关键词</div>';
    return;
  }
  var html = '<div class="grid">';
  for(var j = 0; j < shown.length; j++){
    var k = shown[j];
    html += '<div class="card" data-i="' + k + '">' + coverHTML(albums[k]) +
      '<div class="t">' + albums[k].album + '</div>' +
      '<div class="s">' + albums[k].artist + '</div></div>';
  }
  view.innerHTML = html + '</div>';
}

function renderAlbum(alb){
  currentAlbum = alb;
  view.innerHTML = '<a class="back" href="#" id="back">‹ 返回</a>' +
    '<div class="head">' + coverHTML(alb) +
    '<div class="info"><h2>' + alb.album + '</h2>' +
    '<p>' + alb.artist + ' · ' + alb.count + ' 首</p></div></div>' +
    '<ul class="list" id="tracks"><li class="spin">加载曲目…</li></ul>';
  document.getElementById('back').onclick = function(e){
    e.preventDefault(); renderAlbums();
  };
  fetch('/api/music/album?id=' + enc(alb.id))
    .then(function(r){ return r.json(); })
    .then(function(d){
      var tracks = d.tracks || [];
      queue = [];
      for(var i = 0; i < tracks.length; i++){
        queue.push({ kind:'local', path: tracks[i].path, title: tracks[i].title,
                     artist: alb.artist, album: alb.album,
                     cover: alb.cover });
      }
      var ul = document.getElementById('tracks');
      if(!tracks.length){ ul.innerHTML = '<li class="spin">无可播放文件</li>'; return; }
      var h = '';
      for(var j = 0; j < tracks.length; j++){
        h += '<li class="row" data-t="' + j + '"><span class="n" style="width:24px;color:var(--tx3);font-size:12px">' +
          (j + 1) + '</span><span class="meta"><b>' + tracks[j].title +
          '</b><span>' + alb.artist + '</span></span></li>';
      }
      ul.innerHTML = h;
    });
}

function markRow(){
  var rows = document.querySelectorAll('.row');
  for(var i = 0; i < rows.length; i++){
    var oi = rows[i].getAttribute('data-o'), ti = rows[i].getAttribute('data-t');
    var isCur = (oi !== null && mode === 'online' && parseInt(oi, 10) === qIndex) ||
                (ti !== null && mode === 'local' && parseInt(ti, 10) === qIndex);
    if(isCur) rows[i].classList.add('on'); else rows[i].classList.remove('on');
  }
}

function setPlayerInfo(t){
  document.getElementById('ptitle').textContent = t.title;
  document.getElementById('partist').textContent =
    (t.artist || '') + (t.album ? ' · ' + t.album : '');
  var pc = document.getElementById('pcov');
  var src = '';
  if(t.kind === 'online') src = t.pic || '';
  else if(t.cover) src = '/api/music/cover?p=' + enc(t.cover);
  if(src){ pc.src = src; pc.style.display = 'block'; }
  else { pc.removeAttribute('src'); pc.style.display = 'none'; }
  player.hidden = false;
}

function playAt(i){
  if(i < 0 || i >= queue.length) return;
  qIndex = i;
  var t = queue[i];
  setPlayerInfo(t);
  document.getElementById('toggle').textContent = '❚❚';
  markRow();

  if(t.kind === 'local'){
    au.src = '/api/music/stream?p=' + enc(t.path);
    au.play().catch(function(){});
    return;
  }
  document.getElementById('ptitle').textContent = t.title + '（获取中…）';
  fetch('/api/online/url?id=' + enc(t.id) + '&br=' + enc(prefBr))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d.url){
        document.getElementById('ptitle').textContent = t.title + '（无音源）';
        return;
      }
      au.src = d.url;
      au.play().catch(function(){});
      document.getElementById('ptitle').textContent = t.title;
    })
    .catch(function(){
      document.getElementById('ptitle').textContent = t.title + '（失败）';
    });
}

view.addEventListener('click', function(e){
  var chip = e.target.closest ? e.target.closest('.chip') : null;
  if(chip){
    qEl.value = chip.getAttribute('data-kw');
    doSearch(qEl.value, 1);
    window.scrollTo({ top: 0 });
    return;
  }
  var dl = e.target.closest ? e.target.closest('.dl') : null;
  if(dl){ doDownload(dl); return; }
  var card = e.target.closest ? e.target.closest('.card') : null;
  if(card){
    renderAlbum(albums[parseInt(card.getAttribute('data-i'), 10)]);
    window.scrollTo({ top: 0 });
    return;
  }
  var orow = e.target.closest ? e.target.closest('.row[data-o]') : null;
  if(orow){
    var oi = parseInt(orow.getAttribute('data-o'), 10);
    queue = [];
    for(var i = 0; i < onlineList.length; i++){
      queue.push({ kind:'online', id: onlineList[i].id, title: onlineList[i].name,
                   artist: onlineList[i].artist, album: onlineList[i].album,
                   pic: onlineList[i].pic });
    }
    playAt(oi);
    return;
  }
  var lrow = e.target.closest ? e.target.closest('.row[data-t]') : null;
  if(lrow) playAt(parseInt(lrow.getAttribute('data-t'), 10));
});

document.querySelectorAll('.tab').forEach(function(t){
  t.onclick = function(){ setTab(t.getAttribute('data-tab')); };
});

qEl.addEventListener('input', function(){
  if(mode !== 'local') return;
  kwText = qEl.value.trim();
  renderAlbums();
});

qEl.addEventListener('keydown', function(e){
  if(e.key !== 'Enter') return;
  if(mode === 'online') doSearch(qEl.value, 1);
  else { kwText = qEl.value.trim(); renderAlbums(); }
});

document.getElementById('toggle').onclick = function(){
  if(!au.src) return;
  if(au.paused){ au.play().catch(function(){}); this.textContent = '❚❚'; }
  else { au.pause(); this.textContent = '▶'; }
};
document.getElementById('next').onclick = function(){
  if(qIndex + 1 < queue.length) playAt(qIndex + 1);
};
document.getElementById('prev').onclick = function(){
  if(au.currentTime > 4){ au.currentTime = 0; return; }
  if(qIndex > 0) playAt(qIndex - 1);
};
au.addEventListener('timeupdate', function(){
  document.getElementById('cur').textContent = fmt(au.currentTime);
  if(au.duration && isFinite(au.duration)){
    document.getElementById('dur').textContent = fmt(au.duration);
    var seek = document.getElementById('seek');
    if(!seek.dataset.dragging) seek.value = Math.round(au.currentTime / au.duration * 1000);
  }
});
au.addEventListener('ended', function(){
  if(qIndex + 1 < queue.length) playAt(qIndex + 1);
  else document.getElementById('toggle').textContent = '▶';
});
au.addEventListener('play', function(){
  document.getElementById('toggle').textContent = '❚❚';
});
au.addEventListener('pause', function(){
  document.getElementById('toggle').textContent = '▶';
});
var seekEl = document.getElementById('seek');
seekEl.addEventListener('input', function(){ seekEl.dataset.dragging = '1'; });
seekEl.addEventListener('change', function(){
  delete seekEl.dataset.dragging;
  if(au.duration && isFinite(au.duration)){
    au.currentTime = seekEl.value / 1000 * au.duration;
  }
});

setTab('online');
</script>
</body></html>
"""


# ────────────────────────────── 请求处理 ──────────────────────────────

def handle(h):
    """由 web.py 的 Handler 调用；返回 True 表示已处理该请求。"""
    u = urlparse(h.path)
    p = u.path
    qs = parse_qs(u.query)

    if p == "/music":
        h._html(PAGE)
        return True

    if p == "/api/music/albums":
        h._json({"albums": _scan()})
        return True

    if p == "/api/music/album":
        aid = (qs.get("id") or [""])[0]
        tracks = _album_tracks(aid)
        h._json({"tracks": tracks})
        return True

    if p == "/api/music/cover":
        rel = (qs.get("p") or [""])[0]
        return _send_file(h, rel, cache=True)

    if p == "/api/music/stream":
        rel = (qs.get("p") or [""])[0]
        return _send_file(h, rel, cache=False)

    return False


def _send_file(h, rel, cache):
    path = _safe(rel)
    if not path or not os.path.isfile(path):
        h._json({"error": "not found"}, 404)
        return True

    try:
        size = os.path.getsize(path)
    except OSError:
        h._json({"error": "stat failed"}, 500)
        return True

    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    # .flac 在部分系统里 guess 不出来，显式兜底，否则浏览器可能不解码
    if path.lower().endswith(".flac"):
        ctype = "audio/flac"
    elif path.lower().endswith(".m4a"):
        ctype = "audio/mp4"

    start, end = 0, size - 1
    partial = False
    rng = h.headers.get("Range")
    if rng and rng.startswith("bytes="):
        spec = rng[6:].split(",")[0].strip()
        try:
            if spec.startswith("-"):
                start = max(0, size - int(spec[1:]))
            else:
                a, _, b = spec.partition("-")
                start = int(a)
                if b:
                    end = min(int(b), size - 1)
            if start > end or start >= size:
                raise ValueError("bad range")
            partial = True
        except (ValueError, IndexError):
            start, end = 0, size - 1
            partial = False

    length = end - start + 1
    try:
        h.send_response(206 if partial else 200)
        h.send_header("Content-Type", ctype)
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("Content-Length", str(length))
        if partial:
            h.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if cache:
            h.send_header("Cache-Control", "public, max-age=604800")
        else:
            h.send_header("Cache-Control", "no-cache")
        h.end_headers()

        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                h.wfile.write(chunk)
                left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        # 拖动进度条时浏览器会主动断开旧请求，属正常现象
        pass
    return True
