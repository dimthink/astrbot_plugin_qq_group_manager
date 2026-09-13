/**
 * QQ群管理 · WebUI 管理台（单页 SPA，hash 路由）
 *
 * 依赖 AstrBot 注入的 window.AstrBotPluginPage（bridge-sdk）：
 *   await bridge.ready() / bridge.apiGet(endpoint, params) / bridge.apiPost(endpoint, body)
 *   bridge.subscribeSSE(endpoint, handlers, {topic})
 *
 * 约定：endpoint 为插件内相对路径，不带插件名、不带前导斜杠。
 * 注意：不使用模板字符串与任何 CDN 依赖，保持零构建。
 */

const bridge = window.AstrBotPluginPage;
const PLUGIN = 'astrbot_plugin_qq_group_manager';

const state = {
  config: null,
  summary: null,
  logs: { kind: 'api', page: 1, page_size: 20, data: null, filters: {} },
  sse: null,
  sseLines: [],
  busy: false,
};

const VIEWS = [
  { id: 'dashboard', label: '总览', icon: '📊' },
  { id: 'groups', label: '群管理', icon: '👥' },
  { id: 'logs', label: '日志中心', icon: '🧾' },
  { id: 'tools', label: '工具', icon: '🧰' },
  { id: 'policy', label: '策略', icon: '⚙️', soon: 'M2' },
  { id: 'keywords', label: '关键词', icon: '🔤', soon: 'M2' },
  { id: 'members', label: '成员与禁言', icon: '🚫', soon: 'M3' },
  { id: 'joins', label: '入群审批', icon: '🚪', soon: 'M3' },
];

const LOG_TABS = [
  { kind: 'events', label: '审核事件' },
  { kind: 'actions', label: '动作执行' },
  { kind: 'api', label: 'API 调用' },
  { kind: 'capability', label: '能力受限' },
];

/* ------------------------------------------------------------------ 工具 */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach((key) => {
      const value = attrs[key];
      if (value === undefined || value === null) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'dataset') {
        Object.keys(value).forEach((k) => { node.dataset[k] = value[k]; });
      } else node.setAttribute(key, value);
    });
  }
  (children || []).forEach((child) => {
    if (child === null || child === undefined) return;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  });
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function toast(message, kind) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.className = 'toast ' + (kind || '');
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.hidden = true; }, 4200);
}

function card(title, desc, children) {
  return el('section', { class: 'card' }, [
    el('h2', { text: title }),
    desc ? el('p', { class: 'card-desc', text: desc }) : null,
  ].concat(children || []));
}

function notice(text, kind) {
  return el('div', { class: 'notice ' + (kind || ''), text });
}

function tag(text, kind) {
  return el('span', { class: 'tag ' + (kind || ''), text });
}

function fmtTime(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 19);
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 14 ? text.slice(0, 6) + '…' + text.slice(-4) : text;
}

async function loadConfig(force) {
  if (state.config && !force) return state.config;
  state.config = await bridge.apiGet('config');
  return state.config;
}

async function loadSummary() {
  state.summary = await bridge.apiGet('summary', { days: 1 });
  return state.summary;
}

/* ------------------------------------------------------------------ 顶栏 */

function renderTopbar() {
  const badges = document.getElementById('badges');
  const runtime = (state.config && state.config.runtime) || {};
  const transport = runtime.transport || {};
  const queue = runtime.db_queue || {};
  clear(badges);
  document.getElementById('version').textContent = 'v' + (runtime.version || '?');

  const items = [];
  items.push({ text: '平台：' + (transport.platform_id || '未连接'), kind: transport.available ? 'ok' : 'bad' });
  items.push({ text: runtime.dry_run ? 'DRY-RUN：只记录不处置' : '已开启实际处置', kind: runtime.dry_run ? 'warn' : 'ok' });
  items.push({ text: '审核群：' + (runtime.groups_moderating || 0) + '/' + (runtime.groups_total || 0), kind: '' });
  items.push({ text: '模式：' + (runtime.mode || '-'), kind: '' });
  if (queue.dropped) items.push({ text: '日志丢弃：' + queue.dropped, kind: 'bad' });
  if (queue.last_error) items.push({ text: '审计写入异常', kind: 'bad' });

  items.forEach((item) => badges.appendChild(el('span', { class: 'badge ' + item.kind, text: item.text })));
}

