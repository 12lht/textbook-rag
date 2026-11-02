const http = require('http');
const { chromium } = require('playwright-core');

const CHROME = require('./_browser');
const HOST = '127.0.0.1';
const PORT = 8768;
const BASE = `http://${HOST}:${PORT}`;

const results = [];
function check(name, ok, extra = '') {
  results.push(ok);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${extra ? '  ' + extra : ''}`);
}

function request(method, pathname, body = null) {
  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const headers = payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {};
    const req = http.request({ host: HOST, port: PORT, path: pathname, method, headers }, (res) => {
      let raw = '';
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => resolve(raw ? JSON.parse(raw) : null));
    });
    req.on('error', reject);
    req.end(payload || undefined);
  });
}

async function scenario(browser, width, height) {
  console.log(`\n--- ${width}x${height} ---`);
  await request('POST', '/__reset');

  const page = await browser.newPage({ viewport: { width, height } });
  const dialogs = [];
  const errors = [];
  page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.accept(); });
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto(BASE + '/');
  await page.waitForSelector('.conversation-item');

  const menu = page.locator('#conversationMenu');

  check('菜单默认隐藏', await menu.isHidden());
  check('列表里没有常驻删除按钮', (await page.locator('.delete-conversation').count()) === 0);

  const row2 = page.locator('.conversation-item').nth(1);
  await row2.click({ button: 'right' });
  check('右键后菜单可见', await menu.isVisible());
  const items = await menu.locator('.context-menu-item').allTextContents();
  check('菜单含重命名/删除会话两项', JSON.stringify(items) === JSON.stringify(['重命名', '删除会话']), `items=${JSON.stringify(items)}`);

  await menu.locator('[data-action="rename"]').click();
  const field = row2.locator('input.conversation-rename');
  check('该行变成行内输入框', (await field.count()) === 1);
  check('输入框预填原标题', (await field.inputValue()) === '检索最近的科技新闻', `value=${await field.inputValue()}`);
  check('输入框自动聚焦', (await page.evaluate(() => document.activeElement && document.activeElement.className)) === 'conversation-rename');
  check('点完重命名菜单收起', await menu.isHidden());

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

  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await menu.locator('[data-action="rename"]').click();
  const fieldEsc = page.locator('.conversation-item').nth(0).locator('input.conversation-rename');
  await fieldEsc.fill('不应该保存');
  await fieldEsc.press('Escape');
  await page.waitForTimeout(300);
  titles = await page.locator('.conversation-title').allTextContents();
  check('Escape 取消不改标题', titles.includes('北京今天的天气') && !titles.includes('不应该保存'), JSON.stringify(titles));

  await page.locator('.conversation-item').nth(2).click({ button: 'right' });
  await menu.locator('[data-action="rename"]').click();
  const fieldBlur = page.locator('.conversation-item').nth(2).locator('input.conversation-rename');
  await fieldBlur.fill('失焦提交的标题');
  await page.locator('.brand-name').click();
  await page.waitForTimeout(350);
  titles = await page.locator('.conversation-title').allTextContents();
  check('点别处失焦即提交', titles.includes('失焦提交的标题'), JSON.stringify(titles));

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

  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  check('菜单能再次打开', await menu.isVisible());
  await page.keyboard.press('Escape');
  check('Escape 关闭菜单', await menu.isHidden());

  await page.locator('.conversation-item').nth(0).click({ button: 'right' });
  await page.locator('.brand-name').click();
  check('点别处关闭菜单', await menu.isHidden());

  const last = page.locator('.conversation-item').last();
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

  check('页面没有 JS 报错', errors.length === 0, JSON.stringify(errors));

  await page.close();
}

// 视口收边：单独立一个场景。
// 要证明「收边逻辑真的起作用」，前置条件是这一击本来会溢出屏幕——
// 而 3 条会话的列表离视口底部还差一截，右键最后一行根本溢不出来，
// 断言等于空转。所以这里先把列表播种到撑满侧栏，再在矮视口里右键最后一行。
async function scenarioClamp(browser) {
  console.log('\n--- 视口收边：长列表 + 矮视口 ---');
  await request('POST', '/__seed', { count: 30 });

  const width = 1280;
  // 320 = iPhone SE 横屏的高度。选这个值的依据是实测：菜单自然高约 82px，
  // 列表底在 ~278px，360px 的视口差 4px 收不住，320px 才能让「不收边就溢出」成立。
  const height = 320;
  const page = await browser.newPage({ viewport: { width, height } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto(BASE + '/');
  await page.waitForSelector('.conversation-item');

  const menu = page.locator('#conversationMenu');
  await page.evaluate(() => {
    const container = document.querySelector('#conversationList');
    container.scrollTop = container.scrollHeight;
  });
  await page.waitForTimeout(150);

  const empty = await page.evaluate(() => document.querySelector('#conversationMenu').hidden);
  check('菜单默认隐藏', empty);

  const last = page.locator('.conversation-item').last();
  const lastBox = await last.boundingBox();
  check('拿得到最后一行的位置', Boolean(lastBox), lastBox ? `y=${Math.round(lastBox.y)}..${Math.round(lastBox.y + lastBox.height)} 视口高 ${height}` : 'null');

  const pointX = Math.min(lastBox.x + lastBox.width - 4, width - 4);
  const pointY = Math.min(lastBox.y + lastBox.height - 4, height - 4);
  // 行可能有一部分在折叠线以下，但只要被压回视口内的那个点仍落在行上，
  // 右键就是一次真实的右键，而不是点在空白处。
  const onRow = await page.evaluate(({ x, y }) => {
    const el = document.elementFromPoint(x, y);
    return Boolean(el && el.closest('.conversation-item'));
  }, { x: pointX, y: pointY });
  check('点击点落在会话行上', onRow, `点=(${Math.round(pointX)},${Math.round(pointY)}) 行=${JSON.stringify(lastBox)}`);
  await page.mouse.click(pointX, pointY, { button: 'right' });
  await page.waitForTimeout(150);

  const menuBox = await menu.boundingBox();
  const inViewport = Boolean(menuBox)
    && menuBox.x >= 0 && menuBox.y >= 0
    && menuBox.x + menuBox.width <= width && menuBox.y + menuBox.height <= height;
  check('菜单被收进视口内', inViewport, `右键点=(${pointX},${Math.round(pointY)}) 菜单=${JSON.stringify(menuBox)} 视口=${width}x${height}`);

  const naiveBottom = pointY + menuBox.height;
  check('这一击确实会溢出视口（收边断言非空转）', naiveBottom > height,
    `不收边则底部到 ${Math.round(naiveBottom)}，视口高 ${height}`);

  check('页面没有 JS 报错', errors.length === 0, JSON.stringify(errors));
  await page.close();
}

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
  await scenario(browser, 1280, 800);
  await scenario(browser, 1280, 360);   // 矮视口下整条流程仍要正常
  await scenarioClamp(browser);         // 收边单独验证，不受列表长度左右
  await scenario405(browser);           // 后端没注册 PATCH 的样子
  await browser.close();
  console.log(`\n合计 ${results.filter(Boolean).length}/${results.length} 通过`);
  process.exit(results.every(Boolean) ? 0 : 1);
})();
