/**
 * 验证 APK 内置页面：注入 mock 的 MediaDBNative，检查
 * ① 封面墙渲染 ② fetch 拦截器是否生效 ③ 点卡片出详情 ④ 点集数是否调原生播放。
 *
 * 数据来自真实 library.db（见 make_mock.py），只有 Kotlin 那层是模拟的，
 * 而 SQL 语义已由 verify_sql.py 单独验证过。
 */
const { chromium } = require('playwright-core');
const fs = require('fs');

// Playwright 需要一个 Chromium 可执行文件：
//   npm i playwright-core && npx playwright install chromium
// 然后用环境变量指定路径，或直接改这一行
const EXE = process.env.CHROME ||
  'C:/Users/<you>/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe';
const PAGE = 'file:///D:/codebuddy/2026-10-14-21-50-02/android/app/src/main/assets/www/index.html';
const MOCK = JSON.parse(fs.readFileSync(
  'D:/codebuddy/2026-10-14-21-50-02/build_app/mock.json', 'utf8'));

(async () => {
  const b = await chromium.launch({
    executablePath: EXE, headless: true,
    args: ['--autoplay-policy=no-user-gesture-required'],
  });
  const ctx = await b.newContext({ viewport: { width: 430, height: 900 } });

  // 注入本地桥（模拟 Kotlin 的 @JavascriptInterface）
  await ctx.addInitScript(({ mock }) => {
    const calls = [];
    window.__calls = calls;
    window.MediaDBNative = {
      types: () => JSON.stringify(mock.types),
      browse: (tid, yr, od, lim, pg) => { calls.push(['browse', tid, yr, od, lim, pg]); return JSON.stringify(mock.browse); },
      search: (kw, lim) => { calls.push(['search', kw, lim]); return JSON.stringify(mock.search); },
      detail: (ids) => { calls.push(['detail', ids]); return JSON.stringify(mock.detail); },
      getProgress: (ids) => JSON.stringify(mock.progress),
      saveProgress: (j) => { calls.push(['saveProgress', j]); },
      play: (url, title, idx, total, id) => { calls.push(['play', url, title, idx, total, id]); },
      playSeries: (j, idx, t, id) => { calls.push(['playSeries', idx, t, id]); },
      stats: () => JSON.stringify({ total: 197490, types: 54 }),
      log: (m) => console.log('[native]', m),
    };
  }, { mock: MOCK });

  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message.slice(0, 120)));
  page.on('console', m => { if (m.type() === 'error') errs.push('ERR: ' + m.text().slice(0, 100)); });

  await page.goto(PAGE, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(2500);

  const r1 = await page.evaluate(() => ({
    cards: document.querySelectorAll('.card[data-i]').length,
    cats: document.querySelectorAll('.cat').length,
    title: (document.querySelector('h1') || {}).innerText || '',
    err: (document.getElementById('err') || {}).innerText || '',
    calls: window.__calls.slice(0, 4),
  }));
  console.log('① 首屏:', JSON.stringify({
    cards: r1.cards, cats: r1.cats, title: r1.title.trim(), err: r1.err,
  }));
  console.log('   桥调用:', JSON.stringify(r1.calls));

  if (!r1.cards) {
    console.log('   ✗ 未渲染卡片');
    await page.screenshot({ path: 'D:/codebuddy/2026-10-14-21-50-02/app1.png' });
    await b.close();
    return;
  }

  // 点第一张卡片
  const preDlg = await page.evaluate(() => ({
    hidden: document.getElementById('dlg').hidden,
    n: document.querySelectorAll('.card[data-i="0"]').length,
  }));
  console.log('   点击前弹窗 hidden =', preDlg.hidden, '| 卡片数 =', preDlg.n);
  await page.evaluate(() => document.querySelector('.card[data-i="0"]').click());
  await page.waitForTimeout(1800);
  const r2 = await page.evaluate(() => ({
    dlgOpen: !document.getElementById('dlg').hidden,
    name: (document.getElementById('dname') || {}).innerText || '',
    meta: (document.getElementById('dmeta') || {}).innerText || '',
    eps: document.querySelectorAll('.ep[data-idx]').length,
    calls: window.__calls.filter(c => c[0] === 'detail'),
  }));
  console.log('② 详情:', JSON.stringify({ open: r2.dlgOpen, name: r2.name, meta: r2.meta, eps: r2.eps }));
  console.log('   detail 调用:', JSON.stringify(r2.calls));

  // 点第一个集数
  if (r2.eps > 0) {
    await page.evaluate(() => document.querySelector('.ep[data-idx="0"]').click());
    await page.waitForTimeout(1500);
    const r3 = await page.evaluate(() => window.__calls.filter(
      c => c[0] === 'play' || c[0] === 'playSeries' || c[0] === 'saveProgress'));
    console.log('③ 点集数后原生调用:', JSON.stringify(r3));
  } else {
    console.log('③ 无集数按钮（该条目无播放地址）');
  }

  // 搜索
  // 先关掉详情弹窗，否则弹窗遮罩会挡住搜索框和按钮
  await page.evaluate(() => { try { closeDlg(); } catch (e) {} });
  await page.waitForTimeout(600);

  await page.fill('#q', '末日');
  await page.click('button:has-text("搜索")');
  await page.waitForTimeout(2000);
  const r4 = await page.evaluate(() => ({
    cards: document.querySelectorAll('.card[data-i]').length,
    calls: window.__calls.filter(c => c[0] === 'search'),
  }));
  console.log('④ 搜索「末日」:', r4.cards, '张卡片', JSON.stringify(r4.calls.slice(-2)));

  console.log('JS 错误:', errs.length ? errs.slice(0, 3) : '无');
  await page.screenshot({ path: 'D:/codebuddy/2026-10-14-21-50-02/app1.png' });
  await b.close();
})().catch(e => console.error('FAILED:', e.message));
