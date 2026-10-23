// 移动端抽屉侧栏 e2e：手机视口下侧栏默认藏起、汉堡按钮唤出、选中会话自动收起；桌面视口不受影响。
const { chromium } = require('playwright-core');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8766';

let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });

  // --- 手机视口 ---
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mobile.goto(BASE + '/');
  await mobile.waitForSelector('.sidebar');
  const sidebar = mobile.locator('.sidebar');

  let box = await sidebar.boundingBox();
  check('手机上侧栏默认藏在屏幕外', !box || box.x < 0 || box.x >= 390, `x=${box && Math.round(box.x)}`);
  check('汉堡按钮可见', await mobile.locator('#menuBtn').isVisible());

  await mobile.click('#menuBtn');
  await mobile.waitForTimeout(350);
  box = await sidebar.boundingBox();
  check('点汉堡后侧栏滑入', box && box.x === 0 && box.width > 0, `x=${box && Math.round(box.x)}`);
  check('遮罩出现', await mobile.locator('#sidebarBackdrop').evaluate((el) => el.classList.contains('show')));

  await mobile.click('#sidebarBackdrop', { position: { x: 370, y: 400 }, force: true });
  await mobile.waitForTimeout(350);
  box = await sidebar.boundingBox();
  check('点遮罩收起', box && box.x < 0, `x=${box && Math.round(box.x)}`);

  await mobile.click('#menuBtn');
  await mobile.waitForTimeout(350);
  await mobile.click('#newChat');
  await mobile.waitForTimeout(600);
  box = await sidebar.boundingBox();
  check('新建会话后自动收起', box && box.x < 0, `x=${box && Math.round(box.x)}`);
  await mobile.close();

  // --- 桌面视口不受影响 ---
  const desktop = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await desktop.goto(BASE + '/');
  await desktop.waitForSelector('.sidebar');
  box = await desktop.locator('.sidebar').boundingBox();
  check('桌面侧栏仍在左侧', box && box.x === 0 && box.width === 292, `x=${box && Math.round(box.x)} w=${box && Math.round(box.width)}`);
  check('桌面汉堡按钮隐藏', !(await desktop.locator('#menuBtn').isVisible()));
  await desktop.close();

  await browser.close();
  console.log();
  console.log('结果:', ok ? '通过' : '有失败项');
  process.exit(ok ? 0 : 1);
})();
