const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8766';

async function snapshot(page) {
  return page.evaluate(() => {
    const s = document.querySelector('.sidebar');
    const c = document.querySelector('#chat') || document.querySelector('.chat');
    const composer = document.querySelector('.composer');
    const brand = document.querySelector('.brand');
    return {
      pageScrollTop: Math.round(document.scrollingElement.scrollTop),
      sidebarTop: Math.round(s.getBoundingClientRect().top),
      brandVisible: brand.getBoundingClientRect().bottom > 0,
      newChatVisible: document.querySelector('#newChat').getBoundingClientRect().bottom > 0,
      chatScrollTop: Math.round(c.scrollTop),
      composerVisible: composer.getBoundingClientRect().bottom <= window.innerHeight + 1
        && composer.getBoundingClientRect().top >= -1,
    };
  });
}

async function run(page, path, label) {
  await page.goto(BASE + path);
  await page.fill('#input', '请生成一段较长的回复');
  await page.click('#send');

  const chatLen = () => document.querySelector('#chat') ? document.querySelector('#chat').scrollHeight : document.querySelector('.chat').scrollHeight;
  await page.waitForFunction(`(${chatLen.toString()})() > window.innerHeight + 1500`, null, { timeout: 25000 });
  await page.waitForTimeout(600);

  const before = await snapshot(page);

  await page.mouse.move(640, 420);
  const steps = [];
  for (let i = 0; i < 5; i += 1) {
    await page.mouse.wheel(0, 500);
    await page.waitForTimeout(150);
    steps.push(await snapshot(page));
  }

  return { label, before, steps };
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  const rows = [];
  rows.push(await run(page, '/original', '旧布局'));
  rows.push(await run(page, '/', '新布局'));
  await browser.close();

  for (const r of rows) {
    console.log(`\n===== ${r.label} =====`);
    console.log(`滚动前：页面滚动=${r.before.pageScrollTop}  侧边栏顶部=${r.before.sidebarTop}  消息区滚动=${r.before.chatScrollTop}  输入框可见=${r.before.composerVisible}`);
    console.log('滚轮下滚 5 次，每次 500px：');
    r.steps.forEach((s, i) => {
      console.log(`  ${i + 1}) 页面滚动=${String(s.pageScrollTop).padStart(5)}  侧边栏顶部=${String(s.sidebarTop).padStart(5)}  消息区滚动=${String(s.chatScrollTop).padStart(5)}  能看见"新建会话"=${s.newChatVisible ? 'Y' : 'n'}  输入框可见=${s.composerVisible ? 'Y' : 'n'}`);
    });
  }
})();
