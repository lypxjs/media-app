"""从 web.py 提取浏览页，改造为 APK 内置的本地页面。

只做两件事：
  1. 注入 fetch 拦截器，把 5 个服务端接口改接到 MediaDBNative（本地 SQLite）
  2. 去掉 /config 请求（本地无需服务端配置）

播放不动 —— 页面里 play() 本来就优先调 MediaDBNative.play()，
原生 ExoPlayer 直接连 CDN，只有原生不可用时才回退到 LibreTV。
"""
import io, os, re

SRC = r'D:/codebuddy/2026-10-14-21-50-02/indexer/web.py'
OUT = r'D:/codebuddy/2026-10-14-21-50-02/android/app/src/main/assets/www/index.html'

s = io.open(SRC, encoding='utf-8').read()

m = re.search(r'PAGE = """(.*?)"""\n', s, re.S)
assert m, '未找到 PAGE'
html = m.group(1)
print('提取页面 %d 字符' % len(html))

# ── 去掉 /config ──
old_cfg = """// LibreTV 播放地址，由 /config 注入（服务端持有密码与地址，前端不硬编码）
// ⚠️ 必须挂到 window：此前用 let 声明、却以 window.LIBRETV_BASE 读取，
// let 不会成为 window 属性 → 恒为 undefined → 退化成相对路径 /watch，
// 请求打到浏览页自身的 8091 端口 → {"error":"not found"}。
window.LIBRETV_BASE = '';
fetch('/config').then(r=>r.json()).then(d=>{
  window.LIBRETV_BASE = (d && d.libretv) || '';
});"""
assert old_cfg in html, '/config 片段未匹配'
html = html.replace(old_cfg, """// 独立版：数据与播放全在 App 内，无需向服务端取配置
window.LIBRETV_BASE = '';""")
print('  ✓ 移除 /config')

# ── 注入本地数据层 ──
shim = r"""
/* ===== 本地数据层（APK 独立版） =====
   页面原本通过 HTTP 打服务端接口；这里把它们改接到 App 内置的 SQLite。
   做法是替换 window.fetch —— 页面主体逻辑一行未改，
   只需维护这一处适配层，以后服务端接口变了也不影响 App。 */
(function(){
  var N = window.MediaDBNative;
  if(!N){ console.error('MediaDBNative 缺失，无法读取本地片库'); return; }

  function j(data){
    return Promise.resolve({ ok:true, status:200,
      json:function(){ return Promise.resolve(data); } });
  }
  var origFetch = window.fetch ? window.fetch.bind(window) : null;

  window.fetch = function(url, opt){
    url = String(url);
    var method = ((opt && opt.method) || 'GET').toUpperCase();

    // 分类导航
    if(url.indexOf('/api/php/provide/types') === 0){
      try { return j(JSON.parse(N.types())); } catch(e){ return j({class:[]}); }
    }

    // 观看进度（读）
    if(url.indexOf('/progress?ids=') === 0){
      var ids = decodeURIComponent(url.split('ids=')[1] || '');
      try { return j(JSON.parse(N.getProgress(ids))); } catch(e){ return j({}); }
    }

    // 观看进度（写）
    if(url.indexOf('/progress') === 0 && method === 'POST'){
      try { N.saveProgress(JSON.stringify(JSON.parse((opt && opt.body) || '{}'))); } catch(e){}
      return j({ ok:true });
    }

    // 详情：返回剧集地址
    if(url.indexOf('/api/php/provide/vod') === 0 || url.indexOf('/api.php/provide/vod') === 0){
      var mm = url.match(/ids=([^&]+)/);
      var sid = mm ? decodeURIComponent(mm[1]) : '';
      try { return j(JSON.parse(N.detail(sid))); } catch(e){ return j({list:[]}); }
    }

    // 搜索
    if(url.indexOf('/api/search') === 0){
      var kw = decodeURIComponent((url.match(/wd=([^&]*)/) || [,''])[1]);
      var lim = parseInt((url.match(/limit=(\d+)/) || [,'60'])[1], 10);
      try { return j(JSON.parse(N.search(kw, lim))); } catch(e){ return j({list:[]}); }
    }

    // 列表浏览
    if(url.indexOf('/browse') === 0){
      var tid = (url.match(/type_id=(\d+)/) || [,''])[1];
      var yr  = decodeURIComponent((url.match(/year=([^&]*)/) || [,''])[1] || '');
      var od  = (url.match(/order=([a-z]+)/) || [,'time'])[1];
      var lm  = parseInt((url.match(/limit=(\d+)/) || [,'30'])[1], 10);
      try {
        return j(JSON.parse(N.browse(tid ? parseInt(tid,10) : 0, yr, od, lm, window.__page || 1)));
      } catch(e){ return j({list:[], total:0}); }
    }

    // 视频流代理：独立版不再中转，直连 CDN
    if(url.indexOf('/api/proxy') === 0){
      return Promise.reject(new Error('independent-mode: no proxy'));
    }
    return origFetch ? origFetch(url, opt) : Promise.reject(new Error('no fetch'));
  };
})();
"""
i = html.index('<script>') + len('<script>')
html = html[:i] + shim + html[i:]
print('  ✓ 注入本地数据层')

# ── 修 .dhead 定位（占位图会遮住集数按钮） ──
OLD_CSS = ".dhead{display:flex;gap:16px;padding:18px;border-bottom:1px solid var(--line)}"
if OLD_CSS in html:
    html = html.replace(OLD_CSS,
        ".dhead{display:flex;gap:16px;padding:18px;"
        "border-bottom:1px solid var(--line);position:relative}")
    print('  ✓ 修 .dhead 定位（占位图不再遮挡集数）')
else:
    print('  ! .dhead 样式未匹配（可能已修过）')

# ── 音乐入口指向本地页面 ──
if 'href="/music"' in html:
    html = html.replace('href="/music"', 'href="music.html"')
    print('  ✓ 音乐入口 -> music.html（App 内置在线音乐）')
else:
    print('  ! 未找到音乐入口')

# ── 同步页码给 browse 分页 ──
old_go = "async function go(p){\n  page = p;"
assert html.count(old_go) == 1
html = html.replace(old_go, "async function go(p){\n  page = p;\n  window.__page = p;   // 本地数据层据此分页")
print('  ✓ 同步页码')

os.makedirs(os.path.dirname(OUT), exist_ok=True)
io.open(OUT, 'w', encoding='utf-8', newline='\n').write(html)
print('\n写出 %s（%.1f KB）' % (OUT, len(html.encode('utf-8')) / 1024))
