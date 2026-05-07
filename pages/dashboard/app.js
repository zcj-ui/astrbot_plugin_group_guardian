const bridge = window.AstrBotPluginPage;
if (!bridge) {
  document.body.innerHTML = '<div style="text-align:center;padding:80px;color:#909399">请在 AstrBot 管理面板中打开此页面</div>';
  throw new Error('AstrBot bridge not found');
}
const { ready, apiGet, apiPost } = bridge;

const PREFIX = '/group_guardian';
let CONFIG = {};
let CURRENT_GROUP = null;
let CURRENT_MEMBERS = [];
let CACHED_GROUP_ID = null;

function $(sel, ctx = document) { return ctx.querySelector(sel); }
function $$(sel, ctx = document) { return [...ctx.querySelectorAll(sel)]; }

function toast(msg, type = 'info') {
  const c = $('#toastContainer');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(40px)'; setTimeout(() => t.remove(), 300); }, 2500);
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const ROLE_NAME = { owner: '群主', admin: '管理员', member: '成员' };

async function safeGet(path) {
  try { const r = await apiGet(path); return typeof r === 'string' ? JSON.parse(r) : r; }
  catch { return { status: 'error', message: '请求失败' }; }
}
async function safePost(path, body) {
  try { const r = await apiPost(path, body); return typeof r === 'string' ? JSON.parse(r) : r; }
  catch { return { status: 'error', message: '请求失败' }; }
}

function showModal(title, bodyHtml, footerHtml = '') {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = bodyHtml;
  $('#modalFooter').innerHTML = footerHtml;
  $('#modalOverlay').style.display = 'flex';
}
function closeModal() { $('#modalOverlay').style.display = 'none'; }

// ============ TABS ============
function initTabs() {
  $$('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
      $$('.nav-item').forEach(i => i.classList.remove('active'));
      $$('.tab-panel').forEach(p => p.classList.remove('active'));
      item.classList.add('active');
      $(`#tab-${item.dataset.tab}`).classList.add('active');
      if (item.dataset.tab === 'overview') loadOverview();
      if (item.dataset.tab === 'groups') {
        $('#memberPanel').style.display = 'none';
        const groupCard = $('#groupGrid')?.closest('.card');
        if (groupCard) groupCard.style.display = '';
        CURRENT_GROUP = null;
        CACHED_GROUP_ID = null;
        CURRENT_MEMBERS = [];
        loadGroups();
        loadAdmins();
      }
      if (item.dataset.tab === 'records') { loadUsers(); loadLogs(); }
      if (item.dataset.tab === 'settings') loadSettings();
    });
  });
}

// ============ OVERVIEW ============
async function loadOverview() {
  const [statsRes, todayRes] = await Promise.all([
    safeGet(`${PREFIX}/stats`),
    safeGet(`${PREFIX}/today_stats`)
  ]);
  if (statsRes.status === 'success') {
    const d = statsRes.data;
    $('#statTodayBlocked').textContent = d.today_blocked ?? '--';
    $('#statTodayTotal').textContent = d.today_total ?? '--';
    $('#statTodayPassed').textContent = d.today_passed ?? '--';
    $('#statWhiteCount').textContent = d.group_white_list_count ?? '--';
    $('#statBlackCount').textContent = d.group_black_list_count ?? '--';
    $('#statUserBlackCount').textContent = d.user_black_list_count ?? '--';
    $('#statAdminCount').textContent = d.admin_list_count ?? '--';
    $('#statTotalLogs').textContent = d.total_logs ?? '--';
    $('#logoVer').textContent = d.version || 'v1.8.0';
  }
  if (todayRes.status === 'success') {
    renderRanking('groupRanking', todayRes.data.group_ranking, item =>
      `<span class="ranking-name">群 ${esc(item.group_id)}</span><span class="ranking-count">${item.count}</span>`
    );
    renderRanking('userRanking', todayRes.data.user_ranking, item =>
      `<span class="ranking-name">${esc(item.user_name || item.user_id)}</span><span class="ranking-count">${item.count}</span>`
    );
  }
}

function renderRanking(containerId, list, renderFn) {
  const c = $(`#${containerId}`);
  if (!list || !list.length) { c.innerHTML = '<p class="empty">暂无数据</p>'; return; }
  c.innerHTML = list.map((item, i) => {
    const cls = i === 0 ? 'top1' : i === 1 ? 'top2' : i === 2 ? 'top3' : 'normal';
    return `<div class="ranking-item"><div class="ranking-num ${cls}">${i + 1}</div>${renderFn(item)}</div>`;
  }).join('');
}

