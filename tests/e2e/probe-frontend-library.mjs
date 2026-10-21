// 知识库管理页（/library）的 DOM 测试：把 library.html 里的脚本真跑起来。
//
// 为什么单独测：后端探针只能证明「接口返回了正确的 JSON」。而「分类点了有没有
// 过滤、勾选之后批量条有没有出现、点『移过去』发的到底是不是 PATCH、
// 发给哪个 URL、body 里的 category_id 是 id 还是名字」——这些全在 DOM 层，
// 接口层一个都看不见。之前就有过「下拉框里放的是名字、后端要的是 id」这种错。
//
// 这一页是**独立页面**，不再寄生在 index.html 的侧栏里，所以探针也换了宿主。
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire('C:/Users/13526/.workbuddy/binaries/node/workspace/package.json');
const { parseHTML } = require('linkedom');

const HTML = readFileSync('D:/agent/frontend/library.html', 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const failures = [];
function check(label, condition, detail = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
  if (!condition) failures.push(label);
}

const CATS = [
  { category_id: 'c-1', name: '移动通信', doc_count: 2, created_at: '2026-10-20T09:00:00' },
  { category_id: 'c-2', name: '信号与系统', doc_count: 0, created_at: '2026-10-20T09:10:00' },
];

const DOCS = [
  {
    doc_id: 'd1', title: '通信原理（第 7 版）', filename: 'txyl.pdf', kind: 'pdf',
    status: 'ready', error: null, page_count: 412, chunk_count: 880, asset_count: 154,
    category_id: 'c-1', size_bytes: 1,
    created_at: '2026-10-20T10:00:00', updated_at: '2026-10-20T10:00:00',
  },
  {
    doc_id: 'd2', title: '坏文件', filename: 'bad.pdf', kind: 'pdf',
    status: 'failed', error: '解析失败：FileDataError: 损坏', page_count: 0,
    chunk_count: 0, asset_count: 0, category_id: null, size_bytes: 1,
    created_at: '2026-10-20T09:00:00', updated_at: '2026-10-20T09:00:00',
  },
  {
    doc_id: 'd3', title: '刚传的课件', filename: 'ppt.pptx', kind: 'pptx',
    status: 'parsing', error: null, page_count: 0, chunk_count: 0, asset_count: 0,
    category_id: 'c-1', size_bytes: 1,
    created_at: '2026-10-20T11:00:00', updated_at: '2026-10-20T11:00:00',
  },
];

// 服务端的可变状态：探针要能验证「归类之后列表真的变了」，
// 所以 mock 不能每次都返回同一份常量。
function makeServer({ loggedIn }) {
  const server = {
    docs: DOCS.map((doc) => ({ ...doc })),
    cats: CATS.map((cat) => ({ ...cat })),
    calls: [],
  };
  const recount = () => {
    server.cats.forEach((cat) => {
      cat.doc_count = server.docs.filter((doc) => doc.category_id === cat.category_id).length;
    });
  };
  recount();

  server.handle = async (input, init = {}) => {
    const url = String(input);
    const method = String(init.method || 'GET').toUpperCase();
    server.calls.push({ url, method, body: init.body ? JSON.parse(init.body) : null });

    const json = (body, status = 200) => ({
      ok: status < 400, status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    });
    const count = (prefix) => server.calls.filter((c) => c.url.startsWith(prefix)).length;

    if (url === '/api/auth/me') {
      return loggedIn
        ? json({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true })
        : json({ detail: '未登录' }, 401);
    }
    if (url === '/api/documents' && method === 'GET') {
      return loggedIn ? json(server.docs) : json({ detail: '请先登录' }, 401);
    }
    if (url === '/api/categories' && method === 'GET') {
      recount();
      return json(server.cats);
    }
    if (url === '/api/categories' && method === 'POST') {
      const name = server.calls[server.calls.length - 1].body.name;
      if (server.cats.some((cat) => cat.name === name)) {
        return json({ detail: '已经有同名的分类了' }, 409);
      }
      // id 必须真的唯一。早先用「调用次数」拼 id，结果新建出来的 id 撞上了
      // 已经存在的 `c-2`，页面因此切到了别人头上——那种错会伪装成前端 bug。
      const created = { category_id: `c-new-${server.cats.length + 1}`, name, doc_count: 0 };
      server.cats.push(created);
      return json(created, 201);
    }
    const catMatch = url.match(/^\/api\/categories\/([^/]+)$/);
    if (catMatch && method === 'PATCH') {
      const name = server.calls[server.calls.length - 1].body.name;
      if (server.cats.some((cat) => cat.name === name)) return json({ detail: '撞名了' }, 409);
      const target = server.cats.find((cat) => cat.category_id === catMatch[1]);
      if (!target) return json({ detail: '分类不存在' }, 404);
      target.name = name;
      return json({ category_id: target.category_id, status: 'renamed' });
    }
    if (catMatch && method === 'DELETE') {
      const before = server.cats.length;
      server.cats = server.cats.filter((cat) => cat.category_id !== catMatch[1]);
      if (server.cats.length === before) return json({ detail: '分类不存在' }, 404);
      server.docs.forEach((doc) => { if (doc.category_id === catMatch[1]) doc.category_id = null; });
      return json({ category_id: catMatch[1], status: 'deleted', documents: 'unfiled' });
    }
    const docMatch = url.match(/^\/api\/documents\/([^/]+)$/);
    if (docMatch && method === 'PATCH') {
      const doc = server.docs.find((item) => item.doc_id === docMatch[1]);
      if (!doc) return json({ detail: '教材不存在' }, 404);
      const target = server.calls[server.calls.length - 1].body.category_id;
      if (target && !server.cats.some((cat) => cat.category_id === target)) {
        return json({ detail: '分类不存在' }, 404);
      }
      doc.category_id = target;
      return json({ doc_id: doc.doc_id, category_id: target });
    }
    if (url.includes('/markdown')) {
      const render = url.includes('render=1');
      const body = render
        ? '<!-- page:1 -->\n\n正文段落\n\n![图 3-4 抽样](/api/assets/as_1?u=u-1&t=9999999999.abc)'
        : '<!-- page:1 -->\n\n正文段落\n\n![图 3-4 抽样](as_1.png)';
      return { ok: true, status: 200, json: async () => ({}), text: async () => body };
    }
    return json({ detail: 'not mocked: ' + url }, 404);
  };
  return server;
}

async function boot({ loggedIn, withRenderer = true }) {
  const { window, document } = parseHTML(HTML);
  globalThis.document = document;
  window.document = document;

  // marked / DOMPurify 在 CDN 上，跑测试时拿不到，用最小实现顶上。
  if (withRenderer) {
    window.marked = {
      parse: (text) => String(text)
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/!\[(.*?)\]\(([^)]*)\)/g, '<img alt="$1" src="$2">')
        .replace(/\n+/g, '<br>'),
    };
    window.DOMPurify = { sanitize: (html) => html };
  } else {
    // linkedom 的 window 之间共享一部分全局，必须显式清掉。
    window.marked = undefined;
    window.DOMPurify = undefined;
  }

  const store = new Map();
  if (loggedIn) store.set('agent.token', 'tok-123');
  window.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };

  const timers = [];
  // 轮询回调**不执行**，只记录延迟——真跑会无限循环，这里只需要知道有没有安排。
  window.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
  window.clearTimeout = () => {};
  window.confirm = () => true;
  // 上传要用。真 FormData 传进来会往网络发东西，这里只记录 append 了哪些字段。
  window.FormData = class {
    constructor() { this.fields = []; }
    append(key, value) { this.fields.push([key, value]); }
  };

  const server = makeServer({ loggedIn });
  window.fetch = (input, init) => server.handle(input, init);

  vm.createContext(window);
  try {
    vm.runInContext(script, window);
  } catch (error) {
    check('页面脚本能执行', false, error.message);
    return { window, document, server, timers };
  }
  // 结尾那个 async IIFE 要跑完（loadIdentity → loadAll）。
  // 用 setImmediate 而不是 setTimeout：window.setTimeout 已经被换成只记录的桩。
  for (let i = 0; i < 30; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  return { window, document, server, timers };
}