function renderNav() {
  const nav = document.getElementById('nav');
  const current = location.hash.replace('#/', '') || 'dashboard';
  clear(nav);
  VIEWS.forEach((view) => {
    nav.appendChild(el('a', {
      href: '#/' + view.id,
      class: view.id === current ? 'active' : '',
    }, [
      el('span', { text: view.icon + ' ' + view.label }),
      view.soon ? el('span', { class: 'soon', text: view.soon }) : null,
    ]));
  });
}

/* --------------------------------------------------------------- 总览页 */

function statCard(label, value, hint) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value', text: String(value) }),
    hint ? el('div', { class: 'label', text: hint }) : null,
  ]);
}

async function viewDashboard(root) {
  const config = await loadConfig();
  const runtime = config.runtime || {};
  const settings = config.settings || {};
  let summary = null;
  try { summary = await loadSummary(); } catch (error) { summary = null; }
  const stats = (summary && summary.stats) || {};
  const verdicts = stats.verdicts || {};
  const actions = stats.actions || {};
  const actionTotal = Object.keys(actions).reduce((acc, key) => acc + (actions[key].ok || 0) + (actions[key].fail || 0), 0);

  clear(root);
  root.appendChild(card('运行状态', '插件当前配置与平台连通性', [
    el('div', { class: 'grid cols-4' }, [
      statCard('平台通道', runtime.transport && runtime.transport.available ? '可用' : '不可用'),
      statCard('群数量', runtime.groups_total || 0, '审核中 ' + (runtime.groups_moderating || 0)),
      statCard('今日审核', stats.events_total || 0, '违规 ' + (verdicts.violation || 0) + ' / 可疑 ' + (verdicts.review || 0)),
      statCard('今日处置', actionTotal, '失败 ' + Object.keys(actions).reduce((acc, k) => acc + (actions[k].fail || 0), 0)),
    ]),
  ]));

  const dryRun = el('input', { type: 'checkbox' });
  dryRun.checked = !!settings.dry_run;
  const modeSelect = el('select');
  (config.options && config.options.modes ? config.options.modes : ['lenient']).forEach((mode) => {
    modeSelect.appendChild(el('option', { value: mode, text: mode, selected: mode === settings.mode ? 'selected' : null }));
  });
  const saveBtn = el('button', { class: 'btn', text: '保存运行开关' , onclick: async () => {
    saveBtn.disabled = true;
    try {
      await bridge.apiPost('config', { section: 'settings', data: { dry_run: dryRun.checked, mode: modeSelect.value } });
      state.config = null;
      toast('已保存', 'ok');
      await render();
    } catch (error) {
      toast('保存失败：' + error.message, 'bad');
    } finally { saveBtn.disabled = false; }
  } });

  root.appendChild(card('运行开关', '首次安装默认 dry-run + lenient（只记录、只警告）。确认判定准确后再关闭 dry-run。', [
    el('div', { class: 'row' }, [
      el('label', { class: 'field' }, [el('span', { text: 'dry-run（只记录不处置）' }), dryRun]),
      el('label', { class: 'field' }, [el('span', { text: '默认模式' }), modeSelect]),
      el('div', { class: 'field-actions' }, [saveBtn]),
    ]),
  ]));

  if (runtime.dry_run) {
    root.appendChild(notice('当前处于 dry-run：所有处置动作只会写入审计日志，不会真正撤回或禁言。', 'warn'));
  }
  if (!(runtime.transport && runtime.transport.available)) {
    root.appendChild(notice('未检测到 qq_official 平台通道：请在 AstrBot 中启用 QQ 官方机器人适配器，并让机器人在群里收到一条消息后重试。', 'bad'));
  }

  const alerts = (stats.capability_denied || []).map((item) => item.capability + '：err_code=' + item.err_code + ' ×' + item.count);
  if (alerts.length) {
    root.appendChild(card('能力受限（近 24 小时）', '受限不代表插件异常：平台未授权或能力仍在灰度。详细建议见「工具 → 能力自检」。', [
      el('div', { class: 'grid cols-2' }, alerts.map((text) => el('div', { class: 'notice warn', text }))),
    ]));
  }

  const tasks = (summary && summary.tasks) || [];
  const tbody = el('tbody');
  tasks.forEach((task) => {
    tbody.appendChild(el('tr', {}, [
      el('td', { text: task.name }),
      el('td', { text: String(task.interval) + 's' }),
      el('td', { text: String(task.runs) }),
      el('td', {}, [task.failures ? tag('失败 ' + task.failures, 'bad') : tag('正常', 'ok')]),
      el('td', { text: task.last_error || '—' }),
    ]));
  });
  root.appendChild(card('后台任务', '任务在插件初始化时启动，重载插件会重建。', [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['任务', '周期', '执行次数', '状态', '最近错误'].map((text) => el('th', { text })))]),
        tbody,
      ]),
    ]),
  ]));
}

