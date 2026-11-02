// 前端模块化的连通性检查。
//
// 拆分 app.js 之后，坏了不会报语法错——页面照常显示，只是某个按钮点了没反应，
// 或者某个面板打不开。这个脚本盯着三件最容易坏的事：
//   1. 模块图是否完整加载（少一个文件、路径写错，浏览器只在控制台报 404）
//   2. 全局出口是否还在（e2e 读 window.state / window.addMessage）
//   3. 控制台有没有报错（缺 import 的调用点第一次被点到才炸，这里提前点一遍）
//
// 需要 mock server：node tests/e2e/mock-server.js
const { chromium } = require('playwright-core');
const path = require('path');

const CHROME = require('./_browser');

const BASE = 'http://127.0.0.1:8766';
const EXPECTED_MODULES = 15;

let failures = 0;
const check = (label, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? `  ${detail}` : ''}`);
  if (!ok) failures += 1;
};

async function run() {
  const browser = await chromium.launch({ executablePath: CHROME });

  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));

    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);

    // 1) 模块图
    const loaded = await page.evaluate(() => performance
      .getEntriesByType('resource')
      .map((r) => r.name)
      .filter((n) => n.includes('/static/app/'))
      .map((n) => n.split('/static/app/')[1]));
    check('模块全部加载', loaded.length >= EXPECTED_MODULES, `${loaded.length} 个`);
    check('入口 app.js 被请求', await page.evaluate(() =>
      performance.getEntriesByType('resource').some((r) => r.name.endsWith('/static/app.js'))));

    // 2) 全局出口（e2e 的契约）
    const surface = await page.evaluate(() => ({
      hasState: Boolean(window.state) && typeof window.state === 'object',
      autoFollow: window.state && window.state.autoFollow,
      threadId: window.state && window.state.threadId,
      addMessage: typeof window.addMessage,
    }));
    check('window.state 可用', surface.hasState);
    check('state 初值正确', surface.autoFollow === true && surface.threadId === null);
    check('window.addMessage 可用', surface.addMessage === 'function');

    // 3) 脚本真的跑到了最后的初始化（欢迎区、提示行、会话列表）
    const ui = await page.evaluate(() => ({
      welcome: !document.querySelector('#welcome').hidden,
      hint: document.querySelector('.hint').textContent.trim(),
      conversations: document.querySelectorAll('#conversationList > *').length,
      sendDisabled: document.querySelector('#send').disabled,
    }));
    check('欢迎区可见', ui.welcome);
    check('提示行已由脚本填上', ui.hint.length > 0, ui.hint);
    check('会话列表已渲染', ui.conversations > 0, `${ui.conversations} 条`);

    // 4) 各模块的绑定：逐个点一遍，缺 import 的会在这里抛 TypeError
    await page.click('#attachButton');
    await page.waitForTimeout(200);
    check('附件菜单能打开', await page.evaluate(() => !document.querySelector('#attachMenu').hidden));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    check('Escape 能关掉附件菜单', await page.evaluate(() => document.querySelector('#attachMenu').hidden));

    const before = await page.evaluate(() => localStorage.getItem('agent.toggles'));
    await page.click('#toggleWeb');
    await page.waitForTimeout(200);
    const afterToggle = await page.evaluate(() => localStorage.getItem('agent.toggles'));
    check('开关写入 localStorage', before !== afterToggle, `${before} -> ${afterToggle}`);

    // auth.js 的 applyIdentity 跑过之后，游客态的界面才是对的：
    // 账号区有文案、记忆与邀请入口都不显示。
    const guest = await page.evaluate(() => ({
      account: document.querySelector('#accountName').textContent.trim(),
      action: document.querySelector('#accountAction').textContent.trim(),
      memoryHidden: document.querySelector('#memoryEntry').hidden,
      inviteHidden: document.querySelector('#inviteEntry').hidden,
    }));
    check('身份已应用（账号区有文案）', guest.account.length > 0, `${guest.account} / ${guest.action}`);
    check('游客看不到记忆入口', guest.memoryHidden);
    check('游客看不到邀请入口', guest.inviteHidden);

    // 5) 发送与流式（composer + messages + render 三条链路一起过）
    await page.fill('#input', '请生成一段较长的回复');
    await page.click('#send');
    await page.waitForFunction(() => window.state.busy === true, null, { timeout: 8000 });
    check('发送后进入 busy 态', true);

    await page.waitForFunction(() => window.state.busy === false, null, { timeout: 25000 });
    const done = await page.evaluate(() => ({
      threadId: window.state.threadId,
      messages: document.querySelectorAll('#messages > *').length,
    }));
    check('流结束后退出 busy', true);
    check('会话 id 已落地', typeof done.threadId === 'string' && done.threadId.length > 0, done.threadId);
    check('消息已进入列表', done.messages > 0, `${done.messages} 条`);

    // 6) 移动端抽屉（layout.js）
    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await mobile.goto(`${BASE}/`, { waitUntil: 'networkidle' });
    await mobile.waitForTimeout(400);
    const drawer = await mobile.evaluate(async () => {
      const sidebar = document.querySelector('.sidebar');
      const beforeOpen = sidebar.classList.contains('open');
      document.querySelector('#menuBtn').click();
      await new Promise((r) => setTimeout(r, 350));
      const afterOpen = sidebar.classList.contains('open');
      document.querySelector('#sidebarBackdrop').click();
      await new Promise((r) => setTimeout(r, 350));
      return { beforeOpen, afterOpen, afterClose: sidebar.classList.contains('open') };
    });
    check('抽屉能滑出', drawer.beforeOpen === false && drawer.afterOpen === true);
    check('点遮罩能收起', drawer.afterClose === false);

    check('没有控制台报错', errors.length === 0, errors.slice(0, 3).join(' | '));
  } finally {
    await browser.close();
  }

  console.log(failures ? `\n合计 ${failures} 项失败` : '\n全部通过');
  process.exit(failures ? 1 : 0);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
