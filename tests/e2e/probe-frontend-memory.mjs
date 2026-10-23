import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const { parseHTML } = require('linkedom');

const HTML = readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const failures = [];
function check(label, condition, detail = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
  if (!condition) failures.push(label);
}

const MEMORIES = () => [
  { id: '2a6cf3e94ae2', text: '先给结论再展开', kind: '偏好', hits: 7,
    source: 't-1', created_at: '2026-09-01T10:00:00', last_seen: '2026-10-01T10:00:00' },
  { id: '6940421596ef', text: '叫我涛哥', kind: '身份', hits: 1,
    source: 't-1', created_at: '2026-09-02T10:00:00', last_seen: '2026-09-02T10:00:00' },
  { id: '7e45781bad2b', text: '模型给了个不认识的分类', kind: '其他', hits: 1,
    source: null, created_at: '2026-09-03T10:00:00', last_seen: '2026-09-03T10:00:00' },
];

async function boot({ loggedIn, items = MEMORIES(), patchStatus = 200, deleteStatus = 200 }) {
  const { window, document } = parseHTML(HTML);
  // linkedom 不实现 matchMedia；前端靠它区分手机/桌面，缺了会直接抛
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  globalThis.document = document;
  window.document = document;

  window.marked = { parse: (t) => String(t) };
  window.DOMPurify = { sanitize: (h) => h };

  const store = new Map();
  if (loggedIn) store.set('agent.token', 'tok-123');
  window.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };

  window.setTimeout = () => 0;
  window.clearTimeout = () => {};
  window.requestAnimationFrame = () => 0;
  window.confirm = () => true;
  window.getSelection = () => null;
  window.location = { origin: 'http://127.0.0.1:8000', href: 'http://127.0.0.1:8000/' };

  const calls = [];   // { url, method, body }
  let rows = items;

  window.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    calls.push({ url: String(url), method, body: options.body || null });
    const json = (body, status = 200) => ({
      ok: status < 400,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
      body: null,
    });

    if (url === '/api/auth/me') {
      return loggedIn
        ? json({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true })
        : json({ user_id: 'guest', account: null, guest: true, memory_enabled: false });
    }
    if (url === '/api/conversations') return json([]);
    if (url === '/api/documents') return json(loggedIn ? [] : { detail: '请先登录' }, loggedIn ? 200 : 401);
    if (url === '/api/capabilities') {
      return json({ reasoning: true, web_search: true, memory: true });
    }
    if (url === '/api/memories' && method === 'GET') return json(rows);
    if (String(url).startsWith('/api/memories/') && method === 'DELETE') {
      if (deleteStatus !== 200) return json({ detail: '这条记忆不存在' }, deleteStatus);
      const id = String(url).slice('/api/memories/'.length);
      rows = rows.filter((r) => r.id !== id);
      return json({ status: 'deleted' });
    }
    if (String(url).startsWith('/api/memories/') && method === 'PATCH') {
      if (patchStatus !== 200) {
        return json({ detail: patchStatus === 409 ? '已经有这条了' : '这条记忆不存在' }, patchStatus);
      }
      const id = String(url).slice('/api/memories/'.length);
      const body = JSON.parse(options.body || '{}');
      rows = rows.map((r) => (r.id === id ? { ...r, text: body.text } : r));
      return json({ status: 'updated' });
    }
    return json({}, 404);
  };

  vm.createContext(window);
  try {
    vm.runInContext(script, window);
  } catch (error) {
    check('页面脚本能执行', false, error.message);
    return { window, document, calls };
  }
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
  return { window, document, calls };
}

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
};

{
  const { document } = await boot({ loggedIn: false });
  const entry = document.querySelector('#memoryEntry');
  check('游客看不到记忆入口', entry.hidden === true);
}