/* ------------------------------------------------------------- 群管理页 */

function capabilityTags(group, options) {
  const caps = group.capabilities || {};
  const wrap = el('div', { class: 'row' });
  (options.capabilities || []).forEach((item) => {
    const record = caps[item.key];
    if (!record) return;
    const kind = record.probed === false ? 'warn' : (record.ok ? 'ok' : 'bad');
    const title = record.note || (record.err_code ? 'err_code=' + record.err_code : '');
    wrap.appendChild(el('span', { class: 'tag ' + kind, title, text: item.label }));
  });
  return wrap;
}

async function viewGroups(root) {
  const config = await loadConfig();
  const options = config.options || {};
  const groups = config.groups || [];
  clear(root);

  const idInput = el('input', { type: 'text', placeholder: 'group_openid（可从群消息日志或平台获取）' });
  const nameInput = el('input', { type: 'text', placeholder: '备注名（可选）' });
  const addBtn = el('button', { class: 'btn ghost', text: '添加群', onclick: async () => {
    if (!idInput.value.trim()) { toast('请填写 group_openid', 'bad'); return; }
    addBtn.disabled = true;
    try {
      await bridge.apiPost('groups/add', { group_id: idInput.value.trim(), name: nameInput.value.trim() });
      idInput.value = ''; nameInput.value = '';
      state.config = null;
      toast('已添加，建议立即执行能力探测', 'ok');
      await render();
    } catch (error) { toast('添加失败：' + error.message, 'bad'); }
    finally { addBtn.disabled = false; }
  } });
  const probeAllBtn = el('button', { class: 'btn ghost', text: '全部重探', onclick: async () => {
    probeAllBtn.disabled = true;
    try {
      await bridge.apiPost('groups/probe', { all: true });
      state.config = null;
      toast('能力探测完成', 'ok');
      await render();
    } catch (error) { toast('探测失败：' + error.message, 'bad'); }
    finally { probeAllBtn.disabled = false; }
  } });

  root.appendChild(card('群列表', '群在收到消息后会自动登记；也可手动添加。启用审核前必须先开启「接收全部消息」。', [
    el('div', { class: 'row' }, [idInput, nameInput, el('div', { class: 'field-actions' }, [addBtn, probeAllBtn])]),
  ]));

  if (!groups.length) {
    root.appendChild(notice('还没有记录任何群：把机器人拉进群并在群里 @ 一次机器人，或在上方手动添加 group_openid。'));
    return;
  }

  const tbody = el('tbody');
  groups.forEach((group) => {
    const probeBtn = el('button', { class: 'btn small ghost', text: '探测', onclick: async () => {
      probeBtn.disabled = true;
      try {
        await bridge.apiPost('groups/probe', { group_id: group.group_id });
        state.config = null;
        toast('已重新探测', 'ok');
        await render();
      } catch (error) { toast('探测失败：' + error.message, 'bad'); }
      finally { probeBtn.disabled = false; }
    } });

    const toggleBtn = el('button', {
      class: 'btn small ' + (group.moderation_enabled ? 'ghost' : ''),
      text: group.moderation_enabled ? '停用审核' : '启用审核',
      onclick: async () => {
        toggleBtn.disabled = true;
        const enable = !group.moderation_enabled;
        if (enable) {
          const ok = window.confirm('启用审核将对该群的全部消息做 LLM 判定（消耗 token）。确认继续？');
          if (!ok) { toggleBtn.disabled = false; return; }
        }
        try {
          await bridge.apiPost('groups/moderation', { group_id: group.group_id, enable });
          state.config = null;
          toast(enable ? '审核已启用' : '审核已停用', 'ok');
          await render();
        } catch (error) {
          let message = error.message;
          try {
            const parsed = JSON.parse(message);
            if (parsed && parsed.data && parsed.data.reason_code === 'need_full_msg') {
              message = parsed.message || parsed.data.message;
            }
          } catch (ignore) { /* 非 JSON 错误 */ }
          toast('操作失败：' + message, 'bad');
          window.alert(message + '\n\n（完整指引：工具 → 指令速查 / docs/CONFIG.md）');
        } finally { toggleBtn.disabled = false; }
      },
    });

    const removeBtn = el('button', { class: 'btn small danger', text: '移除记录', onclick: async () => {
      if (!window.confirm('仅移除插件侧的群记录，不影响平台与群成员。确认？')) return;
      removeBtn.disabled = true;
      try {
        await bridge.apiPost('groups/remove', { group_id: group.group_id });
        state.config = null;
        toast('已移除', 'ok');
        await render();
      } catch (error) { toast('移除失败：' + error.message, 'bad'); }
      finally { removeBtn.disabled = false; }
    } });

    const stateTags = [];
    stateTags.push(group.moderation_enabled ? tag('审核中', 'ok') : tag('未开启审核', ''));
    if (group.paused_reason) stateTags.push(tag('已暂停：' + group.paused_reason, 'warn'));
    stateTags.push(group.cap_is_admin ? tag('群管理员', 'ok') : tag('非管理员', 'warn'));
    stateTags.push(group.cap_full_msg ? tag('全量消息', 'ok') : tag('仅 @消息', 'warn'));

    tbody.appendChild(el('tr', {}, [
      el('td', {}, [
        el('div', { text: group.name || '（未获取群名）' }),
        el('div', { class: 'muted mono', text: shortId(group.group_id), title: group.group_id }),
      ]),
      el('td', {}, stateTags),
      el('td', {}, [capabilityTags(group, options)]),
      el('td', { text: group.effective_mode || '-' }),
      el('td', { text: group.last_seen_iso ? fmtTime(group.last_seen_iso) : '—' }),
      el('td', {}, [el('div', { class: 'field-actions' }, [toggleBtn, probeBtn, removeBtn])]),
    ]));
  });

  root.appendChild(card('群明细', '能力标签：绿色=可用，红色=受限（悬停查看 err_code 与原因），黄色=无只读探测接口。', [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, ['群', '状态', '平台能力', '模式', '最近活跃', '操作'].map((text) => el('th', { text })))]),
        tbody,
      ]),
    ]),
  ]));
}

