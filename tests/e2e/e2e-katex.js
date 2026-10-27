const { chromium } = require('playwright-core');
const CHROME = require('./_browser');
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
      // MinerU 解析出来的公式都是单 $（教材表格的单元格就是这样）
      '表格里的最小值是 $-2^{7}(-128)$，字符上限 $2^{16}-1$。',
      '单个字母变量 $x$、$n$ 和函数 $f(x)$ 也要渲染。',
      '行内代码 `$x^2$` 不是公式。',
      '```python',
      'area = 3 * $x**2$  # 围栏代码块里的 $ 不该当公式',
      '```',
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
    const code = node.querySelector('code');
    return {
      katexCount: katexNodes.length,
      displayCount: display.length,
      rawGone: !text.includes('$$'),
      moneySafe: text.includes('$5 人民币'),
      inlineCodeSafe: code ? code.textContent.includes('$x^2$') : false,
      fenceSafe: (() => {
        const pre = node.querySelector('pre code');
        return Boolean(pre) && pre.textContent.includes('$x**2$');
      })(),
      singleLetter: Boolean(node.querySelector('.katex')) && !text.includes('$x$'),
    };
  });
  check('KaTeX 已渲染公式节点', r.katexCount >= 4, `count=${r.katexCount}`);
  check('行间公式（display）生效', r.displayCount === 1);
  check('$$ 定界符已从正文消失', r.rawGone);
  check('普通 $5 金额未被误伤', r.moneySafe);
  check('行内代码里的 $..$ 原样保留', r.inlineCodeSafe);
  check('围栏代码块里的 $..$ 原样保留', r.fenceSafe);
  check('单字母变量 $x$ 已渲染（不再裸着）', r.singleLetter);

  // 引用小窗：内容与章节名都按 MinerU 的形状给（单 $ 公式 + 表格 + <sup> 标题）
  const pop = await page.evaluate(() => {
    const item = {
      code: 'c9',
      doc_title: 'Hello-Algo',
      chapter: '4. 指数阶 O(2<sup>n</sup>)',
      page_start: 12,
      page_end: 12,
      kind: 'table',
      content: '| 类型 | 最小值 |\n|---|---|\n| long | $-2^{63}$ |',
    };
    const badge = document.createElement('sup');
    badge.className = 'cite';
    badge.dataset.code = 'c9';
    document.body.appendChild(badge);
    showCitePopover(badge, item);
    const popover = document.getElementById('citePopover');
    const meta = popover.querySelector('.source-meta').textContent;
    const table = popover.querySelectorAll('table').length;
    const katex = popover.querySelectorAll('.katex').length;
    hideCitePopover();
    badge.remove();
    return { meta, table, katex };
  });
  check('引用小窗渲染出表格', pop.table === 1, `table=${pop.table}`);
  check('引用小窗里的单 $ 公式渲染成 KaTeX', pop.katex >= 1, `katex=${pop.katex}`);
  check('出处行剥掉了 <sup> 标记', !pop.meta.includes('<sup>'), pop.meta);

  await browser.close();
  console.log('结果:', ok ? '通过' : '有失败项');
  process.exit(ok ? 0 : 1);
})();
