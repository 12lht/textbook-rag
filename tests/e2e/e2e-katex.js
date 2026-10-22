const { chromium } = require('playwright-core');
const CHROME = 'C:/Users/13526/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe';
let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto('http://127.0.0.1:8000/');
  await page.waitForFunction(() => window.renderMathInElement, null, { timeout: 10000 });

  // 在页面里直接走真实的渲染管线：addMessage 是历史加载和实时流的公共入口
  await page.evaluate(() => {
    const md = [
      '状态转移方程：',
      '$$dp[i, c] = \\max\\big(dp[i-1, c],\\ dp[i-1, c-wgt[i-1]] + val[i-1]\\big)$$',
      '其中内联公式 \\(dp[i, c]\\) 表示状态。普通文本 $5 人民币不应被误伤。',
    ].join('\n');
    window.__node = null;
    const original = window.addMessage;
    window.addMessage = function (...args) {
      const node = original.apply(this, args);
      window.__node = node;
      return node;
    };
    addMessage('assistant', md);
  });
  await page.waitForTimeout(300);
  const r = await page.evaluate(() => {
    const node = window.__node;
    const katexNodes = node.querySelectorAll('.katex');
    const display = node.querySelectorAll('.katex-display');
    const text = node.textContent;
    return {
      katexCount: katexNodes.length,
      displayCount: display.length,
      rawGone: !text.includes('$$'),
      moneySafe: text.includes('$5 人民币'),
    };
  });
  check('KaTeX 已渲染公式节点', r.katexCount >= 2, `count=${r.katexCount}`);
  check('行间公式（display）生效', r.displayCount === 1);
  check('$$ 定界符已从正文消失', r.rawGone);
  check('普通 $5 金额未被误伤', r.moneySafe);
  await browser.close();
  console.log('结果:', ok ? '通过' : '有失败项');
  process.exit(ok ? 0 : 1);
})();
