const { chromium } = require('playwright-core');

const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:8766';

const probe = () => {
  const doc = document.documentElement;
  const last = document.querySelector('#messages').lastElementChild;
  return {
    gap: Math.round(doc.scrollHeight - (window.scrollY + window.innerHeight)),
    autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
    streaming: document.querySelector('#send').disabled,
    residual: last ? Math.round(doc.scrollHeight - (last.getBoundingClientRect().bottom + window.scrollY)) : -1,
  };
};

async function run(page, path, label) {
  await page.goto(BASE + path);
  await page.fill('#input', '请生成一段较长的回复');
  await page.click('#send');

  await page.waitForFunction(() => document.documentElement.scrollHeight > window.innerHeight + 1500, null, { timeout: 25000 });
  await page.waitForTimeout(700);
  const p1 = await page.evaluate(probe);

  await page.evaluate(() => window.scrollTo({ top: 0 }));
  await page.waitForTimeout(600);
  const p2 = await page.evaluate(probe);

  // 上滚后停在原地，观察输出过程中是否被强行拽回底部
  const hold = await page.evaluate(async () => {
    const samples = [];
    for (let i = 0; i < 8; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
      samples.push({
        scrollY: Math.round(window.scrollY),
        streaming: document.querySelector('#send').disabled,
        autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
      });
    }
    return samples;
  });

  // 模拟用户把滚动条拖到最底部，然后每 200ms 采样一次
  await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight }));
  const trail = await page.evaluate(async () => {
    const samples = [];
    for (let i = 0; i < 12; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
      const doc = document.documentElement;
      samples.push({
        gap: Math.round(doc.scrollHeight - (window.scrollY + window.innerHeight)),
        streaming: document.querySelector('#send').disabled,
        autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
      });
    }
    return samples;
  });

  return { label, p1, p2, hold, trail };
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  const rows = [];
  rows.push(await run(page, '/original', '修复前（原始页面）'));
  rows.push(await run(page, '/', '修复后（当前页面）'));
  await browser.close();

  for (const r of rows) {
    console.log(`\n===== ${r.label} =====`);
    console.log(`最后一条消息下方残留高度 residual=${r.p1.residual}px（isNearBottom 阈值 120px）`);
    console.log(`1) 流式输出中 距底部=${r.p1.gap}px  autoFollow=${r.p1.autoFollow}`);
    console.log(`2) 上滚到顶部 距底部=${r.p2.gap}px  autoFollow=${r.p2.autoFollow}`);
    console.log('2b) 上滚后原地停留，scrollY 轨迹（每 200ms，应保持不被动）：');
    console.log('   ' + r.hold.map((s) => `y=${s.scrollY}${s.autoFollow === null ? '' : ' af=' + s.autoFollow}`).join('  '));
    const drift = Math.max(...r.hold.map((s) => s.scrollY)) - Math.min(...r.hold.map((s) => s.scrollY));
    console.log(`   输出中是否仍在推送=${r.hold.some((s) => s.streaming)}    scrollY 漂移=${drift}px`);
    console.log('3) 拖到底部后的距底轨迹（每 200ms）:');
    console.log('   ' + r.trail.map((s) => `gap=${s.gap}${s.autoFollow === null ? '' : ' af=' + s.autoFollow}`).join('  '));
    console.log('   仍有输出=' + r.trail.map((s) => (s.streaming ? 'Y' : 'n')).join(''));
    const end = r.trail[r.trail.length - 1].gap;
    console.log(`=> 拖到底部后是否恢复跟随: ${end < 150 ? '是' : '否'}（末端距底 ${end}px）`);
  }
})();
