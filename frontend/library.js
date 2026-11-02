    // --- 移动端抽屉分类栏：与聊天页同一套交互 ---
    {
      const cats = document.querySelector('.cats');
      const backdrop = document.getElementById('sidebarBackdrop');
      const close = () => { cats.classList.remove('open'); backdrop.classList.remove('show'); };
      document.getElementById('menuBtn').onclick = () => {
        cats.classList.add('open'); backdrop.classList.add('show');
      };
      backdrop.onclick = close;
      // 手机上点分类是筛选文档区，选完就该把抽屉收起来露出结果
      document.getElementById('catList').addEventListener('click', (event) => {
        if (event.target.closest('.cat') && window.matchMedia('(max-width: 760px)').matches) close();
      });
    }

    const $ = (selector) => document.querySelector(selector);
    // 令牌和主界面共用同一个 localStorage 键：在对话页登录过，这里直接是登录态，
    // 不用再登一次。反向也成立——这里退出会连带对话页退出，所以这一页不提供退出。
    const TOKEN_KEY = 'agent.token';
    // 「未分类」用这个哨兵值做筛选条件，而不是空字符串——空字符串已经被
    // 「全部」占用了。它永远不会和真实的 category_id（`c-xxxx`）撞上。
    const UNFILED = '__unfiled__';
    // 排序方式存在本地：按编号是多数人的默认，但有人习惯看最新上传的，
    // 每次刷新都重置回去很烦。
    const SORT_KEY = 'library.sort';

    function readSort() {
      try {
        const saved = localStorage.getItem(SORT_KEY);
        return saved === 'created' || saved === 'number' ? saved : 'number';
      } catch (error) {
        return 'number';
      }
    }

    const state = {
      identity: null,
      categories: [],
      documents: [],
      filter: '',          // '' = 全部，UNFILED = 未分类，其余是 category_id
      picked: new Set(),   // 勾选的 doc_id
      pollTimer: null,
      // 默认按编号排：教材标题带课程顺序（第2章 / 第3章 / 11_xxx），
      // 按上传时间倒序会把顺序全打乱。想按时间看可以在右上角切。
      sort: readSort(),
    };

    const catList = $('#catList');
    const catForm = $('#catForm');
    const catInput = $('#catInput');
    const catSubmit = $('#catSubmit');
    const catError = $('#catError');
    const docList = $('#docList');
    const docsTitle = $('#docsTitle');
    const docsSub = $('#docsSub');
    const uploadButton = $('#uploadButton');
    const fileInput = $('#fileInput');
    const bulkBar = $('#bulkBar');
    const bulkCount = $('#bulkCount');
    const bulkCategory = $('#bulkCategory');
    const bulkApply = $('#bulkApply');
    const bulkClear = $('#bulkClear');
    const gate = $('#gate');
    const who = $('#who');
    const toast = $('#toast');
    const sortSelect = $('#sortSelect');

    let token = readToken();
    function readToken() {
      try { return localStorage.getItem(TOKEN_KEY) || null; } catch (error) { return null; }
    }
    function writeToken(value) {
      token = value;
      try {
        if (value) localStorage.setItem(TOKEN_KEY, value);
        else localStorage.removeItem(TOKEN_KEY);
      } catch (error) {
        // 无痕模式下写不进去。影响只是「下次进来看不到自己的库」，不值得打断。
      }
    }

    // 所有请求走这里。401 统一回登录门：令牌过期时如果只是几个请求静默失败，
    // 页面会显示成「一本都没有」，用户会以为书丢了。
    async function apiFetch(url, options = {}) {
      const headers = Object.assign({}, options.headers || {});
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(url, Object.assign({}, options, { headers }));
      if (response.status === 401) {
        writeToken(null);
        state.identity = { guest: true };
        showGate('登录已过期，请重新登录后再管理藏书阁。');
      }
      return response;
    }

    async function readError(response, fallback) {
      try {
        const body = await response.json();
        if (body && typeof body.detail === 'string') return body.detail;
      } catch (error) { /* 不是 JSON，用兜底文案 */ }
      return fallback;
    }

    function escapeHtml(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    let toastTimer = null;
    // 打开教材原文：先换一个签名地址再新标签打开。浏览器打开 PDF 时带不上
    // Authorization 头，所以后端给的是带 hmac 签名的 URL（和插图一个套路）。
    async function openDocumentFile(doc) {
      try {
        const response = await apiFetch(`/api/documents/${encodeURIComponent(doc.doc_id)}/file`);
        if (!response.ok) throw new Error(await readError(response, '打不开'));
        const { url } = await response.json();
        // `window.open` 必须紧跟用户操作才不会被弹窗拦截。这里中间隔了 await，
        // 被拦时返回 null——不提示的话用户看到的就是「点了没反应」。
        const opened = window.open(url, '_blank', 'noopener');
        if (!opened) say('浏览器拦下了新标签，请允许本站弹窗后再点一次', true);
      } catch (error) {
        say(`打不开「${doc.title}」：${error.message}`, true);
      }
    }

    function say(message, bad = false) {
      toast.textContent = message;
      toast.classList.toggle('bad', Boolean(bad));
      toast.hidden = false;
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.hidden = true; toastTimer = null; }, 3200);
    }

    function showGate(message) {
      if (message) $('#gateText').textContent = message;
      gate.hidden = false;
      $('#layout').style.visibility = 'hidden';
    }

    // ------------------------------------------------------------------
    // 身份
    // ------------------------------------------------------------------
    async function loadIdentity() {
      try {
        const response = await fetch('/api/auth/me', {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (response.status === 401) { writeToken(null); state.identity = { guest: true }; return false; }
        if (!response.ok) { state.identity = { guest: true }; return false; }
        const me = await response.json();
        state.identity = me && !me.guest
          ? { account: me.account, guest: false }
          : { guest: true };
        return !state.identity.guest;
      } catch (error) {
        state.identity = { guest: true };
        showGate(`连不上后端：${error.message}`);
        return false;
      }
    }

    // ------------------------------------------------------------------
    // 分类
    // ------------------------------------------------------------------
    function categoryName(categoryId) {
      if (!categoryId) return '未分类';
      const found = state.categories.find((item) => item.category_id === categoryId);
      return found ? found.name : '未分类';
    }

    function unfiledCount() {
      return state.documents.filter((doc) => !doc.category_id).length;
    }

    function renderCategories() {
      catList.innerHTML = '';
      const rows = [
        { id: '', name: '全部文档', count: state.documents.length, fixed: true },
        { id: UNFILED, name: '未分类', count: unfiledCount(), fixed: true },
      ];
      state.categories.forEach((item) => {
        rows.push({ id: item.category_id, name: item.name, count: item.doc_count, fixed: false });
      });

      rows.forEach((row) => {
        const node = document.createElement('button');
        node.type = 'button';
        node.className = 'cat' + (state.filter === row.id ? ' active' : '');
        // 用属性记 category_id 而不是靠名字找回节点：`[data-category-id]` 比遍历文本可靠
        // （名字相近时会认错行，改「移动通信」结果改到「移动通信（下）」）。
        node.setAttribute('data-category-id', row.id);
        const name = document.createElement('span');
        name.className = 'cat-name';
        name.textContent = row.name;
        const count = document.createElement('span');
        count.className = 'cat-count';
        count.textContent = String(row.count);
        node.append(name, count);
        node.onclick = () => { state.filter = row.id; renderCategories(); renderDocuments(); };

        // 「全部 / 未分类」是虚拟行，没有改名和删除——给它们加按钮就得解释
        // 「删除『全部』是什么意思」，那是自找的歧义。
        if (!row.fixed) {
          const ops = document.createElement('span');
          ops.className = 'cat-ops';
          const chat = document.createElement('button');
          chat.type = 'button';
          chat.className = 'cat-op';
          chat.textContent = '对话';
          chat.title = '进入这本书的对话，检索限定在此分类内';
          chat.onclick = (event) => {
            event.stopPropagation();
            // 聊天页只挂在 / 上，/index.html 在服务器上是 404；query 参数与路径无关，照样能带
            window.location.href = `/?scope=${encodeURIComponent(row.id)}&scope_name=${encodeURIComponent(row.name)}`;
          };
          const rename = document.createElement('button');
          rename.type = 'button';
          rename.className = 'cat-op';
          rename.textContent = '改名';
          rename.onclick = (event) => { event.stopPropagation(); beginRename(row); };
          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'cat-op danger';
          remove.textContent = '删除';
          remove.onclick = (event) => { event.stopPropagation(); removeCategory(row); };
          ops.append(chat, rename, remove);
          node.appendChild(ops);
        }
        catList.appendChild(node);
      });
    }

    // 改名用「就地换成输入框」而不是弹窗：只改一个短字段，弹窗要多两次点击，
    // 而且弹窗起来时看不清它本来叫什么。
    function beginRename(row) {
      const target = catList.querySelector(`[data-category-id="${row.id}"]`);
      if (!target) return;
      const input = document.createElement('input');
      input.type = 'text';
      input.maxLength = 40;
      input.value = row.name;
      // 内联样式里用 CSS 变量，别写死色值：改主题时这里最容易漏掉。
      input.style.cssText = 'flex:1;min-width:0;padding:5px 7px;border:1px solid var(--accent);border-radius:6px;font-size:12.5px;outline:none;background:var(--panel);color:var(--ink);';
      target.innerHTML = '';
      target.appendChild(input);
      // linkedom（探针用的假 DOM）里 input 没有 select()，不加判断会直接抛。
      if (input.select) input.select();

      let settled = false;
      const finish = async (commit) => {
        if (settled) return;
        settled = true;
        const next = input.value.trim();
        if (!commit || !next || next === row.name) { renderCategories(); return; }
        const response = await apiFetch(`/api/categories/${encodeURIComponent(row.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: next }),
        });
        if (!response.ok) {
          catError.textContent = await readError(response, '改名失败');
          renderCategories();
          return;
        }
        catError.textContent = '';
        await loadAll();
      };
      input.onkeydown = (event) => {
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        if (event.key === 'Escape') { finish(false); }
      };
      input.onblur = () => finish(true);
    }

    async function removeCategory(row) {
      // 文案里必须说清「书不会跟着没」：这是删分类最容易让人犹豫的点，
      // 不写清楚用户会去一个个确认书还在不在。
      const tip = row.count
        ? `删除分类「${row.name}」？\n\n类下的 ${row.count} 本书不会删除，只会退回「未分类」。`
        : `删除分类「${row.name}」？`;
      if (!window.confirm(tip)) return;
      const response = await apiFetch(`/api/categories/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
      if (!response.ok && response.status !== 404) {
        say(await readError(response, '删除分类失败'), true);
        return;
      }
      if (state.filter === row.id) state.filter = '';
      await loadAll();
      say(`分类「${row.name}」已删除，书退回未分类。`);
    }

    async function createCategory(name) {
      const cleaned = String(name || '').trim();
      if (!cleaned) { catError.textContent = '分类名不能为空'; return; }
      catSubmit.disabled = true;
      try {
        const response = await apiFetch('/api/categories', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: cleaned }),
        });
        if (!response.ok) {
          catError.textContent = await readError(response, '新建分类失败');
          return;
        }
        const created = await response.json();
        catInput.value = '';
        catError.textContent = '';
        // 新建完直接跳到这个分类：刚建分类的人下一步一定是往里放书，
        // 留在一堆分类里让他自己再点一下是多余的。
        state.filter = created.category_id;
        await loadAll();
      } finally {
        catSubmit.disabled = false;
      }
    }

    catForm.addEventListener('submit', (event) => {
      event.preventDefault();
      createCategory(catInput.value);
    });

    // ------------------------------------------------------------------
    // 文档
    // ------------------------------------------------------------------
    function visibleDocuments() {
      if (state.filter === '') return state.documents;
      if (state.filter === UNFILED) return state.documents.filter((doc) => !doc.category_id);
      return state.documents.filter((doc) => doc.category_id === state.filter);
    }

    // ------------------------------------------------------------------
    // 排序
    // ------------------------------------------------------------------
    // 标题里的编号是课程顺序，必须按「数字大小」比而不是按字符串比：
    // 字符串比会把「第11章」排到「第2章」前面、「10_基因组学」排到「9_」前面。
    // 做法是把标题切成「数字段 / 文字段」交替的数组，同类型逐段比较。
    function naturalChunks(text) {
      return String(text || '').toLowerCase().match(/\d+|\D+/g) || [];
    }

    function compareByNumber(left, right) {
      const a = naturalChunks(left.title);
      const b = naturalChunks(right.title);
      const size = Math.min(a.length, b.length);
      for (let i = 0; i < size; i += 1) {
        const isNumA = /^\d+$/.test(a[i]);
        const isNumB = /^\d+$/.test(b[i]);
        if (isNumA && isNumB) {
          const diff = Number(a[i]) - Number(b[i]);
          if (diff) return diff;
        } else if (!isNumA && !isNumB) {
          // 中文按拼音排，「附录」不会因为编码值跑到「第1章」前面
          const diff = a[i].localeCompare(b[i], 'zh-Hans-CN');
          if (diff) return diff;
        } else {
          // 一段数字一段文字：让数字开头的那本排前面，避免「11_xxx」被甩到最后
          return isNumA ? -1 : 1;
        }
      }
      if (a.length !== b.length) return a.length - b.length;
      return String(left.title).localeCompare(String(right.title), 'zh-Hans-CN');
    }

    function sortDocuments(documents) {
      const sorted = documents.slice();
      if (state.sort === 'created') {
        sorted.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
      } else {
        sorted.sort(compareByNumber);
      }
      return sorted;
    }

    function documentMeta(doc) {
      if (doc.status === 'failed') {
        return `<span class="bad">入库失败</span> · ${escapeHtml(doc.error || '未知原因')}`;
      }
      if (doc.status !== 'ready') {
        return '<span class="ok">解析中…</span> 大文件需要几十秒，这一页会自动刷新';
      }
      const pages = doc.page_count ? `${doc.page_count} 页 · ` : '';
      const assets = doc.asset_count ? ` · ${doc.asset_count} 图表` : '';
      const summary = `${pages}${doc.chunk_count} 段${assets}`;
      // warning 是「能用但有已知瑕疵」（比如双栏被按单栏解析）。不显示的话，
      // 用户会以为那本书解析得好好的，答案答偏时完全找不到原因。
      return doc.warning
        ? `${summary}<br><span class="warn">${escapeHtml(doc.warning)}</span>`
        : summary;
    }

    // 一本书的「归属」下拉。选项来自 state.categories，所以新建分类后
    // 不用重渲染每一行也能更新——每次渲染都重建，简单且不会漏。
    function buildCategorySelect(doc) {
      const select = document.createElement('select');
      select.className = 'doc-cat';
      select.title = '移到别的分类';
      const options = [{ value: '', name: '未分类' }].concat(
        state.categories.map((item) => ({ value: item.category_id, name: item.name })),
      );
      options.forEach((option) => {
        const node = document.createElement('option');
        node.value = option.value;
        node.textContent = option.name;
        if ((doc.category_id || '') === option.value) node.selected = true;
        select.appendChild(node);
      });
      select.onchange = () => assignDocuments([doc.doc_id], select.value || null);
      return select;
    }

    function renderDocuments() {
      const documents = sortDocuments(visibleDocuments());
      docList.innerHTML = '';

      // 标题和说明跟着筛选走，用户要知道「现在看的是哪一摊」。
      const label = state.filter === ''
        ? '全部文档'
        : state.filter === UNFILED ? '未分类' : categoryName(state.filter);
      docsTitle.textContent = label;
      const ready = documents.filter((doc) => doc.status === 'ready').length;
      docsSub.textContent = documents.length
        ? `${documents.length} 本，其中 ${ready} 本已入库可以提问。勾选多本可以一次性移到同一个分类。`
        : '';

      if (!documents.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.innerHTML = state.documents.length
          ? '这个分类下还没有文档。<br>在上面点<b>上传并入库</b>，传完会自动归到这个分类。'
          : '藏书阁还是空的。<br>点右上角<b>上传并入库</b>，支持 PDF / PPT / Word。';
        docList.appendChild(empty);
        return;
      }

      documents.forEach((doc) => {
        const card = document.createElement('div');
        card.className = 'doc-card' + (state.picked.has(doc.doc_id) ? ' picked' : '');

        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = state.picked.has(doc.doc_id);
        check.title = '选中后可批量归类';
        check.onchange = () => {
          if (check.checked) state.picked.add(doc.doc_id);
          else state.picked.delete(doc.doc_id);
          card.classList.toggle('picked', check.checked);
          renderBulkBar();
        };

        const body = document.createElement('div');
        body.className = 'doc-body';
        const title = document.createElement('div');
        title.className = 'doc-title';
        title.textContent = doc.title;
        title.title = doc.title;
        const file = document.createElement('div');
        file.className = 'doc-file';
        file.textContent = doc.filename || '';
        const meta = document.createElement('div');
        meta.className = 'doc-meta';
        meta.innerHTML = documentMeta(doc);
        body.append(title, file, meta);

        const ops = document.createElement('div');
        ops.className = 'doc-ops';
        const preview = document.createElement('button');
        preview.type = 'button';
        preview.className = 'doc-op';
        preview.textContent = '看原文';
        // 原文件在不在和解析状态无关：解析失败也能翻 PDF，所以不禁用。
        preview.title = '在新标签打开上传的 PDF 原文';
        preview.onclick = () => openDocumentFile(doc);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'doc-op danger';
        remove.textContent = '删除';
        remove.onclick = () => removeDocument(doc);
        ops.append(preview, remove);

        card.append(check, body, buildCategorySelect(doc), ops);
        docList.appendChild(card);
      });
    }

    function renderBulkBar() {
      // 勾选可能残留已删除的书（别处删掉后刷新列表）。渲染前先剔一遍，
      // 否则「已选 3 本」里有一本是幽灵。
      const alive = new Set(state.documents.map((doc) => doc.doc_id));
      Array.from(state.picked).forEach((id) => { if (!alive.has(id)) state.picked.delete(id); });

      bulkBar.hidden = state.picked.size === 0;
      if (!state.picked.size) return;
      bulkCount.textContent = `已选 ${state.picked.size} 本`;

      // 批量归类的目标分类。和单本的下拉分开建，因为这里的值不需要「当前值」。
      const keep = bulkCategory.value;
      bulkCategory.innerHTML = '';
      const options = [{ value: '', name: '未分类' }].concat(
        state.categories.map((item) => ({ value: item.category_id, name: item.name })),
      );
      options.forEach((option) => {
        const node = document.createElement('option');
        node.value = option.value;
        node.textContent = option.name;
        bulkCategory.appendChild(node);
      });
      // 默认填当前筛选的分类（「看着某个分类、勾几本、移进去」是最常见的顺序）；
      // keep 是上一次选过的值，用户可能刚换了目标分类还没点「移过去」。
      bulkCategory.value = keep || (state.filter === UNFILED ? '' : state.filter);
    }

    bulkApply.onclick = () => {
      assignDocuments(Array.from(state.picked), bulkCategory.value || null);
    };
    bulkClear.onclick = () => {
      state.picked.clear();
      renderDocuments();
      renderBulkBar();
    };

    async function assignDocuments(docIds, categoryId) {
      if (!docIds.length) return;
      const label = categoryId ? categoryName(categoryId) : '未分类';
      const results = await Promise.all(docIds.map(async (docId) => {
        const response = await apiFetch(`/api/documents/${encodeURIComponent(docId)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category_id: categoryId }),
        });
        return response.ok;
      }));
      const failed = results.filter((ok) => !ok).length;
      state.picked.clear();
      await loadAll();
      if (failed) say(`${failed} 本没能移动，可能是刚被删掉了。`, true);
      else say(`${docIds.length} 本已移到「${label}」。`);
    }

    async function removeDocument(doc) {
      if (!window.confirm(`删除《${doc.title}》？\n\n解析出的正文和图片会一起删掉，之后提问不会再引用这本书。`)) return;
      const response = await apiFetch(`/api/documents/${encodeURIComponent(doc.doc_id)}`, { method: 'DELETE' });
      if (!response.ok && response.status !== 404) {
        say(await readError(response, '删除失败'), true);
        return;
      }
      state.picked.delete(doc.doc_id);
      await loadAll();
      say(`《${doc.title}》已删除。`);
    }

    // ------------------------------------------------------------------
    // 入库（上传 → 后台解析）
    // ------------------------------------------------------------------
    // 手机蜂窝网络下大请求体容易被链路抖动掐断（表现为 fetch 长时间不返回也不报错）。
    // 超时主动断掉重试：上传是完整 POST、没有部分成功状态，重试安全。
    async function uploadWithRetry(body, attempts = 3) {
      for (let attempt = 1; attempt <= attempts; attempt++) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 120000);
        try {
          const response = await apiFetch('/api/documents', {
            method: 'POST', body, signal: controller.signal,
          });
          clearTimeout(timer);
          // 4xx（除 408/429）是确定性错误（文件超限、没登录），重试也不会好，直接返回给调用方报错
          const retryable = response.status >= 500 || response.status === 408 || response.status === 429;
          if (response.ok || !retryable || attempt === attempts) return response;
          say(`网络不稳（${response.status}），正在重试第 ${attempt}/${attempts - 1} 次…`, true);
        } catch (error) {
          clearTimeout(timer);
          if (attempt === attempts) throw error;
          say(`网络不稳（${error.message}），正在重试第 ${attempt}/${attempts - 1} 次…`, true);
        }
      }
    }

    async function uploadFiles(files) {
      const list = Array.from(files || []);
      if (!list.length) return;
      uploadButton.disabled = true;
      const target = state.filter === '' || state.filter === UNFILED ? null : state.filter;
      let done = 0;
      for (const item of list) {
        uploadButton.textContent = `上传中 (${done + 1}/${list.length})`;
        const body = new FormData();
        body.append('file', item);
        try {
          const response = await uploadWithRetry(body);
          if (!response.ok) {
            say(`${item.name}：${await readError(response, '上传失败')}（换 Wi-Fi/流量再试，或等电脑前再传）`, true);
          } else {
            const record = await response.json();
            // 正在看某个分类时上传的书直接归到那个分类，不然传完还得再单独指派一次。
            if (target && record.doc_id) {
              await apiFetch(`/api/documents/${encodeURIComponent(record.doc_id)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ category_id: target }),
              });
            }
            if (record.duplicated) say(`《${record.title}》已经在库里了，没有重复解析。`);
          }
        } catch (error) {
          say(`${item.name}：${error.message}`, true);
        }
        done += 1;
      }
      uploadButton.disabled = false;
      uploadButton.textContent = '＋ 上传并入库';
      fileInput.value = '';
      await loadAll();
    }

    uploadButton.onclick = () => fileInput.click();
    fileInput.onchange = () => uploadFiles(fileInput.files);

    // ------------------------------------------------------------------
    // 加载与轮询
    // ------------------------------------------------------------------
    async function loadAll() {
      let documents;
      let categories;
      try {
        const [docResponse, catResponse] = await Promise.all([
          apiFetch('/api/documents'),
          apiFetch('/api/categories'),
        ]);
        if (!docResponse.ok || !catResponse.ok) {
          say(await readError(docResponse.ok ? catResponse : docResponse, '加载失败'), true);
          return;
        }
        documents = await docResponse.json();
        categories = await catResponse.json();
      } catch (error) {
        say(`连不上后端：${error.message}`, true);
        return;
      }
      state.documents = documents;
      state.categories = categories;
      // 当前筛选的分类被删了（别的标签页里删的），退回全部，否则页面会一直空着。
      if (state.filter && state.filter !== UNFILED
          && !categories.some((item) => item.category_id === state.filter)) {
        state.filter = '';
      }
      renderCategories();
      renderDocuments();
      renderBulkBar();
      schedulePoll();
    }

    // 解析在后台跑，前端只能问。只在有 pending/parsing 时才继续问，
    // 全部就绪就停——一直转会把这一页变成永久的请求源。
    function schedulePoll() {
      const busy = state.documents.some((doc) => doc.status === 'pending' || doc.status === 'parsing');
      if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
      if (!busy) return;
      state.pollTimer = setTimeout(() => { loadAll(); }, 2500);
    }

    // 排序只影响渲染顺序：后端照旧返回全量，勾选状态和分类归属都不受影响。
    sortSelect.value = state.sort;
    sortSelect.onchange = () => {
      state.sort = sortSelect.value;
      try { localStorage.setItem(SORT_KEY, state.sort); } catch (error) { /* 无痕模式写不进去，不影响本次使用 */ }
      renderDocuments();
    };

    (async function boot() {
      const loggedIn = await loadIdentity();
      if (!loggedIn) {
        if (!state.identity) state.identity = { guest: true };
        showGate();
        return;
      }
      who.innerHTML = `已登录 · <b>${escapeHtml(state.identity.account || '')}</b>`;
      await loadAll();
    })();