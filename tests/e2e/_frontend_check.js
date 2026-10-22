
    const state = { threadId: null, busy: false, autoFollow: true, lastScrollTop: 0, location: null, locationLabel: '' };
    const $ = (selector) => document.querySelector(selector);
    const list = $('#conversationList');
    const messages = $('#messages');
    const chat = $('#chat');
    const welcome = $('#welcome');
    const input = $('#input');
    const send = $('#send');
    const conversationTitle = $('#conversationTitle');
    const hint = $('.hint');
    const scrollBottomButton = $('#scrollBottom');
    const conversationMenu = $('#conversationMenu');
    const locateButton = $('#locate');
    const accountName = $('#accountName');
    const accountNote = $('#accountNote');
    const accountAction = $('#accountAction');
    const authModal = $('#authModal');
    const authForm = $('#authForm');
    const authAccount = $('#authAccount');
    const authPassword = $('#authPassword');
    const authError = $('#authError');
    const authSubmit = $('#authSubmit');
    const HINT_IDLE = 'Enter 发送 · Shift + Enter 换行';
    const LOCATION_KEY = 'agent.location';
    const TOKEN_KEY = 'agent.token';

    let token = readToken();
    let identity = null;
    let authMode = 'login';
    let handlingUnauthorized = false;

    function readToken() {
      try { return localStorage.getItem(TOKEN_KEY) || null; } catch (error) { return null; }
    }

    function writeToken(value) {
      token = value;
      try {
        if (value) localStorage.setItem(TOKEN_KEY, value);
        else localStorage.removeItem(TOKEN_KEY);
      } catch (error) {
      }
    }

    function applyIdentity(me) {
      identity = me && !me.guest
        ? { account: me.account, guest: false, memory_enabled: Boolean(me.memory_enabled) }
        : { account: null, guest: true, memory_enabled: false };
      accountName.textContent = identity.guest ? '未登录' : identity.account;
      accountNote.textContent = identity.guest
        ? '游客模式 · 不记录长期记忆'
        : '已登录 · 长期记忆已开启';
      accountAction.textContent = identity.guest ? '登录 / 注册' : '退出';
      accountAction.classList.toggle('danger', !identity.guest);
      input.placeholder = identity.guest
        ? '以游客身份提问（这次对话不会被记住）'
        : '问点什么…';
    }

    function resetConversationView() {
      state.threadId = null;
      state.autoFollow = true;
      conversationTitle.textContent = '未选择会话';
      messages.innerHTML = '';
      welcome.style.display = 'block';
      updateScrollBottomButton();
    }

    async function apiFetch(url, options = {}) {
      const headers = Object.assign({}, options.headers || {});
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(url, Object.assign({}, options, { headers }));
      if (response.status === 401 && token) await handleUnauthorized();
      return response;
    }

    async function handleUnauthorized() {
      if (handlingUnauthorized) return;
      handlingUnauthorized = true;
      writeToken(null);
      applyIdentity(null);
      resetConversationView();
      setHint('登录已过期，请重新登录。', true);
      try { await loadConversations(); } finally { handlingUnauthorized = false; }
    }

    async function refreshIdentity() {
      let me = null;
      try {
        const response = await fetch('/api/auth/me', {
          headers: token ? { Authorization: `Bearer ${token}` } : {}
        });
        if (response.ok) me = await response.json();
        else if (response.status === 401) writeToken(null);
      } catch (error) {
      }
      applyIdentity(me);
    }

    function openAuth(mode) {
      authMode = mode === 'register' ? 'register' : 'login';
      syncAuthMode();
      authError.textContent = '';
      authPassword.value = '';
      authModal.hidden = false;
      authAccount.focus({ preventScroll: true });
    }

    function closeAuth() {
      authModal.hidden = true;
      authPassword.value = '';
      authError.textContent = '';
    }

    function syncAuthMode() {
      document.querySelectorAll('.modal-tab').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.mode === authMode);
      });
      const registering = authMode === 'register';
      authSubmit.textContent = registering ? '注册' : '登录';
      authPassword.placeholder = registering ? '至少 8 位' : '你的密码';
      authPassword.setAttribute('autocomplete', registering ? 'new-password' : 'current-password');
    }

    async function adoptSession(data, message) {
      writeToken(data.token);
      applyIdentity({ account: data.user.account, guest: false, memory_enabled: true });
      closeAuth();
      resetConversationView();
      await loadConversations();
      setHint(message);
    }

    async function doLogout() {
      try {
        await fetch('/api/auth/logout', {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {}
        });
      } catch (error) {
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
        const response = await fetch(`/api/auth/${authMode}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ account, password })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          authError.textContent = data.detail || `HTTP ${response.status}`;
          return;
        }
        await adoptSession(data, authMode === 'login' ? '已登录。' : '注册成功，已自动登录。');
      } catch (error) {
        authError.textContent = `无法连接后端：${error.message}`;
      } finally {
        authSubmit.disabled = false;
      }
    });

    document.querySelectorAll('.modal-tab').forEach((tab) => {
      tab.onclick = () => { authMode = tab.dataset.mode; syncAuthMode(); authError.textContent = ''; };
    });
    $('#authClose').onclick = closeAuth;
    accountAction.onclick = () => {
      if (identity && !identity.guest) doLogout(); else openAuth('login');
    };
    authModal.addEventListener('mousedown', (event) => {
      if (event.target === authModal) closeAuth();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !authModal.hidden) closeAuth();
    });

    function escapeHtml(value) {
      return value.replace(/[&<>'"]/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      })[character]);
    }

    function renderMarkdown(content) {
      const text = String(content ?? '');
      if (!window.marked || !window.DOMPurify) {
        return escapeHtml(text).replace(/\n/g, '<br>');
      }
      return DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }));
    }

    function setHint(text, isError = false) {
      hint.textContent = text || HINT_IDLE;
      hint.classList.toggle('error', Boolean(text) && isError);
    }

    function setBusy(busy) {
      send.disabled = busy;
      input.disabled = busy;
      state.busy = busy;
      document.querySelectorAll('.rewind').forEach((button) => { button.disabled = busy; });
    }

    function setStatus(text, busy = false) {
      setBusy(busy);
      setHint(text);
    }


    function loadStoredLocation() {
      try {
        const raw = localStorage.getItem(LOCATION_KEY);
        if (!raw) return null;
        const saved = JSON.parse(raw);
        return saved && typeof saved === 'object' && saved.location ? saved : null;
      } catch (error) {
        return null; // 存的东西坏了就当没存过，不影响使用
      }
    }

    function renderLocateButton() {
      locateButton.textContent = state.locationLabel || '定位';
      locateButton.classList.toggle('set', Boolean(state.location));
      locateButton.title = state.locationLabel
        ? `当前位置：${state.locationLabel} · 点击重新设置`
        : '点击设置位置（浏览器授权，或手动输入城市）';
    }

    function setLocation(location, label) {
      state.location = location;
      state.locationLabel = label || '';
      try {
        if (location) {
          localStorage.setItem(LOCATION_KEY, JSON.stringify({ location, label: state.locationLabel }));
        } else {
          localStorage.removeItem(LOCATION_KEY);
        }
      } catch (error) {
      }
      renderLocateButton();
    }

    function locateByCoordinates() {
      return new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          async (position) => {
            const { latitude, longitude } = position.coords;
            try {
              const response = await apiFetch('/api/location', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ latitude, longitude })
              });
              if (!response.ok) throw new Error(await readError(response, '定位解析失败'));
              const data = await response.json();
              if (!data.location) {
                setHint('没能从坐标解析出城市。', true);
                resolve(false);
                return;
              }
              setLocation({ latitude, longitude }, data.location);
              setHint('');
              resolve(true);
            } catch (error) {
              setHint(`定位解析失败：${error.message}`, true);
              resolve(false);
            }
          },
          (error) => {
            setHint(error && error.code === 1
              ? '你拒绝了定位授权。'
              : '无法获取定位。', true);
            resolve(false);
          },
          { timeout: 8000, maximumAge: 600000 }
        );
      });
    }

    function chooseCityByHand(reason) {
      const entered = window.prompt(`${reason}\n\n留空则清除已设置的位置。`, state.locationLabel || '');
      if (entered === null) return; // 用户取消，保持原状
      const city = entered.trim();
      if (!city) { setLocation(null, ''); setHint('已清除位置。'); return; }
      setLocation({ city }, city);
      setHint(`位置已设为 ${city}。`);
    }

    locateButton.onclick = async () => {
      if (!navigator.geolocation) { chooseCityByHand('当前浏览器不支持自动定位。'); return; }
      setHint('正在请求定位授权…');
      if (!await locateByCoordinates()) {
        chooseCityByHand('自动定位没有成功，请手动输入你所在的城市。');
      }
    };

    async function restoreLocation() {
      const saved = loadStoredLocation();
      if (saved) { setLocation(saved.location, saved.label); return; }
      if (!navigator.geolocation || !navigator.permissions?.query) return;
      try {
        const status = await navigator.permissions.query({ name: 'geolocation' });
        if (status.state === 'granted') await locateByCoordinates();
      } catch (error) {
      }
    }

    async function readError(response, fallback) {
      try {
        const problem = await response.json();
        const detail = problem && problem.detail;
        if (typeof detail === 'string' && detail) {
          return `HTTP ${response.status}：${detail}`;
        }
      } catch (error) {
      }
      return fallback || `HTTP ${response.status}`;
    }

    function isNearBottom(threshold = 120) {
      return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= threshold;
    }

    function scrollToBottom() {
      chat.scrollTop = chat.scrollHeight;
    }

    function updateScrollBottomButton() {
      scrollBottomButton.classList.toggle('visible', !isNearBottom());
    }

    scrollBottomButton.onclick = () => {
      state.autoFollow = true;
      scrollToBottom();
      updateScrollBottomButton();
    };

    chat.addEventListener('scroll', () => {
      const currentScrollTop = chat.scrollTop;
      if (currentScrollTop < state.lastScrollTop - 2 && !isNearBottom()) {
        state.autoFollow = false;
      } else if (isNearBottom()) {
        state.autoFollow = true;
      }
      state.lastScrollTop = currentScrollTop;
      updateScrollBottomButton();
    }, { passive: true });

    function addMessage(role, content, activity = false, operations = [], turn = null) {
      const shouldFollow = state.autoFollow;
      const item = document.createElement('article');
      item.className = `message ${role}`;
      const trace = role === 'assistant'
        ? '<details class="trace" hidden><summary>模型思考</summary><div class="trace-list"></div></details>'
        : '';
      const rewindRow = role === 'user' && turn !== null
        ? '<div class="message-actions"><button type="button" class="rewind" title="删除这条消息及其之后的全部对话">回退到发送这条之前</button></div>'
        : '';
      item.innerHTML = `<div class="avatar">${role === 'user' ? '我' : 'A'}</div><div><div class="message-role">${role === 'user' ? 'You' : 'Agent'}</div>${trace}<div class="message-content${activity ? ' activity' : ''}"></div>${rewindRow}</div>`;
      const contentNode = item.querySelector('.message-content');
      contentNode.innerHTML = renderMarkdown(content);
      contentNode.trace = item.querySelector('.trace');
      contentNode.traceList = item.querySelector('.trace-list');
      operations.forEach((operation) => {
        if (operation.kind === 'reasoning') {
          appendReasoning(addReasoning(contentNode), operation.text || '');
          return;
        }
        if (operation.kind === 'result') {
          addToolResult(contentNode, operation.tool, operation.status);
          return;
        }
        const details = operation.kind === 'call' && operation.args && Object.keys(operation.args).length
          ? JSON.stringify(operation.args)
          : '';
        addTrace(contentNode, `调用工具：${operation.tool}`, details);
      });
      const rewindButton = item.querySelector('.rewind');
      if (rewindButton) rewindButton.onclick = () => rewindTo(turn);
      messages.appendChild(item);
      welcome.style.display = 'none';
      if (shouldFollow) scrollToBottom();
      return contentNode;
    }

    function renderHistory(history) {
      messages.innerHTML = '';
      let userTurn = 0;
      history.forEach((message) => {
        const isUser = message.role === 'user';
        addMessage(message.role, message.content, false, message.operations || [], isUser ? userTurn : null);
        if (isUser) userTurn += 1;
      });
      if (!history.length) welcome.style.display = 'block';
      updateScrollBottomButton();
    }

    function addTrace(node, label, details = '', failed = false) {
      if (!node?.traceList) return;
      node.trace.hidden = false;
      const event = document.createElement('div');
      event.className = failed ? 'trace-event trace-failed' : 'trace-event';
      event.textContent = details ? `${label} · ${details}` : label;
      node.traceList.appendChild(event);
    }

    function addToolResult(node, tool, status) {
      const failed = status === 'error';
      addTrace(node, `${failed ? '工具失败' : '工具完成'}：${tool}`, '', failed);
    }

    function addReasoning(node, text = '') {
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

    function appendReasoning(box, text) {
      if (!box || !text) return;
      box.body.appendChild(document.createTextNode(text));
      box.chars += text.length;
      box.head = (box.head + text).slice(0, 40);
      const preview = box.head.replace(/\s+/g, ' ').trim();
      const more = box.chars > preview.length ? '…' : '';
      box.summary.textContent = `思考过程（${box.chars} 字）${preview}${more}`;
    }

    async function loadConversations() {
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
        row.addEventListener('contextmenu', (event) => {
          event.preventDefault();
          openConversationMenu(event.clientX, event.clientY, row, conversation);
        });
        row.append(button);
        list.appendChild(row);
      });
    }

    async function deleteConversation(threadId, summary) {
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

    let menuTarget = null;

    function openConversationMenu(x, y, row, conversation) {
      menuTarget = { row, conversation };
      conversationMenu.hidden = false;
      const rect = conversationMenu.getBoundingClientRect();
      const left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
      const top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
      conversationMenu.style.left = `${left}px`;
      conversationMenu.style.top = `${top}px`;
    }

    function closeConversationMenu() {
      conversationMenu.hidden = true;
      menuTarget = null;
    }

    async function renameConversation(threadId, title) {
      try {
        const response = await apiFetch(`/api/conversations/${encodeURIComponent(threadId)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title })
        });
        if (!response.ok) {
          const detail = await readError(response, '重命名失败');
          throw new Error(response.status === 405
            ? `${detail}。后端可能是在这次改动之前启动的，重启 uvicorn 后重命名接口才会生效。`
            : detail);
        }
        await loadConversations();
        setStatus('');
      } catch (error) {
        setHint(`重命名失败：${error.message}`, true);
        await loadConversations();
      }
    }

    function startInlineRename(row, conversation) {
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
      field.addEventListener('blur', () => { commit(); });
    }

    conversationMenu.querySelector('[data-action="rename"]').onclick = () => {
      const target = menuTarget;
      closeConversationMenu();
      if (target) startInlineRename(target.row, target.conversation);
    };

    conversationMenu.querySelector('[data-action="delete"]').onclick = () => {
      const target = menuTarget;
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

    async function selectConversation(threadId) {
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

    async function rewindTo(turn) {
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
        setStatus('');
      } catch (error) {
        setBusy(false);
        setHint(`回退失败：${error.message}`, true);
      }
    }

    async function newConversation() {
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

    async function sendMessage(text) {
      if (!text || state.busy) return;
      state.autoFollow = true;
      addMessage('user', text, false, [], messages.querySelectorAll('.message.user').length);
      input.value = '';
      input.style.height = 'auto';
      setStatus('正在思考…', true);
      let assistantNode = null;
      let assistantText = '';
      let streamFailed = false;
      let reasoningBox = null;
      try {
        const response = await apiFetch('/api/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text, thread_id: state.threadId, location: state.location })
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
              reasoningBox = null;
              if (!assistantNode) assistantNode = addMessage('assistant', '');
              (event.calls || [{ name: event.tool, args: {} }]).forEach((call) => {
                addTrace(assistantNode, `调用工具：${call.name}`, Object.keys(call.args || {}).length ? JSON.stringify(call.args) : '');
              });
              setStatus(`正在调用工具：${event.tool}…`, true);
            } else if (event.type === 'tool_result') {
              if (!assistantNode) assistantNode = addMessage('assistant', '');
              addToolResult(assistantNode, event.tool, event.status);
            } else if (event.type === 'assistant_delta') {
              if (!assistantNode) assistantNode = addMessage('assistant', '');
              assistantText += event.content;
              assistantNode.innerHTML = renderMarkdown(assistantText);
              if (state.autoFollow) scrollToBottom();
            } else if (event.type === 'assistant') {
              if (!assistantNode) assistantNode = addMessage('assistant', '');
              assistantText = event.content;
              assistantNode.innerHTML = renderMarkdown(assistantText);
            } else if (event.type === 'error') {
              if (!assistantNode) assistantNode = addMessage('assistant', '');
              assistantNode.innerHTML = renderMarkdown(event.message);
              streamFailed = true;
              setHint(`服务端出错：${event.message}`, true);
            }
          }
        }
        await loadConversations();
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

    $('#composer').addEventListener('submit', (event) => { event.preventDefault(); sendMessage(input.value.trim()); });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#composer').requestSubmit(); }
    });
    input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; });
    $('#newChat').onclick = newConversation;
    document.querySelectorAll('.suggestion').forEach((button) => { button.onclick = () => { input.value = button.dataset.prompt; input.focus(); }; });
    renderLocateButton();
    restoreLocation();
    (async () => {
      await refreshIdentity();
      await loadConversations();
    })();
  