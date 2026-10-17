"""生成 mock 数据：模拟 LocalStore 返回的 JSON，供页面逻辑验证。

数据全部取自真实的 library.db，保证格式与实际 App 一致。
"""
import sqlite3, gzip, os, json

GZ = r'D:/codebuddy/2026-10-14-21-50-02/android/app/src/main/assets/library.db.gz'
TMP = r'D:/codebuddy/2026-10-14-21-50-02/build_app/_mock.db'
OUT = r'D:/codebuddy/2026-10-14-21-50-02/build_app/mock.json'

if not os.path.exists(TMP):
    with gzip.open(GZ, 'rb') as fi, open(TMP, 'wb') as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)

con = sqlite3.connect(TMP)
con.row_factory = sqlite3.Row


def card(r):
    src = r['src'] or ''
    code = {'ty': 1, 'ws': 2}.get(src, 9)
    try:
        n = len(json.loads(r['episodes'] or '[]'))
    except Exception:
        n = 0
    return {
        'id': '%d:%d' % (code, r['vod_id']),
        'name': r['name'] or '',
        'pic': r['pic'] or '',
        'type_name': r['type_name'] or '',
        'year': r['year'] or '',
        'remarks': r['remarks'] or '',
        'serial': r['serial'] or '',
        'total': r['total'] or str(n),
        'score': r['score'] or 0,
        'episodes': n,
        'vod_time': r['vod_time'] or '',
    }


types = [{'type_id': r['type_id'], 'type_name': r['type_name'], 'count': r['c']}
         for r in con.execute("""SELECT type_id, type_name, COUNT(*) c FROM videos
             WHERE type_id IS NOT NULL GROUP BY type_id, type_name ORDER BY c DESC""")]

rows = con.execute('SELECT * FROM videos ORDER BY vod_time DESC LIMIT 30').fetchall()
browse = {'list': [card(r) for r in rows], 'total': 197490}

# 挑一条集数多的做详情样本
multi = con.execute("""SELECT * FROM videos
    WHERE LENGTH(episodes) > 1500 ORDER BY vod_time DESC LIMIT 1""").fetchone()
det = None
if multi:
    eps = json.loads(multi['episodes'] or '[]')
    play = '#'.join('第%d集$%s' % (i + 1, u) for i, u in enumerate(eps))
    det = {'list': [{
        'vod_id': multi['vod_id'],
        'vod_name': multi['name'],
        'vod_pic': multi['pic'] or '',
        'vod_year': multi['year'] or '',
        'vod_area': multi['type_name'] or '',
        'vod_remarks': multi['remarks'] or '',
        'vod_play_url': play,
    }]}

search = {'list': [card(r) for r in con.execute(
    r"SELECT * FROM videos WHERE name LIKE ? ESCAPE '\' ORDER BY vod_time DESC LIMIT 10",
    ('%末日%',)).fetchall()]}

data = {
    'types': {'class': types[:30]},
    'browse': browse,
    'search': search,
    'detail': det,
    'progress': {browse['list'][0]['id']: {'episode': 2, 'episodes': 10, 'finished': False}},
}
con.close()
os.remove(TMP)

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print('mock 已生成 %.1f KB' % (os.path.getsize(OUT) / 1024))
print('  分类 %d / 列表 %d 条 / 搜索 %d 条 / 详情 %s' % (
    len(data['types']['class']), len(browse['list']), len(search['list']),
    '有 %d 集' % len(det['list'][0]['vod_play_url'].split('#')) if det else '无'))
print('  详情片名:', det['list'][0]['vod_name'] if det else '-')
