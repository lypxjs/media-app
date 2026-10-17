"""导出 APK 内置精简库（全量 19.7 万条）。

字段取舍
--------
去掉：plot(29.2MB 简介) / actor(7.7MB 演员) / director(0.9MB)
      —— 浏览页与详情弹窗都不显示这些
保留：id/name/pic/type/year/remarks/score/hits/serial/total/episodes/vod_time
      episodes 是剧集地址数组（71.9MB），**必须内置**：
      采集站的详情接口已 404，播放时无法按 id 回查。

搜索不走倒排表
--------------
原库靠 grams(bigram 倒排) 做检索。这里不导出该表（省体积），
改由 App 侧用 LIKE 扫描 —— 19.7 万条实测百毫秒级，体验无感。
"""
import sqlite3, os, sys, time, gzip, shutil

SRC = '/tmp/idx.db'
RAW = '/tmp/app_index.db'
GZ = '/tmp/app_index.db.gz'

COLS = ['src', 'vod_id', 'name', 'pic', 'type_id', 'type_name',
        'year', 'area', 'remarks', 'score', 'hits', 'serial',
        'total', 'episodes', 'vod_time']

for p in (RAW, GZ):
    if os.path.exists(p):
        os.remove(p)

src = sqlite3.connect(SRC)
dst = sqlite3.connect(RAW)
dst.execute('PRAGMA journal_mode=OFF')
dst.execute('PRAGMA synchronous=OFF')
dst.execute('''CREATE TABLE videos(
    src TEXT, vod_id INTEGER, name TEXT, pic TEXT,
    type_id INTEGER, type_name TEXT, year TEXT, area TEXT,
    remarks TEXT, score REAL, hits INTEGER, serial TEXT,
    total TEXT, episodes TEXT, vod_time TEXT)''')

t0 = time.time()
cur = src.execute('SELECT %s FROM videos ORDER BY vod_time DESC' % ','.join(COLS))
ph = ','.join('?' * len(COLS))
n = 0
buf = []
for r in cur:
    buf.append(r)
    n += 1
    if len(buf) >= 3000:
        dst.executemany('INSERT INTO videos VALUES(%s)' % ph, buf)
        buf = []
if buf:
    dst.executemany('INSERT INTO videos VALUES(%s)' % ph, buf)

# 只建必要的两个索引：翻页按时间、按分类
dst.execute('CREATE INDEX i_time ON videos(vod_time)')
dst.execute('CREATE INDEX i_type ON videos(type_id)')
dst.commit()
dst.execute('VACUUM')
dst.close()

raw_size = os.path.getsize(RAW)
print('导出 %d 条，耗时 %.1fs，原始 %.1f MB' % (n, time.time() - t0, raw_size / 1048576.0))

# gzip -9
with open(RAW, 'rb') as fi, gzip.open(GZ, 'wb', compresslevel=9) as fo:
    shutil.copyfileobj(fi, fo, 1 << 20)
gz_size = os.path.getsize(GZ)
print('gzip 后 %.1f MB（压缩率 %.0f%%）' % (gz_size / 1048576.0, raw_size * 100.0 / gz_size))

# 抽查
con = sqlite3.connect(RAW)
print('\n抽查 3 条：')
for r in con.execute('SELECT id, name FROM videos').fetchall()[:0]:
    pass
for r in __import__('itertools').islice(con.execute(
        'SELECT src, vod_id, name, type_name, year, remarks, length(episodes) FROM videos'), 3):
    print('  ', r)
print('\n总条数:', con.execute('SELECT COUNT(*) FROM videos').fetchone()[0])
print('分类数:', con.execute('SELECT COUNT(DISTINCT type_id) FROM videos').fetchone()[0])
con.close()
src.close()
