const { chromium } = require('playwright-core');
const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto('http://127.0.0.1:8766/');
  await page.waitForSelector('.sidebar');
  await page.click('#menuBtn');
  await page.waitForTimeout(350);
  await page.click('#accountAction');
  await page.waitForTimeout(300);
  const modalVisible = await page.locator('#authModal').isVisible().catch(() => false);
  // 兜底：找当前打开的 modal（可能有多个）
  const openModal = await page.evaluate(() => {
    const modal = [...document.querySelectorAll('.modal')].find((m) => !m.hidden);
    if (!modal) return null;
    const card = modal.querySelector('.modal-card') || modal.firstElementChild;
    const rect = card ? card.getBoundingClientRect() : modal.getBoundingClientRect();
    const center = document.elementFromPoint(
      Math.round(rect.left + rect.width / 2), Math.round(rect.top + rect.height / 2)
    );
    return { hidden: modal.hidden, z: getComputedStyle(modal).zIndex, centerInModal: center ? !!center.closest('.modal') : false };
  });
  check('登录弹窗(authModal)已打开', modalVisible || (openModal && !openModal.hidden));
  check('弹窗 z-index 高于侧栏(90)', openModal && Number(openModal.z) > 90, `z=${openModal && openModal.z}`);
  check('弹窗中心点命中的是弹窗自己（没被侧栏挡住）', openModal && openModal.centerInModal);
  await browser.close();
  console.log('结果:', ok ? '通过' : '有失败项');
  process.exit(ok ? 0 : 1);
})();
