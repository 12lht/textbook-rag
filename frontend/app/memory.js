// 长期记忆面板
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch, readError } from './api.js';
import { $, store } from './core.js';

// 本模块用到的 DOM 节点
const input = $('#input');
const memoryEntry = $('#memoryEntry');
const memoryModal = $('#memoryModal');
const memoryList = $('#memoryList');
const memoryError = $('#memoryError');

export function syncMemoryEntry() {
  memoryEntry.hidden = !store.identity || store.identity.guest;
}

export function openMemory() {
  memoryError.textContent = '';
  memoryModal.hidden = false;
  loadMemories();
}

export function closeMemory() {
  memoryModal.hidden = true;
  memoryError.textContent = '';
}

export function memoryNote(message) {
  memoryError.textContent = message || '';
}

export async function loadMemories() {
  memoryList.innerHTML = '<div class="memory-empty">正在读取…</div>';
  let items = [];
  try {
    const response = await apiFetch('/api/memories');
    if (!response.ok) {
      memoryList.innerHTML = '';
      memoryNote(`读取失败：${await readError(response, '读取失败')}`);
      return;
    }
    items = await response.json();
  } catch (error) {
    memoryList.innerHTML = '';
    memoryNote('连不上服务，稍后再试。');
    return;
  }
  renderMemories(items);
}

export function renderMemories(items) {
  memoryList.innerHTML = '';
  if (!items.length) {
    memoryList.innerHTML = '<div class="memory-empty">还没有记住任何东西。<br>多聊几轮，它会自己开始记。</div>';
    return;
  }
  items.forEach((item) => memoryList.appendChild(memoryRow(item)));
}

export function memoryRow(item) {
  const row = document.createElement('div');
  row.className = 'memory-row';

  const head = document.createElement('div');
  head.className = 'memory-head';
  const kind = document.createElement('span');
  // 「其他」是模型给了不认识的 kind 时的兜底，颜色压暗一点，
  // 别让它和「偏好」「身份」这些正常分类一样显眼。
  kind.className = item.kind === '其他' ? 'memory-kind other' : 'memory-kind';
  kind.textContent = item.kind || '其他';
  const hits = document.createElement('span');
  hits.className = 'memory-hits';
  // 提过几次 = 模型重复记了几次。次数高的通常是真的稳定偏好。
  hits.textContent = item.hits > 1 ? `提过 ${item.hits} 次` : '';
  head.append(kind, hits);

  const text = document.createElement('div');
  text.className = 'memory-text';
  text.textContent = item.text;

  const ops = document.createElement('div');
  ops.className = 'memory-ops';
  const edit = document.createElement('button');
  edit.className = 'memory-op';
  edit.type = 'button';
  edit.textContent = '改一下';
  edit.onclick = () => startEditing(row, item);
  const remove = document.createElement('button');
  remove.className = 'memory-op danger';
  remove.type = 'button';
  remove.textContent = '忘掉这条';
  remove.onclick = () => forgetMemory(item, remove);
  ops.append(edit, remove);

  row.append(head, text, ops);
  return row;
}

export function startEditing(row, item) {
  memoryNote('');
  row.innerHTML = '';
  const form = document.createElement('div');
  form.className = 'memory-edit';
  const input = document.createElement('input');
  input.type = 'text';
  input.value = item.text;
  input.maxLength = 120;
  const ops = document.createElement('div');
  ops.className = 'memory-ops';
  const save = document.createElement('button');
  save.className = 'memory-op';
  save.type = 'button';
  save.textContent = '保存';
  const cancel = document.createElement('button');
  cancel.className = 'memory-op';
  cancel.type = 'button';
  cancel.textContent = '取消';
  const submit = () => saveMemory(item, input.value, save, cancel);
  save.onclick = submit;
  // 取消时重拉而不是把旧节点拼回去：这一来顺手也把别处的改动带回来了。
  cancel.onclick = () => loadMemories();
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); submit(); }
    if (event.key === 'Escape') { event.preventDefault(); loadMemories(); }
  });
  ops.append(save, cancel);
  form.append(input, ops);
  row.appendChild(form);
  input.focus();
  // select() 让用户直接覆盖原文，省一次全选。不是必须的，所以判一下：
  // 真浏览器都有，但精简的 DOM 实现（测试里跑的 linkedom）没实现它。
  if (input.select) input.select();
}

export async function saveMemory(item, text, saveButton, cancelButton) {
  const next = text.trim();
  if (!next) { memoryNote('不能改成空的。'); return; }
  // 没改就别白跑一趟服务端。
  if (next === item.text) { loadMemories(); return; }
  if (store.memoryBusy) return;
  store.memoryBusy = true;
  saveButton.disabled = true;
  cancelButton.disabled = true;
  memoryNote('');
  try {
    const response = await apiFetch(`/api/memories/${encodeURIComponent(item.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: next })
    });
    if (response.status === 409) { memoryNote('已经有这条了，不用再改一次。'); return; }
    if (response.status === 404) { memoryNote('这条已经不在了，刷新一下列表。'); await loadMemories(); return; }
    if (!response.ok) { memoryNote(`改失败：${await readError(response, '改失败')}`); return; }
    await loadMemories();
  } catch (error) {
    memoryNote('连不上服务，稍后再试。');
  } finally {
    store.memoryBusy = false;
    saveButton.disabled = false;
    cancelButton.disabled = false;
  }
}

export async function forgetMemory(item, button) {
  if (store.memoryBusy) return;
  store.memoryBusy = true;
  button.disabled = true;
  memoryNote('');
  try {
    const response = await apiFetch(`/api/memories/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
    if (response.status === 404) {
      memoryNote('这条已经不在了，刷新一下列表。');
      await loadMemories();
      return;
    }
    if (!response.ok) {
      memoryNote(`删除失败：${await readError(response, '删除失败')}`);
      return;
    }
    // 不弹二次确认：条目小、删错了再聊一轮就能重新记下来，
    // 每次都拦一下反而把「顺手清掉一条错的」这件小事变得很烦。
    await loadMemories();
  } catch (error) {
    memoryNote('连不上服务，稍后再试。');
  } finally {
    store.memoryBusy = false;
    button.disabled = false;
  }
}

$('#memoryOpen').onclick = openMemory;
$('#memoryClose').onclick = closeMemory;

memoryModal.addEventListener('click', (event) => {
  if (event.target === memoryModal) closeMemory();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !memoryModal.hidden) closeMemory();
});