const catNames = (document) =>
  [...document.querySelectorAll('#catList .cat-name')].map((node) => node.textContent);
const docTitles = (document) =>
  [...document.querySelectorAll('#docList .doc-title')].map((node) => node.textContent);
// linkedom 的 <select> **没有实现 value**（实测读永远 undefined、写不进去）。
// 所以「把下拉设成某个值再触发 change」只能给这个元素实例单独 define 一个属性。
// 绕过的是测试环境的缺口，不是被测逻辑——页面里读的就是 select.value，
// 在真浏览器里它是标准行为。反过来，验证「当前选中的是哪一项」不能用
// select.value，要看 option 上的 selected（那个 linkedom 是实现了的）。
function pick(element, value) {
  Object.defineProperty(element, 'value', { value, writable: true, configurable: true });
  element.onchange();
}
const settle = async (rounds = 20) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

// --- 未登录 ---
{
  const { document, server } = await boot({ loggedIn: false });
  check('未登录时显示登录门', document.querySelector('#gate').hidden === false);
  check('未登录时不拉文档列表',
    !server.calls.some((c) => c.url === '/api/documents'), server.calls.map((c) => c.url).join(' '));
  check('未登录时不拉分类',
    !server.calls.some((c) => c.url === '/api/categories'), server.calls.map((c) => c.url).join(' '));
}