// ============ GROUPS ============
async function loadGroups() {
  const grid = $('#groupGrid');
  grid.innerHTML = '<p class="empty">加载中...</p>';
  const res = await safeGet(`${PREFIX}/groups`);
  if (res.status !== 'success') { grid.innerHTML = `<p class="empty">${esc(res.message)}</p>`; return; }
  CONFIG._groups = res.data;
  renderGroups(res.data);
}

function renderGroups(groups) {
  const grid = $('#groupGrid');
  const keyword = ($('#searchGroupInput')?.value || '').toLowerCase().trim();
  let filtered = groups;
  if (keyword) {
    filtered = groups.filter(g => g.group_id.includes(keyword) || (g.group_name || '').toLowerCase().includes(keyword));
  }
  if (!filtered.length) { grid.innerHTML = '<p class="empty">未找到匹配的群</p>'; return; }
  grid.innerHTML = filtered.map(g => {
    const cls = g.is_white ? 'is-white' : g.is_black ? 'is-black' : '';
    const badges = [];
    if (g.is_white) badges.push('<span class="group-badge badge-white">白名单</span>');
    if (g.is_black) badges.push('<span class="group-badge badge-black">黑名单</span>');
    if (g.is_white && g.today_blocked > 0) badges.push(`<span class="group-badge badge-blocked">今日拦截 ${g.today_blocked}</span>`);
    return `<div class="group-card ${cls}" data-gid="${esc(g.group_id)}">
      <img class="group-avatar" src="${esc(g.avatar)}" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Crect fill=%22%23e4e7ed%22 width=%22100%22 height=%22100%22/%3E%3Ctext x=%2250%22 y=%2255%22 text-anchor=%22middle%22 font-size=%2230%22 fill=%22%23909399%22%3EG%3C/text%3E%3C/svg%3E'" />
      <div class="group-info">
        <div class="group-name">${esc(g.group_name || '未命名群')}</div>
        <div class="group-meta">
          <span class="group-id">${esc(g.group_id)}</span>
          <span class="group-members">${g.member_count} 人</span>
          ${badges.join('')}
        </div>
      </div>
      <div class="group-actions">
        ${g.is_white
          ? `<button class="btn btn-outline btn-sm" data-action="remove-white" data-gid="${esc(g.group_id)}">移出白名单</button>`
          : `<button class="btn btn-primary btn-sm" data-action="add-white" data-gid="${esc(g.group_id)}">加入白名单</button>`
        }
        ${g.is_black
          ? `<button class="btn btn-outline btn-sm" data-action="remove-black" data-gid="${esc(g.group_id)}">移出黑名单</button>`
          : `<button class="btn btn-danger btn-sm" data-action="add-black" data-gid="${esc(g.group_id)}">加入黑名单</button>`
        }
      </div>
    </div>`;
  }).join('');

  $$('.group-card', grid).forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('[data-action]')) return;
      openMemberPanel(card.dataset.gid);
    });
  });

  $$('[data-action]', grid).forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const gid = btn.dataset.gid;
      if (action === 'add-white') { await safePost(`${PREFIX}/whitelist/add`, { group_id: gid }); toast('已加入白名单', 'success'); }
      else if (action === 'remove-white') { await safePost(`${PREFIX}/whitelist/remove`, { group_id: gid }); toast('已移出白名单', 'success'); }
      else if (action === 'add-black') { await safePost(`${PREFIX}/blacklist/add`, { group_id: gid }); toast('已加入黑名单', 'danger'); }
      else if (action === 'remove-black') { await safePost(`${PREFIX}/blacklist/remove`, { group_id: gid }); toast('已移出黑名单', 'success'); }
      loadGroups();
    });
  });
}

async function openMemberPanel(gid) {
  CURRENT_GROUP = gid;
  const panel = $('#memberPanel');
  panel.style.display = 'block';
  $('#memberPanelTitle').textContent = `群 ${gid} 成员`;
  $('#memberList').innerHTML = '<p class="empty">加载中...</p>';
  $('#groupGrid').closest('.card').style.display = 'none';
  if (CACHED_GROUP_ID !== gid) {
    const res = await safeGet(`${PREFIX}/group_members?group_id=${gid}`);
    if (res.status !== 'success') { $('#memberList').innerHTML = `<p class="empty">${esc(res.message)}</p>`; return; }
    CURRENT_MEMBERS = res.data;
    CACHED_GROUP_ID = gid;
  }
  renderMembers(CURRENT_MEMBERS);
}

