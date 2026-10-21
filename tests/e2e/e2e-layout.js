const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8766';

const probe = () => {
  const chat = document.querySelector('#chat') || document.querySelector('.chat');
  const sidebar = document.querySelector('.sidebar');
  const composer = document.querySelector('.composer');
  const scroller = document.scrollingElement;
  return {
    pageScrollTop: Math.round(scroller.scrollTop),
    docOverflow: Math.round(document.documentElement.scrollHeight - window.innerHeight),
    sidebarTop: Math.round(sidebar.getBoundingClientRect().top),
    composerBottom: Math.round(composer.getBoundingClientRect().bottom),
    composerVisible: composer.getBoundingClientRect().bottom <= window.innerHeight + 1,
    chatGap: Math.round(chat.scrollHeight - chat.scrollTop - chat.clientHeight),
    autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
    streaming: document.querySelector('#send').disabled,
  };
};

async function run(page, path, label) {
  await page.goto(BASE + path);
  await page.fill('#input', '请生成一段较长的回复');
  await page.click('#send');

  const chatLen = () => {
    const c = document.querySelector('#chat') || document.querySelector('.chat');
    return c.scrollHeight;
  };
  await page.waitForFunction(`(${chatLen.toString()})() > window.innerHeight + 1500`, null, { timeout: 25000 });
  await page.waitForTimeout(700);
  const p1 = await page.evaluate(probe);

  // 模拟用户往上滚去看前面的内容
  await page.evaluate(() => {
    const c = document.querySelector('#chat') || document.querySelector('.chat');
    c.scrollTop = 0;
  });
  const hold = await page.evaluate(async () => {
    const out = [];
    for (let i = 0; i < 8; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
      const s = document.querySelector('.sidebar');
      const c = document.querySelector('#chat') || document.querySelector('.chat');
      out.push({
        y: Math.round(document.scrollingElement.scrollTop),
        chatTop: Math.round(c.scrollTop),
        sidebarTop: Math.round(s.getBoundingClientRect().top),
        autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
        streaming: document.querySelector('#send').disabled,
      });
    }
    return out;
  });

  // 再把消息区拖到底部
  await page.evaluate(() => {
    const c = document.querySelector('#chat') || document.querySelector('.chat');
    c.scrollTop = c.scrollHeight;
  });
  const trail = await page.evaluate(async () => {
    const out = [];
    for (let i = 0; i < 12; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
      const c = document.querySelector('#chat') || document.querySelector('.chat');
      out.push({
        gap: Math.round(c.scrollHeight - c.scrollTop - c.clientHeight),
        sidebarTop: Math.round(document.querySelector('.sidebar').getBoundingClientRect().top),
        y: Math.round(document.scrollingElement.scrollTop),
        autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
        streaming: document.querySelector('#send').disabled,
      });
    }
    return out;
  });

  const p2 = await page.evaluate(probe);
  await page.screenshot({ path: `D:/agent/.workbuddy/tmp/shot-${path === '/' ? 'fixed' : 'original'}.png` });
  return { label, p1, p2, hold, trail };
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  const rows = [];
  rows.push(await run(page, '/original', '修复前（旧布局）'));
  rows.push(await run(page, '/', '修复后（新布局）'));
  await browser.close();

  for (const r of rows) {
    console.log(`\n===== ${r.label} =====`);
    console.log(`输出中：页面滚动位置=${r.p1.pageScrollTop}px  文档可滚动高度=${r.p1.docOverflow}px  侧边栏顶部=${r.p1.sidebarTop}px  输入框可见=${r.p1.composerVisible}`);
    console.log(`上滚后停留 8 次采样：y=${r.hold.map((s) => s.y).join(',')}  侧边栏顶部=${r.hold.map((s) => s.sidebarTop).join(',')}  autoFollow=${r.hold[7].autoFollow}`);
    console.log(`拖到底部后 .chat 距底：${r.trail.map((s) => s.gap).join(' ')}`);
    console.log(`   同时页面滚动位置=${r.trail.map((s) => s.y).join(',')}  侧边栏顶部=${r.trail.map((s) => s.sidebarTop).join(',')}`);
    console.log(`   仍在输出=${r.trail.map((s) => (s.streaming ? 'Y' : 'n')).join('')}`);
    console.log(`=> 侧边栏是否跟随页面滚动: ${Math.max(...r.trail.map((s) => Math.abs(s.sidebarTop))) > 1 ? '是（会滑走）' : '否（固定）'}`);
    console.log(`=> 拖到底部后是否恢复跟随: ${r.trail[r.trail.length - 1].gap < 150 ? '是' : '否'}`);
  }
})();
