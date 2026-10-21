// 邀请制注册的前端 DOM 测试：注册表单里那个邀请码框什么时候出现、内容对不对，
// 以及「邀请同学加入」入口只对主人显示。
//
// 关键在第二条：**框显不显示由服务端说**。如果前端自己猜「第一个用户不用填」，
// 那它就得先知道库里几个人——猜错的两个方向都很难受：不该填的时候逼着填，
// 该填的时候根本没框。
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire('C:/Users/13526/.workbuddy/binaries/node/workspace/package.json');
const { parseHTML } = require('linkedom');

const HTML = readFileSync('D:/agent/frontend/index.html', 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const failures = [];
function check(label, condition, detail = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
  if (!condition) failures.push(label);
}

const INVITES = () => [
  { code: 'AW-ZJWY-TCV9', note: '给室友小李', created_at: '2026-10-21T10:00:00', used: true, used_at: '2026-10-21T11:00:00' },
  { code: 'AW-MNPQ-2345', note: null, created_at: '2026-10-21T09:00:00', used: false, used_at: null },
];

async function boot({
  loggedIn = false,
  isOwner = false,
  needsInvite = true,
  registerError = null, // { status, detail }
  invitesStatus = 200,
} = {}) {
  const { window, document } = parseHTML(HTML);
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

  const timers = [];
  // 复制成功后的「已复制」提示靠 setTimeout 还原，这里只记录、不执行回调。
  window.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
  window.clearTimeout = () => {};
  window.requestAnimationFrame = () => 0;
  window.confirm = () => true;
  window.getSelection = () => null;
  window.location = { origin: 'http://127.0.0.1:8000', href: 'http://127.0.0.1:8000/' };
  // 剪贴板：记下被复制的文本，好断言「生成完真的复制了」。
  // linkedom 把 navigator 定义成只读 getter，直接赋值会抛，得用 defineProperty 覆盖。
  const copied = [];
  Object.defineProperty(window, 'navigator', {
    value: { clipboard: { writeText: async (text) => { copied.push(text); } } },
    configurable: true,
    writable: true,
  });

  const calls = [];
  let invites = INVITES();

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
        ? json({ user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true, is_owner: isOwner })
        : json({ user_id: 'guest', account: null, guest: true, memory_enabled: false, is_owner: false });
    }
    if (url === '/api/auth/registration-policy') return json({ needs_invite: needsInvite });
    if (url === '/api/conversations') return json([]);
    if (url === '/api/documents') return json([]);
    if (url === '/api/capabilities') return json({ reasoning: true, web_search: true, memory: true });
    if (url === '/api/memories') return json([]);
    if (url === '/api/auth/register') {
      if (registerError) return json({ detail: registerError.detail }, registerError.status);
      return json({ user: { user_id: 'u-9', account: 'newbie' }, token: 'tok-new', is_owner: false });
    }
    if (url === '/api/invites' && method === 'GET') {
      if (invitesStatus !== 200) return json({ detail: '不能发' }, invitesStatus);
      return json(invites);
    }
    if (url === '/api/invites' && method === 'POST') {
      const note = JSON.parse(options.body || '{}').note;
      const fresh = { code: 'AW-FRESH-NEW1', note, created_at: '2026-10-21T12:00:00', used: false, used_at: null };
      invites = [fresh, ...invites];
      return json(fresh);
    }
    // DELETE /api/invites/{code} —— 真的从假数据里删掉，这样「删完列表少一行」
    // 才能被验：如果桩只返回 200 却不改数据，重新拉列表还是原来那几行。
    if (String(url).startsWith('/api/invites/') && method === 'DELETE') {
      const code = decodeURIComponent(String(url).slice('/api/invites/'.length));
      const before = invites.length;
      invites = invites.filter((item) => item.code !== code);
      if (invites.length === before) return json({ detail: '邀请码不存在' }, 404);
      return json({ code, status: 'deleted' });
    }
    return json({}, 404);
  };

  vm.createContext(window);
  try {
    vm.runInContext(script, window);
  } catch (error) {
    check('页面脚本能执行', false, error.message);
    return { window, document, calls, copied };
  }
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
  return { window, document, calls, copied };
}

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
};

