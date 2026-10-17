"""抽样检测剧集地址的可达性。

背景：索引里的 play_url 是采集站给的完整 CDN 地址，域名分散在
tyyszywvod1/2.com 与 v1~v14.wsyzym3u8.com 等。域名一旦失效，
该条就播不了 —— 这直接决定「独立 App」能不能真用。
"""
import sqlite3, gzip, os, json, ssl, urllib.request, random, time
from collections import defaultdict
from urllib.parse import urlparse

GZ = r'D:/codebuddy/2026-10-14-21-50-02/android/app/src/main/assets/library.db.gz'
TMP = r'D:/codebuddy/2026-10-14-21-50-02/build_app/_probe.db'

if not os.path.exists(TMP):
    with gzip.open(GZ, 'rb') as fi, open(TMP, 'wb') as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)

con = sqlite3.connect(TMP)
con.row_factory = sqlite3.Row

# 按域名分组，每个域名抽 3 条不同的
by_dom = defaultdict(list)
random.seed(7)
for r in con.execute('SELECT name, episodes FROM videos WHERE episodes IS NOT NULL '
                     'AND episodes != "[]" ORDER BY RANDOM() LIMIT 6000'):
    try:
        eps = json.loads(r['episodes'] or '[]')
    except Exception:
        continue
    if not eps:
        continue
    u = eps[0]
    d = urlparse(u).netloc
    if len(by_dom[d]) < 3:
        by_dom[d].append((r['name'], u))
    if sum(len(v) for v in by_dom.values()) >= 60:
        break
con.close()

op = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),           # 绕开系统代理（MyClash 会中断 TLS）
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

print('域名 %d 个，共测 %d 条\n' % (len(by_dom), sum(len(v) for v in by_dom.values())))
ok_by_dom = {}
for dom in sorted(by_dom):
    ok = 0
    n = len(by_dom[dom])
    note = ''
    for name, u in by_dom[dom]:
        try:
            req = urllib.request.Request(u, headers={'User-Agent': UA})
            with op.open(req, timeout=12) as resp:
                body = resp.read(200)
                if b'#EXTM3U' in body:
                    ok += 1
                else:
                    note = '非 m3u8'
        except Exception as e:
            note = type(e).__name__
        time.sleep(0.2)
    ok_by_dom[dom] = (ok, n)
    flag = '✓' if ok == n else ('~' if ok else '✗')
    print('  %s %-28s %d/%d  %s' % (flag, dom, ok, n, note))

total_ok = sum(v[0] for v in ok_by_dom.values())
total_n = sum(v[1] for v in ok_by_dom.values())
print('\n合计 %d/%d 可用（%.0f%%）' % (total_ok, total_n, total_ok * 100.0 / max(1, total_n)))

os.remove(TMP)
