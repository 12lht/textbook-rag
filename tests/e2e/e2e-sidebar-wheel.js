const { chromium } = require('playwright-core');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8766';

async function run(browser, width, height, count) {
  const page = await browser.newPage({ viewport: { width, height } });
  await page.route('**/api/conversations', async (route) => {
    const list = [];
    for (let i = 1; i <= count; i += 1) {
      list.push({ thread_id: `t-${i}`, summary: `第 ${i} 个会话：关于天气与搜索的讨论`, message_count: i * 2 });
    }
    await route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(list) });
  });
  await page.goto(BASE + '/');
  await page.waitForTimeout(600);

  const geometry = await page.evaluate(() => {
    const list = document.querySelector('#conversationList');
    const sidebar = document.querySelector('.sidebar');
    const lr = list.getBoundingClientRect();
    return {
      listTop: Math.round(lr.top), listBottom: Math.round(lr.bottom),
      sidebarClientH: sidebar.clientHeight,
      listClientH: list.clientHeight, listScrollH: list.scrollHeight,
      overflowY: getComputedStyle(list).overflowY,
    };
  });

  const midX = 146;
  const midY = Math.round((geometry.listTop + geometry.listBottom) / 2);

  await page.mouse.move(midX, midY);
  const trail = [];
  for (let i = 0; i < 3; i += 1) {
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(180);
    trail.push(await page.evaluate(() => Math.round(document.querySelector('#conversationList').scrollTop)));
  }

  await page.mouse.move(midX, 60);
  const trailTop = [];
  for (let i = 0; i < 2; i += 1) {
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(180);
    trailTop.push(await page.evaluate(() => Math.round(document.querySelector('#conversationList').scrollTop)));
  }

  console.log(`\n--- ${width}x${height}，${count} 个会话 ---`);
  console.log(`列表可视高=${geometry.listClientH} 内容高=${geometry.listScrollH} overflowY=${geometry.overflowY}`);
  console.log(`指针在列表中间滚动(${midX},${midY}) → scrollTop 轨迹: ${trail.join(' → ')}`);
  console.log(`指针在顶部区域滚动(${midX},60)     → scrollTop 轨迹: ${trailTop.join(' → ')}`);
  const listOk = trail[trail.length - 1] > 0;
  const topIdle = trailTop[trailTop.length - 1] === trail[trail.length - 1];
  console.log(`判定：停在列表上可滚动 = ${listOk ? '是' : '否'}；停在顶部区域不接管 = ${topIdle ? '是' : '否'}`);
  await page.close();
  return listOk && topIdle;
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const results = [];
  results.push(await run(browser, 1280, 800, 40));
  results.push(await run(browser, 1280, 600, 40));
  results.push(await run(browser, 1440, 900, 80));
  await browser.close();
  console.log('\n全部通过:', results.every(Boolean));
})();