{
  const { document, calls } = await boot({ loggedIn: true });
  const entry = document.querySelector('#memoryEntry');
  check('登录后入口出现', entry.hidden === false);

  const modal = document.querySelector('#memoryModal');
  check('默认不打开', modal.hidden === true);

  document.querySelector('#memoryOpen').onclick();
  await flush();
  check('点了才打开', modal.hidden === false);
  check('打开时重新拉了一次列表',
    calls.filter((c) => c.url === '/api/memories' && c.method === 'GET').length === 1,
    JSON.stringify(calls.map((c) => `${c.method} ${c.url}`)));

  const rows = document.querySelectorAll('#memoryList .memory-row');
  check('渲染出 3 条', rows.length === 3, `实际 ${rows.length}`);

  const kinds = [...document.querySelectorAll('#memoryList .memory-kind')].map((n) => n.textContent);
  check('分类标签正确', kinds.join('/') === '偏好/身份/其他', kinds.join('/'));
  const otherKind = document.querySelectorAll('#memoryList .memory-kind')[2];
  check('「其他」有单独的弱化样式', otherKind.className.includes('other'), otherKind.className);

  const hitTexts = [...document.querySelectorAll('#memoryList .memory-hits')].map((n) => n.textContent);
  check('提过多次的显示次数', hitTexts[0] === '提过 7 次', hitTexts[0]);
  check('只提过一次的不显示次数', hitTexts[1] === '', JSON.stringify(hitTexts[1]));

  const texts = [...document.querySelectorAll('#memoryList .memory-text')].map((n) => n.textContent);
  check('原文照原样显示', texts[0] === '先给结论再展开', texts[0]);
}

{
  const { document, calls } = await boot({ loggedIn: true });
  document.querySelector('#memoryOpen').onclick();
  await flush();

  const firstDelete = document.querySelectorAll('#memoryList .memory-op.danger')[1]; // 第二条：叫我涛哥
  await firstDelete.onclick();
  await flush();

  const del = calls.filter((c) => c.method === 'DELETE');
  check('发出了 DELETE', del.length === 1, JSON.stringify(del));
  check('URL 用的是 id 而不是原文',
    del[0].url === '/api/memories/6940421596ef',
    del[0] && del[0].url);
  check('删完重新拉了列表',
    calls.filter((c) => c.url === '/api/memories' && c.method === 'GET').length === 2);

  const texts = [...document.querySelectorAll('#memoryList .memory-text')].map((n) => n.textContent);
  check('删掉的那条从界面上消失', !texts.includes('叫我涛哥'), texts.join('/'));
  check('剩两条', texts.length === 2, `实际 ${texts.length}`);
}

{
  const { document, calls } = await boot({ loggedIn: true });
  document.querySelector('#memoryOpen').onclick();
  await flush();

  const editButton = document.querySelectorAll('#memoryList .memory-op')[2]; // 第二条的「改一下」
  editButton.onclick();

  const input = document.querySelector('#memoryList .memory-edit input');
  check('点「改一下」出现输入框', Boolean(input));
  check('输入框预填了原文', input.value === '叫我涛哥', input && input.value);

  input.value = '叫我涛哥就行';
  await document.querySelectorAll('#memoryList .memory-edit .memory-op')[0].onclick();
  await flush();

  const patch = calls.filter((c) => c.method === 'PATCH');
  check('发出了 PATCH', patch.length === 1, JSON.stringify(patch));
  check('PATCH 的 URL 是 id', patch[0].url === '/api/memories/6940421596ef', patch[0] && patch[0].url);
  check('body 里是新措辞', JSON.parse(patch[0].body).text === '叫我涛哥就行', patch[0] && patch[0].body);

  const texts = [...document.querySelectorAll('#memoryList .memory-text')].map((n) => n.textContent);
  check('界面显示新措辞', texts.includes('叫我涛哥就行'), texts.join('/'));

  const stillThere = [...document.querySelectorAll('#memoryList .memory-row')]
    .find((r) => r.querySelector('.memory-text').textContent === '叫我涛哥就行');
  check('改完那条还在列表里', Boolean(stillThere));
  if (stillThere) {
    await stillThere.querySelector('.memory-op.danger').onclick();
    await flush();
    const after = calls.filter((c) => c.method === 'DELETE');
    check('改完马上删，用的还是同一个 id',
      after.length === 1 && after[0].url === '/api/memories/6940421596ef',
      JSON.stringify(after));
    const left = [...document.querySelectorAll('#memoryList .memory-text')].map((n) => n.textContent);
    check('确实删掉了', !left.includes('叫我涛哥就行'), left.join('/'));
  }
}

