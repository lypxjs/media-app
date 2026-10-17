"""复刻 LocalStore.kt 的 SQL，对同一份库验证输出格式是否正确。

Kotlin 侧跑不了，但 SQL 语义是确定的 —— 这里逐条比对，
确保 App 拿到的 JSON 结构与页面期望一致。
"""
import sqlite3, gzip, os, json, io

GZ = r'D:/codebuddy/2026-10-14-21-50-02/android/app/src/main/assets/library.db.gz'
TMP = r'D:/codebuddy/2026-10-14-21-50-02/build_app/_verify.db'

if not os.path.exists(TMP) or os.path.getsize(TMP) < 1024:
    print('解压校验库…')
    with gzip.open(GZ, 'rb') as fi, open(TMP, 'wb') as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)

con = sqlite3.connect(TMP)
con.row_factory = sqlite3.Row


def card(r):
    """等价于 LocalStore.card()"""
    src = r['src'] or ''
    code = {'ty': 1, 'ws': 2}.get(src, 9)
    try:
        ep_count = len(json.loads(r['episodes'] or '[]'))
    except Exception:
        ep_count = 0
    return {
        'id': '%d:%d' % (code, r['vod_id']),
        'name': r['name'] or '',
        'pic': r['pic'] or '',
        'type_name': r['type_name'] or '',
        'year': r['year'] or '',
        'remarks': r['remarks'] or '',
        'serial': r['serial'] or '',
        'total': r['total'] or str(ep_count),
        'score': r['score'] or 0,
        'episodes': ep_count,
        'vod_time': r['vod_time'] or '',
    }


print('=' * 60)
print('1) types()')
rows = con.execute("""SELECT type_id, type_name, COUNT(*) c FROM videos
    WHERE type_id IS NOT NULL GROUP BY type_id, type_name ORDER BY c DESC""").fetchall()
print('   分类数:', len(rows))
for r in rows[:5]:
    print('     type_id=%-4d %-12s %d 条' % (r['type_id'], r['type_name'], r['c']))

print()
print('2) browse(page=1, size=5, 最新)')
total = con.execute('SELECT COUNT(*) FROM videos').fetchone()[0]
rows = con.execute(
    'SELECT * FROM videos ORDER BY vod_time DESC, vod_id DESC LIMIT 5 OFFSET 0').fetchall()
print('   total =', total)
for r in rows:
    c = card(r)
    print('     %-12s %-22s %s / %s / %d集' % (
        c['id'], c['name'][:20], c['type_name'], c['year'], c['episodes']))

print()
print('3) browse(type_id 过滤)')
tid = rows[0]['type_id']
t2 = con.execute('SELECT COUNT(*) FROM videos WHERE type_id = ?', (tid,)).fetchone()[0]
r2 = con.execute('SELECT name FROM videos WHERE type_id = ? ORDER BY vod_time DESC LIMIT 2',
                 (tid,)).fetchall()
print('   type_id=%d 共 %d 条，示例: %s' % (tid, t2, [x['name'][:14] for x in r2]))

print()
print('4) search("末日") —— LIKE 全表扫')
rows = con.execute(
    r"SELECT * FROM videos WHERE name LIKE ? ESCAPE '\' ORDER BY vod_time DESC LIMIT 5",
    ('%末日%',)).fetchall()
print('   命中前 5 条:')
for r in rows:
    c = card(r)
    print('     %-12s %s' % (c['id'], c['name'][:30]))

print()
print('5) search("100%") —— 特殊字符转义')
esc = '100\\%'
try:
    n = con.execute(r"SELECT COUNT(*) FROM videos WHERE name LIKE ? ESCAPE '\'",
                    ('%' + esc + '%',)).fetchone()[0]
    print('   转义后命中 %d 条（不报错即可）' % n)
except Exception as e:
    print('   ✗ 报错:', e)

print()
print('6) detail() —— 拼 vod_play_url')
c0 = card(rows[0]) if rows else card(con.execute('SELECT * FROM videos LIMIT 1').fetchone())
s_, v_ = c0['id'].split(':')
src = {'1': 'ty', '2': 'ws'}[s_]
d = con.execute('SELECT * FROM videos WHERE src=? AND vod_id=?', (src, int(v_))).fetchone()
eps = json.loads(d['episodes'] or '[]')
play = '#'.join('第%d集$%s' % (i + 1, u) for i, u in enumerate(eps))
print('   片名:', d['name'])
print('   集数:', len(eps))
print('   vod_play_url 前 110 字符:')
print('     ', play[:110])
print('   格式检查: 段数=%d，含"第1集$"=%s' % (
    len(play.split('#')), play.startswith('第1集$')))
u0 = eps[0] if eps else ''
print('   首集地址:', u0[:80])
print('   可推导类型:', 'm3u8' if u0.endswith('.m3u8') else '其它')

print()
print('7) 可用域名抽样（100 条随机）')
import random
random.seed(1)
sample = con.execute('SELECT episodes FROM videos ORDER BY RANDOM() LIMIT 100').fetchall()
from collections import Counter
from urllib.parse import urlparse
dom = Counter()
for r in sample:
    try:
        e = json.loads(r['episodes'] or '[]')
        if e:
            dom[urlparse(e[0]).netloc] += 1
    except Exception:
        pass
for k, v in dom.most_common(6):
    print('   %-32s %d' % (k, v))

con.close()
os.remove(TMP)
print()
print('=' * 60)
print('SQL 逻辑验证完成')
