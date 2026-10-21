
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

    // ---------- 身份 ----------
    //
    // 令牌是服务端签发的、放在 localStorage 里的唯一凭证。**前端不再自己声称
    // 「我是谁」**：以前聊天请求里带一个写死的 user_id，任何人都能改成别人的。
    // 现在服务端只认令牌，请求体里的 user_id 会被忽略。
    //
    // `identity` 是 /api/auth/me 的返回，用来决定界面怎么显示；它只是**展示用**，
    // 真正的权限判断在服务端。别拿它当安全边界。
    let token = readToken();
    let identity = null;
    let authMode = 'login';
    // 401 只处理一次：一次页面操作可能并发打好几个请求，全都 401 的话
    // 会重复清状态、重复弹提示。处理期间把它挡住。
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
        // 无痕模式下 localStorage 会抛错。记不住令牌的后果是「刷新后退出登录」，
        // 本次会话仍然可用，不值得打断用户。
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

    // 回到「未选择会话」的空白页。切换身份时必须调用：上一个身份的会话不属于
    // 新身份，留着旧消息会让人以为那是自己的。
    function resetConversationView() {
      state.threadId = null;
      state.autoFollow = true;
      conversationTitle.textContent = '未选择会话';
      messages.innerHTML = '';
      welcome.style.display = 'block';
      updateScrollBottomButton();
    }

    // 所有接口都走这里：统一带令牌、统一处理 401。
    //
    // 401 的含义是「这个令牌不认了」（过期或被撤销）。这时候必须清掉本地令牌、
    // 退回游客态——不然界面一直显示已登录，而每个请求都失败，用户完全不知道
    // 发生了什么。后端**不会**在令牌失效时静默降级成游客，就是为了让这里能
    // 明确地分辨出「该重新登录了」。
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

    // 用原生 fetch 而不是 apiFetch：这个函数本身就是用来判断令牌好坏的，
    // 再走一层 401 处理会绕回来。
    async function refreshIdentity() {
      let me = null;
      try {
        const response = await fetch('/api/auth/me', {
          headers: token ? { Authorization: `Bearer ${token}` } : {}
        });
        if (response.ok) me = await response.json();
        else if (response.status === 401) writeToken(null);
      } catch (error) {
        // 后端没起来：先按游客显示，真正聊天时会在 .hint 上给出连接错误
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

    // 登录成功后的收尾：换身份必然换会话列表，所以整页重置再重新拉。
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
        // 登出接口刻意不要求登录态有效：令牌过期时也得能登出，否则用户会卡在一个
        // 点了没反应的按钮上。所以这里失败也不拦着下面清本地状态。
        await fetch('/api/auth/logout', {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {}
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
    // 点遮罩关闭（点卡片内部不关）
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

    // 状态和错误都显示在输入框下面那行小字上，出错时必须让用户看见，
    // 否则接口失败会表现为「点了没反应」。
    function setHint(text, isError = false) {
      hint.textContent = text || HINT_IDLE;
      hint.classList.toggle('error', Boolean(text) && isError);
    }

    // 忙碌状态和提示文案必须分开：出错时只解除忙碌，不能顺手把错误文案清掉。
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

    // ---------- 位置：浏览器授权优先，失败降级为用户手选 ----------
    //
    // 位置只由前端提供，服务端**不会**用 IP 去猜：IP 定位收的是请求来源地址，
    // 多用户部署下会把所有人都定位到服务器所在地，模型会据此向用户断言错误的城市。
    // 拿不到就如实说拿不到，不编一个。
    //
    // 「手选城市」优先于「授权坐标」——手选是用户明确确认过的，坐标可能来自 VPN
    // 或粗粒度 Wi-Fi 定位，可靠性反而更低。这个优先级由后端 service 统一裁决。

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
        // 隐私模式下 localStorage 会抛错。记不住不影响这次会话，忽略。
      }
      renderLocateButton();
    }

    // 坐标换城市名。走 /api/location 而不是等聊天接口返回，是因为聊天是 SSE 流，
    // 坐标在那一轮里只会进模型上下文，前端拿不到解析结果去回显。
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

    // 手选兜底。留空 = 清除已设置的位置。
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

    // 页面加载时：记住过就直接用；否则只在「用户以前就已授权」时静默取一次，
    // 不在页面刚打开就弹权限框出来。
    async function restoreLocation() {
      const saved = loadStoredLocation();
      if (saved) { setLocation(saved.location, saved.label); return; }
      if (!navigator.geolocation || !navigator.permissions?.query) return;
      try {
        const status = await navigator.permissions.query({ name: 'geolocation' });
        if (status.state === 'granted') await locateByCoordinates();
      } catch (error) {
        // 部分浏览器不支持查询 geolocation 权限（含 file:// 与本机 http），静默跳过
      }
    }

    // 把后端返回的 detail 取出来，附加到错误信息上。
    async function readError(response, fallback) {
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

    // 滚动只发生在 .chat 内部：页面本身不滚动，所以侧边栏、顶栏和输入框始终固定。
    function isNearBottom(threshold = 120) {
      return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= threshold;
    }

    function scrollToBottom() {
      chat.scrollTop = chat.scrollHeight;
    }

    // 只要不在底部就显示「回到底部」按钮
    function updateScrollBottomButton() {
      scrollBottomButton.classList.toggle('visible', !isNearBottom());
    }

    scrollBottomButton.onclick = () => {
      state.autoFollow = true;
      scrollToBottom();
      updateScrollBottomButton();
    };

    // 是否跟随由真实滚动行为决定：向上滚离底部就停止，重新接近底部就恢复。
    // 不能在每个流式分片里现算 isNearBottom()——跟随滚动本身就会把跟随关掉。
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
        // 历史里的思维链（kind=reasoning）整段拿回来，一次性填进折叠块。
        if (operation.kind === 'reasoning') {
          appendReasoning(addReasoning(contentNode), operation.text || '');
          return;
        }
        // 工具返回：按 status 分「完成 / 失败」，和历史流走同一套渲染。
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

    // 按历史渲染消息，同时给每条用户消息标上它的序号（从 0 开始），回退要用。
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

    // 工具成功和失败必须长得不一样。
    // 后端一直在 `tool_result` 事件里发 `status`，前端以前把它整个丢掉了，于是
    // 「工具失败」也渲染成「工具完成」——用户会以为事情办成了，继续往下说。
    // 一个界面在无声地说反话，比不显示更糟。
    function addToolResult(node, tool, status) {
      const failed = status === 'error';
      addTrace(node, `${failed ? '工具失败' : '工具完成'}：${tool}`, '', failed);
    }

    // 思维链做成可折叠块，而不是像工具调用那样平铺一行。
    // 理由：它比正文长一个量级（实测一次提问：思维链 317 字 / 正文 80 字，
    // 分片数 166 / 47）。平铺会把工具调用挤到看不见。折叠起来、summary 上带
    // 实时字数，既能看出「它正在想」，又不占地方。
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

    async function loadConversations() {
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

    // ---------- 会话右键菜单（重命名 / 删除） ----------
    let menuTarget = null;

    function openConversationMenu(x, y, row, conversation) {
      menuTarget = { row, conversation };
      conversationMenu.hidden = false;
      // 先显示再量尺寸，才拿得到菜单自身的宽高来做视口内收边
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

    // 就地改标题：把整行的内容换成输入框（不能把 input 塞进 .conversation 按钮里，
    // 那样点输入框会连带触发按钮的选中逻辑）。提交或取消后靠 loadConversations 重建整行。
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
      // 点走即提交，和常见的待办应用一致；settled 保证 Enter 之后 blur 不会再提交一次
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

    // 左键点别处、滚轮、Esc、改窗口大小、失焦，都把菜单收起来。
    // 这里刻意不用 scroll：scroll 事件是异步派发的，右键之前那次滚动的 scroll
    // 可能在菜单打开之后才到达，会把刚打开的菜单立刻关掉（点击可滚动区域时
    // 浏览器的 scrollIntoView 就会稳定触发这个竞态）。wheel / mousedown 是
    // 同步的用户意图，不会误伤。
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

    // 回退：删掉这条用户消息以及它之后的全部对话，并把原文放回输入框方便改完重发。
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
        // 不额外播报「已回退…」：原文已经在输入框里了，直接把提示行还原成默认输入提示
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
      // 当前这一轮模型的思考块。模型一轮里「先想、再答」，调完工具进入下一轮时
      // 要另起一块，所以 tool_call 那里会把它清掉（见下）。
      let reasoningBox = null;
      try {
        const response = await apiFetch('/api/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // 请求体里**没有** user_id：身份只认 Authorization 头里的令牌。
          // 以前这里带一个写死的 user_id，改成可编辑就等于谁都能读别人的会话。
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
              // 这条之后模型要重新开始推理，下一段思维链另起一块。
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
    // **先问服务端「我是谁」，再按身份拉列表**。顺序反了会先按游客拉一次
    // （本地有令牌时那次请求还是会带上令牌，但界面上会先闪一下游客态）。
    (async () => {
      await refreshIdentity();
      await loadConversations();
    })();
  