// --- 已登录：列表渲染 ---
{
  const { document, server, timers } = await boot({ loggedIn: true });
  check('登录门挡起来', document.querySelector('#gate').hidden === true);

  // 「全部文档 / 未分类」两个虚拟分类 + 两个真分类
  const names = catNames(document);
  check('分类栏有 4 行', names.length === 4, names.join('/'));
  check('前两行是虚拟分类', names[0] === '全部文档' && names[1] === '未分类', names.join('/'));
  check('虚拟分类显示总数', 
    document.querySelectorAll('#catList .cat-count')[0].textContent === '3',
    document.querySelectorAll('#catList .cat-count')[0].textContent);
  check('未分类计数只数没归类的',
    document.querySelectorAll('#catList .cat-count')[1].textContent === '1',
    document.querySelectorAll('#catList .cat-count')[1].textContent);
  check('虚拟分类没有改名/删除按钮',
    document.querySelectorAll('#catList .cat')[0].querySelectorAll('.cat-op').length === 0);
  check('真分类有改名/删除按钮',
    document.querySelectorAll('#catList .cat')[2].querySelectorAll('.cat-op').length === 2);

  check('列出 3 本书', docTitles(document).length === 3, docTitles(document).join('/'));

  const metas = [...document.querySelectorAll('#docList .doc-meta')].map((n) => n.textContent);
  check('ready 显示页数/段数/图表',
    metas[0].includes('412 页') && metas[0].includes('880 段') && metas[0].includes('154 图表'), metas[0]);
  check('failed 显示入库失败和原因',
    metas[1].includes('入库失败') && metas[1].includes('损坏'), metas[1]);
  check('parsing 显示解析中', metas[2].includes('解析中'), metas[2]);

  check('每本书都带归类下拉',
    document.querySelectorAll('#docList .doc-cat').length === 3,
    String(document.querySelectorAll('#docList .doc-cat').length));
  const firstSelect = document.querySelectorAll('#docList .doc-cat')[0];
  check('下拉的选项是 id 不是名字',
    [...firstSelect.querySelectorAll('option')].map((o) => o.value).join(',') === ',c-1,c-2',
    [...firstSelect.querySelectorAll('option')].map((o) => o.value).join(','));
  const chosen = firstSelect.querySelector('option[selected]');
  check('下拉默认选中书当前的分类', Boolean(chosen) && chosen.value === 'c-1',
    chosen ? chosen.value : '(没有任何 option 被选中)');

  check('有书在解析 → 安排了轮询', timers.includes(2500), JSON.stringify(timers));
  check('解析中的书不能看解析',
    document.querySelectorAll('#docList .doc-op')[2].disabled === true);
  check('已就绪的书能看解析',
    document.querySelectorAll('#docList .doc-op')[0].disabled === false);

  // --- 点分类过滤 ---
  const catNodes = [...document.querySelectorAll('#catList .cat')];
  catNodes[1].onclick();   // 未分类
  check('点「未分类」只剩 1 本', docTitles(document).length === 1, docTitles(document).join('/'));
  check('剩的那本是未分类的', docTitles(document)[0] === '坏文件', docTitles(document).join('/'));
  check('标题跟着筛选变', document.querySelector('#docsTitle').textContent === '未分类',
    document.querySelector('#docsTitle').textContent);

  catNodes[0].onclick();   // 全部
  check('点回「全部文档」恢复 3 本', docTitles(document).length === 3, docTitles(document).join('/'));

  // --- 单本归类：下拉 ---
  const select2 = [...document.querySelectorAll('#docList .doc-cat')][1];
  pick(select2, 'c-2');
  await settle(10);
  const patch = server.calls.filter((c) => c.method === 'PATCH' && c.url.includes('/api/documents/'));
  check('下拉改分类发了 PATCH', patch.length === 1, JSON.stringify(patch));
  check('PATCH 打到正确的书上', patch[0] && patch[0].url === '/api/documents/d2', patch[0] && patch[0].url);
  check('PATCH body 里是 category_id', patch[0] && patch[0].body.category_id === 'c-2',
    JSON.stringify(patch[0] && patch[0].body));
  check('归类后未分类计数变成 0',
    document.querySelectorAll('#catList .cat-count')[1].textContent === '0',
    document.querySelectorAll('#catList .cat-count')[1].textContent);

  // --- 批量归类 ---
  const boxes = [...document.querySelectorAll('#docList input[type=checkbox]')];
  check('批量条默认隐藏', document.querySelector('#bulkBar').hidden === true);
  boxes.forEach((box) => { box.checked = true; box.onchange(); });
  check('勾选后批量条出现', document.querySelector('#bulkBar').hidden === false);
  check('批量条显示已选数量',
    document.querySelector('#bulkCount').textContent === '已选 3 本',
    document.querySelector('#bulkCount').textContent);
  check('勾选的书被标记',
    document.querySelectorAll('#docList .doc-card.picked').length === 3,
    String(document.querySelectorAll('#docList .doc-card.picked').length));

  const bulk = document.querySelector('#bulkCategory');
  // 批量下拉只能整块伪造 value：它是页面自己新建的元素，实例上 define 即可。
  Object.defineProperty(bulk, 'value', { value: 'c-2', writable: true, configurable: true });
  server.calls.length = 0;
  document.querySelector('#bulkApply').onclick();
  await settle();
  const bulkCalls = server.calls.filter((c) => c.method === 'PATCH' && c.url.includes('/api/documents/'));
  check('批量归类发了 3 个 PATCH', bulkCalls.length === 3, String(bulkCalls.length));
  check('三本都移到 c-2',
    bulkCalls.every((c) => c.body.category_id === 'c-2'), JSON.stringify(bulkCalls.map((c) => c.body)));
  check('移动完批量条收起', document.querySelector('#bulkBar').hidden === true);

  // --- 预览 ---
  server.calls.length = 0;
  await document.querySelectorAll('#docList .doc-op')[0].onclick();
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
  check('预览默认走渲染（要求服务端重写图地址）',
    server.calls.some((c) => c.url.includes('/markdown?render=1')),
    server.calls.map((c) => c.url).join(' '));
  const body = document.querySelector('#previewBody');
  check('渲染视图里的图变成真 <img>', /<img[^>]+src="\/api\/assets\/as_1\?u=u-1/.test(body.innerHTML),
    body.innerHTML.slice(0, 120));
  check('渲染视图里锚点被渲染器吃掉', !body.innerHTML.includes('page:1'), body.innerHTML.slice(0, 120));

  await document.querySelector('#tabRaw').onclick();
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
  check('原文模式里有页码锚点', body.innerHTML.includes('&lt;!-- page:1 --&gt;'),
    body.innerHTML.slice(0, 80));
  check('原文模式是 <pre> 且没残留「加载中」',
    body.innerHTML.startsWith('<pre>') && !body.innerHTML.includes('加载中'),
    body.innerHTML.slice(0, 60));
  document.querySelector('#previewClose').onclick();
  check('关掉后浮层隐藏', document.querySelector('#preview').hidden === true);
}

