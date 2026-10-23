const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8767';

const hintText = () => document.querySelector('.hint').textContent;
const hintIsError = () => document.querySelector('.hint').classList.contains('error');

async function send(page, text) {
  await page.fill('#input', text);
  await page.click('#send');
  await page.waitForFunction(() => !document.querySelector('#send').disabled, null, { timeout: 20000 });
  await page.waitForTimeout(150);
}

async function reset(page) {
  await page.click('#newChat');
  await page.waitForTimeout(400);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('dialog', (dialog) => dialog.accept());

  await page.route('**/rewind', (route) => route.fulfill({
    status: 404,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify({ detail: 'Not Found' }),
  }));
  await page.goto(BASE + '/');
  await page.waitForTimeout(400);
  await reset(page);
  await send(page, '第一条');
  await send(page, '第二条');
  console.log('[A] 点击回退前 hint:', JSON.stringify(await page.evaluate(hintText)));
  await page.locator('.message.user').nth(1).locator('.rewind').click();
  await page.waitForTimeout(700);
  const errText = await page.evaluate(hintText);
  const errFlag = await page.evaluate(hintIsError);
  const stillThere = await page.evaluate(() => document.querySelectorAll('.message.user').length);
  console.log('[A] 404 时 hint:', JSON.stringify(errText), ' 标红:', errFlag, ' 消息仍在:', stillThere);
  await page.screenshot({ path: path.join(os.tmpdir(), 'rewind-404.png') });
  const passA = errText.includes('404') && errText.includes('重启') && errFlag && stillThere === 2;

  await page.unroute('**/rewind');
  await page.reload();
  await page.waitForTimeout(400);
  await reset(page);
  await send(page, '第一条');
  await send(page, '第二条');
  await page.locator('.message.user').nth(1).locator('.rewind').click();
  await page.waitForFunction(() => !document.querySelector('#send').disabled, null, { timeout: 20000 });
  await page.waitForTimeout(400);
  const okText = await page.evaluate(hintText);
  const okFlag = await page.evaluate(hintIsError);
  const remain = await page.evaluate(() => document.querySelectorAll('.message.user').length);
  const inputValue = await page.evaluate(() => document.querySelector('#input').value);
  console.log('[B] 成功后 hint:', JSON.stringify(okText), ' 标红:', okFlag, ' 剩余用户消息:', remain, ' 输入框:', JSON.stringify(inputValue));
  const passB = okText.includes('已回退') && !okFlag && remain === 1 && inputValue === '第二条';

  console.log(`\n断言：404 有可见提示=${passA ? 'PASS' : 'FAIL'}   正常回退提示正确=${passB ? 'PASS' : 'FAIL'}`);
  await browser.close();
  process.exit(passA && passB ? 0 : 1);
})();
