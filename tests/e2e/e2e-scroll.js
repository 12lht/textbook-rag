// 滚动跟随的三个阶段，全程针对 `.chat` 滚动容器（现状）：
//   1. 流式输出中贴底：距底≈0，autoFollow=true
//   2. 用户上滚后：autoFollow 变 false，且后续输出**不得**把视口往下拽
//   3. 拖回底部后：autoFollow 恢复 true，输出继续时仍然贴底
//
// 第 2 阶段的「不被动」是最容易回退的：只要某处忘了判断 autoFollow，
// 每来一段增量就 scrollToBottom，用户正在读上文，页面却自己往下跑。
const { chromium } = require('playwright-core');
const path = require('path');
const os = require('os');

const CHROME = require('./_browser');
const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8766';

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? `  ${detail}` : ''}`);
  if (!ok) failures += 1;
};

const probe = () => {
  const chat = document.querySelector('#chat');
  return {
    gap: Math.round(chat.scrollHeight - chat.scrollTop - chat.clientHeight),
    autoFollow: typeof state !== 'undefined' ? state.autoFollow : null,
    streaming: document.querySelector('#send').disabled,
  };
};

async function run(page) {
  await page.goto(BASE + '/');
  await page.fill('#input', '请生成一段较长的回复');
  await page.click('#send');
  await page.waitForFunction(() => document.querySelector('#chat').scrollHeight > window.innerHeight + 1500, null, { timeout: 25000 });
  await page.waitForTimeout(700);

  // 阶段 1：跟随中
  const following = await page.evaluate(probe);
  check('流式输出中贴底', following.autoFollow === true && following.gap < 40,
    `gap=${following.gap}px af=${following.autoFollow}`);

  // 阶段 2：上滚，观察 8 次 × 200ms 的滚动位置是否被输出拽动
  await page.evaluate(() => { document.querySelector('#chat').scrollTop = 0; });
  await page.waitForTimeout(500);
  const scrolledUp = await page.evaluate(probe);
  check('上滚后 autoFollow 关闭', scrolledUp.autoFollow === false, `af=${scrolledUp.autoFollow}`);
  check('上滚时输出仍在继续', scrolledUp.streaming === true, '否则后面测不到「不被动」');
  const hold = await page.evaluate(async () => {
    const chat = document.querySelector('#chat');
    const samples = [];
    for (let i = 0; i < 8; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
      samples.push({ top: Math.round(chat.scrollTop), af: state.autoFollow });
    }
    return samples;
  });
  const drift = Math.max(...hold.map((s) => s.top)) - Math.min(...hold.map((s) => s.top));
  check('上滚后位置不被输出拽动', drift <= 2, `8 次采样漂移 ${drift}px`);

  // 阶段 3：拖回底部
  await page.evaluate(() => { document.querySelector('#chat').scrollTop = document.querySelector('#chat').scrollHeight; });
  await page.waitForTimeout(500);
  const dragged = await page.evaluate(probe);
  check('拖到底部后恢复跟随', dragged.autoFollow === true, `af=${dragged.autoFollow}`);
  await page.waitForTimeout(1200);
  const stillStreaming = await page.evaluate(probe);
  check('恢复后继续贴底', stillStreaming.gap < 40, `gap=${stillStreaming.gap}px 仍在输出=${stillStreaming.streaming}`);

  await page.screenshot({ path: path.join(os.tmpdir(), 'scroll-follow.png') });
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  try {
    await run(page);
  } finally {
    await browser.close();
  }
  console.log(failures ? `\n${failures} 项失败` : '\n全部通过');
  process.exit(failures ? 1 : 0);
})();
