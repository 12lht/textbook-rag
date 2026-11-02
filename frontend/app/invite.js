// 邀请码面板
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch, readError } from './api.js';
import { $, state, store } from './core.js';

// 本模块用到的 DOM 节点
const inviteEntry = $('#inviteEntry');
const inviteModal = $('#inviteModal');
const inviteList = $('#inviteList');
const inviteError = $('#inviteError');
const inviteNote = $('#inviteNote');
const inviteMake = $('#inviteMake');

export function syncInviteEntry() {
  // 游客没有身份，普通用户不是主人——两种都不显示。
  inviteEntry.hidden = !store.identity || store.identity.guest || !store.identity.is_owner;
}

export function openInvite() {
  inviteError.textContent = '';
  inviteNote.value = '';
  inviteModal.hidden = false;
  loadInvites();
}

export function closeInvite() {
  inviteModal.hidden = true;
  inviteError.textContent = '';
}

export function inviteNote_(message) {
  inviteError.textContent = message || '';
}

export async function loadInvites() {
  inviteList.innerHTML = '<div class="memory-empty">正在读取…</div>';
  let items = [];
  try {
    const response = await apiFetch('/api/invites');
    if (!response.ok) {
      inviteList.innerHTML = '';
      inviteNote_(response.status === 403
        ? '这个账号不能发邀请码。'
        : `读取失败：${await readError(response, '读取失败')}`);
      return;
    }
    items = await response.json();
  } catch (error) {
    inviteList.innerHTML = '';
    inviteNote_('连不上服务，稍后再试。');
    return;
  }
  renderInvites(items);
}

export function renderInvites(items) {
  inviteList.innerHTML = '';
  if (!items.length) {
    inviteList.innerHTML = '<div class="memory-empty">还没发过邀请码。<br>上面填个备注，生成一个发给同学。</div>';
    return;
  }
  items.forEach((item) => inviteList.appendChild(inviteRow(item)));
}

export function inviteRow(item) {
  const row = document.createElement('div');
  // 已用的码整行压暗 + 划掉：一眼能看出还剩几个能用
  row.className = item.used ? 'memory-row invite-row used' : 'memory-row invite-row';

  const head = document.createElement('div');
  head.className = 'memory-head';
  const code = document.createElement('span');
  code.className = 'invite-code';
  code.textContent = item.code;
  const state = document.createElement('span');
  state.className = 'memory-hits';
  state.textContent = item.used ? '已用掉' : '还没人用';
  head.append(code, state);

  const meta = document.createElement('div');
  meta.className = 'memory-text';
  meta.textContent = item.note ? `备注：${item.note}` : '（没写备注）';

  row.append(head, meta);

  // 删除对已用和未用都开着，但后果不同：未用的删掉 = 立刻作废；
  // 已用的删掉 = 只丢掉「这个账号是用哪个码进来的」。只让删未用的，列表永远清不干净。
  const ops = document.createElement('div');
  ops.className = 'memory-ops';
  // 用过的码没复制的意义了，省掉按钮也少一次误点。
  if (!item.used) {
    const copy = document.createElement('button');
    copy.className = 'memory-op';
    copy.type = 'button';
    copy.textContent = '复制';
    copy.onclick = () => copyInvite(item.code, copy);
    ops.appendChild(copy);
  }
  const remove = document.createElement('button');
  remove.className = 'memory-op danger';
  remove.type = 'button';
  remove.textContent = '删除';
  remove.onclick = () => deleteInvite(item, remove);
  ops.appendChild(remove);
  row.appendChild(ops);
  return row;
}

export async function deleteInvite(item, button) {
  const tip = item.used
    ? `删除邀请码 ${item.code}？\n\n这个码已经被用掉了。删除后「是谁用它注册的」就查不到了，但那个账号本身不受影响。`
    : `删除邀请码 ${item.code}？\n\n它还没被用过，删掉就立刻失效——拿着这个码的人将无法注册。`;
  if (!window.confirm(tip)) return;
  button.disabled = true;
  try {
    const response = await apiFetch(
      `/api/invites/${encodeURIComponent(item.code)}`,
      { method: 'DELETE' },
    );
    if (response.status === 404) {
      // 别处（另一个标签页）已经删过了。照样刷新，列表才对得上。
      inviteNote_(`${item.code} 已经不在了。`);
      await loadInvites();
      return;
    }
    if (!response.ok) {
      inviteNote_(`删除失败：${await readError(response, '删除失败')}`);
      button.disabled = false;
      return;
    }
    inviteNote_(`已删除 ${item.code}。`);
    await loadInvites();
  } catch (error) {
    inviteNote_('连不上服务，稍后再试。');
    button.disabled = false;
  }
}

export function canCopy() {
  return typeof navigator !== 'undefined'
    && navigator.clipboard
    && typeof navigator.clipboard.writeText === 'function';
}

export async function copyInvite(code, button) {
  if (!canCopy()) {
    inviteNote_(`复制不了，手动抄一下：${code}`);
    return;
  }
  try {
    await navigator.clipboard.writeText(code);
    const original = button.textContent;
    button.textContent = '已复制';
    window.setTimeout(() => { button.textContent = original; }, 1400);
    inviteNote_('');
  } catch (error) {
    // 浏览器明确拒绝（权限、非安全上下文）。同样把码写出来让人手抄。
    inviteNote_(`复制不了，手动抄一下：${code}`);
  }
}

export async function makeInvite() {
  if (store.inviteBusy) return;
  store.inviteBusy = true;
  inviteMake.disabled = true;
  inviteNote_('');
  try {
    const response = await apiFetch('/api/invites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 备注可以空着；空着传 null 而不是空字符串，免得库里存一堆 ''。
      body: JSON.stringify({ note: inviteNote.value.trim() || null })
    });
    if (response.status === 403) { inviteNote_('这个账号不能发邀请码。'); return; }
    if (!response.ok) {
      inviteNote_(`生成失败：${await readError(response, '生成失败')}`);
      return;
    }
    const created = await response.json();
    inviteNote.value = '';
    await loadInvites();
    // 生成完顺手复制：这一步几乎总是紧接着「发给同学」，省掉再点一次。
    // 复制失败也只是提示，不影响「码已经生成」这个事实。
    if (canCopy()) {
      try {
        await navigator.clipboard.writeText(created.code);
        inviteNote_(`已生成并复制：${created.code}`);
      } catch (error) {
        inviteNote_(`已生成：${created.code}（复制不了，手动抄一下）`);
      }
    } else {
      inviteNote_(`已生成：${created.code}（复制不了，手动抄一下）`);
    }
  } catch (error) {
    inviteNote_('连不上服务，稍后再试。');
  } finally {
    store.inviteBusy = false;
    inviteMake.disabled = false;
  }
}

$('#inviteOpen').onclick = openInvite;
$('#inviteClose').onclick = closeInvite;
inviteMake.onclick = makeInvite;

inviteNote.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); makeInvite(); }
});

inviteModal.addEventListener('click', (event) => {
  if (event.target === inviteModal) closeInvite();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !inviteModal.hidden) closeInvite();
});
