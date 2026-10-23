const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8766';

async function run(browser, width, height, count, label) {
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

  const before = await page.evaluate(() => {
    const list = document.querySelector('#conversationList');
    const items = [...document.querySelectorAll('.conversation-item')];
    const btn = document.querySelector('#newChat');
    return {
      listClientH: list.clientHeight,
      listScrollH: list.scrollHeight,
      itemH: items.length ? Math.round(items[0].getBoundingClientRect().height) : 0,
      sectionsTotal: items.length ? Math.round(items.reduce((a, it) => a + it.getBoundingClientRect().height, 0)) : 0,
      btnTop: Math.round(btn.getBoundingClientRect().top),
      btnVisible: btn.getBoundingClientRect().top >= 0 && btn.getBoundingClientRect().bottom <= window.innerHeight,
      scrollbar: list.scrollHeight > list.clientHeight + 1,
    };
  });

  const after = await page.evaluate(() => {
    const list = document.querySelector('#conversationList');
    list.scrollTop = list.scrollHeight;
    const btn = document.querySelector('#newChat');
    const brand = document.querySelector('.brand');
    const items = [...document.querySelectorAll('.conversation-item')];
    const last = items[items.length - 1];
    const lr = list.getBoundingClientRect();
    return {
      listScrollTop: Math.round(list.scrollTop),
      maxScroll: list.scrollHeight - list.clientHeight,
      btnTop: Math.round(btn.getBoundingClientRect().top),
      brandTop: Math.round(brand.getBoundingClientRect().top),
      btnVisible: btn.getBoundingClientRect().bottom <= window.innerHeight && btn.getBoundingClientRect().top >= 0,
      lastInside: last.getBoundingClientRect().bottom <= lr.bottom + 1,
      lastBottom: Math.round(last.getBoundingClientRect().bottom),
      listBottom: Math.round(lr.bottom),
      pageScrollTop: Math.round(document.scrollingElement.scrollTop),
    };
  });

  await page.screenshot({ path: path.join(os.tmpdir(), `side-${count}-${width}.png`) });
  await page.close();

  console.log(`\n----- ${label}（${width}x${height}，${count} 个会话）-----`);
  console.log(`列表可视高=${before.listClientH}  内容高=${before.listScrollH}  需要滚动=${before.scrollbar ? '是' : '否'}  单个会话项高度=${before.itemH}px`);
  console.log(`列表滚到底后 scrollTop=${after.listScrollTop}（上限 ${after.maxScroll}）`);
  console.log(`"新建会话"按钮 top=${before.btnTop} -> ${after.btnTop}（未被滚走=${after.btnTop === before.btnTop ? '是' : '否'}）  可见=${after.btnVisible ? 'Y' : 'n'}`);
  console.log(`最后一项是否进入可视区=${after.lastInside ? '是' : '否'}（${after.lastBottom} <= ${after.listBottom}）  页面滚动=${after.pageScrollTop}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  await run(browser, 1280, 800, 3, '桌面端 会话少');
  await run(browser, 1280, 800, 40, '桌面端 会话多');
  await run(browser, 1280, 500, 40, '桌面端 矮窗口');
  await run(browser, 390, 844, 40, '移动端 会话多');
  await browser.close();
})();