function renderMembers(members) {
  const keyword = ($('#searchMemberInput')?.value || '').toLowerCase().trim();
  let filtered = members;
  if (keyword) {
    filtered = members.filter(m => (m.display_name || '').toLowerCase().includes(keyword) || m.user_id.includes(keyword));
  }
  const ownerCount = filtered.filter(m => m.role === 'owner').length;
  const adminCount = filtered.filter(m => m.role === 'admin').length;
  $('#memberStats').innerHTML = `
    <span class="ms-item"><span class="ms-dot owner"></span> 群主 ${ownerCount}</span>
    <span class="ms-item"><span class="ms-dot admin"></span> 管理员 ${adminCount}</span>
    <span class="ms-item"><span class="ms-dot member"></span> 成员 ${filtered.length - ownerCount - adminCount}</span>
    <span class="ms-item">共 ${filtered.length} 人</span>
  `;
  $('#memberList').innerHTML = filtered.map(m => {
    const roleName = ROLE_NAME[m.role] || m.role;
    const roleCls = m.role === 'owner' ? 'role-owner' : m.role === 'admin' ? 'role-admin' : 'role-member';
    const titleHtml = m.title ? `<span class="member-title" title="${esc(m.title)}">${esc(m.title)}</span>` : '';
    const adminBtn = m.is_plugin_admin
      ? `<button class="btn btn-outline btn-sm" data-action="remove-admin" data-uid="${esc(m.user_id)}">移除管理</button>`
      : `<button class="btn btn-primary btn-sm" data-action="add-admin" data-uid="${esc(m.user_id)}">设为管理</button>`;
    return `<div class="member-item">
      <img class="member-avatar" src="${esc(m.avatar)}" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Crect fill=%22%23e4e7ed%22 width=%22100%22 height=%22100%22/%3E%3Ctext x=%2250%22 y=%2255%22 text-anchor=%22middle%22 font-size=%2228%22 fill=%22%23909399%22%3E?%3C/text%3E%3C/svg%3E'" />
      <span class="member-name">${esc(m.display_name)}</span>
      <span class="member-role ${roleCls}">${roleName}</span>
      ${titleHtml}
      <span class="member-uid">${esc(m.user_id)}</span>
      <span class="member-actions">${adminBtn}</span>
    </div>`;
  }).join('');

  $$('[data-action="add-admin"]', $('#memberList')).forEach(btn => {
    btn.addEventListener('click', async () => {
      await safePost(`${PREFIX}/admin/add`, { user_id: btn.dataset.uid });
      toast('已添加为管理员', 'success');
      CACHED_GROUP_ID = null;
      openMemberPanel(CURRENT_GROUP);
    });
  });
  $$('[data-action="remove-admin"]', $('#memberList')).forEach(btn => {
    btn.addEventListener('click', async () => {
      await safePost(`${PREFIX}/admin/remove`, { user_id: btn.dataset.uid });
      toast('已移除管理员', 'success');
      CACHED_GROUP_ID = null;
      openMemberPanel(CURRENT_GROUP);
    });
  });
}

// ============ ADMIN MANAGEMENT ============
async function loadAdmins() {
  const res = await safeGet(`${PREFIX}/config`);
  if (res.status !== 'success') return;
  CONFIG = { ...CONFIG, ...res.data };
  renderAdmins();
}

function renderAdmins() {
  const admins = CONFIG._admin_list || CONFIG.admin_list || [];
  const c = $('#adminList');
  if (!admins.length) { c.innerHTML = '<p class="empty">暂无管理员</p>'; return; }
  c.innerHTML = admins.map(uid => `
    <span class="admin-tag">
      ${esc(String(uid))}
      <button class="btn-remove" data-uid="${esc(String(uid))}">&times;</button>
    </span>
  `).join('');
  $$('.btn-remove', c).forEach(btn => {
    btn.addEventListener('click', async () => {
      await safePost(`${PREFIX}/admin/remove`, { user_id: btn.dataset.uid });
      toast('已移除管理员', 'success');
      loadAdmins();
    });
  });
}

