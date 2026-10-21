// 会话右键菜单（重命名 / 删除）的端到端验证，走真实鼠标右键。
const http = require('http');
const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const HOST = '127.0.0.1';
const PORT = 8768;
const BASE = `http://${HOST}:${PORT}`;

const results = [];
function check(name, ok, extra = '') {
  results.push(ok);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${extra ? '  ' + extra : ''}`);
}

function request(method, pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: HOST, port: PORT, path: pathname, method }, (res) => {
      let raw = '';
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => resolve(raw ? JSON.parse(raw) : null));
    });
    req.on('error', reject);
    req.end();
  });
}

async function scenario(browser, width, height, expectClamp) {
  console.log(`\n--- ${width}x${height}${expectClamp ? '（预期触发视口收边）' : ''} ---`);
  await request('POST', '/__reset');

  const page = await browser.newPage({ viewport: { width, height } });
  const dialogs = [];
  const errors = [];
  page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.accept(); });
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto(BASE + '/');
  await page.waitForSelector('.conversation-item');

  const menu = page.locator('#conversationMenu');

  // 1. 初始状态
  check('菜单默认隐藏', await menu.isHidden());
  check('列表里没有常驻删除按钮', (await page.locator('.delete-conversation').count()) === 0);

  // 2. 右键打开菜单
  const row2 = page.locator('.conversation-item').nth(1);
  await row2.click({ button: 'right' });
  check('右键后菜单可见', await menu.isVisible());
  const items = await menu.locator('.context-menu-item').allTextContents();
  check('菜单含重命名/删除会话两项', JSON.stringify(items) === JSON.stringify(['重命名', '删除会话']), `items=${JSON.stringify(items)}`);

  // 3. 点「重命名」→ 行内输入框
  await menu.locator('[data-action="rename"]').click();
  const field = row2.locator('input.conversation-rename');
  check('该行变成行内输入框', (await field.count()) === 1);
  check('输入框预填原标题', (await field.inputValue()) === '检索最近的科技新闻', `value=${await field.inputValue()}`);
  check('输入框自动聚焦', (await page.evaluate(() => document.activeElement && document.activeElement.className)) === 'conversation-rename');
  check('点完重命名菜单收起', await menu.isHidden());

  // 4. Enter 提交
  await field.fill('AI 科技周报');
  await field.press('Enter');
  await page.waitForTimeout(350);
  let requests = await request('GET', '/__requests');
  const patch = requests.find((item) => item.method === 'PATCH');
  check('发出了 PATCH 请求', Boolean(patch), JSON.stringify(patch));
  check('PATCH 目标与请求体正确', Boolean(patch) && patch.threadId === 't-bbb22222' && patch.body.title === 'AI 科技周报', JSON.stringify(patch));
  let titles = await page.locator('.conversation-title').allTextContents();
  check('列表显示新标题', titles.includes('AI 科技周报'), JSON.stringify(titles));
  check('提交后输入框消失', (await page.locator('input.conversation-rename').count()) === 0);
  const hintAfterRename = await page.locator('.hint').textContent();
  check('重命名成功后提示行是默认输入提示', hintAfterRename === 'Enter 发送 · Shift + Enter 换行', `hint=${JSON.stringify(hintAfterRename)}`);

  // 5. Escape 取消
  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await menu.locator('[data-action="rename"]').click();
  const fieldEsc = page.locator('.conversation-item').nth(0).locator('input.conversation-rename');
  await fieldEsc.fill('不应该保存');
  await fieldEsc.press('Escape');
  await page.waitForTimeout(300);
  titles = await page.locator('.conversation-title').allTextContents();
  check('Escape 取消不改标题', titles.includes('北京今天的天气') && !titles.includes('不应该保存'), JSON.stringify(titles));

  // 6. 失焦提交
  await page.locator('.conversation-item').nth(2).click({ button: 'right' });
  await menu.locator('[data-action="rename"]').click();
  const fieldBlur = page.locator('.conversation-item').nth(2).locator('input.conversation-rename');
  await fieldBlur.fill('失焦提交的标题');
  await page.locator('.brand-name').click();
  await page.waitForTimeout(350);
  titles = await page.locator('.conversation-title').allTextContents();
  check('点别处失焦即提交', titles.includes('失焦提交的标题'), JSON.stringify(titles));

  // 7. 删除
  const before = await page.locator('.conversation-item').count();
  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await menu.locator('[data-action="delete"]').click();
  await page.waitForTimeout(350);
  const after = await page.locator('.conversation-item').count();
  check('删除后条目少一个', after === before - 1, `${before} -> ${after}`);
  check('删除前出现确认框', dialogs.some((message) => message.includes('北京今天的天气')), JSON.stringify(dialogs));
  requests = await request('GET', '/__requests');
  const deleted = requests.find((item) => item.method === 'DELETE');
  check('发出了 DELETE 请求', Boolean(deleted) && deleted.threadId === 't-aaa11111', JSON.stringify(deleted));

  // 8. 菜单的关闭方式
  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  check('菜单能再次打开', await menu.isVisible());
  await page.keyboard.press('Escape');
  check('Escape 关闭菜单', await menu.isHidden());

  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await page.locator('.brand-name').click();
  check('点别处关闭菜单', await menu.isHidden());

  // 9. 贴着视口底部右键，菜单必须收进视口内
  const last = page.locator('.conversation-item').last();
  // 先把列表滚到底，保证最后一项完整贴在列表容器底部
  await page.evaluate(() => {
    const container = document.querySelector('#conversationList');
    container.scrollTop = container.scrollHeight;
  });
  await page.waitForTimeout(150);
  const lastBox = await last.boundingBox();
  const pointX = Math.min(lastBox.x + lastBox.width - 4, width - 4);
  const pointY = Math.min(lastBox.y + lastBox.height - 4, height - 4);
  await page.mouse.click(pointX, pointY, { button: 'right' });
  const menuBox = await menu.boundingBox();
  const inViewport = Boolean(menuBox)
    && menuBox.x >= 0 && menuBox.y >= 0
    && menuBox.x + menuBox.width <= width && menuBox.y + menuBox.height <= height;
  check('菜单被收进视口内', inViewport, `右键点=(${pointX},${pointY}) 菜单=${JSON.stringify(menuBox)} 视口=${width}x${height}`);
  if (expectClamp) {
    const naiveBottom = pointY + menuBox.height;
    check('这一击确实会溢出视口（收边断言非空转）', naiveBottom > height, `不收边则底部到 ${naiveBottom}，视口高 ${height}`);
  }

  check('页面没有 JS 报错', errors.length === 0, JSON.stringify(errors));

  await page.close();
}

// 模拟「后端进程还是改 PATCH 之前启动的那个」：路径在、但方法没注册 → 405
async function scenario405(browser) {
  console.log('\n--- 405：后端没注册 PATCH ---');
  await request('POST', '/__reset');
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));

  await page.route('**/api/conversations/*', async (route) => {
    if (route.request().method() === 'PATCH') {
      await route.fulfill({
        status: 405,
        contentType: 'application/json; charset=utf-8',
        body: JSON.stringify({ detail: 'Method Not Allowed' }),
      });
      return;
    }
    await route.continue();
  });

  await page.goto(BASE + '/');
  await page.waitForSelector('.conversation-item');

  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await page.locator('#conversationMenu [data-action="rename"]').click();
  const field = page.locator('.conversation-item').nth(0).locator('input.conversation-rename');
  await field.fill('改不动的新名字');
  await field.press('Enter');
  await page.waitForTimeout(400);

  const hint = page.locator('.hint');
  const text = await hint.textContent();
  check('405 时提示行标红', await hint.evaluate((node) => node.classList.contains('error')));
  check('405 提示里点明要重启 uvicorn', text.includes('405') && text.includes('uvicorn'), `hint=${JSON.stringify(text)}`);
  const titles = await page.locator('.conversation-title').allTextContents();
  check('405 时标题保持不变', !titles.includes('改不动的新名字'), JSON.stringify(titles));
  check('输入框已收掉', (await page.locator('input.conversation-rename').count()) === 0);
  check('405 场景没有 JS 报错', errors.length === 0, JSON.stringify(errors));
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  await scenario(browser, 1280, 800, false);
  await scenario(browser, 1280, 360, true);   // 矮视口，专门压菜单的视口收边
  await scenario405(browser);                 // 后端没注册 PATCH 的样子
  await browser.close();
  console.log(`\n合计 ${results.filter(Boolean).length}/${results.length} 通过`);
  process.exit(results.every(Boolean) ? 0 : 1);
})();
