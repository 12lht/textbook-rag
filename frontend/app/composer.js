// 输入区：附件、粘贴、拖拽、发送
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch, readError } from './api.js';
import { loadConversations } from './conversations.js';
import { $, state, toggles } from './core.js';
import { scrollToBottom, setBusy, setHint, setStatus } from './feedback.js';
import { addMessage, addReasoning, addToolResult, addTrace, appendReasoning } from './messages.js';
import { beautifyMath, buildAssetMap, buildCiteIndex, renderAnswer, renderMarkdown } from './render.js';

// 本模块用到的 DOM 节点
const messages = $('#messages');
const input = $('#input');
const conversationTitle = $('#conversationTitle');
const attachButton = $('#attachButton');
const attachInput = $('#attachInput');
const attachBox = $('#composerAttachments');
const attachWrap = $('#attachWrap');
const attachMenu = $('#attachMenu');
const composerBox = $('#composer');

export const IMAGE_MAX_SIDE = 1024;
export const IMAGE_QUALITY = 0.9;
export const MAX_PENDING_IMAGES = 8;

export async function shrinkImage(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, IMAGE_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  if (bitmap.close) bitmap.close();
  // 统一转 JPEG：PNG 截图转过来体积小一个量级，透明通道对教材截图没有意义。
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', IMAGE_QUALITY));
  return blob || file;
}

export function renderPending() {
  attachBox.innerHTML = '';
  attachBox.hidden = state.pending.length === 0;
  state.pending.forEach((item) => {
    const chip = document.createElement('div');
    chip.className = 'attachment-chip' + (item.uploading ? ' loading' : '');
    const img = document.createElement('img');
    img.src = item.preview;
    img.alt = '待发送的图片';
    chip.appendChild(img);
    // 上传中不给「移除」按钮：这时点掉会让已经飞出去的请求落地到一个已删除的槽位。
    if (!item.uploading) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'attachment-remove';
      remove.textContent = '×';
      remove.title = '移除这张图';
      remove.onclick = () => {
        state.pending = state.pending.filter((entry) => entry !== item);
        renderPending();
      };
      chip.appendChild(remove);
    }
    attachBox.appendChild(chip);
  });
}

export async function addImageFiles(fileList) {
  const files = Array.from(fileList || []).filter((file) => file.type.startsWith('image/'));
  if (!files.length) return;
  // 串行上传而不是 Promise.all：一次贴 8 张时并发打满上传带宽，反而每张都更慢，
  // 而且失败时会给用户冒出 8 条错误提示。
  for (const file of files) {
    if (state.pending.length >= MAX_PENDING_IMAGES) {
      setHint(`一次最多 ${MAX_PENDING_IMAGES} 张图`, true);
      break;
    }
    const slot = { preview: URL.createObjectURL(file), uploading: true, att_id: null };
    state.pending.push(slot);
    renderPending();
    try {
      const blob = await shrinkImage(file);
      const form = new FormData();
      // 文件名固定：后端按文件头判断真实格式，不信名字也不信 Content-Type。
      form.append('file', blob, 'image.jpg');
      const response = await apiFetch('/api/attachments', { method: 'POST', body: form });
      if (!response.ok) throw new Error(await readError(response, '上传失败'));
      const data = await response.json();
      slot.att_id = data.att_id;
      slot.uploading = false;
    } catch (error) {
      state.pending = state.pending.filter((entry) => entry !== slot);
      setHint(`图片上传失败：${error.message}`, true);
    }
    renderPending();
  }
  input.focus({ preventScroll: true });
}

