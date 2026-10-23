const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8000';

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
    contentType: 'application/json', body: '[]',
  }));
  await page.route('**/api/conversationsapi/documents', (route) => route.fulfill({ contentType: 'application/json', body: '[]' }));

  await page.goto(BASE + '/');
  await page.waitForTimeout(800);

  await page.evaluate(() => selectConversation('t-cite'));
  await page.waitForTimeout(600);

  const hasBadge = await page.locator('sup.cite[data-code="c1"]').count();
  console.log('历史角标数量:', hasBadge);

  if (!hasBadge) {
    console.log('FAIL: 页面上没有角标（历史渲染或索引问题）');
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_no_badge.png'), fullPage: true });
    await browser.close();
    process.exit(1);
  }

  await page.click('sup.cite[data-code="c1"]');
  await page.waitForTimeout(300);

  const pop = await page.locator('#citePopover').count();
  console.log('小窗出现:', pop === 1);
  if (pop === 1) {
    const box = await page.locator('#citePopover').boundingBox();
    const style = await page.locator('#citePopover').evaluate((el) => {
      const cs = getComputedStyle(el);
      return { position: cs.position, zIndex: cs.zIndex, display: cs.display, visibility: cs.visibility,
               left: el.style.left, top: el.style.top, width: cs.width, height: cs.height };
    });
    console.log('小窗样式:', JSON.stringify(style));
    console.log('小窗位置(box):', JSON.stringify(box));
    const inViewport = box && box.y >= 0 && box.y < 900 && box.x >= 0 && box.x < 1280;
    const meta = await page.locator('#citePopover .source-meta').textContent();
    console.log('出处行:', meta);
    console.log('视口内可见:', inViewport);
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_visible.png') });
  } else {
    await page.screenshot({ path: path.join(os.tmpdir(), '_popover_missing.png') });
  }
  console.log('JS 错误:', errors.length ? errors.join(' | ') : '无');
  await browser.close();
})();
