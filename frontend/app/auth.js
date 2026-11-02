// 身份：登录注册、令牌落地、401 后的降级
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { loadConversations, resetConversationView } from './conversations.js';
import { $, store, writeToken } from './core.js';
import { setHint } from './feedback.js';
import { closeInvite, syncInviteEntry } from './invite.js';
import { closeMemory, syncMemoryEntry } from './memory.js';

// 本模块用到的 DOM 节点
const input = $('#input');
const accountName = $('#accountName');
const accountNote = $('#accountNote');
const accountAction = $('#accountAction');
const authModal = $('#authModal');
const authForm = $('#authForm');
const authAccount = $('#authAccount');
const authPassword = $('#authPassword');
const authInviteField = $('#authInviteField');
const authInvite = $('#authInvite');
const authError = $('#authError');
const authSubmit = $('#authSubmit');

export function applyIdentity(me) {
  store.identity = me && !me.guest
    ? {
        account: me.account,
        guest: false,
        memory_enabled: Boolean(me.memory_enabled),
        // 老服务端不返回这个字段时按「不是主人」处理：宁可少显示一个入口，
        // 也不要让每个人看到一个点进去挨 403 的按钮。
        is_owner: Boolean(me.is_owner),
      }
    : { account: null, guest: true, memory_enabled: false, is_owner: false };
  accountName.textContent = store.identity.guest ? '未登录' : store.identity.account;
  // 访客的身份是按浏览器发的（签名 cookie），所以会话只在这里可见；
  // 数据还有 7 天过期。这两句话是「为什么别人看不到我的会话」的答案，
  // 不写清楚会被当成 bug 问。
  accountNote.textContent = store.identity.guest
    ? '游客模式 · 仅本浏览器可见，7 天后自动清理'
    : '已登录 · 长期记忆已开启';
  accountAction.textContent = store.identity.guest ? '登录 / 注册' : '退出';
  accountAction.classList.toggle('danger', !store.identity.guest);
  input.placeholder = store.identity.guest
    ? '以游客身份提问（仅本浏览器可见）'
    : '问点什么…';
  // 两个侧栏入口都跟着身份走。弹窗要是还开着，里面装的是上一个身份的东西，
  // 必须一起关掉——留着会让人以为看见的是自己新账号的数据。
  syncMemoryEntry();
  syncInviteEntry();
  closeMemory();
  closeInvite();
}

export async function refreshIdentity() {
  let me = null;
  try {
    const response = await fetch('/api/auth/me', {
      headers: store.token ? { Authorization: `Bearer ${store.token}` } : {}
    });
    if (response.ok) me = await response.json();
    else if (response.status === 401) writeToken(null);
  } catch (error) {
    // 后端没起来：先按游客显示，真正聊天时会在 .hint 上给出连接错误
  }
  applyIdentity(me);
}

export async function loadRegistrationPolicy() {
  // 原生 fetch：这个端点不需要登录，走 apiFetch 会在 401 时误触发登出逻辑。
  try {
    const response = await fetch('/api/auth/registration-policy');
    if (response.ok) {
      const data = await response.json();
      store.needsInvite = Boolean(data.needs_invite);
    }
  } catch (error) {
    // 拿不到就先保持 null（界面按「要填」显示），注册时服务端会给出真相。
  }
  syncAuthMode();
}

export function openAuth(mode) {
  store.authMode = mode === 'register' ? 'register' : 'login';
  syncAuthMode();
  authError.textContent = '';
  authPassword.value = '';
  authInvite.value = '';
  // 每次点「注册」都重新问一次：上一个页面停留期间可能有人在别处
  // 注册了第一个账号，规矩就变了。这个请求就一次 COUNT，便宜。
  if (store.authMode === 'register') loadRegistrationPolicy();
  authModal.hidden = false;
  authAccount.focus({ preventScroll: true });
}

export function closeAuth() {
  authModal.hidden = true;
  authPassword.value = '';
  authInvite.value = '';
  authError.textContent = '';
}

