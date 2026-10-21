// 引用溯源前端探针（交互 v2：角标 → 弹出小窗）：把 index.html 里的脚本真跑起来。
// 1) 历史消息：角标索引建好，点角标弹小窗显示出处+原文（C5）；
// 2) 实时流：citations 事件后正文图占位符换成签名 URL，点角标弹小窗（C1/C3）；
// 3) 小窗行为：重复点同一角标=关闭，点外部/Esc/聊天区滚动=关闭。
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire('C:/Users/13526/.workbuddy/binaries/node/workspace/package.json');
const { parseHTML } = require('linkedom');

const HTML = readFileSync('D:/agent/frontend/index.html', 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const failures = [];
function check(label, condition, detail = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + String(detail).slice(0, 120) : ''}`);
  if (!condition) failures.push(label);
}

// 历史消息：内容里的图引用已经是签名 URL（服务端 _decorate_history 重写后的契约）。
const HISTORY = [
  { role: 'user', content: '第3章的分集接收讲了什么？' },
  {
    role: 'assistant',
    content: '书里用 [c1] 讲了 MRC。\n\n![图 3-4 MRC](/api/assets/as_1f3a?u=u-1&t=9999999999.sig)',
    operations: [
      { kind: 'call', tool: 'search_textbook', args: { query: '分集接收' } },
      { kind: 'result', tool: 'search_textbook', status: 'success' },
    ],
    citations: [
      {
        code: 'c1', doc_title: '移动通信', chapter: '第3章 分集接收',
        page_start: 45, page_end: 46, kind: 'figure',
        asset_id: 'as_1f3a', asset_url: '/api/assets/as_1f3a?u=u-1&t=9999999999.sig',
        content: '![图 3-4 MRC](/api/assets/as_1f3a?u=u-1&t=9999999999.sig)\n最大比值合并的示意图。',
      },
    ],
  },
  { role: 'user', content: '谢谢' },
  { role: 'assistant', content: '普通回答，没有引用。' },
];

// 实时流：正文先出（图还是占位文件名），citations 事件最后才到。
const SSE_EVENTS = [
  { type: 'conversation', thread_id: 't-cite' },
  { type: 'tool_call', tool: 'search_textbook', calls: [{ name: 'search_textbook', args: { query: 'MRC' } }] },
  { type: 'tool_result', tool: 'search_textbook', status: 'success',
    content: '[c1] 移动通信 · 第3章 分集接收 · 第 45 页\n最大比值合并的示意图。' },
  { type: 'assistant_delta', content: '书里用 [c1] 讲了 MRC。\n\n![图 3-4 MRC](as_1f3a.png)' },
  {
    type: 'citations',
    items: [{
      code: 'c1', doc_title: '移动通信', chapter: '第3章 分集接收',
      page_start: 45, page_end: 46, kind: 'figure',
      asset_id: 'as_1f3a', asset_url: '/api/assets/as_1f3a?u=u-1&t=8888888888.live',
      content: '![图 3-4 MRC](as_1f3a.png)\n最大比值合并的示意图。',
    }],
  },
  { type: 'done' },
];

async function boot() {
  const { window, document } = parseHTML(HTML);
  globalThis.document = document;
  window.document = document;

  // marked / DOMPurify 是 CDN 的，用最小桩：原样放行（保留 sup/img 标签）。
  window.marked = { parse: (text) => String(text) };
  window.DOMPurify = { sanitize: (html) => html };

  window.localStorage = {
    getItem: () => 'tok-123', setItem: () => {}, removeItem: () => {},
  };
  window.setTimeout = (fn, ms) => 0;
  window.clearTimeout = () => {};
  window.requestAnimationFrame = () => 0;
  window.confirm = () => true;
  window.getSelection = () => null;
  window.innerWidth = 1200;
  window.innerHeight = 800;
  window.location = { origin: 'http://127.0.0.1:8000', href: 'http://127.0.0.1:8000/' };
  window.scrollTo = () => {};

  window.fetch = async (url, options = {}) => {
    const json = (body, status = 200) => ({
      ok: status < 400, status,
      json: async () => body,
      text: async () => JSON.stringify(body),
      body: null,
    });
    const path = String(url);
    if (path === '/api/auth/me') return json({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true });
    if (path === '/api/conversations') return json([]);
    if (path === '/api/documents') return json([]);
    if (path === '/api/conversations/t-cite/messages') return json({ thread_id: 't-cite', messages: HISTORY });
    if (path === '/api/chat/stream') {
      const encoder = new TextEncoder();
      const chunks = SSE_EVENTS.map((event) => encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      let index = 0;
      return {
        ok: true, status: 200,
        body: {
          getReader: () => ({
            read: async () => (index < chunks.length
              ? { value: chunks[index++], done: false }
              : { value: undefined, done: true }),
          }),
        },
      };
    }
    return json({ detail: 'not found' }, 404);
  };

  vm.createContext(window);
  try {
    vm.runInContext(script, window);
  } catch (error) {
    check('页面脚本能执行', false, error.stack);
    return null;
  }
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
  return { window, document };
}

function clickBadge(window, badge) {
  const ClickEvent = window.Event || globalThis.Event;
  badge.dispatchEvent(new ClickEvent('click', { bubbles: true }));
}

// --- 1. 历史渲染（C5：刷新后点角标还能看引用） ---
{
  const ctx = await boot();
  if (ctx) {
    const { window, document } = ctx;
    await window.selectConversation('t-cite');

    const first = document.querySelector('#messages .message.assistant .message-content');
    check('历史正文出现 [c1] 角标', Boolean(first.querySelector('sup.cite[data-code="c1"]')));
    check('历史正文图引用是签名 URL', first.innerHTML.includes('/api/assets/as_1f3a?u=u-1'));
    check('角标索引建在 contentNode 上', first._citations && first._citations.c1?.code === 'c1');
    check('没有引用的消息没有索引', !document.querySelectorAll('#messages .message.assistant')[1]._citations);

    // 点角标 → 小窗
    const badge = first.querySelector('sup.cite[data-code="c1"]');
    clickBadge(window, badge);
    let pop = document.getElementById('citePopover');
    check('点角标弹出小窗', Boolean(pop));
    check('小窗出处 = 书名 · 章节 · 页码 · 图',
      pop && pop.querySelector('.source-meta').textContent === '移动通信 · 第3章 分集接收 · 第 45-46 页 · 图',
      pop && pop.querySelector('.source-meta').textContent);
    check('小窗原文含图和文字',
      pop.querySelector('.body').innerHTML.includes('/api/assets/as_1f3a')
        && pop.querySelector('.body').textContent.includes('最大比值合并'));
    check('角标高亮 active', badge.classList.contains('active'));
    check('小窗定位了坐标', pop.style.top !== '' && pop.style.top !== 'NaNpx',
      `${pop.style.left},${pop.style.top}`);

    // 再点同一个角标 = 关闭
    clickBadge(window, badge);
    check('重复点同一角标关闭小窗', !document.getElementById('citePopover'));

    // 点外部 = 关闭
    clickBadge(window, badge);
    check('重新打开成功', Boolean(document.getElementById('citePopover')));
    const MousedownEvent = window.Event || globalThis.Event;
    document.body.dispatchEvent(new MousedownEvent('mousedown', { bubbles: true }));
    check('点外部关闭小窗', !document.getElementById('citePopover'));

    // Esc = 关闭（linkedom 没有 KeyboardEvent，用 Event 手动补 key 属性）
    clickBadge(window, badge);
    check('再次打开', Boolean(document.getElementById('citePopover')));
    const esc = new (window.Event || globalThis.Event)('keydown', { bubbles: true });
    esc.key = 'Escape';
    document.body.dispatchEvent(esc);
    check('Esc 关闭小窗', !document.getElementById('citePopover'));
  }
}

// --- 2. 实时流（C1/C3：citations 事件 → 图出正文，角标可点开小窗） ---
{
  const ctx = await boot();
  if (ctx) {
    const { window, document } = ctx;
    await window.sendMessage('第3章的分集接收讲了什么？');

    const assistant = document.querySelector('#messages .message.assistant .message-content');
    check('流式正文已渲染', Boolean(assistant) && assistant.textContent.includes('MRC'));
    // 工具返回原文：折叠块出现、摘要有字数、正文是返回原文
    const toolBox = document.querySelector('#messages .trace-tool-result');
    check('工具返回折叠块出现', Boolean(toolBox), toolBox && toolBox.outerHTML.slice(0, 80));
    check('返回摘要带字数', toolBox && toolBox.querySelector('summary').textContent.includes('返回'),
      toolBox && toolBox.querySelector('summary').textContent);
    check('返回原文可见', toolBox && toolBox.querySelector('.trace-tool-result-body').textContent.includes('最大比值合并'));
    check('正文图占位符换成签名 URL',
      assistant.innerHTML.includes('/api/assets/as_1f3a')
        && assistant.innerHTML.includes('t=8888888888.live')
        && !assistant.innerHTML.includes('(as_1f3a.png)'),
      assistant.innerHTML.slice(0, 100));
    check('流式后角标索引可用', assistant._citations?.c1?.asset_url === '/api/assets/as_1f3a?u=u-1&t=8888888888.live');

    const badge = assistant.querySelector('sup.cite[data-code="c1"]');
    clickBadge(window, badge);
    const pop = document.getElementById('citePopover');
    check('实时流点角标弹小窗', Boolean(pop));
    check('小窗原文里的图换成签名 URL',
      pop && pop.querySelector('.body').innerHTML.includes('t=8888888888.live'),
      pop && pop.querySelector('.body').innerHTML.slice(0, 90));

    // 聊天区滚动 → 关闭
    const ScrollEvent = window.Event || globalThis.Event;
    document.querySelector('#chat').dispatchEvent(new ScrollEvent('scroll', { bubbles: false }));
    check('聊天区滚动关闭小窗', !document.getElementById('citePopover'));
  }
}

console.log();
if (failures.length) {
  console.log(`共 ${failures.length} 项失败：${failures.join(' / ')}`);
  process.exit(1);
}
console.log('全部通过');