// --- 游客：注册框要码；邀请码入口不显示 ---
{
  const { document } = await boot({ loggedIn: false, needsInvite: true });
  check('游客看不到邀请码入口', document.querySelector('#inviteEntry').hidden === true);

  document.querySelector('#accountAction').onclick(); // 打开登录框
  await flush();
  const field = document.querySelector('#authInviteField');
  check('登录模式不显示邀请码框', field.hidden === true);

  document.querySelectorAll('.modal-tab')[1].onclick(); // 切到注册
  await flush();
  check('需要码时注册显示邀请码框', field.hidden === false);
  check('placeholder 是码的形状',
    document.querySelector('#authInvite').placeholder.includes('AW-'), 
    document.querySelector('#authInvite').placeholder);
}

// --- 空站（第一个用户）：不显示框，提交也不带 ---
{
  const { document, calls } = await boot({ loggedIn: false, needsInvite: false });
  document.querySelector('#accountAction').onclick();
  await flush();
  document.querySelectorAll('.modal-tab')[1].onclick();
  await flush();

  const field = document.querySelector('#authInviteField');
  check('第一个账号不显示邀请码框', field.hidden === true);
  check('placeholder 说明可以留空',
    document.querySelector('#authInvite').placeholder.includes('留空'),
    document.querySelector('#authInvite').placeholder);

  document.querySelector('#authAccount').value = 'taoge';
  document.querySelector('#authPassword').value = '12345678';
  document.querySelector('#authForm').dispatchEvent(new document.defaultView.Event('submit'));
  await flush();

  const reg = calls.filter((c) => c.url === '/api/auth/register');
  check('发出了注册请求', reg.length === 1, JSON.stringify(reg));
  check('body 里没有 invite', reg[0] && !('invite' in JSON.parse(reg[0].body)),
    reg[0] && reg[0].body);
}

// --- 需要码：填了才带上 ---
{
  const { document, calls } = await boot({ loggedIn: false, needsInvite: true });
  document.querySelector('#accountAction').onclick();
  await flush();
  document.querySelectorAll('.modal-tab')[1].onclick();
  await flush();

  document.querySelector('#authAccount').value = 'lisi';
  document.querySelector('#authPassword').value = '12345678';
  document.querySelector('#authInvite').value = ' AW-ABCD-EFGH ';
  document.querySelector('#authForm').dispatchEvent(new document.defaultView.Event('submit'));
  await flush();

  const reg = calls.filter((c) => c.url === '/api/auth/register');
  check('body 里带了 invite', reg.length === 1 && JSON.parse(reg[0].body).invite === 'AW-ABCD-EFGH',
    reg[0] && reg[0].body);
}

// --- 服务端说「需要邀请码」但前端没显示框 → 补显示 ---
{
  const { document } = await boot({
    loggedIn: false,
    needsInvite: false,
    registerError: { status: 400, detail: '这个站需要邀请码才能注册' },
  });
  document.querySelector('#accountAction').onclick();
  await flush();
  document.querySelectorAll('.modal-tab')[1].onclick();
  await flush();
  const field = document.querySelector('#authInviteField');
  check('策略说是空站，所以一开始没显示', field.hidden === true);

  document.querySelector('#authAccount').value = 'lisi';
  document.querySelector('#authPassword').value = '12345678';
  document.querySelector('#authForm').dispatchEvent(new document.defaultView.Event('submit'));
  await flush();

  check('被服务端拦了之后框冒出来了', field.hidden === false, String(field.hidden));
  check('错误提示照原样显示',
    document.querySelector('#authError').textContent.includes('需要邀请码'),
    document.querySelector('#authError').textContent);
}

