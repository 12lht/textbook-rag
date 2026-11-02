// 会话：列表、切换、回退、重命名、删除
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch, readError } from './api.js';
import { $, state, store } from './core.js';
import { setBusy, setHint, setStatus, updateScrollBottomButton } from './feedback.js';
import { closeSidebarIfMobile } from './layout.js';
import { renderHistory } from './messages.js';

// 本模块用到的 DOM 节点
const list = $('#conversationList');
const messages = $('#messages');
const welcome = $('#welcome');
const input = $('#input');
const conversationTitle = $('#conversationTitle');
const conversationMenu = $('#conversationMenu');

export function resetConversationView() {
  state.threadId = null;
  state.autoFollow = true;
  conversationTitle.textContent = '未选择会话';
  messages.innerHTML = '';
  welcome.style.display = 'block';
  updateScrollBottomButton();
}

export async function loadConversations() {
  // 列表每次都是整块重建，菜单里缓存的 row 会变成脱离 DOM 的旧节点；
  // 顺手收起来，避免「重命名」改到一个看不见的行上。
  closeConversationMenu();
  let data;
  try {
    const response = await apiFetch('/api/conversations');
    if (!response.ok) {
      setHint(`会话列表加载失败：${await readError(response, '会话列表加载失败')}`, true);
      return;
    }
    data = await response.json();
  } catch (error) {
    setHint(`无法连接后端：${error.message}`, true);
    return;
  }
  const activeConversation = data.find((conversation) => conversation.thread_id === state.threadId);
  conversationTitle.textContent = activeConversation ? activeConversation.summary : '未选择会话';
  list.innerHTML = '';
  data.forEach((conversation) => {
    const row = document.createElement('div');
    row.className = 'conversation-item';
    const button = document.createElement('button');
    button.className = `conversation ${conversation.thread_id === state.threadId ? 'active' : ''}`;
    button.innerHTML = `<div class="conversation-title"></div><div class="conversation-count">${conversation.message_count} 条消息</div>`;
    button.querySelector('.conversation-title').textContent = conversation.summary;
    button.onclick = () => selectConversation(conversation.thread_id);
    // 删除和重命名都收进右键菜单，列表里不再常住一个删除按钮
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      openConversationMenu(event.clientX, event.clientY, row, conversation);
    });
    row.append(button);
    list.appendChild(row);
  });
}

export async function deleteConversation(threadId, summary) {
  if (!window.confirm(`确定删除“${summary}”吗？此操作不可恢复。`)) return;
  const response = await apiFetch(`/api/conversations/${encodeURIComponent(threadId)}`, { method: 'DELETE' });
  if (!response.ok) {
    setHint(`删除失败：${await readError(response, '删除失败')}`, true);
    return;
  }
  if (state.threadId === threadId) {
    state.threadId = null;
    state.autoFollow = true;
    conversationTitle.textContent = '未选择会话';
    messages.innerHTML = '';
    welcome.style.display = 'block';
    updateScrollBottomButton();
  }
  await loadConversations();
  setStatus('');
}

export function openConversationMenu(x, y, row, conversation) {
  store.menuTarget = { row, conversation };
  conversationMenu.hidden = false;
  // 先显示再量尺寸，才拿得到菜单自身的宽高来做视口内收边
  const rect = conversationMenu.getBoundingClientRect();
  const left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
  const top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
  conversationMenu.style.left = `${left}px`;
  conversationMenu.style.top = `${top}px`;
}

export function closeConversationMenu() {
  conversationMenu.hidden = true;
  store.menuTarget = null;
}