/* ------------------------------------------------------------- 日志中心 */

function logColumns(kind) {
  if (kind === 'events') return ['ts', 'group_id', 'sender_name', 'verdict', 'category', 'severity', 'confidence', 'reason'];
  if (kind === 'actions') return ['ts', 'group_id', 'action', 'target_openid', 'ok', 'err_code', 'dry_run'];
  if (kind === 'api') return ['ts_unix', 'group_id', 'method', 'path', 'ok', 'err_code', 'caller', 'duration_ms'];
  return ['ts_unix', 'group_id', 'capability', 'ok', 'err_code', 'note'];
}

function cellValue(kind, key, row) {
  const value = row[key];
  if (key === 'ts' || key === 'ts_unix') {
    const raw = row.ts || (row.ts_unix ? new Date(row.ts_unix * 1000).toISOString() : '');
    return fmtTime(raw);
  }
  if (key === 'group_id' || key === 'target_openid') return shortId(value);
  if (key === 'ok') return value ? '成功' : '失败';
  if (key === 'dry_run') return value ? 'dry-run' : '';
  if (key === 'confidence' && typeof value === 'number') return value.toFixed(2);
  if (key === 'path') return String(value || '').replace('/v2/groups/{group_openid}', '');
  return value === null || value === undefined ? '—' : String(value);
}