export async function sendMessage(text) {
  if (state.busy) return;
  // 只发图不写字是合法的（「这题怎么做」可以不写），但不能两样都没有。
  const images = state.pending.filter((item) => !item.uploading && item.att_id);
  if (!text && !images.length) return;
  if (state.pending.some((item) => item.uploading)) {
    setHint('图片还在上传，稍等一下再发', true);
    return;
  }
  state.autoFollow = true;
  addMessage('user', text, false, [], messages.querySelectorAll('.message.user').length, null,
    images.map((item) => item.preview));
  // 清空待发送区，但**不** revoke 这些 blob URL：消息气泡里还在用它们显示。
  // 页面刷新时浏览器自然回收，比手动管理引用计数省事且不会闪白。
  state.pending = [];
  renderPending();
  input.value = '';
  input.style.height = 'auto';
  setStatus('正在思考…', true);
  let assistantNode = null;
  let assistantText = '';
  let streamFailed = false;
  // 本轮的 asset_id → 签名 URL 映射。citations 事件在正文生成完之后才到
  // （后端要在流结束前才凑齐全部引用），到了以后要把整段正文重渲染一遍，
  // 正文里的图才能真的显示出来。
  let assetMap = {};
  // 当前这一轮模型的思考块。模型一轮里「先想、再答」，调完工具进入下一轮时
  // 要另起一块，所以 tool_call 那里会把它清掉（见下）。
  let reasoningBox = null;
  try {
    const response = await apiFetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 请求体里**没有** user_id：身份只认 Authorization 头里的令牌。
      // 以前这里带一个写死的 user_id，改成可编辑就等于谁都能读别人的会话。
      body: JSON.stringify({
        message: text,
        thread_id: state.threadId,
        location: state.location,
        reasoning: toggles.reasoning,
        web_search: toggles.web_search,
        scope_category_id: state.scope || null,
        attachments: images.map((item) => item.att_id),
      })
    });
    if (!response.ok) throw new Error(await readError(response, '请求失败'));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split('\n\n');
      buffer = blocks.pop();
      for (const block of blocks) {
        const line = block.split('\n').find((part) => part.startsWith('data: '));
        if (!line) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === 'conversation') {
          state.threadId = event.thread_id;
        } else if (event.type === 'title') {
          conversationTitle.textContent = event.title;
        } else if (event.type === 'reasoning_delta') {
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          if (!reasoningBox) reasoningBox = addReasoning(assistantNode);
          appendReasoning(reasoningBox, event.content);
          if (state.autoFollow) scrollToBottom();
        } else if (event.type === 'tool_call') {
          // 这条之后模型要重新开始推理，下一段思维链另起一块。
          reasoningBox = null;
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          (event.calls || [{ name: event.tool, args: {} }]).forEach((call) => {
            addTrace(assistantNode, `调用工具：${call.name}`, Object.keys(call.args || {}).length ? JSON.stringify(call.args) : '');
          });
          setStatus(`正在调用工具：${event.tool}…`, true);
        } else if (event.type === 'tool_result') {
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          addToolResult(assistantNode, event.tool, event.status, event.content);
        } else if (event.type === 'assistant_delta') {
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          assistantText += event.content;
          assistantNode.innerHTML = renderAnswer(assistantText, assetMap);
          if (state.autoFollow) scrollToBottom();
        } else if (event.type === 'assistant') {
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          assistantText = event.content;
          assistantNode.innerHTML = renderAnswer(assistantText, assetMap);
        } else if (event.type === 'citations') {
          // 流结束前最后一批结构化事件：先攒 asset 映射，再把正文重渲染
          // 一遍（图从占位文件名变成真 URL），最后给角标建索引供小窗查。
          assetMap = buildAssetMap(event.items);
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          if (assistantText) assistantNode.innerHTML = renderAnswer(assistantText, assetMap);
          beautifyMath(assistantNode);
          buildCiteIndex(assistantNode, event.items || []);
        } else if (event.type === 'error') {
          if (!assistantNode) assistantNode = addMessage('assistant', '');
          assistantNode.innerHTML = renderMarkdown(event.message);
          streamFailed = true;
          setHint(`服务端出错：${event.message}`, true);
        }
      }
    }
    await loadConversations();
    // 无引用的回答没有 citations 事件可借力，收尾时统一补一次公式渲染
    if (assistantNode) beautifyMath(assistantNode);
    if (streamFailed) setBusy(false); else setStatus('');
  } catch (error) {
    addMessage('assistant', `请求失败：${error.message}`);
    setBusy(false);
    setHint(`请求失败：${error.message}`, true);
  } finally {
    setBusy(false);
    input.focus({ preventScroll: true });
  }
}

export function openAttachMenu() {
  attachMenu.hidden = false;
  attachButton.setAttribute('aria-expanded', 'true');
}

export function closeAttachMenu() {
  attachMenu.hidden = true;
  attachButton.setAttribute('aria-expanded', 'false');
}

export async function pasteFromClipboard() {
  try {
    const clips = await navigator.clipboard.read();
    const files = [];
    for (const clip of clips) {
      const type = (clip.types || []).find((entry) => entry.startsWith('image/'));
      if (!type) continue;
      const blob = await clip.getType(type);
      files.push(new File([blob], 'clipboard.png', { type }));
    }
    if (!files.length) {
      setHint('剪贴板里没有图片', true);
      return;
    }
    addImageFiles(files);
  } catch (error) {
    setHint('浏览器没给剪贴板权限，在输入框里按 Ctrl+V 也一样', true);
  }
}

export const hasFiles = (event) => Array.from((event.dataTransfer && event.dataTransfer.types) || []).includes('Files');

attachButton.addEventListener('click', () => {
  if (attachMenu.hidden) openAttachMenu(); else closeAttachMenu();
});

document.addEventListener('mousedown', (event) => {
  if (attachMenu.hidden || attachWrap.contains(event.target)) return;
  closeAttachMenu();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !attachMenu.hidden) {
    closeAttachMenu();
    attachButton.focus();
  }
});

attachMenu.addEventListener('click', async (event) => {
  const item = event.target.closest('.attach-item');
  if (!item || item.disabled) return;
  closeAttachMenu();
  if (item.dataset.action === 'upload') attachInput.click();
  if (item.dataset.action === 'paste') await pasteFromClipboard();
});

attachInput.addEventListener('change', () => {
  addImageFiles(attachInput.files);
  // 清空 value：否则连选两次同一张图不会再触发 change。
  attachInput.value = '';
});

input.addEventListener('paste', (event) => {
  const files = Array.from((event.clipboardData && event.clipboardData.files) || [])
    .filter((file) => file.type.startsWith('image/'));
  if (!files.length) return;
  event.preventDefault();
  addImageFiles(files);
});

['dragenter', 'dragover'].forEach((type) => {
  composerBox.addEventListener(type, (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    composerBox.classList.add('dragging');
  });
});

['dragleave', 'dragend'].forEach((type) => {
  composerBox.addEventListener(type, () => composerBox.classList.remove('dragging'));
});

composerBox.addEventListener('drop', (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  composerBox.classList.remove('dragging');
  if (event.dataTransfer.files.length) addImageFiles(event.dataTransfer.files);
});

$('#composer').addEventListener('submit', (event) => { event.preventDefault(); sendMessage(input.value.trim()); });

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); }
});

input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; });
