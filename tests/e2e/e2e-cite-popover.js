// 引用角标小窗：端到端验证"历史里带 citations 的回答，角标能点出原文小窗"。
//
// 历史 API 用 page.route 拦截，所以这个测试不依赖真实后端——只要有一个
// 能把页面送出来的服务就行（mock-server 或 uvicorn，用 E2E_BASE 指定）。
// 之前它对着 8000 端口的真后端跑，历史接口没人 mock，永远 0 个角标，
// 整个测试就只剩一条"没有角标"的失败。
const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8766';

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? `  ${detail}` : ''}`);
  if (!ok) failures += 1;
};

// renderHistory 期望的形态：role / content / operations / citations（见 messages.js）。
const HISTORY = [
  { role: 'user', content: '讲讲分集接收' },
  {
    role: 'assistant',
    content: '书里用 [c1] 讲了 MRC。\n\n![图 3-4 MRC](/api/assets/as_1f3a?u=u-1&t=9999999999.sig)',
    operations: [
      { kind: 'call', tool: 'search_textbook', args: { query: '分集接收' } },
      { kind: 'result', tool: 'search_textbook', status: 'success' },
    ],
    citations: [{
      code: 'c1', doc_title: '移动通信', chapter: '第3章 分集接收',
      page_start: 45, page_end: 46, kind: 'figure',
      asset_id: 'as_1f3a', asset_url: '/api/assets/as_1f3a?u=u-1&t=9999999999.sig',
      content: '![图 3-4 MRC](/api/assets/as_1f3a?u=u-1&t=9999999999.sig)\n最大比值合并的示意图。',
    }],
  },
];

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push('console: ' + msg.text()); });

  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true }),
  }));
  await page.route('**/api/conversations', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify([{ thread_id: 't-cite', summary: '分集接收', message_count: 2 }]),
  }));
  await page.route('**/api/conversations/t-cite/messages', (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ thread_id: 't-cite', messages: HISTORY }),
  }));
  await page.route('**/api/documents', (route) => route.fulfill({ contentType: 'application/json', body: '[]' }));
  await page.route('**/api/capabilities', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ reasoning_toggleable: true, web_search_available: true }),
  }));
  // 角标的 citation 里带图，不补一张的话控制台会挂 404，
  // 而这条 404 会把「没有 JS 报错」这条断言变成假失败。
  const TINY_PNG = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
    'base64',
  );
  await page.route('**/api/assets/**', (route) => route.fulfill({ contentType: 'image/png', body: TINY_PNG }));

  await page.goto(BASE + '/');
  await page.waitForTimeout(800);

  await page.evaluate(() => selectConversation('t-cite'));
  await page.waitForTimeout(600);

  const badges = await page.locator('sup.cite[data-code="c1"]').count();
  check('历史里的引用角标渲染出来', badges === 1, `${badges} 个`);
  if (!badges) {
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_no_badge.png'), fullPage: true });
    await browser.close();
    process.exit(1);
  }

  await page.click('sup.cite[data-code="c1"]');
  await page.waitForTimeout(300);

  const pop = await page.locator('#citePopover').count();
  check('点角标后小窗出现', pop === 1);
  if (pop === 1) {
    const box = await page.locator('#citePopover').boundingBox();
    const meta = await page.locator('#citePopover .source-meta').textContent();
    const inViewport = box && box.y >= 0 && box.y < 900 && box.x >= 0 && box.x < 1280;
    check('小窗在视口内', inViewport, box ? JSON.stringify(box) : 'null');
    check('出处行带书名与页码', /移动通信/.test(meta) && /4[56]/.test(meta), JSON.stringify(meta));
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_visible.png') });
  } else {
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_missing.png') });
  }
  check('没有 JS 报错', errors.length === 0, errors.slice(0, 3).join(' | '));

  await browser.close();
  console.log(failures ? `\n${failures} 项失败` : '\n全部通过');
  process.exit(failures ? 1 : 0);
})();