// ============ RECORDS ============
async function loadUsers() {
  const c = $('#userContainer');
  c.innerHTML = '<p class="empty">加载中...</p>';
  const res = await safeGet(`${PREFIX}/moderation_users`);
  if (res.status !== 'success') { c.innerHTML = `<p class="empty">${esc(res.message)}</p>`; return; }
  renderUsers(res.data);
}

function renderUsers(users) {
  const c = $('#userContainer');
  if (!users || !users.length) { c.innerHTML = '<p class="empty">暂无数据</p>'; return; }
  c.innerHTML = users.map(u => {
    const groups = [...new Set(u.records.map(r => r.group_id).filter(Boolean))];
    const groupStr = groups.slice(0, 2).join(', ') + (groups.length > 2 ? '...' : '');
    const latest = u.records[u.records.length - 1];
    return `<div class="user-card" data-uid="${esc(u.user_id)}">
      <div class="user-header">
        <label class="user-check"><input type="checkbox" class="ck" data-uid="${esc(u.user_id)}" /></label>
        <div class="user-info"><span class="user-name">${esc(u.user_name)}</span><span class="user-uid">${esc(u.user_id)}</span></div>
        <div class="user-meta">
          <span class="user-group">${esc(groupStr)}</span>
          <span class="user-count">${u.count}次</span>
          <span class="user-time">${fmtTime(latest.ts)}</span>
          <span class="expand-icon">▶</span>
        </div>
      </div>
      <div class="user-records">
        <table class="record-table"><thead><tr><th>时间</th><th>群</th><th>消息</th><th>原因</th><th>操作</th></tr></thead>
        <tbody>${u.records.map(r => `<tr>
          <td>${fmtTime(r.ts)}</td>
          <td>${esc(r.group_id)}</td>
          <td title="${esc(r.msg_preview)}">${esc((r.msg_preview || '').slice(0, 30))}</td>
          <td>${esc((r.reason || '').slice(0, 20))}</td>
          <td><button class="btn btn-danger btn-sm" data-del="${esc(r.id)}">删除</button></td>
        </tr>`).join('')}</tbody></table>
      </div>
    </div>`;
  }).join('');

  $$('.user-header', c).forEach(h => {
    h.addEventListener('click', (e) => {
      if (e.target.closest('.user-check') || e.target.closest('.ck')) return;
      const icon = $('.expand-icon', h);
      const rec = h.nextElementSibling;
      icon.classList.toggle('expanded');
      rec.style.display = rec.style.display === 'block' ? 'none' : 'block';
    });
  });

  $$('[data-del]', c).forEach(btn => {
    btn.addEventListener('click', async () => {
      await safePost(`${PREFIX}/logs/delete`, { ids: [btn.dataset.del] });
      toast('已删除', 'success');
      loadUsers();
    });
  });
}

