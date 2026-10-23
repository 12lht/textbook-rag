const { chromium } = require('playwright-core');

const CHROME = require('./_browser');
const BASE = 'http://127.0.0.1:8766';

const results = [];
function check(name, ok, extra = '') {
  results.push(ok);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${extra ? '  ' + extra : ''}`);
}

async function probe(browser, path) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const faviconRequests = [];
  const allRequests = [];
  page.on('request', (request) => {
    const url = request.url();
    allRequests.push(url);
    if (url.includes('favicon')) faviconRequests.push(url.slice(0, 80));
  });

  await page.goto(BASE + path);
  await page.waitForTimeout(1500);   // 给浏览器留出抓 favicon 的时间

  const icon = await page.evaluate(() => new Promise((resolve) => {
    const link = document.querySelector('link[rel="icon"]');
    if (!link) { resolve({ hasLink: false }); return; }
    const probeImage = new Image();
    probeImage.onload = () => resolve({ hasLink: true, decoded: true, w: probeImage.naturalWidth, h: probeImage.naturalHeight, href: link.href.slice(0, 40) });
    probeImage.onerror = () => resolve({ hasLink: true, decoded: false, href: link.href.slice(0, 40) });
    probeImage.src = link.href;
  }));

  await page.close();
  return { icon, faviconRequests, allRequests };
}

(async () => {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: false,
    args: ['--headless=new'],
  });

  const current = await probe(browser, '/');
  console.log('--- 当前页面（已加图标声明） ---');
  console.log('  icon 元素:', JSON.stringify(current.icon));
  console.log('  favicon 请求:', current.faviconRequests);

  const control = await probe(browser, '/original');
  console.log('--- 对照组：旧备份页（无图标声明） ---');
  console.log('  icon 元素:', JSON.stringify(control.icon));
  console.log('  favicon 请求:', control.faviconRequests);
  console.log('  该页抓到的全部请求:', control.allRequests.length, '条');
  control.allRequests.forEach((url) => console.log('    -', url.slice(0, 70)));
  console.log();

  check('请求监听器是活的（对照组抓到了网络请求）', control.allRequests.length > 0, `共 ${control.allRequests.length} 条`);
  check('当前页面声明了 icon', current.icon.hasLink === true);
  check('图标能被解码成 32x32 图像', current.icon.decoded === true && current.icon.w === 32 && current.icon.h === 32,
    `decoded=${current.icon.decoded} ${current.icon.w}x${current.icon.h}`);

  const controlFetches = control.faviconRequests.length > 0;
  if (controlFetches) {
    console.log('对照组确实会去要 favicon → 请求数对比是有意义的');
    check('加了声明后不再请求 /favicon.ico', current.faviconRequests.length === 0, `当前 ${current.faviconRequests.length} 次，对照组 ${control.faviconRequests.length} 次`);
  } else {
    console.log('注意：对照组也没有发出 favicon 请求 —— 说明 headless 浏览器本来就不抓 favicon，');
    console.log('      「0 次请求」这条无法区分好坏，本次只以「图标能被解码」作为有效证据。');
  }

  check('对照组确实没有 icon 声明（对照有效）', control.icon.hasLink === false);

  await browser.close();
  console.log(`\n合计 ${results.filter(Boolean).length}/${results.length} 通过`);
  process.exit(results.every(Boolean) ? 0 : 1);
})();
