const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8767';

async function send(page, text) {
  await page.fill('#input', text);
  await page.click('#send');
  await page.waitForFunction(() => !document.querySelector('#send').disabled, null, { timeout: 20000 });
  await page.waitForTimeout(150);
}

const snapshot = () => ({
  userMessages: [...document.querySelectorAll('.message.user .message-content')].map((n) => n.textContent),
  rewindButtons: document.querySelectorAll('.rewind').length,
  rewindDisabled: [...document.querySelectorAll('.rewind')].map((b) => b.disabled),
  inputValue: document.querySelector('#input').value,
  welcomeShown: document.querySelector('#welcome').style.display !== 'none',
  sidebarCount: document.querySelector('.conversation-count')?.textContent || '',
  hint: document.querySelector('.hint').textContent,
  hintIsError: document.querySelector('.hint').classList.contains('error'),
});

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('dialog', (dialog) => dialog.accept());

  await page.goto(BASE + '/');
  await page.waitForTimeout(400);

  console.log('--- 发送三条消息 ---');
  await send(page, '第一条消息');
  await send(page, '第二条消息');
  await send(page, '第三条消息');
  let s = await page.evaluate(snapshot);
  console.log('用户消息:', s.userMessages);
  console.log('回退按钮数量:', s.rewindButtons, '（应等于用户消息数）');
  console.log('侧边栏:', s.sidebarCount);

  await page.screenshot({ path: path.join(os.tmpdir(), 'rewind-before.png') });

  console.log('\n--- 点击第 2 条消息下的「回退到发送这条之前」 ---');
  await page.locator('.message.user').nth(1).locator('.rewind').click();
  await page.waitForFunction(() => !document.querySelector('#send').disabled, null, { timeout: 20000 });
  await page.waitForTimeout(400);
  s = await page.evaluate(snapshot);
  console.log('剩余用户消息:', s.userMessages);
  console.log('输入框内容:', JSON.stringify(s.inputValue));
  console.log('回退按钮数量:', s.rewindButtons);
  console.log('侧边栏:', s.sidebarCount);
  console.log('提示行:', JSON.stringify(s.hint), ' 标红:', s.hintIsError, '（期望回到默认输入提示）');
  await page.screenshot({ path: path.join(os.tmpdir(), 'rewind-after.png') });

  const hintRestored = s.hint === 'Enter 发送 · Shift + Enter 换行' && !s.hintIsError;
  const pass1 = s.userMessages.length === 1 && s.inputValue === '第二条消息' && s.rewindButtons === 1 && hintRestored;

  console.log('\n--- 点击剩下那条消息的回退（应清空） ---');
  await page.locator('.message.user').nth(0).locator('.rewind').click();
  await page.waitForFunction(() => !document.querySelector('#send').disabled, null, { timeout: 20000 });
  await page.waitForTimeout(400);
  s = await page.evaluate(snapshot);
  console.log('剩余用户消息:', s.userMessages, ' 输入框:', JSON.stringify(s.inputValue), ' 欢迎页显示:', s.welcomeShown, ' 回退按钮:', s.rewindButtons);
  const pass2 = s.userMessages.length === 0 && s.inputValue === '第一条消息' && s.welcomeShown && s.rewindButtons === 0;

  console.log('\n--- 回退后重新发送，验证会话仍然正常 ---');
  await send(page, '重新问一次');
  s = await page.evaluate(snapshot);
  console.log('用户消息:', s.userMessages, ' 回复存在:', s.userMessages.length === 1);
  const pass3 = s.userMessages.length === 1;

  console.log(`\n断言：截断正确=${pass1 ? 'PASS' : 'FAIL'}  清空正确=${pass2 ? 'PASS' : 'FAIL'}  回退后可继续对话=${pass3 ? 'PASS' : 'FAIL'}`);
  await browser.close();
  process.exit(pass1 && pass2 && pass3 ? 0 : 1);
})();