async function loadLogs() {
  const c = $('#logContainer');
  c.innerHTML = '<p class="empty">加载中...</p>';
  const res = await safeGet(`${PREFIX}/logs`);
  if (res.status !== 'success') { c.innerHTML = `<p class="empty">${esc(res.message)}</p>`; return; }
  const logs = res.data || [];
  if (!logs.length) { c.innerHTML = '<p class="empty">暂无日志</p>'; return; }
  const rows = logs.slice(0, 100).map(l => {
    const a = l.action || '';
    const cls = a.includes('撤回') ? 'badge-danger' : a.includes('放行') ? 'badge-success' : a.includes('提醒') ? 'badge-warning' : 'badge-default';
    return `<tr>
      <td>${fmtTime(l.ts)}</td>
      <td>${esc(l.group_id)}</td>
      <td>${esc(l.user_name)}<br><span style="font-size:10px;color:#909399">${esc(l.user_id)}</span></td>
      <td class="msg-cell" title="${esc(l.msg_preview)}">${esc(l.msg_preview)}</td>
      <td><span class="badge ${cls}">${esc(a)}</span></td>
      <td class="reason-cell" title="${esc(l.reason)}">${esc((l.reason || '').slice(0, 30))}</td>
    </tr>`;
  }).join('');
  c.innerHTML = `<table class="log-table"><thead><tr><th>时间</th><th>群号</th><th>用户</th><th>消息</th><th>操作</th><th>原因</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ============ SETTINGS ============
const CORE_TOGGLES = [
  { key: 'enabled', label: '插件总开关', desc: '启用/禁用整个插件' },
  { key: 'auto_moderate_enabled', label: '自动审核', desc: '开启后自动审核群消息' },
  { key: 'auto_moderate_notice', label: '审核通知', desc: '审核结果通知管理员' },
];
const AUDIT_TOGGLES = [
  { key: 'scan_swear', label: '脏话检测', desc: '使用正则检测脏话' },
  { key: 'scan_ad', label: '广告检测', desc: '使用正则检测广告' },
  { key: 'llm_moderation_enabled', label: 'AI审核', desc: '使用LLM进行深度审核' },
  { key: 'llm_moderation_ban', label: 'AI审核后禁言', desc: 'AI判定违规后自动禁言' },
  { key: 'prompt_injection_enabled', label: '防注入检测', desc: '检测恶意Prompt注入' },
];
const LEXICON_TOGGLES = [
  { key: 'lexicon_political_enabled', label: '政治敏感' },
  { key: 'lexicon_porn_enabled', label: '色情内容' },
  { key: 'lexicon_violent_enabled', label: '暴力恐怖' },
  { key: 'lexicon_reactionary_enabled', label: '反动言论' },
  { key: 'lexicon_weapons_enabled', label: '管制器具' },
  { key: 'lexicon_corruption_enabled', label: '贪污腐败' },
  { key: 'lexicon_illegal_url_enabled', label: '违法网址' },
  { key: 'lexicon_other_enabled', label: '其他违规' },
];
const FEATURE_TOGGLES = [
  { key: 'ban_enabled', label: '禁言功能' },
  { key: 'unban_enabled', label: '解禁功能' },
  { key: 'kick_enabled', label: '踢人功能' },
  { key: 'recall_enabled', label: '撤回消息' },
  { key: 'whole_ban_enabled', label: '全体禁言' },
  { key: 'set_admin_enabled', label: '设置管理员' },
  { key: 'set_card_enabled', label: '设置名片' },
  { key: 'set_title_enabled', label: '设置头衔' },
  { key: 'member_list_enabled', label: '成员列表' },
  { key: 'banned_list_enabled', label: '禁言列表' },
  { key: 'join_verify_enabled', label: '加群验证' },
  { key: 'essence_enabled', label: '精华消息' },
  { key: 'group_files_enabled', label: '群文件管理' },
  { key: 'set_group_name_enabled', label: '修改群名' },
  { key: 'send_announcement_enabled', label: '发布公告' },
  { key: 'delete_announcement_enabled', label: '删除公告' },
  { key: 'list_announcements_enabled', label: '查看公告' },
  { key: 'group_honor_enabled', label: '群荣誉查看' },
  { key: 'at_all_remain_enabled', label: '@全体次数查询' },
  { key: 'ignore_requests_enabled', label: '被忽略请求查看' },
  { key: 'group_msg_history_enabled', label: '群消息历史' },
  { key: 'group_portrait_enabled', label: '群头像设置' },
  { key: 'group_sign_enabled', label: '群打卡' },
];

async function loadSettings() {
  const res = await safeGet(`${PREFIX}/config`);
  if (res.status !== 'success') { toast('加载设置失败', 'error'); return; }
  CONFIG = { ...CONFIG, ...res.data };
  renderToggles('settingsCore', CORE_TOGGLES);
  renderToggles('settingsAudit', AUDIT_TOGGLES);
  renderToggles('settingsLexicon', LEXICON_TOGGLES);
  renderToggles('settingsFeatures', FEATURE_TOGGLES);
  renderListSettings();
  renderExtraSettings();
}

function renderToggles(containerId, items) {
  const c = $(`#${containerId}`);
  c.innerHTML = items.map(item => {
    const checked = CONFIG[item.key] ? 'checked' : '';
    return `<div class="setting-item">
      <div class="setting-info">
        <div class="setting-label">${esc(item.label)}</div>
        ${item.desc ? `<div class="setting-desc">${esc(item.desc)}</div>` : ''}
      </div>
      <label class="toggle"><input type="checkbox" data-key="${item.key}" ${checked} /><span class="slider"></span></label>
    </div>`;
  }).join('');
}

