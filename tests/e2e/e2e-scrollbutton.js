const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8766';

const probe = () => {
  const chat = document.querySelector('#chat');
  const button = document.querySelector('#scrollBottom');
  const rect = button.getBoundingClientRect();
  const composer = document.querySelector('.composer').getBoundingClientRect();
  return {
    visible: button.classList.contains('visible') && getComputedStyle(button).display !== 'none',
    gap: Math.round(chat.scrollHeight - chat.scrollTop - chat.clientHeight),
    autoFollow: state.autoFollow,
    streaming: document.querySelector('#send').disabled,
    buttonAboveComposer: Math.round(rect.bottom) <= Math.round(composer.top) + 1,
    buttonInViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
  };
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  await page.goto(BASE + '/');
  await page.fill('#input', '请生成一段较长的回复');
  await page.click('#send');
  await page.waitForFunction(() => document.querySelector('#chat').scrollHeight > window.innerHeight + 1500, null, { timeout: 25000 });
  await page.waitForTimeout(700);

  const following = await page.evaluate(probe);
  console.log('1) 流式输出中且跟随底部 → 按钮可见:', following.visible, ' 距底:', following.gap + 'px', ' (期望 按钮隐藏、距底≈0)');

  await page.evaluate(() => { document.querySelector('#chat').scrollTop = 0; });
  await page.waitForTimeout(400);
  const scrolledUp = await page.evaluate(probe);
  console.log('2) 用户上滚到顶部     → 按钮可见:', scrolledUp.visible, ' 距底:', scrolledUp.gap + 'px', ' autoFollow:', scrolledUp.autoFollow);
  console.log('   按钮位置合理:', scrolledUp.buttonInViewport && scrolledUp.buttonAboveComposer, '（在视口内且在输入框上方）');
  await page.screenshot({ path: 'D:/agent/.workbuddy/tmp/scrollbtn-shown.png' });

  await page.click('#scrollBottom');
  await page.waitForTimeout(400);
  const clicked = await page.evaluate(probe);
  console.log('3) 点击回到底部       → 按钮可见:', clicked.visible, ' 距底:', clicked.gap + 'px', ' autoFollow:', clicked.autoFollow, ' (期望 按钮隐藏、距底≈0、autoFollow=true)');

  await page.waitForTimeout(1400);
  const after = await page.evaluate(probe);
  console.log('4) 继续输出 1.4s 后   → 按钮可见:', after.visible, ' 距底:', after.gap + 'px', ' 仍在输出:', after.streaming, ' (期望 持续贴底)');

  const pass = !following.visible && scrolledUp.visible && !clicked.visible
    && clicked.gap < 5 && clicked.autoFollow === true && after.gap < 5;
  console.log('\n断言：', pass ? 'PASS' : 'FAIL');
  await browser.close();
  process.exit(pass ? 0 : 1);
})();