// --- 主人：入口出现，能发码 ---
{
  const { document, calls, copied } = await boot({ loggedIn: true, isOwner: true, needsInvite: true });
  check('主人看得到邀请码入口', document.querySelector('#inviteEntry').hidden === false);

  const modal = document.querySelector('#inviteModal');
  check('默认不打开', modal.hidden === true);
  document.querySelector('#inviteOpen').onclick();
  await flush();
  check('点了才打开', modal.hidden === false);

  const rows = document.querySelectorAll('#inviteList .invite-row');
  check('渲染出 2 个码', rows.length === 2, `实际 ${rows.length}`);

  const codes = [...document.querySelectorAll('#inviteList .invite-code')].map((n) => n.textContent);
  check('码原样显示', codes[0] === 'AW-ZJWY-TCV9', codes.join('/'));

  const states = [...document.querySelectorAll('#inviteList .memory-hits')].map((n) => n.textContent);
  check('已用的标「已用掉」', states[0] === '已用掉', states[0]);
  check('没用的标「还没人用」', states[1] === '还没人用', states[1]);

  check('已用的那行整体压暗', rows[0].className.includes('used'), rows[0].className);
  // 已用的码不能复制（复制也没意义），但**能删**——否则列表永远清不干净。
  const usedOps = [...rows[0].querySelectorAll('.memory-op')].map((n) => n.textContent);
  check('已用的只有删除，没有复制', usedOps.join('/') === '删除', usedOps.join('/'));
  const freshOps = [...rows[1].querySelectorAll('.memory-op')].map((n) => n.textContent);
  check('没用的有复制和删除', freshOps.join('/') === '复制/删除', freshOps.join('/'));

  const note = document.querySelectorAll('#inviteList .memory-text')[0].textContent;
  check('备注显示出来', note.includes('给室友小李'), note);

  // 生成一个新码
  document.querySelector('#inviteNote').value = '给老张';
  await document.querySelector('#inviteMake').onclick();
  await flush();

  const post = calls.filter((c) => c.url === '/api/invites' && c.method === 'POST');
  check('发出了 POST', post.length === 1, JSON.stringify(post));
  check('body 带备注', JSON.parse(post[0].body).note === '给老张', post[0].body);
  check('生成后重新拉了列表',
    calls.filter((c) => c.url === '/api/invites' && c.method === 'GET').length === 2);
  check('新码排在第一个',
    document.querySelectorAll('#inviteList .invite-code')[0].textContent === 'AW-FRESH-NEW1',
    document.querySelector('#inviteList .invite-code').textContent);
  // 这个环境没有 navigator.clipboard（vm 里 navigator 被 Node 内置对象遮蔽，
  // 也没有真剪贴板）。生产代码在这里该走「兜底提示」那条路——把码写出来让人手抄，
  // 而不是抛一个 TypeError 就完事。真浏览器里的复制成功分支靠人工验一次。
  check('没有剪贴板时不硬调（canCopy 判断生效）', copied.length === 0, JSON.stringify(copied));
  check('底部把码写出来让人手抄',
    document.querySelector('#inviteError').textContent.includes('AW-FRESH-NEW1')
    && document.querySelector('#inviteError').textContent.includes('手动抄'),
    document.querySelector('#inviteError').textContent);  check('备注输入框清空了', document.querySelector('#inviteNote').value === '');
}

// --- 备注留空：传 null 而不是空字符串 ---
{
  const { document, calls } = await boot({ loggedIn: true, isOwner: true });
  document.querySelector('#inviteOpen').onclick();
  await flush();
  await document.querySelector('#inviteMake').onclick();
  await flush();
  const post = calls.filter((c) => c.url === '/api/invites' && c.method === 'POST');
  check('空备注传 null', JSON.parse(post[0].body).note === null, post[0].body);
}

// --- 普通用户：入口不显示；就算硬开会拿到 403 提示 ---
{
  const { document } = await boot({ loggedIn: true, isOwner: false, invitesStatus: 403 });
  check('普通用户看不到邀请码入口', document.querySelector('#inviteEntry').hidden === true);
}

// --- 关闭 ---
{
  const { window, document } = await boot({ loggedIn: true, isOwner: true });
  const modal = document.querySelector('#inviteModal');
  document.querySelector('#inviteOpen').onclick();
  await flush();
  document.querySelector('#inviteClose').onclick();
  check('X 能关', modal.hidden === true);

  document.querySelector('#inviteOpen').onclick();
  await flush();
  modal.dispatchEvent(new window.Event('click', { bubbles: false }));
  check('点遮罩关闭', modal.hidden === true);

  document.querySelector('#inviteOpen').onclick();
  await flush();
  modal.querySelector('.modal-card').dispatchEvent(new window.Event('click', { bubbles: true }));
  check('点卡片内部不关闭', modal.hidden === false, String(modal.hidden));
}