async function loadLogs(kind, page) {
  const query = Object.assign({ page: page || 1, page_size: state.logs.page_size }, state.logs.filters || {});
  clear(document.getElementById('content'));
  const content = document.getElementById('content');
  content.appendChild(el('div', { class: 'loading', text: '正在加载日志…' }));
  try {
    state.logs.kind = kind;
    state.logs.page = page || 1;
    state.logs.data = await bridge.apiGet('logs/' + kind, query);
  } catch (error) {
    state.logs.data = null;
    toast('加载日志失败：' + error.message, 'bad');
  }
  await render();
}

async function viewLogs(root) {
  const kind = state.logs.kind;
  const data = state.logs.data;
  const filters = state.logs.filters || {};

  const tabs = el('div', { class: 'tabs' });
  LOG_TABS.forEach((tab) => {
    tabs.appendChild(el('button', {
      class: tab.kind === kind ? 'active' : '',
      text: tab.label,
      onclick: () => loadLogs(tab.kind, 1),
    }));
  });

  const groupInput = el('input', { type: 'text', placeholder: '按 group_openid 过滤', value: filters.group_id || '' });
  const keywordInput = el('input', { type: 'text', placeholder: '关键字（路径/昵称/片段）', value: filters.keyword || '' });
  const daysSelect = el('select');
  [1, 7, 30].forEach((days) => {
    daysSelect.appendChild(el('option', { value: String(days), text: '近 ' + days + ' 天', selected: String(filters.days) === String(days) ? 'selected' : null }));
  });
  const applyBtn = el('button', { class: 'btn ghost', text: '筛选', onclick: () => {
    state.logs.filters = {
      group_id: groupInput.value.trim() || undefined,
      keyword: keywordInput.value.trim() || undefined,
      days: daysSelect.value,
    };
    loadLogs(kind, 1);
  } });
  const clearBtn = el('button', { class: 'btn ghost', text: '清空筛选', onclick: () => {
    state.logs.filters = {};
    loadLogs(kind, 1);
  } });
  const exportBtn = el('button', { class: 'btn ghost', text: '导出 CSV', onclick: async () => {
    exportBtn.disabled = true;
    try {
      await bridge.download('logs/export', Object.assign({ kind, format: 'csv' }, state.logs.filters), kind + '.csv');
    } catch (error) { toast('导出失败：' + error.message, 'bad'); }
    finally { exportBtn.disabled = false; }
  } });

  clear(root);
  root.appendChild(card('日志中心', '审核事件与处置将在 M2 之后产生；API 调用与能力受限日志现在即可查看。', [
    tabs,
    el('div', { class: 'row' }, [groupInput, keywordInput, daysSelect, el('div', { class: 'field-actions' }, [applyBtn, clearBtn, exportBtn])]),
  ]));

  if (!data) {
    root.appendChild(notice('暂无数据或加载失败。'));
    return;
  }

  const columns = logColumns(kind);
  const tbody = el('tbody');
  (data.items || []).forEach((row) => {
    tbody.appendChild(el('tr', {}, columns.map((column) => el('td', {
      text: cellValue(kind, column, row),
      class: column === 'note' || column === 'reason' ? 'mono' : '',
    }))));
  });
  if (!(data.items || []).length) {
    root.appendChild(notice('该筛选条件下没有记录。'));
    return;
  }

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 20)));
  const prev = el('button', { class: 'btn small ghost', text: '上一页', disabled: data.page <= 1 ? 'disabled' : null, onclick: () => loadLogs(kind, data.page - 1) });
  const next = el('button', { class: 'btn small ghost', text: '下一页', disabled: data.page >= totalPages ? 'disabled' : null, onclick: () => loadLogs(kind, data.page + 1) });

  root.appendChild(card('记录（共 ' + (data.total || 0) + ' 条）', null, [
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, columns.map((column) => el('th', { text: column })))]),
        tbody,
      ]),
    ]),
    el('div', { class: 'pager' }, [prev, el('span', { text: '第 ' + data.page + ' / ' + totalPages + ' 页' }), next]),
  ]));

  const clearLogsBtn = el('button', { class: 'btn danger', text: '清空当前类型日志', onclick: async () => {
    if (!window.confirm('将删除 ' + kind + ' 类型的全部日志，操作不可恢复。确认？')) return;
    clearLogsBtn.disabled = true;
    try {
      const result = await bridge.apiPost('logs/clear', { scope: kind });
      toast('已删除 ' + JSON.stringify(result.deleted || {}), 'ok');
      loadLogs(kind, 1);
    } catch (error) { toast('清空失败：' + error.message, 'bad'); }
    finally { clearLogsBtn.disabled = false; }
  } });
  root.appendChild(card('危险操作', '清空后不可恢复；导出 CSV 可先留档。', [
    el('div', { class: 'field-actions' }, [clearLogsBtn]),
  ]));
}