export function syncAuthMode() {
  document.querySelectorAll('.modal-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.mode === store.authMode);
  });
  const registering = store.authMode === 'register';
  authSubmit.textContent = registering ? '注册' : '登录';
  authPassword.placeholder = registering ? '至少 8 位' : '你的密码';
  authPassword.setAttribute('autocomplete', registering ? 'new-password' : 'current-password');
  // 只有「确定不需要码」时才藏起来。还没问到（null）就照常显示——
  // 万一其实要码，藏起来会让用户彻底没法注册；多显示一个可选框是无害的。
  const askInvite = registering && store.needsInvite !== false;
  authInviteField.hidden = !askInvite;
  // 只有真的知道「这是空站」时才说可以留空。还没问到就别乱许诺，
  // 不然用户会以为不填也能注册，提交后才发现被拦。
  authInvite.placeholder = store.needsInvite === false ? '第一个账号可以留空' : 'AW-XXXX-XXXX';
}

export async function adoptSession(data, message) {
  writeToken(data.token);
  // 注册/登录接口直接把 is_owner 一起返回了，不用为此再打一次 /api/auth/me。
  applyIdentity({
    account: data.user.account,
    guest: false,
    memory_enabled: true,
    is_owner: Boolean(data.is_owner),
  });
  closeAuth();
  resetConversationView();
  await loadConversations();
  setHint(message);
}

export async function doLogout() {
  try {
    // 登出接口刻意不要求登录态有效：令牌过期时也得能登出，否则用户会卡在一个
    // 点了没反应的按钮上。所以这里失败也不拦着下面清本地状态。
    await fetch('/api/auth/logout', {
      method: 'POST',
      headers: store.token ? { Authorization: `Bearer ${store.token}` } : {}
    });
  } catch (error) {
    // 忽略：本地状态清掉就够了
  }
  writeToken(null);
  applyIdentity(null);
  resetConversationView();
  await loadConversations();
  setHint('已退出登录，之后的对话不会被记住。');
}

authForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const account = authAccount.value.trim();
  const password = authPassword.value;
  authError.textContent = '';
  if (!account || !password) {
    authError.textContent = '账号和密码都要填。';
    return;
  }
  authSubmit.disabled = true;
  try {
    // 走原生 fetch：这里失败了只该显示在弹窗里，不该顺手把外面的界面
    // 也切回游客态（用户只是输错一次密码）。
    const payload = { account, password };
    // 注册才可能带邀请码。**不在前端拦「必须填」**——要不要码只有服务端知道，
    // 这里拦了就会在「第一个账号」上逼人填一个根本不该存在的码。
    if (store.authMode === 'register') {
      const invite = authInvite.value.trim();
      if (invite) payload.invite = invite;
    }
    const response = await fetch(`/api/auth/${store.authMode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      authError.textContent = data.detail || `HTTP ${response.status}`;
      // 服务端说「需要邀请码」而我们没显示输入框（策略拿过时了）→ 补显示出来，
      // 别让用户对着「需要邀请码」四个字却无处可填。
      if (response.status === 400 && String(data.detail || '').includes('邀请码')) {
        store.needsInvite = true;
        syncAuthMode();
      }
      return;
    }
    await adoptSession(data, store.authMode === 'login' ? '已登录。' : '注册成功，已自动登录。');
  } catch (error) {
    authError.textContent = `无法连接后端：${error.message}`;
  } finally {
    authSubmit.disabled = false;
  }
});

document.querySelectorAll('.modal-tab').forEach((tab) => {
  tab.onclick = () => {
    store.authMode = tab.dataset.mode;
    syncAuthMode();
    authError.textContent = '';
    // 直接点「注册」标签页进来的（没经过 openAuth）也要问一次规矩，
    // 否则 store.needsInvite 一直是 null，界面只能按「要填」猜。
    if (store.authMode === 'register' && store.needsInvite === null) loadRegistrationPolicy();
  };
});

$('#authClose').onclick = closeAuth;

accountAction.onclick = () => {
  if (store.identity && !store.identity.guest) doLogout(); else openAuth('login');
};

authModal.addEventListener('mousedown', (event) => {
  if (event.target === authModal) closeAuth();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !authModal.hidden) closeAuth();
});