// --- 渲染组件没加载出来（CDN 被拦 / 离线）---
{
  const { document } = await boot({ loggedIn: true, withRenderer: false });
  await document.querySelectorAll('#docList .doc-op')[0].onclick();
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
  check('没有 marked → 渲染标签禁用', document.querySelector('#tabRender').disabled === true);
  check('没有 marked → 直接给原文而不是空白',
    document.querySelector('#previewBody').innerHTML.includes('&lt;!-- page:1 --&gt;'),
    document.querySelector('#previewBody').innerHTML.slice(0, 60));
}

// --- 新建分类 ---
{
  const { window, document, server } = await boot({ loggedIn: true });
  const input = document.querySelector('#catInput');
  input.value = '数字信号处理';
  // 表单的 submit 监听是 addEventListener 注册的；linkedom 不会自己触发，
  // 手动派发一个真 Event（commit 的默认行为它不会做，所以不用额外拦）。
  document.querySelector('#catForm').dispatchEvent(new window.Event('submit'));
  await settle();
  const posts = server.calls.filter((c) => c.method === 'POST' && c.url === '/api/categories');
  check('提交表单发了 POST', posts.length === 1, JSON.stringify(server.calls.map((c) => c.url)));
  check('POST body 是清洗后的名字', posts[0] && posts[0].body.name === '数字信号处理',
    JSON.stringify(posts[0] && posts[0].body));
  check('新建后分类栏多一行', catNames(document).includes('数字信号处理'),
    catNames(document).join('/'));
  check('新建后输入框清空', document.querySelector('#catInput').value === '',
    document.querySelector('#catInput').value);
  check('新建后直接切到新分类',
    document.querySelector('#docsTitle').textContent === '数字信号处理',
    document.querySelector('#docsTitle').textContent);
  check('新分类下还没有书，给空状态提示',
    (document.querySelector('#docList').textContent || '').includes('这个分类下还没有文档'),
    (document.querySelector('#docList').textContent || '').slice(0, 40));
}