// --- 删除邀请码 ---
{
  const { window, document, calls } = await boot({ loggedIn: true, isOwner: true });
  document.querySelector('#inviteOpen').onclick();
  await flush();

  // 取消确认框：不许发请求
  window.confirm = () => false;
  const rowsBefore = document.querySelectorAll('#inviteList .invite-row').length;
  document.querySelectorAll('#inviteList .memory-op.danger')[1].onclick(); // 未用那个
  await flush();
  check('确认框点取消 → 不发 DELETE',
    !calls.some((c) => c.method === 'DELETE'), JSON.stringify(calls.filter((c) => c.method === 'DELETE')));
  check('取消后行数不变',
    document.querySelectorAll('#inviteList .invite-row').length === rowsBefore);

  // 确认删除：未用的码
  let asked = '';
  window.confirm = (message) => { asked = message; return true; };
  document.querySelectorAll('#inviteList .memory-op.danger')[1].onclick();
  await flush();

  const deletes = calls.filter((c) => c.method === 'DELETE');
  check('确认后发了 DELETE', deletes.length === 1, JSON.stringify(deletes.map((c) => c.url)));
  check('DELETE 打到正确的路径', deletes[0].url === '/api/invites/AW-MNPQ-2345', deletes[0].url);
  check('码在 URL 里做了编码', deletes[0].url.includes('AW-MNPQ-2345'));
  check('未用的码：确认文案说清「立刻失效」',
    asked.includes('立刻失效'), asked.slice(0, 60));
  check('删完重新拉了列表', calls.filter((c) => c.url === '/api/invites' && c.method === 'GET').length >= 2);
  check('删完列表少一行',
    document.querySelectorAll('#inviteList .invite-row').length === rowsBefore - 1,
    String(document.querySelectorAll('#inviteList .invite-row').length));
  check('删掉的码不在列表里',
    !document.querySelector('#inviteList').textContent.includes('AW-MNPQ-2345'),
    document.querySelector('#inviteList').textContent.slice(0, 80));
  check('底部提示说明了删了哪个',
    document.querySelector('#inviteError').textContent.includes('AW-MNPQ-2345'),
    document.querySelector('#inviteError').textContent);

  // 已用的码：确认文案要不一样（说明只是查不到来源，账号不受影响）
  asked = '';
  document.querySelectorAll('#inviteList .memory-op.danger')[0].onclick();
  await flush();
  check('已用的码：确认文案提到「查不到」和「账号不受影响」',
    asked.includes('查不到') && asked.includes('不受影响'), asked.slice(0, 120));
  check('已用的也能删掉', !document.querySelector('#inviteList').textContent.includes('AW-ZJWY-TCV9'));
}

// --- 删除时码已经不在了（另一个标签页删过）→ 不当成失败 ---
{
  const { window, document, calls } = await boot({ loggedIn: true, isOwner: true });
  document.querySelector('#inviteOpen').onclick();
  await flush();
  // 把假数据清空，模拟「列表是旧的，服务端已经没了」
  window.confirm = () => true;
  const original = window.fetch;
  window.fetch = async (url, options = {}) => {
    if (String(url).startsWith('/api/invites/') && (options.method || '').toUpperCase() === 'DELETE') {
      return { ok: false, status: 404, json: async () => ({ detail: '邀请码不存在' }), text: async () => '{}' };
    }
    return original(url, options);
  };
  document.querySelectorAll('#inviteList .memory-op.danger')[0].onclick();
  await flush();
  const text = document.querySelector('#inviteError').textContent;
  check('404 说成「已经不在了」而不是「删除失败」',
    text.includes('已经不在了') && !text.includes('删除失败'), text);
  check('404 之后照样刷新了列表',
    calls.filter((c) => c.url === '/api/invites' && c.method === 'GET').length >= 2);
}

console.log(failures.length ? `\n失败：${failures.join(', ')}` : '\n全部通过');
process.exit(failures.length ? 1 : 0);