/* ---------------------------------------------------------------- 工具页 */

async function viewTools(root) {
  const config = await loadConfig();
  clear(root);

  const selfcheckOut = el('pre', { class: 'guide', text: '尚未执行。' });
  const selfcheckBtn = el('button', { class: 'btn', text: '执行能力自检', onclick: async () => {
    selfcheckBtn.disabled = true;
    selfcheckOut.textContent = '正在探测平台能力…';
    try {
      const result = await bridge.apiPost('selfcheck', {});
      const lines = [];
      Object.keys(result.report || {}).forEach((groupId) => {
        const item = result.report[groupId];
        lines.push('群 ' + shortId(groupId));
        Object.keys(item.capabilities || {}).forEach((cap) => {
          const record = item.capabilities[cap];
          const flag = record.probed === false ? '➖' : (record.ok ? '✅' : '❌');
          let line = '  ' + flag + ' ' + cap;
          if (record.err_code) line += ' err_code=' + record.err_code;
          if (record.note) line += ' · ' + record.note;
          lines.push(line);
        });
        (item.suggestions || []).forEach((text) => lines.push('  👉 ' + text));
        lines.push('');
      });
      lines.push('平台通道：' + (result.transport && result.transport.available ? '可用' : '不可用'));
      selfcheckOut.textContent = lines.join('\n') || '没有可自检的群。';
    } catch (error) {
      selfcheckOut.textContent = '自检失败：' + error.message;
    } finally { selfcheckBtn.disabled = false; }
  } });

  root.appendChild(card('能力自检', '逐项调用平台只读接口。受限（err_code=11253）表示该接口未对本机器人开放。', [
    el('div', { class: 'field-actions' }, [selfcheckBtn]),
    selfcheckOut,
  ]));

  let dbInfo = null;
  try { dbInfo = await bridge.apiGet('db/info'); } catch (error) { dbInfo = null; }
  const dbOut = el('div', { class: 'grid cols-2' });
  if (dbInfo && dbInfo.db) {
    dbOut.appendChild(el('div', { class: 'notice', text: '数据库：' + dbInfo.db.path }));
    dbOut.appendChild(el('div', { class: 'notice', text: '文件大小：' + Math.round((dbInfo.db.size_bytes || 0) / 1024) + ' KB（WAL ' + Math.round((dbInfo.db.wal_bytes || 0) / 1024) + ' KB）' }));
    Object.keys(dbInfo.db.tables || {}).forEach((key) => {
      const table = dbInfo.db.tables[key];
      dbOut.appendChild(el('div', { class: 'notice', text: table.table + '：' + table.count + ' 行' + (table.oldest ? '（' + fmtTime(table.oldest) + ' 起）' : '') }));
    });
    dbOut.appendChild(el('div', { class: 'notice', text: '写队列：' + JSON.stringify(dbInfo.queue || {}) }));
  } else {
    dbOut.appendChild(notice('无法读取数据库信息。', 'bad'));
  }

  const mkDbBtn = (label, op, confirmText) => el('button', { class: 'btn ghost', text: label, onclick: async () => {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await bridge.apiPost('db/maintain', { op });
      toast('已执行：' + op, 'ok');
      await render();
    } catch (error) { toast(op + ' 失败：' + error.message, 'bad'); }
  } });

  root.appendChild(card('审计库维护', '按保留策略裁剪历史、整理文件体积；备份会直接下载一份一致性副本。', [
    dbOut,
    el('div', { class: 'field-actions' }, [
      mkDbBtn('按保留策略裁剪', 'prune', null),
      mkDbBtn('VACUUM 整理', 'vacuum', '整理期间数据库会短暂锁定，确认执行？'),
      el('button', { class: 'btn ghost', text: '备份并下载', onclick: async () => {
        try { await bridge.download('db/maintain', { op: 'backup' }, 'moderation-backup.db'); }
        catch (error) { toast('备份失败：' + error.message, 'bad'); }
      } }),
    ]),
  ]));

  const sseBox = el('pre', { class: 'guide', text: '实时事件将显示在这里（默认订阅 audit）…' });
  const sseBtn = el('button', { class: 'btn ghost', text: '开始实时订阅', onclick: async () => {
    if (state.sse) {
      try { await bridge.unsubscribeSSE(state.sse); } catch (ignore) { /* 已断开 */ }
      state.sse = null;
      sseBtn.textContent = '开始实时订阅';
      return;
    }
    try {
      state.sse = await bridge.subscribeSSE('events/stream', {
        onMessage: (event) => {
          state.sseLines.unshift(fmtTime(new Date().toISOString()) + ' ' + (event.raw || ''));
          state.sseLines = state.sseLines.slice(0, 60);
          sseBox.textContent = state.sseLines.join('\n');
        },
      }, { topic: 'audit' });
      sseBtn.textContent = '停止订阅';
    } catch (error) { toast('订阅失败：' + error.message, 'bad'); }
  } });

  root.appendChild(card('实时事件（SSE）', '用于验证 WebUI ↔ 后端通道；审核事件在 M2 接入后会大量出现。', [
    el('div', { class: 'field-actions' }, [sseBtn]),
    sseBox,
  ]));

  let instructions = null;
  try { instructions = await bridge.apiGet('instructions'); } catch (error) { instructions = null; }
  if (instructions) {
    const list = el('div', { class: 'grid cols-2' });
    [['public', '所有人'], ['group_admin', '群主 / 群管理员'], ['admin', 'AstrBot 管理员']].forEach((pair) => {
      const items = instructions[pair[0]] || [];
      if (!items.length) return;
      const box = el('div', { class: 'notice' });
      box.appendChild(el('div', { text: '【' + pair[1] + '】' }));
      items.forEach((item) => box.appendChild(el('div', { class: 'mono', text: item.command + ' — ' + item.desc })));
      list.appendChild(box);
    });
    root.appendChild(card('指令速查', instructions.note || '', [list]));
  }

  root.appendChild(card('当前配置摘要', '完整编辑在「策略 / 关键词」视图（M2 提供）。', [
    el('pre', { class: 'guide', text: JSON.stringify(config.settings || {}, null, 2) }),
  ]));
}

