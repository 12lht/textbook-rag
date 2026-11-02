// 藏书阁的移动端抽屉：390px 下分类栏默认藏在屏外，点汉堡滑入。
//
// 藏书阁有登录门——`/api/auth/me` 返回游客时整个页面只剩一个登录提示，
// 分类栏根本不渲染。所以这里用 page.route 把身份拦成已登录的普通用户，
// 并把文档/分类接口给空数组，测试只关心布局，不关心数据。
const { chromium } = require('playwright-core');
const CHROME = require('./_browser');

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8766';

let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });

  await mobile.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true, is_owner: true }),
  }));
  await mobile.route('**/api/documents**', (route) => route.fulfill({ contentType: 'application/json', body: '[]' }));
  await mobile.route('**/api/categories**', (route) => route.fulfill({ contentType: 'application/json', body: '[]' }));

  await mobile.goto(BASE + '/library');
  await mobile.waitForSelector('.cats', { state: 'attached' });
  const gateHidden = await mobile.locator('#gate').isHidden();
  check('登录门已放行', gateHidden);
  const cats = mobile.locator('.cats');
  let box = await cats.boundingBox();
  check('手机上分类栏默认藏屏外', box && box.x < 0, `x=${box && Math.round(box.x)}`);
  check('汉堡按钮可见', await mobile.locator('#menuBtn').isVisible());
  const docsWidth = await mobile.locator('.docs').evaluate((el) => el.getBoundingClientRect().width);
  check('文档区占满屏宽', Math.round(docsWidth) >= 370, `w=${Math.round(docsWidth)}`);
  await mobile.click('#menuBtn');
  await mobile.waitForTimeout(350);
  box = await cats.boundingBox();
  check('点汉堡滑入', box && box.x === 0, `x=${box && Math.round(box.x)}`);
  await browser.close();
  console.log('结果:', ok ? '通过' : '有失败项');
  process.exit(ok ? 0 : 1);
})();
