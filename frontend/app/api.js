// 请求层：统一的 fetch 封装、错误文案、401 处理
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { applyIdentity } from './auth.js';
import { loadConversations, resetConversationView } from './conversations.js';
import { store, writeToken } from './core.js';
import { setHint } from './feedback.js';

export async function apiFetch(url, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  const response = await fetch(url, Object.assign({}, options, { headers }));
  if (response.status === 401 && store.token) await handleUnauthorized();
  return response;
}

export async function readError(response, fallback) {
  try {
    const problem = await response.json();
    const detail = problem && problem.detail;
    if (typeof detail === 'string' && detail) {
      return `HTTP ${response.status}：${detail}`;
    }
  } catch (error) {
    // 后端没有返回 JSON，用兜底文案
  }
  return fallback || `HTTP ${response.status}`;
}

export async function handleUnauthorized() {
  if (store.handlingUnauthorized) return;
  store.handlingUnauthorized = true;
  writeToken(null);
  applyIdentity(null);
  resetConversationView();
  setHint('登录已过期，请重新登录。', true);
  try { await loadConversations(); } finally { store.handlingUnauthorized = false; }
}