// --- 重名：错误写在分类栏，不吞掉 ---
{
  const { window, document } = await boot({ loggedIn: true });
  document.querySelector('#catInput').value = '移动通信';
  document.querySelector('#catForm').dispatchEvent(new window.Event('submit'));
  await settle();
  check('重名时把服务端的话显示出来',
    document.querySelector('#catError').textContent === '已经有同名的分类了',
    document.querySelector('#catError').textContent);
  check('重名时分类数量没变', catNames(document).length === 4, catNames(document).join('/'));
  check('重名时输入框保留，方便改一个字母',
    document.querySelector('#catInput').value === '移动通信',
    document.querySelector('#catInput').value);
}

// --- 删除分类：书退回未分类 ---
{
  const { document, server } = await boot({ loggedIn: true });
  const target = document.querySelector('[data-category-id="c-1"]');
  check('分类行带 data-category-id', Boolean(target));
  const remove = target.querySelectorAll('.cat-op')[1];
  remove.onclick({ stopPropagation() {} });
  await settle();
  const deletes = server.calls.filter((c) => c.method === 'DELETE');
  check('发了 DELETE', deletes.length === 1 && deletes[0].url === '/api/categories/c-1',
    JSON.stringify(deletes.map((c) => c.url)));
  check('分类行消失', !catNames(document).includes('移动通信'), catNames(document).join('/'));
  check('书一本都没少', docTitles(document).length === 3, docTitles(document).join('/'));
  // 删之前：c-1 装着 d1/d3，d2 本来就没归类 → 未分类 1 本。
  // 删掉 c-1 之后三本全归未分类。
  check('书退回未分类', document.querySelectorAll('#catList .cat-count')[1].textContent === '3',
    document.querySelectorAll('#catList .cat-count')[1].textContent);
}

// --- 上传：正在看某个分类时，上传的书直接落到那个分类 ---
{
  const { document, server } = await boot({ loggedIn: true });
  // 先切到「信号与系统」（c-2）
  const target = document.querySelector('[data-category-id="c-2"]');
  target.onclick();
  check('切到 c-2', document.querySelector('#docsTitle').textContent === '信号与系统',
    document.querySelector('#docsTitle').textContent);

  const fileInput = document.querySelector('#fileInput');
  Object.defineProperty(fileInput, 'files', {
    value: [{ name: '新书.pdf' }],
    configurable: true,
  });
  server.calls.length = 0;
  // 让上传成功：mock 里没有处理 POST /api/documents，这里临时补一个分支。
  const original = server.handle;
  server.handle = async (input, init = {}) => {
    if (String(input) === '/api/documents' && String(init.method).toUpperCase() === 'POST') {
      server.calls.push({ url: '/api/documents', method: 'POST', body: null });
      return {
        ok: true, status: 200,
        json: async () => ({ doc_id: 'd-new', title: '新书', duplicated: false }),
        text: async () => '{}',
      };
    }
    return original(input, init);
  };
  fileInput.onchange();
  for (let i = 0; i < 30; i += 1) await new Promise((r) => setImmediate(r));
  const assigns = server.calls.filter((c) => c.method === 'PATCH' && c.url.includes('/api/documents/'));
  check('上传后自动归到当前分类', assigns.length === 1 && assigns[0].url === '/api/documents/d-new',
    JSON.stringify(assigns.map((c) => c.url)));
  check('自动归类用的是当前筛选的分类 id',
    assigns[0] && assigns[0].body.category_id === 'c-2',
    JSON.stringify(assigns[0] && assigns[0].body));
  check('上传完按钮恢复可点',
    document.querySelector('#uploadButton').disabled === false
    && document.querySelector('#uploadButton').textContent === '＋ 上传并入库',
    document.querySelector('#uploadButton').textContent);
}

console.log(failures.length ? `\n失败：${failures.join(', ')}` : '\n全部通过');
process.exit(failures.length ? 1 : 0);
