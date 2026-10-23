const { chromium } = require('playwright-core');
const CHROME = require('./_browser');
let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mobile.goto('http://127.0.0.1:8766/library');
  await mobile.waitForSelector('.cats', { state: 'attached' });
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
