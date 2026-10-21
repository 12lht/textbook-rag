// 真浏览器验证访客身份隔离（2026-10-22）。
//
// 为什么必须真浏览器：
// 1. cookie 是不是真的落地、HttpOnly 有没有生效、下次请求有没有带上——
//    curl 能验一部分，但「浏览器自己会不会带」只有浏览器说了算；
// 2. 前端两个静态依赖（marked / DOMPurify）从 CDN 换成了本地 `/static/vendor/`，
//    路径写错的表现是「页面能开、但 Markdown 变成一堆原始符号」，
//    接口测试完全看不出来；
// 3. 用两个独立 BrowserContext 等于两台机器：cookie 罐互不相通。
//
// 前置：真服务在跑（`uvicorn api:app`，端口见 BASE）。
//   NODE_PATH=C:/Users/13526/.workbuddy/binaries/node/workspace/node_modules \
//     node tests/e2e/e2e-guest-cookie.js
const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8000';

const results = [];
function check(name, ok, extra = '') {
  results.push(ok);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${extra ? '  ' + extra : ''}`);
}

async function openBrowser(browser, label) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push('console: ' + msg.text());
  });
  await page.goto(BASE, { waitUntil: 'networkidle' });
  // 页面初始化会打 /api/auth/me，等身份渲染出来再取
  await page.waitForFunction(() => {
    const el = document.getElementById('accountNote');
    return el && el.textContent.trim().length > 0;
  }, { timeout: 10000 });

  const note = (await page.textContent('#accountNote')) || '';
  const me = await page.evaluate(async () => {
    const r = await fetch('/api/auth/me');
    return r.json();
  });
  const deps = await page.evaluate(() => ({
    marked: typeof window.marked !== 'undefined' && typeof window.marked.parse === 'function',
    purify: typeof window.DOMPurify !== 'undefined' && typeof window.DOMPurify.sanitize === 'function',
  }));
  const cookies = await context.cookies(BASE);
  return { context, page, errors, note, me, deps, cookies, label };
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  try {
    console.log('\n=== 浏览器 A ===');
    const a = await openBrowser(browser, 'A');
    console.log('  身份:', a.me.user_id, '侧栏:', a.note);
    check('A 是访客', a.me.guest === true);
    check('A 的 id 带 g- 前缀', String(a.me.user_id).startsWith('g-'), a.me.user_id);
    check('A 的 cookie 落地', a.cookies.some((c) => c.name === 'aw_guest'),
      a.cookies.map((c) => c.name).join(','));
    const guestCookie = a.cookies.find((c) => c.name === 'aw_guest');
    check('cookie 是 HttpOnly', Boolean(guestCookie && guestCookie.httpOnly));
    check('cookie 带签名（有点号）', Boolean(guestCookie && guestCookie.value.includes('.')));

    console.log('\n=== 静态依赖（CDN → 本地）===');
    check('marked 已加载', a.deps.marked);
    check('DOMPurify 已加载', a.deps.purify);
    check('侧栏文案说明了隔离与过期',
      a.note.includes('仅本浏览器可见') && a.note.includes('7 天'), `got="${a.note}"`);

    // 页面加载阶段的控制台错误要**现在**判：后面会故意制造 404，
    // 那时浏览器会往 console 打 "Failed to load resource"，混在一起就分不清了。
    check('A 页面加载无 JS 错误', a.errors.length === 0, a.errors.join(' | '));

    console.log('\n=== 浏览器 B（独立 cookie 罐）===');
    const b = await openBrowser(browser, 'B');
    console.log('  身份:', b.me.user_id, '侧栏:', b.note);
    check('B 与 A 身份不同', a.me.user_id !== b.me.user_id,
      `${a.me.user_id} vs ${b.me.user_id}`);
    check('B 页面加载无 JS 错误', b.errors.length === 0, b.errors.join(' | '));

    console.log('\n=== 会话隔离（走真实前端）===');
    // A 建一个会话，B 的列表里不该出现
    const tidA = await a.page.evaluate(async () => {
      const r = await fetch('/api/conversations', { method: 'POST' });
      return (await r.json()).thread_id;
    });
    const listB = await b.page.evaluate(async () => {
      const r = await fetch('/api/conversations');
      return (await r.json()).map((x) => x.thread_id);
    });
    check('B 看不到 A 新建的会话', !listB.includes(tidA), `A=${tidA} B列表=${JSON.stringify(listB)}`);
    const statusB = await b.page.evaluate(async (tid) => {
      const r = await fetch(`/api/conversations/${tid}/messages`);
      return r.status;
    }, tidA);
    check('B 直接访问 A 的会话 → 404', statusB === 404, `status=${statusB}`);

    console.log('\n=== 控制台 ===');
    // 上面那次「B 访问 A 的会话」会故意拿到 404，浏览器必然往 console 打一条
    // "Failed to load resource"。那是**预期结果**，不是缺陷——所以这里只筛掉
    // 带 404 的那些，其余任何错误仍然要报。
    const unexpected = [...a.errors, ...b.errors].filter((e) => !e.includes('404'));
    check('除预期的 404 外没有其它 JS 错误', unexpected.length === 0, unexpected.join(' | '));

    // 清理：A 的会话删掉，别留在生产库里
    await a.page.evaluate(async (tid) => {
      await fetch(`/api/conversations/${tid}`, { method: 'DELETE' });
    }, tidA);

    await a.context.close();
    await b.context.close();
  } finally {
    await browser.close();
  }

  console.log();
  const total = results.length;
  const passed = results.filter(Boolean).length;
  console.log(`合计 ${passed}/${total} 通过`);
  process.exit(passed === total ? 0 : 1);
})().catch((err) => {
  console.error('脚本异常:', err);
  process.exit(1);
});