{
  const { document, calls } = await boot({ loggedIn: true, patchStatus: 409 });
  document.querySelector('#memoryOpen').onclick();
  await flush();

  document.querySelectorAll('#memoryList .memory-op')[0].onclick();
  const input = document.querySelector('#memoryList .memory-edit input');
  input.value = '叫我涛哥';
  await document.querySelectorAll('#memoryList .memory-edit .memory-op')[0].onclick();
  await flush();

  const err = document.querySelector('#memoryError').textContent;
  check('409 时给出「已经有了」的提示', err.includes('已经有这条了'), err);
  check('409 时输入框还在（能接着改）',
    Boolean(document.querySelector('#memoryList .memory-edit input')));
}

{
  const { document, calls } = await boot({ loggedIn: true });
  document.querySelector('#memoryOpen').onclick();
  await flush();

  document.querySelectorAll('#memoryList .memory-op')[0].onclick();
  const input = document.querySelector('#memoryList .memory-edit input');
  const before = calls.length;
  input.value = '   ';
  await document.querySelectorAll('#memoryList .memory-edit .memory-op')[0].onclick();
  await flush();
  check('改成空不发请求', calls.filter((c) => c.method === 'PATCH').length === 0,
    JSON.stringify(calls.slice(before)));
  check('改成空有提示', document.querySelector('#memoryError').textContent.includes('不能改成空'));
}

{
  const { document, calls } = await boot({ loggedIn: true });
  document.querySelector('#memoryOpen').onclick();
  await flush();

  document.querySelectorAll('#memoryList .memory-op')[0].onclick();
  document.querySelector('#memoryList .memory-edit input').value = '随便改改';
  await document.querySelectorAll('#memoryList .memory-edit .memory-op')[1].onclick(); // 取消
  await flush();
  check('取消不发 PATCH', calls.filter((c) => c.method === 'PATCH').length === 0);
  const texts = [...document.querySelectorAll('#memoryList .memory-text')].map((n) => n.textContent);
  check('取消后回到原文', texts[0] === '先给结论再展开', texts[0]);
}

{
  const { document } = await boot({ loggedIn: true, items: [] });
  document.querySelector('#memoryOpen').onclick();
  await flush();
  const empty = document.querySelector('#memoryList .memory-empty');
  check('空列表显示空态', Boolean(empty), empty && empty.textContent.slice(0, 20));
  check('空态不提「删除」之类的误导文案',
    !(empty.textContent || '').includes('删除'), empty && empty.textContent);
}

{
  const { document } = await boot({ loggedIn: true, deleteStatus: 404 });
  document.querySelector('#memoryOpen').onclick();
  await flush();
  await document.querySelector('#memoryList .memory-op.danger').onclick();
  await flush();
  check('404 时有提示', document.querySelector('#memoryError').textContent.includes('已经不在了'),
    document.querySelector('#memoryError').textContent);
}

{
  const { window, document } = await boot({ loggedIn: true });
  const modal = document.querySelector('#memoryModal');
  document.querySelector('#memoryOpen').onclick();
  await flush();

  document.querySelector('#memoryClose').onclick();
  check('X 能关', modal.hidden === true);

  document.querySelector('#memoryOpen').onclick();
  await flush();
  modal.dispatchEvent(new window.Event('click', { bubbles: false }));
  check('点遮罩关闭', modal.hidden === true);

  document.querySelector('#memoryOpen').onclick();
  await flush();
  const card = modal.querySelector('.modal-card');
  card.dispatchEvent(new window.Event('click', { bubbles: true }));
  check('点卡片内部不关闭', modal.hidden === false, String(modal.hidden));
}

console.log(failures.length ? `\n失败：${failures.join(', ')}` : '\n全部通过');
process.exit(failures.length ? 1 : 0);
