// 消息：气泡、工具轨迹、思考过程、历史重放
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { rewindTo } from './conversations.js';
import { $, state } from './core.js';
import { scrollToBottom, updateScrollBottomButton } from './feedback.js';
import { beautifyMath, buildAssetMap, buildCiteIndex, renderAnswer, renderMarkdown } from './render.js';

// 本模块用到的 DOM 节点
const messages = $('#messages');
const welcome = $('#welcome');

export function addMessage(role, content, activity = false, operations = [], turn = null, citations = null, images = []) {
  const shouldFollow = state.autoFollow;
  const item = document.createElement('article');
  item.className = `message ${role}`;
  const trace = role === 'assistant'
    ? '<details class="trace" hidden><summary>模型思考</summary><div class="trace-list"></div></details>'
    : '';
  const rewindRow = role === 'user' && turn !== null
    ? '<div class="message-actions"><button type="button" class="rewind" title="删除这条消息及其之后的全部对话">回退到发送这条之前</button></div>'
    : '';
  // 角色标签跟界面语言保持一致：原来写死 You/Agent，中文界面里显得是别的东西。
  const roleLabel = role === 'user' ? '你' : '教材助手';
  const avatarText = role === 'user' ? '我' : '教';
  item.innerHTML = `<div class="avatar">${avatarText}</div><div><div class="message-role">${roleLabel}</div>${trace}<div class="message-content${activity ? ' activity' : ''}"></div>${rewindRow}</div>`;
  const contentNode = item.querySelector('.message-content');
  // 助手的回答走 renderAnswer：[cN] 变可点角标、图引用换签名 URL。
  // urlMap 只在历史消息带引用条目时非空（实时流是先出正文、后到引用事件）。
  const urlMap = buildAssetMap(citations);
  contentNode.innerHTML = role === 'assistant'
    ? renderAnswer(content, urlMap)
    : renderMarkdown(content);
  // 图片放在文字上面：读的时候先看到「问的是哪张图」，再看问题。
  if (images.length) {
    const grid = document.createElement('div');
    grid.className = 'message-attachments';
    images.forEach((src) => {
      const img = document.createElement('img');
      img.src = src;
      img.alt = '我上传的图片';
      img.loading = 'lazy';
      grid.appendChild(img);
    });
    contentNode.insertBefore(grid, contentNode.firstChild);
  }
  beautifyMath(contentNode);
  contentNode.trace = item.querySelector('.trace');
  contentNode.traceList = item.querySelector('.trace-list');
  operations.forEach((operation) => {
    // 历史里的思维链（kind=reasoning）整段拿回来，一次性填进折叠块。
    if (operation.kind === 'reasoning') {
      appendReasoning(addReasoning(contentNode), operation.text || '');
      return;
    }
    // 工具返回：按 status 分「完成 / 失败」，和历史流走同一套渲染。
    if (operation.kind === 'result') {
      addToolResult(contentNode, operation.tool, operation.status, operation.content);
      return;
    }
    const details = operation.kind === 'call' && operation.args && Object.keys(operation.args).length
      ? JSON.stringify(operation.args)
      : '';
    addTrace(contentNode, `调用工具：${operation.tool}`, details);
  });
  // 历史消息自带的引用条目 → 建索引（点正文角标弹小窗，C5：刷新后引用还在）。
  if (role === 'assistant' && citations && citations.length) {
    buildCiteIndex(contentNode, citations);
  }
  const rewindButton = item.querySelector('.rewind');
  if (rewindButton) rewindButton.onclick = () => rewindTo(turn);
  messages.appendChild(item);
  welcome.style.display = 'none';
  if (shouldFollow) scrollToBottom();
  return contentNode;
}

export function renderHistory(history) {
  messages.innerHTML = '';
  let userTurn = 0;
  history.forEach((message) => {
    const isUser = message.role === 'user';
    // 历史里的图片是后端签好名的地址（见 service._decorate_history），直接能加载。
    addMessage(message.role, message.content, false, message.operations || [],
      isUser ? userTurn : null, message.citations || null,
      (message.attachments || []).map((item) => item.url));
    if (isUser) userTurn += 1;
  });
  if (!history.length) welcome.style.display = 'block';
  updateScrollBottomButton();
}

export function addTrace(node, label, details = '', failed = false) {
  if (!node?.traceList) return;
  node.trace.hidden = false;
  const event = document.createElement('div');
  event.className = failed ? 'trace-event trace-failed' : 'trace-event';
  event.textContent = details ? `${label} · ${details}` : label;
  node.traceList.appendChild(event);
}

export function addToolResult(node, tool, status, content = '') {
  const failed = status === 'error';
  const text = String(content || '').trim();
  if (!text) {
    addTrace(node, `${failed ? '工具失败' : '工具完成'}：${tool}`, '', failed);
    return;
  }
  if (!node?.traceList) return;
  node.trace.hidden = false;
  const box = document.createElement('details');
  box.className = failed ? 'trace-tool-result trace-failed' : 'trace-tool-result';
  const summary = document.createElement('summary');
  summary.textContent = `${failed ? '工具失败' : '工具完成'}：${tool}（返回 ${text.length} 字）`;
  const body = document.createElement('div');
  body.className = 'trace-tool-result-body';
  body.textContent = text;
  box.append(summary, body);
  node.traceList.appendChild(box);
}

export function addReasoning(node, text = '') {
  if (!node?.traceList) return null;
  node.trace.hidden = false;
  const box = document.createElement('details');
  box.className = 'trace-reasoning';
  box.innerHTML = '<summary></summary><div class="trace-reasoning-body"></div>';
  box.summary = box.querySelector('summary');
  box.body = box.querySelector('.trace-reasoning-body');
  box.chars = 0;
  box.head = '';
  node.traceList.appendChild(box);
  if (text) appendReasoning(box, text);
  return box;
}

export function appendReasoning(box, text) {
  if (!box || !text) return;
  // 用 appendChild(TextNode) 而不是 `textContent +=`：后者每来一片都要把
  // 整块内容读出来再写回去，几百个分片下来会退化成 O(n²)。
  box.body.appendChild(document.createTextNode(text));
  box.chars += text.length;
  // 只留开头一小截做预览，别让 summary 随着思维链一起变长。
  box.head = (box.head + text).slice(0, 40);
  const preview = box.head.replace(/\s+/g, ' ').trim();
  const more = box.chars > preview.length ? '…' : '';
  box.summary.textContent = `思考过程（${box.chars} 字）${preview}${more}`;
}