export async function renameConversation(threadId, title) {
  try {
    const response = await apiFetch(`/api/conversations/${encodeURIComponent(threadId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title })
    });
    if (!response.ok) {
      const detail = await readError(response, '重命名失败');
      // 405 说明路径存在、只是这个 HTTP 方法没注册——一般是后端进程还是改 PATCH
      // 之前启动的那个，重启 uvicorn 才会生效（和回退接口的 404 是同一类问题）。
      throw new Error(response.status === 405
        ? `${detail}。后端可能是在这次改动之前启动的，重启 uvicorn 后重命名接口才会生效。`
        : detail);
    }
    await loadConversations();
    // 和回退一致：不在提示行播报成功，直接还原成默认输入提示
    setStatus('');
  } catch (error) {
    setHint(`重命名失败：${error.message}`, true);
    await loadConversations();
  }
}

export function startInlineRename(row, conversation) {
  const field = document.createElement('input');
  field.className = 'conversation-rename';
  field.type = 'text';
  field.maxLength = 30;
  field.value = conversation.summary;
  field.setAttribute('aria-label', '会话标题');
  row.replaceChildren(field);
  field.focus();
  field.select();

  let settled = false;
  const commit = async () => {
    if (settled) return;
    settled = true;
    const title = field.value.trim();
    if (!title || title === conversation.summary) {
      await loadConversations();
      return;
    }
    await renameConversation(conversation.thread_id, title);
  };
  const cancel = async () => {
    if (settled) return;
    settled = true;
    await loadConversations();
  };

  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    else if (event.key === 'Escape') { event.preventDefault(); cancel(); }
  });
  // 点走即提交，和常见的待办应用一致；settled 保证 Enter 之后 blur 不会再提交一次
  field.addEventListener('blur', () => { commit(); });
}

export async function selectConversation(threadId) {
  closeSidebarIfMobile();
  setStatus('正在加载历史…', true);
  state.autoFollow = true;
  const response = await apiFetch(`/api/conversations/${encodeURIComponent(threadId)}/messages`);
  if (!response.ok) {
    setHint(`加载历史失败：${await readError(response, '加载历史失败')}`, true);
    setBusy(false);
    return;
  }
  const data = await response.json();
  state.threadId = threadId;
  renderHistory(data.messages);
  await loadConversations();
  setStatus('');
}

export async function rewindTo(turn) {
  if (state.busy) return;
  if (state.threadId === null) {
    setHint('回退失败：当前没有活动会话。', true);
    return;
  }
  if (!window.confirm('回退到这条消息之前？这条消息以及它之后的对话都会被删除，原文会放回输入框。')) return;
  setStatus('正在回退…', true);
  try {
    const response = await apiFetch(`/api/conversations/${encodeURIComponent(state.threadId)}/rewind`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ turn })
    });
    if (!response.ok) {
      const detail = await readError(response, '回退失败');
      throw new Error(response.status === 404
        ? `${detail}。如果后端是在这次改动之前启动的，重启 uvicorn 后接口才会生效。`
        : detail);
    }
    const data = await response.json();
    state.autoFollow = true;
    renderHistory(data.messages);
    input.value = data.text || '';
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
    input.focus({ preventScroll: true });
    await loadConversations();
    // 不额外播报「已回退…」：原文已经在输入框里了，直接把提示行还原成默认输入提示
    setStatus('');
  } catch (error) {
    setBusy(false);
    setHint(`回退失败：${error.message}`, true);
  }
}

export async function newConversation() {
  closeSidebarIfMobile();
  try {
    const response = await apiFetch('/api/conversations', { method: 'POST' });
    if (!response.ok) throw new Error(await readError(response, '新建会话失败'));
    const data = await response.json();
    state.threadId = data.thread_id;
    state.autoFollow = true;
    conversationTitle.textContent = data.summary;
    messages.innerHTML = '';
    welcome.style.display = 'block';
    updateScrollBottomButton();
    await loadConversations();
    setStatus('');
    input.focus({ preventScroll: true });
  } catch (error) {
    setHint(`新建会话失败：${error.message}`, true);
  }
}

conversationMenu.querySelector('[data-action="rename"]').onclick = () => {
  const target = store.menuTarget;
  closeConversationMenu();
  if (target) startInlineRename(target.row, target.conversation);
};

conversationMenu.querySelector('[data-action="delete"]').onclick = () => {
  const target = store.menuTarget;
  closeConversationMenu();
  if (target) deleteConversation(target.conversation.thread_id, target.conversation.summary);
};

document.addEventListener('mousedown', (event) => {
  if (!event.target.closest('#conversationMenu')) closeConversationMenu();
}, true);

document.addEventListener('wheel', () => closeConversationMenu(), { passive: true });

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeConversationMenu();
});

window.addEventListener('resize', () => closeConversationMenu());
window.addEventListener('blur', () => closeConversationMenu());