function renderListSettings() {
  const c = $('#settingsLists');
  const whites = CONFIG._white_list || CONFIG.group_white_list || [];
  const blacks = CONFIG._black_list || CONFIG.group_black_list || [];
  const users = CONFIG._user_black_list || CONFIG.user_black_list || [];

  c.innerHTML = `
    <div class="list-section">
      <h3>群白名单</h3>
      <div class="list-add-row">
        <input type="text" class="input-sm" id="addWhiteInput" placeholder="输入群号..." />
        <button class="btn btn-primary btn-sm" id="btnAddWhite">添加</button>
      </div>
      <div class="list-items" id="whiteItems">${renderListTags(whites, 'white')}</div>
    </div>
    <div class="list-section">
      <h3>群黑名单</h3>
      <div class="list-add-row">
        <input type="text" class="input-sm" id="addBlackInput" placeholder="输入群号..." />
        <button class="btn btn-danger btn-sm" id="btnAddBlack">添加</button>
      </div>
      <div class="list-items" id="blackItems">${renderListTags(blacks, 'black')}</div>
    </div>
    <div class="list-section">
      <h3>用户黑名单</h3>
      <div class="list-add-row">
        <input type="text" class="input-sm" id="addUserInput" placeholder="输入QQ号..." />
        <button class="btn btn-warning btn-sm" id="btnAddUser" style="background:var(--warning);color:#fff">添加</button>
      </div>
      <div class="list-items" id="userItems">${renderListTags(users, 'user')}</div>
    </div>
  `;

  $('#btnAddWhite').addEventListener('click', async () => {
    const val = $('#addWhiteInput').value.trim();
    if (!val) return;
    await safePost(`${PREFIX}/whitelist/add`, { group_id: val });
    $('#addWhiteInput').value = '';
    toast('已添加到白名单', 'success');
    loadSettings();
  });
  $('#btnAddBlack').addEventListener('click', async () => {
    const val = $('#addBlackInput').value.trim();
    if (!val) return;
    await safePost(`${PREFIX}/blacklist/add`, { group_id: val });
    $('#addBlackInput').value = '';
    toast('已添加到黑名单', 'danger');
    loadSettings();
  });
  $('#btnAddUser').addEventListener('click', async () => {
    const val = $('#addUserInput').value.trim();
    if (!val) return;
    await safePost(`${PREFIX}/user_blacklist/add`, { user_id: val });
    $('#addUserInput').value = '';
    toast('已添加到用户黑名单', 'warning');
    loadSettings();
  });

  $$('.list-tag .btn-remove', c).forEach(btn => {
    btn.addEventListener('click', async () => {
      const type = btn.dataset.type;
      const val = btn.dataset.val;
      if (type === 'white') await safePost(`${PREFIX}/whitelist/remove`, { group_id: val });
      else if (type === 'black') await safePost(`${PREFIX}/blacklist/remove`, { group_id: val });
      else if (type === 'user') await safePost(`${PREFIX}/user_blacklist/remove`, { user_id: val });
      toast('已移除', 'success');
      loadSettings();
    });
  });
}

function renderListTags(list, type) {
  const cls = type === 'white' ? 'list-tag-white' : type === 'black' ? 'list-tag-black' : 'list-tag-user';
  if (!list.length) return '<span style="font-size:12px;color:#909399">暂无</span>';
  return list.map(v => `<span class="list-tag ${cls}">${esc(String(v))}<button class="btn-remove" data-type="${type}" data-val="${esc(String(v))}">&times;</button></span>`).join('');
}

function renderExtraSettings() {
  const c = $('#settingsExtra');
  if (!c) return;
  c.innerHTML = `
    <div class="setting-item">
      <div class="setting-info">
        <div class="setting-label">禁言时长（秒）</div>
        <div class="setting-desc">违规用户自动禁言时长，默认 1800 秒（30分钟）</div>
      </div>
      <input type="number" class="input-sm" id="inputBanDuration" value="${CONFIG.moderation_ban_duration ?? 1800}" min="60" max="2592000" style="width:100px" />
    </div>
    <div class="setting-item">
      <div class="setting-info">
        <div class="setting-label">LLM Provider ID</div>
        <div class="setting-desc">用于 AI 审核的 LLM 提供商 ID（留空使用默认）</div>
      </div>
      <input type="text" class="input-sm" id="inputProviderId" value="${esc(CONFIG.moderation_llm_provider_id || '')}" placeholder="留空使用默认" style="width:160px" />
    </div>
    <div class="setting-item">
      <div class="setting-info">
        <div class="setting-label">封禁通知模板</div>
        <div class="setting-desc">支持变量：{name} 用户名、{uid} QQ号、{group} 群号</div>
      </div>
      <input type="text" class="input-sm" id="inputBanNotice" value="${esc(CONFIG.ban_notice || '[群管] {name}({uid}) 的消息已被撤回（违规内容）')}" style="width:260px" />
    </div>
  `;
}