/* ------------------------------------------------------------- 占位视图 */

function viewComingSoon(root, view) {
  clear(root);
  root.appendChild(card(view.label, null, [
    notice('该视图将在 ' + view.soon + ' 版本提供：' + view.label + '。当前版本（M1）已交付能力探测、群列表、日志中心与工具页。'),
  ]));
}

/* ------------------------------------------------------------------ 路由 */

async function render() {
  const viewId = location.hash.replace('#/', '') || 'dashboard';
  const view = VIEWS.find((item) => item.id === viewId) || VIEWS[0];
  const root = document.getElementById('content');
  renderNav();
  try {
    await loadConfig(true);
    renderTopbar();
  } catch (error) {
    clear(root);
    root.appendChild(notice('无法读取插件配置：' + error.message + '（请确认插件已启用并在 WebUI 中重载过）', 'bad'));
    return;
  }
  if (view.soon && view.id !== 'dashboard') {
    if (['policy', 'keywords', 'members', 'joins'].indexOf(view.id) >= 0) {
      viewComingSoon(root, view);
      return;
    }
  }
  if (view.id === 'dashboard') await viewDashboard(root);
  else if (view.id === 'groups') await viewGroups(root);
  else if (view.id === 'logs') await viewLogs(root);
  else if (view.id === 'tools') await viewTools(root);
  else viewComingSoon(root, view);
}

async function boot() {
  try {
    if (bridge && typeof bridge.ready === 'function') await bridge.ready();
  } catch (error) {
    // 忽略：bridge 不可用时仍尝试直接请求
  }
  document.getElementById('btn-refresh').addEventListener('click', async () => {
    state.config = null;
    state.logs.data = null;
    await render();
    toast('已刷新', 'ok');
  });
  window.addEventListener('hashchange', render);
  await render();
}

boot();