async function saveSettings() {
  const payload = {};
  $$('input[data-key]').forEach(inp => {
    payload[inp.dataset.key] = inp.checked;
  });
  const banDuration = $('#inputBanDuration');
  if (banDuration) payload.moderation_ban_duration = parseInt(banDuration.value, 10) || 1800;
  const providerId = $('#inputProviderId');
  if (providerId) payload.moderation_llm_provider_id = providerId.value.trim();
  const banNotice = $('#inputBanNotice');
  if (banNotice) payload.ban_notice = banNotice.value.trim();
  const res = await safePost(`${PREFIX}/config`, payload);
  if (res.status === 'success') {
    toast('设置已保存', 'success');
  } else {
    toast('保存失败: ' + (res.message || ''), 'error');
  }
}

// ============ INIT ============
function init() {
  initTabs();

  $('#btnRefreshAll').addEventListener('click', loadOverview);
  $('#btnRefreshTodayStats').addEventListener('click', loadOverview);
  $('#btnRefreshGroups').addEventListener('click', loadGroups);
  $('#btnRefreshUsers').addEventListener('click', loadUsers);
  $('#btnRefreshLogs').addEventListener('click', loadLogs);
  $('#btnSaveSettings').addEventListener('click', saveSettings);
  $('#btnModalClose').addEventListener('click', closeModal);
  $('#modalOverlay').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal(); });

  $('#btnBackGroups').addEventListener('click', () => {
    $('#memberPanel').style.display = 'none';
    $('#groupGrid').closest('.card').style.display = '';
    CURRENT_GROUP = null;
    CACHED_GROUP_ID = null;
    CURRENT_MEMBERS = [];
  });

  $('#searchGroupInput').addEventListener('input', () => {
    if (CONFIG._groups) renderGroups(CONFIG._groups);
  });
  $('#searchMemberInput').addEventListener('input', () => {
    if (CURRENT_MEMBERS.length) renderMembers(CURRENT_MEMBERS);
  });

  $('#btnAddAdmin').addEventListener('click', async () => {
    const val = $('#adminInput').value.trim();
    if (!val) return;
    await safePost(`${PREFIX}/admin/add`, { user_id: val });
    $('#adminInput').value = '';
    toast('已添加管理员', 'success');
    loadAdmins();
  });

  $('#btnBatchDelete').addEventListener('click', async () => {
    const ids = $$('.ck:checked').map(ck => ck.dataset.uid);
    if (!ids.length) { toast('请先勾选用户', 'error'); return; }
    showModal('批量删除', `<p>确认删除所有记录？<br>将删除 ${ids.length} 个用户的全部审核记录。</p>`,
      `<button class="btn btn-ghost btn-sm" id="btnCancelBatchDelete">取消</button>
       <button class="btn btn-danger btn-sm" id="btnConfirmBatchDelete">确认删除</button>`
    );
    $('#btnCancelBatchDelete').addEventListener('click', closeModal);
    $('#btnConfirmBatchDelete').addEventListener('click', async () => {
      const allRes = await safeGet(`${PREFIX}/logs/export`);
      if (allRes.status === 'success') {
        const delIds = (allRes.data || []).filter(l => ids.includes(String(l.user_id))).map(l => l.id).filter(id => id !== undefined && id !== null);
        if (delIds.length) await safePost(`${PREFIX}/logs/delete`, { ids: delIds });
      }
      closeModal();
      toast('批量删除完成', 'success');
      loadUsers();
    });
  });

  $('#btnExport').addEventListener('click', async () => {
    const res = await safeGet(`${PREFIX}/logs/export`);
    if (res.status !== 'success') { toast('导出失败', 'error'); return; }
    const data = res.data || [];
    if (!data.length) { toast('暂无数据可导出', 'error'); return; }
    const headers = Object.keys(data[0]);
    const csv = [headers.join(','), ...data.map(r => headers.map(h => `"${String(r[h] ?? '').replace(/"/g, '""')}"`).join(','))].join('\n');
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `group_guardian_logs_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click(); URL.revokeObjectURL(url);
    toast('导出成功', 'success');
  });

  $('#statusDot').style.background = 'var(--success)';
  $('#statusText').textContent = '已连接';

  loadOverview();
  loadAdmins();
}

ready(init);
