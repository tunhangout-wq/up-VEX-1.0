const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));
const number = value => Number(value || 0).toLocaleString('ar');
const date = value => value ? new Date(Number(value) * 1000).toLocaleString('ar', {dateStyle: 'medium', timeStyle: 'short'}) : '—';
const rankNames = {owner: 'المالك', dev: 'ديفيلوبر', founder: 'فاوندر', team: 'فريق'};
const groupNames = {server: 'السيرفر', channel: 'القنوات', member: 'الأعضاء', role: 'الرتب', security: 'الأمان', utility: 'الأدوات', ai: 'الذكاء الاصطناعي', career: 'المهن', loan: 'القروض'};
const commandPath = value => String(value || '').trim().replace(/^\/+/, '').replace(/[\/\s]+/g, '.').split('.').filter(Boolean).join('.');

const API = {
  token: localStorage.getItem('vixen_token'),
  me: null,
  async req(path, options = {}) {
    const headers = {...(options.headers || {}), Authorization: `Bearer ${this.token}`};
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    let response;
    try {
      response = await fetch(path, {...options, headers, signal: controller.signal});
    } catch (err) {
      if (err.name === 'AbortError') throw Error('انتهت مهلة الانتظار (30 ثانية) — تأكد أن البوت متصل وحاول مجددًا');
      throw Error('تعذر الاتصال بالسيرفر');
    } finally {
      clearTimeout(timer);
    }
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      localStorage.removeItem('vixen_token');
      this.token = null;
      $('#app')?.classList.add('hidden');
      $('#login')?.classList.remove('hidden');
    }
    if (!response.ok || data.ok === false) throw Error(data.error || 'تعذر إكمال الطلب');
    return data;
  },
  get(path) { return this.req(path); },
  post(path, body) { return this.req(path, {method: 'POST', body: JSON.stringify(body)}); },
  async streamPost(path, body, onEvent, externalSignal = null) {
    const headers = {Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json', Accept: 'text/event-stream'};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 240000);
    let relayAbort = null;
    try {
      if (externalSignal) {
        relayAbort = () => controller.abort();
        if (externalSignal.aborted) controller.abort();
        else externalSignal.addEventListener('abort', relayAbort, {once: true});
      }
      const response = await fetch(path, {method: 'POST', headers, body: JSON.stringify(body), signal: controller.signal});
      if (!response.ok) {
        let message = `HTTP ${response.status}`;
        try { const data = await response.json(); message = data.error || message; } catch (_) {}
        throw Error(message);
      }
      if (!response.body) throw Error('المتصفح لا يدعم Streaming لهذا الطلب');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || '';
        for (const frame of frames) {
          const line = frame.split(/\r?\n/).find(item => item.startsWith('data:'));
          if (!line) continue;
          try {
            const event = JSON.parse(line.slice(5).trim());
            onEvent(event);
          } catch (parseError) { /* malformed SSE frame: ignore only the frame */ }
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        const line = buffer.split(/\n/).find(item => item.startsWith('data:'));
        if (line) {
          try {
            const event = JSON.parse(line.slice(5).trim());
            onEvent(event);
          } catch (parseError) { /* malformed final frame */ }
        }
      }
    } finally {
      clearTimeout(timer);
      if (externalSignal && relayAbort) externalSignal.removeEventListener('abort', relayAbort);
    }
  }
};

let commands = [];
let members = [];
let memberAfter = '';
let memberTimer;
let currentPage = 'overview';
let dmGuilds = [];
let dmMembers = [];
let dmTemplates = [];
let currentDmTemplateId = '';
let dmMemberTimer;
let dmPreviewTimer;
let dmBulkRefreshTimer = null;
let dmBulkPreviewToken = null;
let dmBulkTemplates = [];
let dmBulkRoles = [];
let dmHistoryTimer = null;
let dmFavoriteOnly = false;
let terminalAbortController = null;
let aiChatTestAbortController = null;
let aiChatTestStartedAt = 0;
let aiChatTestTimer = null;
let healthRefreshTimer = null;
let overviewRefreshTimer = null;
let aiStatusInFlight = null;
let aiStatusSequence = 0;

function debounce(fn, wait = 250) { let timer = null; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); }; }

function toast(message, kind = 'success') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.innerHTML = `<span>${kind === 'success' ? '✓' : '!'}</span><b>${esc(message)}</b>`;
  $('#toastStack').appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

function setResult(selector, message, kind = 'success') {
  const node = $(selector);
  if (!node) return;
  node.className = `inline-result ${kind}`;
  node.textContent = message;
}

function page(id) {
  const section = document.getElementById(id);
  if (!section || !section.classList.contains('page')) {
    console.error(`[dashboard] page not found: ${id}`);
    toast(`القسم المطلوب غير موجود: ${id}`, 'error');
    return;
  }
  currentPage = id;
  $$('.page').forEach(section => section.classList.toggle('hidden', section.id !== id));
  $$('[data-page]').forEach(button => {
    const active = button.dataset.page === id;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  const titles = {
    overview: 'نظرة عامة', commands: 'مركز الأوامر', members: 'الأعضاء والرسائل',
    security: 'الأمان والإشراف', economy: 'الاقتصاد', staff: 'الفريق والصلاحيات',
    atria: 'AI Moderation', 'ai-chat': 'AI Chat', voice: 'Voice Control', 'dm-center': 'DM Center', 'dm-all': 'DM All', 'bot-profile': 'Bot Profile', terminal: 'Live Terminal', logs: 'سجل التدقيق', settings: 'الإعدادات'
  };
  $('#title').textContent = titles[id] || id;
  $('#app').classList.remove('menu-open');
  if (id !== 'terminal') stopTerminal();
  if (id === 'overview') stats();
  if (id === 'commands') renderCommands();
  if (id === 'members') loadMembers(true);
  if (id === 'economy') loadMarket();
  if (id === 'staff') { loadStaff(); loadStaffWarnings(); }
  if (id === 'atria') loadAtria();
  if (id === 'ai-chat') loadAIChat();
  if (id === 'voice') loadVoice();
  if (id === 'dm-center') loadDMCenter();
  if (id === 'dm-all') { loadDMAll(); startDMAllPolling(); } else stopDMAllPolling();
  if (id === 'bot-profile') loadBotProfile();
  if (id === 'terminal') startTerminal();
  if (id === 'logs') loadLogs();
  if (id === 'settings') loadSettings();
}

function startDashboardRefreshers() {
  clearInterval(healthRefreshTimer);
  clearInterval(overviewRefreshTimer);
  healthRefreshTimer = setInterval(() => {
    if (currentPage === 'ai-chat') loadAIStatus();
  }, 15000);
  overviewRefreshTimer = setInterval(() => {
    if (currentPage === 'overview') stats();
  }, 20000);
}

function applySession(session) {
  API.me = session;
  $('#operatorName').textContent = session.name || (session.owner ? 'المالك' : 'Dashboard operator');
  $('#operatorRole').textContent = session.rank_name || rankNames[session.rank] || session.rank || 'Staff';
  $('#sessionRank').textContent = session.rank_name || rankNames[session.rank] || 'جلسة الإدارة';
  $('#welcomeName').textContent = session.name || 'operator';
  $('#operatorAvatar').textContent = (session.name || 'V').trim().charAt(0).toUpperCase();
}

async function boot() {
  if (!API.token) return;
  try {
    const session = await API.get('/api/me');
    applySession(session);
    $('#login').classList.add('hidden');
    $('#app').classList.remove('hidden');
    await loadCommands();
    startDashboardRefreshers();
    page('overview');
  } catch (_) {
    localStorage.removeItem('vixen_token');
    API.token = null;
  }
}

function setLoginMode(mode) {
  const owner = mode === 'owner';
  $('#ownerTab').classList.toggle('active', owner);
  $('#codeTab').classList.toggle('active', !owner);
  $('#ownerFields').classList.toggle('hidden', !owner);
  $('#codeFields').classList.toggle('hidden', owner);
  $('#loginErr').textContent = '';
}

$('#ownerTab').onclick = () => setLoginMode('owner');
$('#codeTab').onclick = () => setLoginMode('code');
$('#loginForm').onsubmit = async event => {
  event.preventDefault();
  const codeMode = $('#codeFields').classList.contains('hidden') === false;
  const payload = codeMode
    ? {method: 'code', code: $('#staffCode').value.trim()}
    : {method: 'owner', owner_id: $('#owner').value.trim(), password: $('#pass').value};
  try {
    const session = await API.post('/api/login', payload);
    API.token = session.token;
    localStorage.setItem('vixen_token', session.token);
    applySession(session);
    $('#login').classList.add('hidden');
    $('#app').classList.remove('hidden');
    await loadCommands();
    startDashboardRefreshers();
    page('overview');
  } catch (error) {
    $('#loginErr').textContent = error.message;
  }
};
$('#logout').onclick = () => {
  localStorage.removeItem('vixen_token');
  API.token = null;
  location.reload();
};
$('#mobileOpen').onclick = () => $('#app').classList.add('menu-open');
$('#mobileClose').onclick = () => $('#app').classList.remove('menu-open');

// Sidebar collapse (icon-rail) mode: persisted, desktop-only, with tooltips for the hidden labels.
$$('.main-nav button[data-page]').forEach(button => {
  const label = $('.nav-text', button)?.textContent.trim();
  if (label) button.dataset.tooltip = label;
});
const collapseBtn = $('#sidebarCollapse');
if (collapseBtn) {
  if (localStorage.getItem('vixen_sidebar_collapsed') === '1') {
    $('#app').classList.add('sidebar-collapsed');
  }
  collapseBtn.onclick = () => {
    const collapsed = $('#app').classList.toggle('sidebar-collapsed');
    localStorage.setItem('vixen_sidebar_collapsed', collapsed ? '1' : '0');
  };
}

document.addEventListener('click', event => {
  const pageButton = event.target.closest('[data-page]');
  if (pageButton) {
    event.preventDefault();
    page(pageButton.dataset.page);
    return;
  }
  const commandButton = event.target.closest('[data-command]');
  if (commandButton) {
    event.preventDefault();
    openCommand(commandButton.dataset.command);
    return;
  }
  const dmButton = event.target.closest('[data-dm-member]');
  if (dmButton) {
    $('#dmId').value = dmButton.dataset.dmMember;
    $('#dmText').focus();
  }
  const revokeButton = event.target.closest('[data-revoke-code]');
  if (revokeButton) revokeCode(revokeButton.dataset.revokeCode);
  const removeButton = event.target.closest('[data-remove-staff]');
  if (removeButton) removeStaff(removeButton.dataset.removeStaff);
});

async function stats() {
  try {
    const data = await API.get('/api/stats');
    $('#sUsers').textContent = number(data.users);
    $('#sMoney').textContent = number(data.total_money);
    $('#sLoans').textContent = number(data.active_loans);
    const latency = Number(data.bot?.latency_ms);
    $('#sPing').textContent = Number.isFinite(latency) && latency > 0 ? `${number(latency)}ms` : '—';
    $('#currencyLabel').textContent = `${data.currency_symbol || ''} إجمالي المحفظة والبنك`;
    const connected = Boolean(data.bot?.connected);
    $('#botLabel').textContent = connected ? (data.bot?.name || 'البوت متصل') : 'بانتظار اتصال Discord';
    $('#serverName').textContent = connected ? (data.bot?.name || 'Vixen Discord') : 'وضع الإعداد';
    $('#statusText').textContent = connected ? 'Discord متصل' : 'Dashboard يعمل';
    $('#healthApi').textContent = 'يعمل';
    $('#statusText').previousElementSibling?.classList.toggle('offline', !connected);
    $('#healthGateway').textContent = connected ? 'متصل' : 'بانتظار البوت';
    const gatewayDot = $('#healthGateway').previousElementSibling?.querySelector('.health-dot');
    gatewayDot?.classList.toggle('good', connected);
    gatewayDot?.classList.toggle('offline', !connected);
    const apiDot = $('#healthApi').previousElementSibling?.querySelector('.health-dot');
    apiDot?.classList.add('good');
    apiDot?.classList.remove('offline');
  } catch (error) {
    $('#statusText').textContent = 'تعذر تحديث البيانات';
    $('#healthApi').textContent = 'تعذر الاتصال';
    const apiDot = $('#healthApi').previousElementSibling?.querySelector('.health-dot');
    apiDot?.classList.remove('good');
    apiDot?.classList.add('offline');
  }
}

async function loadCommands() {
  try {
    $('#registryState').textContent = 'جارٍ التحديث…';
    const data = await API.get('/api/commands');
    commands = data.commands || [];
    $('#cmdCount').textContent = commands.length;
    $('#healthCommands').textContent = `${commands.length} أمر`;
    const groups = [...new Set(commands.map(command => command.name.split('.')[0]))].sort();
    $('#commandGroupCount').textContent = groups.length;
    $('#commandRequiredCount').textContent = commands.reduce((total, command) => total + (command.args || []).filter(arg => arg.required).length, 0);
    $('#registryState').textContent = 'متصل';
    $('#cmdGroup').innerHTML = `<option value="">كل المجموعات</option>${groups.map(group => `<option value="${esc(group)}">${esc(groupNames[group] || group)} · ${esc(group)}</option>`).join('')}`;
    renderCommands();
    syncCommandButtons();
  } catch (error) {
    $('#registryState').textContent = 'تعذر الاتصال';
    toast(error.message, 'error');
  }
}

function syncCommandButtons() {
  $$('[data-command]').forEach(button => {
    const wanted = commandPath(button.dataset.command);
    const available = commands.some(command => commandPath(command.name) === wanted);
    button.disabled = !available;
    button.classList.toggle('command-unavailable', !available);
    if (!available) button.title = 'هذا الأمر غير محمّل في البوت الحالي';
  });
}

function renderCommands() {
  const query = ($('#cmdSearch')?.value || '').trim().toLowerCase();
  const group = $('#cmdGroup')?.value || '';
  const sort = $('#cmdSort')?.value || 'name';
  let visible = commands.filter(command => {
    const haystack = `${command.name} ${command.description} ${(command.aliases || []).join(' ')}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!group || command.name.startsWith(`${group}.`) || command.name === group);
  });
  if (sort === 'group') visible.sort((a, b) => a.name.split('.')[0].localeCompare(b.name.split('.')[0]) || a.name.localeCompare(b.name));
  else visible.sort((a, b) => a.name.localeCompare(b.name));
  $('#visibleCommandCount').textContent = visible.length;
  $('#commandsGrid').innerHTML = visible.map(command => {
    const groupName = command.name.split('.')[0];
    const requiredCount = (command.args || []).filter(arg => arg.required).length;
    const args = command.args?.length
      ? command.args.map(arg => `<span class="${arg.required ? 'required' : ''}">${esc(arg.name)}${arg.required ? ' *' : ''}</span>`).join('')
      : '<span>بدون معاملات</span>';
    const aliases = (command.aliases || []).length ? `<small class="aliases">بدائل: ${esc(command.aliases.join('، '))}</small>` : '';
    return `<article class="command-card"><div class="command-meta"><span class="command-group">${esc(groupNames[groupName] || groupName)}</span><span class="command-index">/${esc(command.name)}</span></div><h3>/${esc(command.name.replace('.', ' / '))}</h3><p>${esc(command.description || 'أمر إداري من البوت')}</p><div class="command-card-foot"><span>${requiredCount ? `${requiredCount} معامل مطلوب` : 'جاهز للتنفيذ'}</span><span>${esc(command.cog || 'Vixen')}</span></div><div class="command-args">${args}</div>${aliases}<button class="secondary wide command-run" data-command="${esc(command.name)}">فتح نموذج التنفيذ <span>←</span></button></article>`;
  }).join('') || '<div class="empty-state panel"><span>⌕</span><h3>لا توجد أوامر مطابقة</h3><p>جرّب البحث باسم مختلف أو اختر مجموعة أخرى.</p></div>';
  syncCommandButtons();
}
$('#cmdSearch').oninput = renderCommands;
$('#cmdGroup').onchange = renderCommands;
$('#cmdSort').onchange = renderCommands;
$('#refreshCommands').onclick = loadCommands;

function inputForArg(arg, preset) {
  const value = preset ?? (arg.default ?? '');
  const type = `${arg.type || ''} ${arg.discord_type || ''}`.toLowerCase();
  const label = `${arg.name}${arg.required ? ' *' : ''}`;
  if ((arg.choices || []).length) {
    return `<label>${esc(label)}<select data-arg="${esc(arg.name)}"><option value="">اختر...</option>${arg.choices.map(choice => `<option value="${esc(choice.value)}" ${String(choice.value) === String(value) ? 'selected' : ''}>${esc(choice.name)}${String(choice.value) !== String(choice.name) ? ` — ${esc(choice.value)}` : ''}</option>`).join('')}</select></label>`;
  }
  if (type.includes('bool')) {
    return `<label>${esc(label)}<select data-arg="${esc(arg.name)}"><option value="false" ${String(value) === 'false' ? 'selected' : ''}>لا</option><option value="true" ${String(value) === 'true' ? 'selected' : ''}>نعم</option></select></label>`;
  }
  const numeric = type.includes('int') || type.includes('float');
  const objectHint = type.includes('member') || type.includes('user') || type.includes('role') || type.includes('channel')
    ? '<small class="field-hint">أدخل Discord ID — يمكن نسخه من السيرفر.</small>' : '';
  const longText = ['message', 'prompt', 'text', 'description', 'topic', 'reason'].includes(arg.name);
  const control = longText
    ? `<textarea data-arg="${esc(arg.name)}" placeholder="${esc(arg.type || 'value')}">${esc(value)}</textarea>`
    : `<input data-arg="${esc(arg.name)}" ${numeric ? `type="${type.includes('float') ? 'number' : 'number'}" ${type.includes('float') ? 'step="any"' : 'step="1"'}` : ''} value="${esc(value)}" placeholder="${esc(arg.type || arg.discord_type || 'value')}">`;
  return `<label>${esc(label)}${control}${objectHint}</label>`;
}

async function openCommand(name, presets = {}) {
  const wanted = commandPath(name);
  let command = commands.find(item => commandPath(item.name) === wanted);
  if (!command) {
    await loadCommands();
    command = commands.find(item => commandPath(item.name) === wanted);
    if (!command) {
      toast('هذا الزر قديم أو الأمر غير محمّل حاليًا — حدّث الكتالوج أولًا.', 'error');
      return;
    }
  }
  name = command.name;
  $('#modalTitle').textContent = `/${name.replace('.', ' / ')}`;
  $('#modalDescription').textContent = command.description || 'سيتم استدعاء الأمر الحقيقي بصلاحيات جلسة الإدارة.';
  $('#modalFields').innerHTML = command.args?.length
    ? command.args.map(arg => inputForArg(arg, presets[arg.name])).join('')
    : '<div class="empty-fields">هذا الأمر لا يحتاج معاملات.</div>';
  $('#modalResult').textContent = '';
  $('#modal').classList.remove('hidden');
  $('#modalRun').onclick = async () => {
    const args = {};
    $$('[data-arg]', $('#modalFields')).forEach(field => {
      if (field.value !== '') args[field.dataset.arg] = field.value;
    });
    const missing = (command.args || []).find(arg => arg.required && !args[arg.name]);
    if (missing) {
      $('#modalResult').className = 'modal-result error';
      $('#modalResult').textContent = `المعامل ${missing.name} مطلوب`;
      return;
    }
    const button = $('#modalRun');
    button.disabled = true;
    button.innerHTML = 'جاري التنفيذ…';
    try {
      const data = await API.post('/api/commands/execute', {command: name, args});
      $('#modalResult').className = 'modal-result success';
      $('#modalResult').textContent = data.message || 'تم التنفيذ بنجاح.';
      toast('تم تنفيذ الأمر بنجاح');
      stats();
    } catch (error) {
      $('#modalResult').className = 'modal-result error';
      $('#modalResult').textContent = error.message;
    } finally {
      button.disabled = false;
      button.innerHTML = 'تنفيذ الأمر <span>←</span>';
    }
  };
}

function closeModal() { $('#modal').classList.add('hidden'); }
$('#modalClose').onclick = closeModal;
$('#modalCancel').onclick = closeModal;
$('#modal').onclick = event => { if (event.target.id === 'modal') closeModal(); };
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeModal(); });

async function loadMembers(reset = false) {
  if (reset) { members = []; memberAfter = ''; }
  try {
    const query = encodeURIComponent($('#memberSearch').value || '');
    const data = await API.get(`/api/members?limit=50&after=${memberAfter || 0}&q=${query}`);
    members.push(...(data.members || []));
    memberAfter = data.next || '';
    $('#loadMore').disabled = !memberAfter;
    $('#membersList').innerHTML = members.map(member => `<div class="member-row"><div class="member-main"><span class="avatar small">${esc((member.name || '?').charAt(0))}</span><div><b>${esc(member.name)}</b><small>${esc(member.username)} · ${esc(member.id)}</small></div></div><button class="ghost-button" data-dm-member="${esc(member.id)}">رسالة</button></div>`).join('') || '<div class="empty-state"><span>♙</span><p>لا يوجد أعضاء مطابقون.</p></div>';
  } catch (error) {
    $('#membersList').innerHTML = `<div class="empty-state error-text">${esc(error.message)}</div>`;
  }
}
$('#memberSearch').oninput = () => {
  clearTimeout(memberTimer);
  memberTimer = setTimeout(() => loadMembers(true), 260);
};
$('#loadMore').onclick = () => loadMembers(false);
$('#sendDm').onclick = async () => {
  const id = $('#dmId').value.trim();
  const message = $('#dmText').value.trim();
  if (!id || !message) return setResult('#dmResult', 'أدخل ID ورسالة قبل الإرسال.', 'error');
  try {
    const data = await API.post('/api/commands/execute', {command: 'member.dm', args: {member: id, message}});
    setResult('#dmResult', data.message || 'تم إرسال الرسالة.');
    toast('تم إرسال الرسالة');
    $('#dmText').value = '';
  } catch (error) { setResult('#dmResult', error.message, 'error'); }
};

async function loadMarket() {
  try {
    const data = await API.get('/api/market');
    $('#marketList').innerHTML = (data.stocks || []).map(stock => `<div class="market-row"><span class="market-symbol">${esc(stock.emoji)}<b>${esc(stock.symbol)}</b><small>${esc(stock.name)}</small></span><span><b>${number(stock.price)}</b><small class="${stock.change >= 0 ? 'positive' : 'negative'}">${stock.change >= 0 ? '+' : ''}${stock.change}%</small></span></div>`).join('') || '<div class="empty-state">لا توجد أسهم مضبوطة.</div>';
    $('#loansList').innerHTML = (data.loans || []).map(loan => `<tr><td>#${esc(loan.id)}</td><td>${esc(loan.borrower)}</td><td>${number(loan.total)}</td><td>${number(loan.paid)}</td><td><span class="status-tag ${esc(loan.status)}">${esc(loan.status)}</span></td><td>${date(loan.due)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty-cell">لا توجد قروض.</td></tr>';
  } catch (error) { toast(error.message, 'error'); }
}
$('#refreshMarket').onclick = loadMarket;
$('#moneySubmit').onclick = async () => {
  const user_id = $('#moneyUser').value.trim();
  const action = $('#moneyAction').value;
  const amount = Number($('#moneyAmount').value || 0);
  if (!/^\d+$/.test(user_id)) return setResult('#moneyResult', 'أدخل Discord ID صحيحًا.', 'error');
  if (action !== 'reset' && amount <= 0) return setResult('#moneyResult', 'المبلغ يجب أن يكون أكبر من صفر.', 'error');
  try {
    const data = await API.post('/api/money', {user_id, action, mode: $('#moneyMode').value, amount});
    setResult('#moneyResult', `تم التحديث — المحفظة: ${number(data.wallet)} · البنك: ${number(data.bank)}`);
    toast('تم تحديث رصيد العضو');
    stats();
  } catch (error) { setResult('#moneyResult', error.message, 'error'); }
};

async function loadStaff() {
  try {
    const data = await API.get('/api/staff');
    const staffMembers = data.staff || [];
    $('#staffList').innerHTML = staffMembers.length ? staffMembers.map(staff => {
      const id = String(staff.id || '');
      return `<div class="staff-row"><span class="avatar small">${esc((staff.name || id || '?').charAt(0))}</span><div><b>${esc(staff.name || `عضو ${id.slice(-4)}`)}</b><small>${esc(id)} · ${esc(rankNames[staff.rank] || staff.rank)}</small></div><button class="ghost-button danger-text" data-remove-staff="${esc(id)}">إزالة</button></div>`;
    }).join('') : '<div class="empty-state"><span>◇</span><p>لا يوجد أعضاء فريق مسجلون.</p></div>';
    $('#codesList').innerHTML = (data.codes || []).map(code => `<div class="code-row"><div><code>${esc(code.code)}</code><small>${esc(code.rank_name)} · ${code.used_by ? `مستخدم بواسطة ${esc(code.used_by_name || code.used_by)}` : 'متاح'}${code.revoked ? ' · مسحوب' : ''}</small></div>${!code.revoked && !code.used_by ? `<button class="ghost-button danger-text" data-revoke-code="${esc(code.code)}">سحب</button>` : ''}</div>`).join('') || '<div class="empty-state">لا توجد أكواد.</div>';
  } catch (error) { toast(error.message, 'error'); }
}
$('#refreshStaff').onclick = loadStaff;
let staffWarningTimer;
$('#staffWarningSearch').oninput = () => {
  clearTimeout(staffWarningTimer);
  staffWarningTimer = setTimeout(loadStaffWarnings, 250);
};
$('#refreshStaffWarnings').onclick = loadStaffWarnings;
async function loadStaffWarnings() {
  try {
    const query = encodeURIComponent($('#staffWarningSearch').value || '');
    const data = await API.get(`/api/moderation/warnings?limit=100&q=${query}`);
    $('#staffWarnings').innerHTML = (data.warnings || []).map(warning => `<tr><td>${esc(warning.member_name)} · ${esc(warning.user_id)}</td><td>${esc(warning.moderator_name)} · ${esc(warning.moderator_id)}</td><td>${esc(warning.reason)}</td><td>${date(warning.timestamp)}</td></tr>`).join('') || '<tr><td colspan="4" class="empty-cell">لا توجد تحذيرات محفوظة.</td></tr>';
  } catch (error) {
    $('#staffWarnings').innerHTML = `<tr><td colspan="4" class="empty-cell error-text">${esc(error.message)}</td></tr>`;
  }
}
$('#generateCode').onclick = async () => {
  try {
    const data = await API.post('/api/staff/code', {rank: $('#codeRank').value, count: Number($('#codeCount').value || 1), note: $('#codeNote').value.trim()});
    $('#generatedCodes').innerHTML = (data.codes || []).map(code => `<code>${esc(code)}</code>`).join('');
    toast('تم توليد كود التفعيل');
    loadStaff();
  } catch (error) { setResult('#generatedCodes', error.message, 'error'); }
};
async function revokeCode(code) {
  if (!confirm(`سحب الكود ${code}؟`)) return;
  try { await API.post('/api/staff/revoke', {code}); toast('تم سحب الكود'); loadStaff(); }
  catch (error) { toast(error.message, 'error'); }
}
async function removeStaff(user_id) {
  if (!confirm('إزالة رتبة هذا العضو؟')) return;
  try { await API.post('/api/staff/remove', {user_id}); toast('تمت إزالة العضو من الفريق'); loadStaff(); }
  catch (error) { toast(error.message, 'error'); }
}

async function loadLogs() {
  try {
    const data = await API.get('/api/logs');
    $('#logsOut').innerHTML = (data.logs || []).map(log => `<div class="log-row"><span class="log-icon">${log.source === 'dashboard' ? '⌘' : '◈'}</span><div><b>${esc(log.action)}</b><p>${esc(log.details)}</p><small>${esc(log.actor_name || log.actor_id)} · ${date(log.ts)}</small></div><span class="log-source">${esc(log.source || 'discord')}</span></div>`).join('') || '<div class="empty-state"><span>≡</span><p>لا توجد عمليات مسجلة بعد.</p></div>';
  } catch (error) { $('#logsOut').innerHTML = `<div class="empty-state error-text">${esc(error.message)}</div>`; }
}
$('#refreshLogs').onclick = loadLogs;

async function loadAIModerationLogs() {
  try {
    const data = await API.get('/api/ai/moderation/logs?limit=100');
    $('#aiModLogs').innerHTML = (data.logs || []).map(log => `<tr><td>${date(log.timestamp)}</td><td>${esc(log.user_id)}</td><td>${esc(log.classification)}${log.confidence == null ? '' : ` · ${Math.round(Number(log.confidence) * 100)}%`}</td><td>${esc(log.action_requested || '—')}</td><td>${esc(log.result)}</td><td>${esc(log.reason)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty-cell">لا توجد قرارات محفوظة.</td></tr>';
  } catch (error) {
    $('#aiModLogs').innerHTML = `<tr><td colspan="6" class="empty-cell error-text">${esc(error.message)}</td></tr>`;
  }
}
$('#refreshAiLogs').onclick = loadAIModerationLogs;

async function loadSettings() {
  try {
    const data = await API.get('/api/settings');
    $('#settingsEditor').value = JSON.stringify(data.settings || {}, null, 2);
  } catch (error) { setResult('#settingsResult', error.message, 'error'); }
}
$('#saveSettings').onclick = async () => {
  try {
    const settings = JSON.parse($('#settingsEditor').value);
    const data = await API.post('/api/settings', {settings});
    if (data.runtime_synced === false) {
      setResult('#settingsResult', 'تم الحفظ لكن AI Runtime لم يتزامن لأن Atria Cog غير محمّل.', 'error');
      toast('تم الحفظ دون مزامنة AI Runtime', 'error');
    } else {
      setResult('#settingsResult', 'تم حفظ الإعدادات وتطبيق AI Runtime فورًا.');
      toast('تم حفظ وتطبيق الإعدادات');
    }
    await loadAIStatus();
  } catch (error) { setResult('#settingsResult', error.message.includes('JSON') ? 'صيغة JSON غير صالحة.' : error.message, 'error'); }
};

async function loadAtria() {
  try {
    const data = await API.get('/api/ai/moderation/settings');
    const moderation = data.config || {};
    const legacy = data.legacy || {};
    const channels = (data.guilds || []).flatMap(guild => (guild.channels || []).map(channel =>
      `<option value="${esc(channel.id)}">${esc(guild.name)} · #${esc(channel.name)}</option>`
    )).join('');
    $('#aiModChannels').innerHTML = channels || '<option disabled>لا توجد قنوات نصية متاحة</option>';
    const allowedChannels = new Set((moderation.channels || []).map(String));
    $$('option', $('#aiModChannels')).forEach(option => { option.selected = allowedChannels.has(option.value); });
    const detections = new Set((moderation.detection_types || ['spam', 'flood', 'repeated_messages', 'toxicity', 'harassment']).map(String));
    $$('option', $('#aiModDetections')).forEach(option => { option.selected = detections.has(option.value); });
    const actions = new Set((moderation.automatic_actions || []).map(String));
    $('#aiModEnabled').checked = Boolean(moderation.enabled ?? legacy.moderation_enabled) && !moderation.kill_switch;
    $('#aiModSensitivity').value = moderation.sensitivity || 'medium';
    $('#aiModMaxActions').value = moderation.max_actions_per_hour || 20;
    $('#aiTimeout').value = moderation.timeout_minutes || legacy.timeout_minutes || 10;
    $('#aiFastPath').checked = moderation.fast_path_enabled !== false;
    $('#aiModAITimeout').value = moderation.ai_timeout_seconds ?? 6;
    $('#aiModWindow').value = moderation.window_seconds ?? 8;
    $('#aiRepeatThreshold').value = moderation.repeat_threshold ?? 5;
    $('#aiFloodThreshold').value = moderation.flood_threshold ?? 8;
    $('#aiMentionThreshold').value = moderation.mention_threshold ?? 8;
    $('#aiModLogChannel').value = moderation.mod_log_channel || moderation.mod_log_channel_id || '';
    $('#aiWhitelistUsers').value = (moderation.whitelist_users || []).join(',');
    $('#aiWhitelistRoles').value = (moderation.whitelist_roles || []).join(',');
    $('#aiDeleteOffending').checked = moderation.delete_offending_messages !== false;
    $('#aiAllowInvites').checked = moderation.allow_server_invites === true;
    $('#aiRaidWindow').value = moderation.raid_window_seconds ?? 20;
    $('#aiRaidThreshold').value = moderation.raid_join_threshold ?? 8;
    $('#aiActionWarn').checked = actions.has('warn');
    $('#aiActionTimeout').checked = actions.has('timeout');
    $('#aiActionBan').checked = actions.has('ban');
    $('#aiAllowBan').checked = moderation.allow_ai_ban === true;
    await Promise.all([loadAIStatus(), loadAIModerationLogs()]);
  } catch (error) { setResult('#aiSaveOut', error.message, 'error'); }
}
async function loadAIStatus() {
  if (aiStatusInFlight) return aiStatusInFlight;
  const sequence = ++aiStatusSequence;
  aiStatusInFlight = (async () => {
  try {
    const data = await API.get('/api/ai/status');
    if (sequence !== aiStatusSequence) return;
    const provider = data.provider || {};
    const providerStatus = String(provider.status || 'Unknown');
    const hasProvider = Boolean(provider.configured || provider.enabled || providerStatus !== 'Not Configured');
    const degraded = providerStatus === 'Recovering';
    const uiStatus = degraded ? 'RECOVERING' : (providerStatus === 'Connected' ? 'ONLINE' : providerStatus.toUpperCase());
    $('#aiProviderStatus').textContent = `${provider.provider || 'AI'} · ${uiStatus}`;
    const pill = document.getElementById('aiProviderPill');
    if (pill) {
      pill.dataset.status = degraded ? 'recovering' : (hasProvider && providerStatus !== 'Error' ? 'ok' : 'off');
      pill.setAttribute('aria-label', `AI provider status: ${uiStatus}`);
    }
    const pulse = document.getElementById('aiDashboardPulse');
    if (pulse) {
      pulse.textContent = degraded ? 'RECOVERING' : (providerStatus === 'Connected' ? 'LIVE' : 'OFFLINE');
      pulse.dataset.status = degraded ? 'recovering' : (providerStatus === 'Connected' ? 'ok' : 'off');
    }
    const moderationMetrics = data.runtime?.live_policy?.metrics || data.runtime?.metrics || {};
    $('#aiModAITimeouts') && ($('#aiModAITimeouts').textContent = number(moderationMetrics.moderation_ai_timeouts || 0));
    $('#aiModAIInvalid') && ($('#aiModAIInvalid').textContent = number(moderationMetrics.moderation_ai_invalid || 0));
    $('#aiModActions') && ($('#aiModActions').textContent = number(moderationMetrics.moderation_actions || 0));
    const runtime = data.runtime || {};
    const live = runtime.live_policy || {};
    const runtimeDetails = [
      runtime.message_content_intent_requested ? 'Message Content ✓' : 'Message Content ✗',
      runtime.atria_cog_loaded ? 'Atria Cog ✓' : 'Atria Cog ✗',
      `Chat channels: ${runtime.chat_allowed_channels || 0}`,
      `Moderation channels: ${runtime.moderation_channels || 0}`,
      live.chat_enabled ? 'Live Chat ✓' : 'Live Chat off',
      live.moderation_enabled && !live.moderation_killed ? 'Live Moderation ✓' : 'Live Moderation off',
      live.revision ? `Policy rev ${live.revision}` : null,
    ];
    const details = [provider.model, provider.latency_ms == null ? null : `${provider.latency_ms} ms`, provider.last_request, provider.error, ...runtimeDetails]
      .filter(Boolean).join(' · ');
    $('#aiProviderDetails').textContent = details || 'لم يُجر اتصال بالمزود بعد.';
    $('#aiProviderDetails').classList.toggle('error', provider.status === 'Error' || provider.status === 'Not Configured');
    const metrics = runtime.chat_metrics || {};
    $('#aiChatProviderState') && ($('#aiChatProviderState').textContent = provider.status || 'Unknown');
    $('#aiChatProviderModel') && ($('#aiChatProviderModel').textContent = provider.model || '—');
    $('#aiChatLatency') && ($('#aiChatLatency').textContent = provider.total_latency_ms == null ? (provider.latency_ms == null ? '—' : `${number(provider.latency_ms)} ms`) : `${number(provider.total_latency_ms)} ms`);
    $('#aiChatFirstToken') && ($('#aiChatFirstToken').textContent = provider.first_token_latency_ms == null ? 'TTFT —' : `TTFT ${number(provider.first_token_latency_ms)} ms`);
    $('#aiChatLastRequest') && ($('#aiChatLastRequest').textContent = provider.last_request ? `آخر طلب: ${date(Date.parse(provider.last_request) / 1000)}` : 'آخر طلب: —');
    $('#aiChatRequests') && ($('#aiChatRequests').textContent = number(metrics.requests || 0));
    $('#aiChatCompleted') && ($('#aiChatCompleted').textContent = number(metrics.completed || 0));
    $('#aiChatFailed') && ($('#aiChatFailed').textContent = number(metrics.failed || 0));
    const recovery = provider.recovery?.type || 'ready';
    const providerMetrics = provider.metrics || {};
    $('#aiChatRecovery') && ($('#aiChatRecovery').textContent = String(recovery).replaceAll('_', ' ').toUpperCase());
    $('#aiChatProviderRecoveries') && ($('#aiChatProviderRecoveries').textContent = number(providerMetrics.rate_limit_recoveries || 0));
    $('#aiChatProviderTransient') && ($('#aiChatProviderTransient').textContent = number(providerMetrics.transient_recoveries || 0));
    $('#aiChatProviderContinuations') && ($('#aiChatProviderContinuations').textContent = number(providerMetrics.continuations || 0));
    const microBypass = Number(metrics.micro_social_provider_bypass || 0);
    const recoveryEl = $('#aiChatRecovery');
    if (recoveryEl && microBypass) recoveryEl.title = `Micro-social provider bypasses: ${microBypass}`;
    $('#aiChatActiveRequests') && ($('#aiChatActiveRequests').textContent = number(provider.active_requests || 0));
    $('#aiChatQueueWait') && ($('#aiChatQueueWait').textContent = `${number(providerMetrics.queue_wait_ms || 0)} ms`);
    $('#aiChatCircuit') && ($('#aiChatCircuit').textContent = provider.circuit_open ? 'OPEN' : 'CLOSED');
    const qualityScore = Number(metrics.quality_score_avg ?? providerMetrics.quality_score_avg ?? 1);
    $('#aiChatQualityScore') && ($('#aiChatQualityScore').textContent = `${Math.round(Math.max(0, Math.min(1, qualityScore)) * 100)}%`);
    $('#aiChatQualityEvals') && ($('#aiChatQualityEvals').textContent = number(metrics.quality_evals || 0));
    $('#aiChatTestRequest') && ($('#aiChatTestRequest').textContent = provider.request_id || '—');
  } catch (error) {
    $('#aiProviderStatus').textContent = 'Unavailable';
    setResult('#aiProviderDetails', error.message, 'error');
  } finally {
    aiStatusInFlight = null;
  }
  })();
  return aiStatusInFlight;
}
async function runAtria(mode) {
  const prompt = $('#aiPrompt').value.trim();
  if (!prompt) return setResult('#aiOut', 'اكتب طلبًا أولًا.', 'error');
  $('#aiOut').textContent = 'جاري التحليل…';
  try { const data = await API.post('/api/atria', {prompt, mode}); $('#aiOut').textContent = data.answer || 'لم تصل نتيجة.'; }
  catch (error) { $('#aiOut').textContent = `❌ ${error.message}`; }
}
$('#aiModerate').onclick = () => runAtria('moderation');
$('#aiTestProvider').onclick = async () => {
  const button = $('#aiTestProvider');
  button.disabled = true;
  try {
    await API.post('/api/ai/test', {});
    await loadAIStatus();
  } catch (error) {
    setResult('#aiProviderDetails', error.message, 'error');
  } finally { button.disabled = false; }
};
$('#aiKill').onclick = async () => {
  const button = $('#aiKill');
  button.disabled = true;
  try {
    await API.post('/api/ai/moderation/kill', {});
    $('#aiModEnabled').checked = false;
    setResult('#aiSaveOut', 'تم تعطيل AI Moderation فورًا.');
    toast('تم تفعيل مفتاح إيقاف AI Moderation');
    await loadAIStatus();
  } catch (error) { setResult('#aiSaveOut', error.message, 'error'); }
  finally { button.disabled = false; }
};
$('#saveAi').onclick = async () => {
  const automatic_actions = [
    $('#aiActionWarn').checked ? 'warn' : null,
    $('#aiActionTimeout').checked ? 'timeout' : null,
    $('#aiActionBan').checked ? 'ban' : null
  ].filter(Boolean);
  try {
    const data = await API.post('/api/ai/moderation/settings', {
      enabled: $('#aiModEnabled').checked,
      channels: selectedValues('#aiModChannels'),
      detection_types: selectedValues('#aiModDetections'),
      sensitivity: $('#aiModSensitivity').value,
      automatic_actions,
      allow_ai_ban: $('#aiAllowBan').checked,
      max_actions_per_hour: Number($('#aiModMaxActions').value),
      timeout_minutes: Number($('#aiTimeout').value),
      fast_path_enabled: $('#aiFastPath').checked,
      ai_timeout_seconds: Number($('#aiModAITimeout').value),
      window_seconds: Number($('#aiModWindow').value),
      repeat_threshold: Number($('#aiRepeatThreshold').value),
      flood_threshold: Number($('#aiFloodThreshold').value),
      mention_threshold: Number($('#aiMentionThreshold').value)
      ,mod_log_channel: $('#aiModLogChannel').value.trim()
      ,whitelist_users: $('#aiWhitelistUsers').value.split(',').map(value => value.trim()).filter(Boolean)
      ,whitelist_roles: $('#aiWhitelistRoles').value.split(',').map(value => value.trim()).filter(Boolean)
      ,delete_offending_messages: $('#aiDeleteOffending').checked
      ,allow_server_invites: $('#aiAllowInvites').checked
      ,raid_window_seconds: Number($('#aiRaidWindow').value)
      ,raid_join_threshold: Number($('#aiRaidThreshold').value)
    });
    if (data.runtime_synced === false) {
      setResult('#aiSaveOut', 'تم الحفظ لكن لم تُحمّل السياسة داخل الـBot — أعد تشغيل البوت.', 'error');
      toast('تم الحفظ دون مزامنة Runtime', 'error');
    } else {
      setResult('#aiSaveOut', 'تم الحفظ وتطبيق AI Moderation داخل الـBot فورًا.');
      toast('تم حفظ وتطبيق AI Moderation policy');
    }
    await Promise.all([loadAIStatus(), loadAIModerationLogs()]);
  } catch (error) { setResult('#aiSaveOut', error.message, 'error'); }
};

function selectedValues(selector) {
  return $$('option:checked', $(selector)).map(option => option.value);
}
let voiceGuilds = [];
async function loadVoice() {
  try {
    const data = await API.get('/api/voice/targets');
    voiceGuilds = data.guilds || [];
    $('#voiceGuild').innerHTML = voiceGuilds.map(guild => `<option value="${esc(guild.id)}">${esc(guild.name)}</option>`).join('') || '<option value="">No connected servers</option>';
    renderVoiceChannels();
    const current = voiceGuilds.find(guild => guild.id === $('#voiceGuild').value);
    $('#voiceStatus').textContent = current?.connected_channel_id
      ? `Connected · ${current.channels.find(channel => channel.id === current.connected_channel_id)?.name || current.connected_channel_id}`
      : 'Disconnected';
  } catch (error) { setResult('#voiceStatus', error.message, 'error'); }
}
function renderVoiceChannels() {
  const guild = voiceGuilds.find(item => item.id === $('#voiceGuild').value);
  const channels = guild?.channels || [];
  $('#voiceChannel').innerHTML = channels.map(channel => `<option value="${esc(channel.id)}">${esc(channel.name)}</option>`).join('') || '<option value="">No voice channels</option>';
}
$('#voiceGuild').onchange = renderVoiceChannels;
$('#refreshVoice').onclick = loadVoice;
async function voiceAction(action) {
  const guild_id = $('#voiceGuild').value;
  const channel_id = $('#voiceChannel').value;
  if (!guild_id) return setResult('#voiceStatus', 'لا يوجد سيرفر متاح.', 'error');
  try {
    const data = await API.post('/api/voice/action', {action, guild_id, channel_id});
    setResult('#voiceStatus', `${action === 'join' ? 'Connected' : 'Disconnected'}${data.channel_name ? ` · ${data.channel_name}` : ''}`);
    await loadVoice();
  } catch (error) { setResult('#voiceStatus', error.message, 'error'); }
}
$('#voiceJoin').onclick = () => voiceAction('join');
$('#voiceLeave').onclick = () => voiceAction('leave');
async function loadAIChat() {
  try {
    const data = await API.get('/api/ai/chat/settings');
    const config = data.config || {};
    const options = (data.guilds || []).flatMap(guild => (guild.channels || []).map(channel =>
      `<option value="${esc(channel.id)}" data-guild="${esc(guild.name)}">${esc(guild.name)} · #${esc(channel.name)}</option>`
    )).join('');
    $('#aiChatChannels').innerHTML = options || '<option disabled>لا توجد قنوات نصية متاحة</option>';
    $('#aiChatIgnored').innerHTML = options || '<option disabled>لا توجد قنوات نصية متاحة</option>';
    const allowed = new Set((config.allowed_channels || []).map(String));
    const ignored = new Set((config.ignored_channels || []).map(String));
    $$('option', $('#aiChatChannels')).forEach(option => { option.selected = allowed.has(option.value); });
    $$('option', $('#aiChatIgnored')).forEach(option => { option.selected = ignored.has(option.value); });
    const logChannels = (data.guilds || []).flatMap(guild => (guild.channels || [])
      .filter(channel => allowed.has(String(channel.id)))
      .map(channel => `<option value="${esc(channel.id)}">${esc(guild.name)} · #${esc(channel.name)}</option>`));
    $('#aiChatLogChannel').innerHTML = logChannels.join('') || '<option value="">لا توجد قناة مسموحة</option>';
    $('#aiChatEnabled').checked = Boolean(config.enabled);
    $('#aiChatMode').value = config.response_mode || 'every_message';
    $('#aiChatSpeed').value = config.speed_mode || 'flash';
    $('#aiChatLanguage').value = config.language || 'auto';
    $('#aiChatContext').value = String(config.context_messages || 100);
    $('#aiChatRetention').value = config.retention_days || 30;
    $('#aiChatMaxOutput').value = config.max_output_tokens || 2600;
    $('#aiChatContextChars').value = config.context_char_budget || 50000;
    $('#aiChatHumor').value = String(config.humor_level ?? 5);
    $('#aiChatEmoji').value = String(config.emoji_level ?? 3);
    $('#aiChatDensity').value = config.answer_density || 'smart';
    $('#aiChatInitiative').value = config.initiative || 'balanced';
    $('#aiChatStreaming').checked = config.streaming !== false;
    $('#aiChatSmartContext').checked = config.smart_context_enabled !== false;
    $('#aiChatIntentRouter').checked = config.intent_router_enabled !== false;
    $('#aiChatFlashPath').checked = config.flash_path_enabled !== false;
    $('#aiChatQualityGuard').checked = config.quality_guard_enabled !== false;
    $('#aiChatSelfRepair').checked = config.self_repair_enabled !== false;
    $('#aiChatAutoContinue').checked = config.auto_continuation_enabled !== false;
    $('#aiChatMicroFast').checked = config.micro_social_fast_lane !== false;
    $('#aiChatOwnerActions').checked = config.owner_actions_enabled !== false;
    $('#aiChatTunisian').checked = Boolean(config.tunisian_mode);
    $('#aiChatName').value = config.name || 'Vixen';
    $('#aiChatTone').value = config.tone || 'friendly';
    $('#aiChatStyle').value = config.style || 'conversational';
    $('#aiChatPrompt').value = config.system_prompt || '';
    if (logChannels.length) await loadAIChatLogs();
    else $('#aiChatLogs').innerHTML = '<tr><td colspan="3" class="empty-cell">لا توجد قنوات AI Chat مسموحة.</td></tr>';
  } catch (error) { setResult('#aiChatSaveResult', error.message, 'error'); }
  await loadAIStatus();
}
$('#saveAIChat').onclick = async () => {
  const payload = {
    enabled: $('#aiChatEnabled').checked,
    allowed_channels: selectedValues('#aiChatChannels'),
    ignored_channels: selectedValues('#aiChatIgnored'),
    response_mode: $('#aiChatMode').value,
    speed_mode: $('#aiChatSpeed').value,
    language: $('#aiChatLanguage').value,
    context_messages: Number($('#aiChatContext').value),
    retention_days: Number($('#aiChatRetention').value),
    max_output_tokens: Number($('#aiChatMaxOutput').value),
    context_char_budget: Number($('#aiChatContextChars').value),
    streaming: $('#aiChatStreaming').checked,
    smart_context_enabled: $('#aiChatSmartContext').checked,
    intent_router_enabled: $('#aiChatIntentRouter').checked,
    flash_path_enabled: $('#aiChatFlashPath').checked,
    quality_guard_enabled: $('#aiChatQualityGuard').checked,
    self_repair_enabled: $('#aiChatSelfRepair').checked,
    auto_continuation_enabled: $('#aiChatAutoContinue').checked,
    micro_social_fast_lane: $('#aiChatMicroFast').checked,
    owner_actions_enabled: $('#aiChatOwnerActions').checked,
    humor_level: Number($('#aiChatHumor').value),
    emoji_level: Number($('#aiChatEmoji').value),
    answer_density: $('#aiChatDensity').value,
    initiative: $('#aiChatInitiative').value,
    tunisian_mode: $('#aiChatTunisian').checked,
    name: $('#aiChatName').value.trim(),
    tone: $('#aiChatTone').value.trim(),
    style: $('#aiChatStyle').value.trim(),
    system_prompt: $('#aiChatPrompt').value,
  };
  try {
    const data = await API.post('/api/ai/chat/settings', payload);
    if (data.runtime_synced === false) {
      setResult('#aiChatSaveResult', 'تم الحفظ لكن لم تُحمّل السياسة داخل الـBot — أعد تشغيل البوت.', 'error');
      toast('تم الحفظ دون مزامنة Runtime', 'error');
    } else {
      setResult('#aiChatSaveResult', 'تم الحفظ وتطبيق AI Chat داخل الـBot فورًا.');
      toast('تم حفظ وتطبيق AI Chat');
    }
    await Promise.all([loadAIChat(), loadAIStatus()]);
  } catch (error) { setResult('#aiChatSaveResult', error.message, 'error'); }
};
async function loadAIChatLogs() {
  const channelId = $('#aiChatLogChannel').value;
  if (!channelId) return;
  try {
    const data = await API.get(`/api/ai/chat/logs?channel_id=${encodeURIComponent(channelId)}&limit=100`);
    $('#aiChatLogs').innerHTML = (data.messages || []).map(message => `<tr><td>${date(message.timestamp)}</td><td>${message.role === 'assistant' ? 'Vixen' : esc(message.user_id)}</td><td class="dm-log-content">${esc(message.content)}</td></tr>`).join('') || '<tr><td colspan="3" class="empty-cell">لا توجد محادثات محفوظة.</td></tr>';
  } catch (error) { $('#aiChatLogs').innerHTML = `<tr><td colspan="3" class="empty-cell error-text">${esc(error.message)}</td></tr>`; }
}
$('#aiChatLogChannel').onchange = loadAIChatLogs;
$('#refreshAIChatLogs').onclick = loadAIChatLogs;
function stopAIChatTestTimer() {
  if (aiChatTestTimer) { clearInterval(aiChatTestTimer); aiChatTestTimer = null; }
}

function updateAIChatTestMeta(text) {
  $('#aiChatTestChars').textContent = number(String(text || '').length);
  if (aiChatTestStartedAt) $('#aiChatTestElapsed').textContent = `${((performance.now() - aiChatTestStartedAt) / 1000).toFixed(1)}s`;
}

$('#testAIChat').onclick = async () => {
  const prompt = $('#aiChatTestPrompt').value.trim();
  if (!prompt) return setResult('#aiChatTestResult', 'اكتب رسالة للاختبار.', 'error');
  $('#testAIChat').disabled = true;
  $('#stopAIChatTest').disabled = false;
  $('#aiChatTestState').textContent = 'RUNNING';
  $('#aiChatTestResult').textContent = '';
  aiChatTestAbortController = new AbortController();
  aiChatTestStartedAt = performance.now();
  stopAIChatTestTimer();
  aiChatTestTimer = setInterval(() => updateAIChatTestMeta($('#aiChatTestResult').textContent), 100);
  try {
    await API.streamPost('/api/ai/chat/stream', {prompt}, event => {
      if (event.type === 'start') {
        $('#aiChatTestState').textContent = 'THINKING';
        if (event.request_id) $('#aiChatTestRequest').textContent = event.request_id;
        if (event.intent) $('#aiChatTestRoute').textContent = String(event.intent);
        if (event.profile) $('#aiChatTestProfile').textContent = String(event.profile);
      }
      if (event.type === 'delta') {
        $('#aiChatTestState').textContent = 'STREAMING';
        if (event.request_id) $('#aiChatTestRequest').textContent = event.request_id;
        $('#aiChatTestResult').textContent += String(event.text || '');
        updateAIChatTestMeta($('#aiChatTestResult').textContent);
      }
      if (event.type === 'done') {
        $('#aiChatTestState').textContent = event.recovery ? `DONE · ${String(event.recovery).replaceAll('_', ' ')}` : 'DONE';
        if (event.request_id) $('#aiChatTestRequest').textContent = event.request_id;
        if (event.recovery) $('#aiChatTestRecovery').textContent = String(event.recovery).replaceAll('_', ' ');
        if (event.ttft_ms != null) $('#aiChatTestRoute').title = `TTFT ${number(event.ttft_ms)} ms`;
        if (event.latency_ms != null) updateAIChatTestMeta($('#aiChatTestResult').textContent);
      }
      if (event.type === 'error') throw Error(event.message || 'تعذر تشغيل الـAI');
    }, aiChatTestAbortController.signal);
    await loadAIStatus();
  } catch (error) {
    if (aiChatTestAbortController?.signal.aborted) {
      $('#aiChatTestState').textContent = 'STOPPED';
      setResult('#aiChatTestResult', 'تم إيقاف الاختبار بدون تغيير إعدادات البوت.', 'success');
    } else {
      $('#aiChatTestState').textContent = 'ERROR';
      setResult('#aiChatTestResult', error.message, 'error');
    }
  } finally {
    stopAIChatTestTimer();
    $('#testAIChat').disabled = false;
    $('#stopAIChatTest').disabled = true;
    updateAIChatTestMeta($('#aiChatTestResult').textContent);
    aiChatTestAbortController = null;
  }
};
$('#stopAIChatTest').onclick = () => {
  if (aiChatTestAbortController) aiChatTestAbortController.abort();
};


function buildDMDesign() {
  const fields = $$('.dm-field-row', $('#dmFields')).map(row => ({
    name: $('.dm-field-name', row).value.trim(),
    value: $('.dm-field-value', row).value.trim(),
    inline: $('.dm-field-inline', row).checked
  })).filter(field => field.name || field.value);
  const authorName = $('#dmAuthor').value.trim();
  const author = authorName ? {name: authorName} : null;
  if (author && $('#dmAuthorUrl').value.trim()) author.url = $('#dmAuthorUrl').value.trim();
  if (author && $('#dmAuthorIcon').value.trim()) author.icon_url = $('#dmAuthorIcon').value.trim();
  const embed = {
    title: $('#dmTitle').value.trim(),
    description: $('#dmDescription').value.trim(),
    color: $('#dmColor').value,
    thumbnail: $('#dmThumbnail').value.trim(),
    image: $('#dmImage').value.trim(),
    fields,
    footer: $('#dmFooterIcon').value.trim() ? {text: $('#dmFooter').value.trim(), icon_url: $('#dmFooterIcon').value.trim()} : $('#dmFooter').value.trim(),
    timestamp: $('#dmTimestamp').checked
  };
  if (author) embed.author = author;
  const hasEmbed = Boolean(embed.title || embed.description || embed.thumbnail || embed.image || embed.footer || embed.fields.length || author);
  return {content: $('#dmContent').value.trim(), embed: hasEmbed ? embed : {}};
}

function addDMField(field = {}) {
  const row = document.createElement('div');
  row.className = 'dm-field-row form-grid';
  row.innerHTML = `<label>Field name<input class="dm-field-name" maxlength="256" value="${esc(field.name || '')}"></label><label>Field value<textarea class="dm-field-value" maxlength="1024">${esc(field.value || '')}</textarea></label><label class="dm-inline-option"><input class="dm-field-inline" type="checkbox" ${field.inline ? 'checked' : ''}> Inline</label><button type="button" class="icon-button" data-dm-remove-field aria-label="Remove field">×</button>`;
  $('#dmFields').appendChild(row);
}

function applyDMDesign(source = {}) {
  const design = source.embed && !source.title ? source : {content: source.content || '', embed: source.embed || source};
  const embed = design.embed || {};
  $('#dmContent').value = design.content || '';
  $('#dmContentCount').textContent = String($('#dmContent').value.length);
  $('#dmTitle').value = embed.title || '';
  $('#dmDescription').value = embed.description || '';
  $('#dmColor').value = typeof embed.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(embed.color) ? embed.color : '#f5a623';
  const author = embed.author || {};
  $('#dmAuthor').value = author.name || '';
  $('#dmAuthorUrl').value = author.url || '';
  $('#dmAuthorIcon').value = author.icon_url || '';
  $('#dmThumbnail').value = embed.thumbnail?.url || embed.thumbnail || '';
  $('#dmImage').value = embed.image?.url || embed.image || '';
  $('#dmFooter').value = embed.footer?.text || embed.footer || '';
  $('#dmFooterIcon').value = embed.footer?.icon_url || '';
  $('#dmTimestamp').checked = Boolean(embed.timestamp);
  $('#dmFields').replaceChildren();
  (embed.fields || []).forEach(field => addDMField(field));
  scheduleDMPreview();
}

function scheduleDMPreview() {
  clearTimeout(dmPreviewTimer);
  if ($('#dmContentCount')) $('#dmContentCount').textContent = String($('#dmContent').value.length);
  dmPreviewTimer = setTimeout(renderDMPreview, 450);
}

function templateOptions(list, emptyLabel = 'بدون قالب') {
  return `<option value="">${esc(emptyLabel)}</option>${(list || []).map(template => `<option value="${esc(template.template_id)}">${esc(template.name)}${template.favorite ? ' ★' : ''}${template.category ? ` · ${esc(template.category)}` : ''}</option>`).join('')}`;
}

async function loadDMCenter() {
  try {
    const data = await API.get('/api/dm/center');
    const previousGuild = $('#dmGuild').value;
    dmGuilds = data.guilds || [];
    $('#dmGuild').innerHTML = dmGuilds.map(guild => `<option value="${esc(guild.id)}">${esc(guild.name)} · ${number(guild.member_count)}</option>`).join('') || '<option value="">No connected servers</option>';
    if (dmGuilds.some(guild => guild.id === previousGuild)) $('#dmGuild').value = previousGuild;
    const selected = dmGuilds.find(guild => guild.id === $('#dmGuild').value);
    $('#dmMemberCacheState').textContent = selected?.member_cache_complete ? 'جاهز' : 'ناقص';
    await Promise.all([loadDMTemplates(), loadDMHistory()]);
    $('#dmTemplateCount').textContent = number(dmTemplates.length);
    if ($('#dmMemberSearch').value.trim().length >= 2) await loadDMMembers();
  } catch (error) { setResult('#dmCenterResult', error.message, 'error'); }
}

async function loadDMMembers() {
  const guildId = $('#dmGuild').value;
  const query = $('#dmMemberSearch').value.trim();
  if (!guildId || query.length < 2) {
    $('#dmMemberResults').innerHTML = '<div class="empty-state">اكتب حرفين على الأقل للبحث.</div>';
    return;
  }
  try {
    const data = await API.get(`/api/dm/members?guild_id=${encodeURIComponent(guildId)}&q=${encodeURIComponent(query)}`);
    dmMembers = data.members || [];
    $('#dmMemberResults').innerHTML = dmMembers.map(member => `<button type="button" class="member-row dm-member-choice" data-dm-center-member="${esc(member.id)}"><span class="member-main"><img class="avatar small" src="${esc(member.avatar)}" alt=""><span><b>${esc(member.display_name)}</b><small>${esc(member.username)} · ${esc(member.id)} · ${esc(member.roles.join(', ') || (member.is_bot ? 'Bot' : 'User'))}</small></span></span></button>`).join('') || '<div class="empty-state">لا يوجد عضو مطابق.</div>';
  } catch (error) { $('#dmMemberResults').innerHTML = `<div class="empty-state error-text">${esc(error.message)}</div>`; }
}

async function loadDMTemplates() {
  const guildId = $('#dmGuild').value;
  if (!guildId) return;
  const params = new URLSearchParams({guild_id: guildId});
  if ($('#dmTemplateSearch')?.value.trim()) params.set('q', $('#dmTemplateSearch').value.trim());
  if ($('#dmTemplateCategoryFilter')?.value) params.set('category', $('#dmTemplateCategoryFilter').value);
  if (dmFavoriteOnly) params.set('favorites', '1');
  const categoryBefore = $('#dmTemplateCategoryFilter')?.value || '';
  const data = await API.get(`/api/dm/templates?${params.toString()}`);
  dmTemplates = data.templates || [];
  $('#dmTemplate').innerHTML = templateOptions(dmTemplates, 'تصميم جديد');
  const categories = data.categories || [];
  $('#dmTemplateCategoryFilter').innerHTML = `<option value="">كل الفئات</option>${categories.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('')}`;
  if (categories.includes(categoryBefore)) $('#dmTemplateCategoryFilter').value = categoryBefore;
  if (dmTemplates.some(template => String(template.template_id) === String(currentDmTemplateId))) $('#dmTemplate').value = String(currentDmTemplateId);
  else { currentDmTemplateId = ''; $('#dmTemplate').value = ''; }
}

async function loadDMHistory() {
  const guildId = $('#dmGuild').value;
  if (!guildId) return;
  try {
    const params = new URLSearchParams({guild_id: guildId, limit: '100'});
    if ($('#dmHistoryStatus')?.value) params.set('status', $('#dmHistoryStatus').value);
    if ($('#dmHistorySearch')?.value.trim()) params.set('q', $('#dmHistorySearch').value.trim());
    const data = await API.get(`/api/dm/history?${params.toString()}`);
    const history = data.history || [];
    const last = history[0];
    $('#dmLastSend').textContent = last ? date(last.timestamp) : '—';
    $('#dmHistory').innerHTML = history.map(item => `<tr><td><b>${esc(item.recipient_name || item.recipient_id)}</b><small class="cell-sub">${esc(item.recipient_id)}</small></td><td>${item.template_id ? `#${esc(item.template_id)}` : 'Custom'}</td><td>${esc(item.sender_name || item.sender_id)}</td><td><span class="status-tag ${esc(item.status)}">${esc(item.status)}</span></td><td class="dm-history-message">${esc(item.content || 'Embed only')}</td><td>${esc(item.error || '—')}</td><td>${date(item.timestamp)}</td></tr>`).join('') || '<tr><td colspan="7" class="empty-cell">لا يوجد سجل إرسال.</td></tr>';
  } catch (error) { $('#dmHistory').innerHTML = `<tr><td colspan="7" class="empty-cell error-text">${esc(error.message)}</td></tr>`; }
}

function renderDMMessagePreview(targetSelector, data, label = '') {
  const embed = data.embed;
  const content = data.content || '';
  const color = `#${Number(embed?.color || 0xf5a623).toString(16).padStart(6, '0')}`;
  const author = embed?.author?.name ? `<b>${esc(embed.author.name)}</b><br>` : '';
  const fields = (embed?.fields || []).map(field => `<div class="dm-preview-field"><b>${esc(field.name)}</b><p>${esc(field.value)}</p></div>`).join('');
  const image = embed?.image?.url ? `<img class="dm-preview-image" src="${esc(embed.image.url)}" alt="">` : '';
  const thumbnail = embed?.thumbnail?.url ? `<img class="dm-preview-thumbnail" src="${esc(embed.thumbnail.url)}" alt="">` : '';
  $(targetSelector).innerHTML = `<div class="dm-preview-card" style="border-color:${esc(color)}">${label ? `<span class="preview-label">${esc(label)}</span>` : ''}${content ? `<div class="dm-preview-content">${esc(content)}</div>` : ''}${author}${embed?.title ? `<h3>${esc(embed.title)}</h3>` : ''}${embed?.description ? `<p>${esc(embed.description)}</p>` : ''}${thumbnail}${fields}${image}${embed?.footer?.text ? `<small>${esc(embed.footer.text)}</small>` : ''}</div>`;
}

async function renderDMPreview() {
  const guildId = $('#dmGuild').value;
  const memberId = $('#dmRecipientId').value.trim();
  if (!guildId || !/^\d+$/.test(memberId)) {
    $('#dmPreview').textContent = 'اختر سيرفرًا وعضوًا لمعاينة المتغيرات.';
    return null;
  }
  try {
    const body = {guild_id: guildId, member_id: memberId, reason: $('#dmReason').value, content: $('#dmContent').value, embed: buildDMDesign().embed};
    if (currentDmTemplateId) body.template_id = currentDmTemplateId;
    const data = await API.post('/api/dm/preview', body);
    renderDMMessagePreview('#dmPreview', data, `إلى ${data.recipient?.name || memberId}`);
    return data;
  } catch (error) {
    setResult('#dmCenterResult', error.message, 'error');
    $('#dmPreview').textContent = 'تعذر إنشاء المعاينة؛ راجع قيم الرسالة والـEmbed.';
    return null;
  }
}

async function refreshDMTemplateSelection() {
  await loadDMTemplates();
  if (currentDmTemplateId) {
    const template = dmTemplates.find(item => String(item.template_id) === String(currentDmTemplateId));
    if (template) {
      $('#dmTemplateName').value = template.name;
      $('#dmTemplateDescription').value = template.description || '';
      $('#dmTemplateCategory').value = template.category || '';
      $('#dmFavoriteTemplate').textContent = template.favorite ? '★ مفضلة' : '☆ مفضلة';
      applyDMDesign(template);
    }
  }
}

$('#dmGuild').onchange = async () => {
  currentDmTemplateId = '';
  $('#dmMemberResults').innerHTML = '';
  await Promise.all([loadDMTemplates(), loadDMHistory()]);
  scheduleDMPreview();
};
$('#dmMemberSearch').oninput = () => { clearTimeout(dmMemberTimer); dmMemberTimer = setTimeout(loadDMMembers, 250); };
document.addEventListener('click', event => {
  const memberButton = event.target.closest('[data-dm-center-member]');
  if (memberButton) {
    const member = dmMembers.find(item => item.id === memberButton.dataset.dmCenterMember);
    if (!member) return;
    $('#dmRecipientId').value = member.id;
    $('#dmSelectedMember').textContent = `${member.display_name} · ${member.username} · ${member.roles.join(', ') || (member.is_bot ? 'Bot' : 'User')}`;
    scheduleDMPreview();
  }
  if (event.target.closest('[data-dm-remove-field]')) {
    event.target.closest('.dm-field-row')?.remove();
    scheduleDMPreview();
  }
});
$('#dmTemplate').onchange = () => {
  currentDmTemplateId = $('#dmTemplate').value;
  const template = dmTemplates.find(item => String(item.template_id) === String(currentDmTemplateId));
  if (template) {
    $('#dmTemplateName').value = template.name;
    $('#dmTemplateDescription').value = template.description || '';
    $('#dmTemplateCategory').value = template.category || '';
    $('#dmFavoriteTemplate').textContent = template.favorite ? '★ مفضلة' : '☆ مفضلة';
    applyDMDesign(template);
  } else {
    $('#dmTemplateName').value = ''; $('#dmTemplateDescription').value = ''; $('#dmTemplateCategory').value = '';
    applyDMDesign({content: '', embed: {}});
  }
};
$('#dmFields').addEventListener('input', scheduleDMPreview);
$('#dm-center').addEventListener('input', scheduleDMPreview);
$('#dm-center').addEventListener('change', scheduleDMPreview);
$('#dmAddField').onclick = () => { if ($$('.dm-field-row', $('#dmFields')).length >= 25) return setResult('#dmCenterResult', 'وصلت للحد الأقصى: 25 field.', 'error'); addDMField(); scheduleDMPreview(); };
$('#dmPreviewButton').onclick = renderDMPreview;
$('#refreshDM').onclick = loadDMCenter;
$('#refreshDMHistory').onclick = loadDMHistory;
$('#dmTemplateSearch').oninput = debounce(() => loadDMTemplates(), 220);
$('#dmTemplateCategoryFilter').onchange = loadDMTemplates;
$('#dmFavoriteOnly').onclick = async () => { dmFavoriteOnly = !dmFavoriteOnly; $('#dmFavoriteOnly').classList.toggle('active', dmFavoriteOnly); await loadDMTemplates(); };
$('#dmInstallTemplates').onclick = async () => { try { const result = await API.post('/api/dm/templates', {guild_id: $('#dmGuild').value, action: 'install_defaults'}); await loadDMTemplates(); setResult('#dmCenterResult', `تمت إضافة ${result.created} قالبًا جاهزًا.`); toast('تم تحديث مكتبة القوالب'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } };
$('#dmUseBranding').onclick = () => { const guild = dmGuilds.find(item => item.id === $('#dmGuild').value); if (!guild) return setResult('#dmCenterResult', 'اختر سيرفرًا أولًا.', 'error'); $('#dmAuthor').value = guild.name; $('#dmAuthorIcon').value = guild.icon || ''; $('#dmColor').value = /^#[0-9a-fA-F]{6}$/.test(guild.primary_color || '') ? guild.primary_color : '#f5a623'; $('#dmFooter').value = '{server} · {timestamp}'; scheduleDMPreview(); };
$('#dmUseBotBranding').onclick = () => { const guild = dmGuilds.find(item => item.id === $('#dmGuild').value); if (!guild) return setResult('#dmCenterResult', 'اختر سيرفرًا أولًا.', 'error'); $('#dmAuthor').value = guild.bot_name || 'Vixen'; $('#dmAuthorIcon').value = guild.bot_avatar || ''; $('#dmColor').value = /^#[0-9a-fA-F]{6}$/.test(guild.accent_color || '') ? guild.accent_color : '#2ecc71'; scheduleDMPreview(); };
$('#dmFavoriteTemplate').onclick = async () => { if (!currentDmTemplateId) return setResult('#dmCenterResult', 'اختر قالبًا أولًا.', 'error'); const current = dmTemplates.find(item => String(item.template_id) === String(currentDmTemplateId)); try { await API.post('/api/dm/templates', {guild_id: $('#dmGuild').value, action: 'favorite', template_id: currentDmTemplateId, favorite: !current?.favorite}); await refreshDMTemplateSelection(); toast(current?.favorite ? 'أزيلت من المفضلة' : 'أضيفت للمفضلة'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } };
$('#dmSaveTemplate').onclick = async () => { const name = $('#dmTemplateName').value.trim(); try { const design = buildDMDesign(); const payload = {guild_id: $('#dmGuild').value, action: 'save', name, content: design.content, embed: design.embed, description: $('#dmTemplateDescription').value.trim(), category: $('#dmTemplateCategory').value.trim() || 'General'}; if (currentDmTemplateId) payload.template_id = currentDmTemplateId; const result = await API.post('/api/dm/templates', payload); currentDmTemplateId = String(result.template_id); await loadDMTemplates(); $('#dmTemplate').value = currentDmTemplateId; setResult('#dmCenterResult', 'تم حفظ القالب وتحديث النسخة الحية.'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } };
$('#dmDuplicateTemplate').onclick = async () => { if (!currentDmTemplateId) return setResult('#dmCenterResult', 'اختر قالبًا لنسخه.', 'error'); const name = `${$('#dmTemplateName').value.trim()} copy`.trim(); try { const result = await API.post('/api/dm/templates', {guild_id: $('#dmGuild').value, action: 'duplicate', template_id: currentDmTemplateId, name}); currentDmTemplateId = String(result.template_id); await loadDMTemplates(); $('#dmTemplate').value = currentDmTemplateId; $('#dmTemplateName').value = name; toast('تم نسخ القالب'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } };
$('#dmDeleteTemplate').onclick = async () => { if (!currentDmTemplateId) return setResult('#dmCenterResult', 'اختر قالبًا لحذفه.', 'error'); if (!confirm('حذف القالب المحدد؟ سيتم حذف التعريف فقط ولن يتم حذف سجل الإرسال.')) return; try { await API.post('/api/dm/templates', {guild_id: $('#dmGuild').value, action: 'delete', template_id: currentDmTemplateId}); currentDmTemplateId = ''; $('#dmTemplateName').value = ''; applyDMDesign({content: '', embed: {}}); await loadDMTemplates(); toast('تم حذف القالب'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } };
$('#dmSendButton').onclick = async () => { const preview = await renderDMPreview(); if (!preview) return; if (!confirm(`إرسال الرسالة إلى ${preview.recipient.name}؟\nهذه المعاينة مرتبطة بهذا العضو وبجلسة المرسل.`)) return; const button = $('#dmSendButton'); button.disabled = true; try { const result = await API.post('/api/dm/send', {guild_id: $('#dmGuild').value, member_id: $('#dmRecipientId').value.trim(), confirm: true, preview_token: preview.preview_token}); setResult('#dmCenterResult', `تم الإرسال فعليًا · سجل #${result.dm_id}`); toast('تم إرسال DM'); } catch (error) { setResult('#dmCenterResult', error.message, 'error'); } finally { button.disabled = false; await loadDMHistory(); } };

function parseMemberIds(raw) { return String(raw || '').split(/[\s,;]+/).map(value => value.trim()).filter(value => /^\d+$/.test(value)); }
function selectedValues(select) { return $$("option", select).filter(option => option.selected).map(option => option.value); }
function buildDMAllFilters() {
  return {mode: $('#dmAllMode').value, role_ids: selectedValues($('#dmAllRoles')), exclude_role_ids: selectedValues($('#dmAllExcludeRoles')), member_ids: parseMemberIds($('#dmAllSelectedIds').value), exclude_bots: $('#dmAllExcludeBots').checked, exclude_staff: $('#dmAllExcludeStaff').checked, exclude_self: $('#dmAllExcludeSelf').checked};
}
function updateDMAllModeUI() {
  const mode = $('#dmAllMode').value;
  $('#dmAllRoleWrap').classList.toggle('hidden', mode === 'selected');
  $('#dmAllExcludeRoleWrap').classList.toggle('hidden', mode === 'selected');
  $('#dmAllSelectedWrap').classList.toggle('hidden', mode !== 'selected');
  $('#dmAllTargetReadout').textContent = mode === 'all' ? 'All members' : mode === 'roles' ? 'Role audience' : 'Selected IDs';
  dmBulkPreviewToken = null;
}

async function loadDMAll() {
  try {
    const center = await API.get('/api/dm/center');
    const previous = $('#dmAllGuild').value;
    dmGuilds = center.guilds || [];
    $('#dmAllGuild').innerHTML = dmGuilds.map(guild => `<option value="${esc(guild.id)}">${esc(guild.name)} · ${number(guild.member_count)}</option>`).join('') || '<option value="">No connected servers</option>';
    if (dmGuilds.some(guild => guild.id === previous)) $('#dmAllGuild').value = previous;
    await Promise.all([loadDMAllRoles(), loadDMAllTemplates(), loadDMAllJobs()]);
  } catch (error) { setResult('#dmAllResult', error.message, 'error'); }
}
async function loadDMAllRoles() {
  const guildId = $('#dmAllGuild').value; if (!guildId) return;
  const data = await API.get(`/api/dm/roles?guild_id=${encodeURIComponent(guildId)}`);
  dmBulkRoles = data.roles || [];
  const options = dmBulkRoles.map(role => `<option value="${esc(role.id)}">${esc(role.name)} · ${number(role.member_count)}</option>`).join('');
  $('#dmAllRoles').innerHTML = options || '<option disabled>لا توجد Roles</option>';
  $('#dmAllExcludeRoles').innerHTML = options || '<option disabled>لا توجد Roles</option>';
}
async function loadDMAllTemplates() {
  const guildId = $('#dmAllGuild').value; if (!guildId) return;
  const data = await API.get(`/api/dm/templates?guild_id=${encodeURIComponent(guildId)}`);
  dmBulkTemplates = data.templates || [];
  $('#dmAllTemplate').innerHTML = templateOptions(dmBulkTemplates, 'استعمل محتوى يدوي');
}
async function loadDMAllJobs() {
  const guildId = $('#dmAllGuild').value; if (!guildId) return;
  const data = await API.get(`/api/dm/bulk/jobs?guild_id=${encodeURIComponent(guildId)}&limit=30`);
  renderDMAllJobs(data.jobs || []);
}
function jobProgress(job) { const done = Number(job.success || 0) + Number(job.failed || 0) + Number(job.cancelled || 0) + Number(job.uncertain || 0); return job.total ? Math.min(100, Math.round((done / job.total) * 100)) : 100; }
function renderDMAllJobs(jobs) {
  $('#dmJobs').innerHTML = jobs.map(job => {
    const progress = jobProgress(job);
    const status = job.status;
    const controls = ['queued','running'].includes(status) ? `<button class="secondary dm-job-control" data-dm-job="${esc(job.job_id)}" data-dm-action="pause">Pause</button><button class="danger dm-job-control" data-dm-job="${esc(job.job_id)}" data-dm-action="cancel">Cancel</button>` : status === 'paused' || status === 'interrupted' ? `<button class="secondary dm-job-control" data-dm-job="${esc(job.job_id)}" data-dm-action="resume">Resume</button>` : '';
    return `<article class="dm-job-card"><div class="dm-job-head"><div><b>Campaign ${esc(job.job_id.slice(0, 8))}</b><small>${date(job.created_at)} · ${esc(job.options?.target?.mode || 'all')}</small></div><span class="status-tag ${esc(status)}">${esc(status)}</span></div><div class="dm-job-progress"><i style="width:${progress}%"></i></div><div class="dm-job-stats"><span><b>${number(job.success)}</b> success</span><span><b>${number(job.failed)}</b> failed</span><span><b>${number(job.cancelled)}</b> cancelled</span><span><b>${number(job.uncertain)}</b> uncertain</span><span><b>${progress}%</b> progress</span></div>${job.last_error ? `<div class="dm-job-error">${esc(job.last_error)}</div>` : ''}<div class="dm-job-actions">${controls}<button class="ghost-button dm-job-refresh" data-dm-job-refresh="${esc(job.job_id)}">Details ↻</button></div></article>`;
  }).join('') || '<div class="empty-state">لا توجد حملات DM محفوظة لهذا السيرفر.</div>';
}
async function previewDMAll() {
  const guildId = $('#dmAllGuild').value;
  if (!guildId) return setResult('#dmAllResult', 'اختر سيرفرًا أولًا.', 'error');
  const template = dmBulkTemplates.find(item => String(item.template_id) === String($('#dmAllTemplate').value));
  const content = $('#dmAllContent').value.trim() || template?.content || '';
  const embed = template?.embed || {};
  try {
    const result = await API.post('/api/dm/bulk/preview', {guild_id: guildId, template_id: template?.template_id || undefined, reason: $('#dmAllReason').value, content, embed, filters: buildDMAllFilters(), messages_per_minute: Number($('#dmAllPPM').value || 20)});
    dmBulkPreviewToken = result.preview_token;
    $('#dmAllCount').textContent = number(result.count);
    $('#dmAllStart').disabled = false;
    const sampleNames = (result.sample_members || []).map(item => item.name).join('، '); $('#dmAllCacheState').textContent = `${result.sample?.length || 0} عيّنة · snapshot جاهز${sampleNames ? ` · ${sampleNames}` : ''}`; 
    renderDMMessagePreview('#dmAllPreview', result, `Preview · ${number(result.count)} recipients · ${result.messages_per_minute}/min`);
    setResult('#dmAllResult', 'المعاينة جاهزة. الـStart سيستعمل نفس snapshot وليس قائمة جديدة.', 'success');
  } catch (error) { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; setResult('#dmAllResult', error.message, 'error'); $('#dmAllPreview').textContent = 'فشلت المعاينة.'; }
}
async function startDMAll() {
  if (!dmBulkPreviewToken) return setResult('#dmAllResult', 'اعمل Preview أولًا قبل Start.', 'error');
  const count = Number($('#dmAllCount').textContent.replace(/[^0-9]/g, '') || 0);
  if (!count) return setResult('#dmAllResult', 'لا توجد أهداف مؤكدة.', 'error');
  if (!confirm(`سيتم إطلاق حملة DM إلى ${number(count)} مستلمًا بسرعة ${$('#dmAllPPM').value}/دقيقة.\nتم استبعاد العناصر المحددة في المعاينة. ابدأ؟`)) return;
  try {
    const data = await API.post('/api/dm/bulk/start', {preview_token: dmBulkPreviewToken, confirm: true, confirmed_count: count});
    dmBulkPreviewToken = null;
    $('#dmAllStart').disabled = true;
    setResult('#dmAllResult', `تم بدء الحملة ${data.job_id.slice(0, 8)} · تابع التقدم من الأسفل.`);
    await loadDMAllJobs();
  } catch (error) { setResult('#dmAllResult', error.message, 'error'); }
}
function startDMAllPolling() { stopDMAllPolling(); dmBulkRefreshTimer = setInterval(() => { if (currentPage === 'dm-all') loadDMAllJobs(); }, 2000); }
function stopDMAllPolling() { if (dmBulkRefreshTimer) { clearInterval(dmBulkRefreshTimer); dmBulkRefreshTimer = null; } }

$('#dmAllGuild').onchange = async () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; $('#dmAllCount').textContent = '—'; await Promise.all([loadDMAllRoles(), loadDMAllTemplates(), loadDMAllJobs()]); };
$('#dmAllMode').onchange = updateDMAllModeUI;
$('#dmAllRoles').addEventListener('change', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllExcludeRoles').addEventListener('change', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllSelectedIds').addEventListener('input', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllExcludeBots').addEventListener('change', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllExcludeStaff').addEventListener('change', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllExcludeSelf').addEventListener('change', () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; });
$('#dmAllTemplate').onchange = () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; $('#dmAllCount').textContent = '—'; };
$('#dmAllReason').oninput = () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; };
$('#dmAllContent').oninput = () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; };
$('#dmAllPPM').oninput = () => { dmBulkPreviewToken = null; $('#dmAllStart').disabled = true; };
$('#dmAllLoadTemplate').onclick = () => { const template = dmBulkTemplates.find(item => String(item.template_id) === String($('#dmAllTemplate').value)); if (!template) return setResult('#dmAllResult', 'اختر قالبًا أولًا.', 'error'); $('#dmAllContent').value = template.content || ''; setResult('#dmAllResult', `تم تحميل محتوى قالب: ${template.name}`); };
$('#dmAllPreviewButton').onclick = previewDMAll;
$('#dmAllStart').onclick = startDMAll;
$('#refreshDMAll').onclick = loadDMAll;
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-dm-job]');
  if (button) {
    const jobId = button.dataset.dmJob; const action = button.dataset.dmAction;
    try { const result = await API.post('/api/dm/bulk/control', {job_id: jobId, action}); toast(`${action} → ${result.status}`); await loadDMAllJobs(); } catch (error) { toast(error.message, 'error'); }
    return;
  }
  const detail = event.target.closest('[data-dm-job-refresh]');
  if (detail) {
    try { const result = await API.get(`/api/dm/bulk/status?guild_id=${encodeURIComponent($('#dmAllGuild').value)}&job_id=${encodeURIComponent(detail.dataset.dmJobRefresh)}`); const job = result.job; setResult('#dmAllResult', `Campaign ${job.job_id.slice(0,8)} · ${job.status} · ${number(job.success)} success / ${number(job.failed)} failed`); } catch (error) { toast(error.message, 'error'); }
  }
});
updateDMAllModeUI();

function uptimeLabel(seconds) {
  if (!Number.isFinite(Number(seconds))) return '—';
  let remaining = Math.max(0, Math.floor(Number(seconds)));
  const days = Math.floor(remaining / 86400); remaining %= 86400;
  const hours = Math.floor(remaining / 3600); remaining %= 3600;
  const minutes = Math.floor(remaining / 60); const secs = remaining % 60;
  return `${days}d ${hours}h ${minutes}m ${secs}s`;
}
async function loadBotProfile() {
  try {
    const data = await API.get('/api/bot/profile');
    $('#profileAvatar').src = data.avatar || '';
    $('#profileAvatar').classList.toggle('hidden', !data.avatar);
    $('#profileName').textContent = data.name || 'Bot not connected';
    $('#profileUsername').textContent = data.display_name || '';
    $('#profileId').textContent = data.id ? `ID ${data.id} · ${data.version}` : data.version;
    $('#profileConnection').textContent = data.connected ? 'Connected' : 'Not connected';
    $('#profileConnection').classList.toggle('success', Boolean(data.connected));
    $('#profileLatency').textContent = data.latency_ms == null ? '—' : `${number(data.latency_ms)} ms`;
    $('#profileUptime').textContent = uptimeLabel(data.uptime_seconds);
    $('#profileGuilds').textContent = data.guilds == null ? '—' : number(data.guilds);
    $('#profileMembers').textContent = data.members == null ? '—' : number(data.members);
    $('#profileCommands').textContent = data.commands == null ? '—' : number(data.commands);
    $('#profileAI').textContent = data.ai?.status || 'Unknown';
    $('#presenceType').value = data.presence?.type || 'playing';
    $('#presenceName').value = data.presence?.name || '';
  } catch (error) { toast(error.message, 'error'); }
}
$('#refreshBotProfile').onclick = loadBotProfile;
$('#savePresence').onclick = async () => {
  const name = $('#presenceName').value.trim();
  try {
    const result = await API.post('/api/bot/presence', {type: $('#presenceType').value, name});
    setResult('#presenceResult', result.applied ? 'Presence تطبقت على Discord.' : 'Presence محفوظة وستطبق عند اتصال Gateway.');
    await loadBotProfile();
  } catch (error) { setResult('#presenceResult', error.message, 'error'); }
};

function appendTerminalEvent(event) {
  const line = document.createElement('div');
  line.className = `terminal-line ${String(event.level || '').toLowerCase()}`;
  const timestamp = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '';
  line.textContent = `[${timestamp}] [${event.level || 'INFO'}] ${event.logger || 'Vixen'} ${event.message || ''}`;
  $('#terminalOutput').appendChild(line);
  while ($('#terminalOutput').children.length > 300) $('#terminalOutput').firstElementChild.remove();
  $('#terminalOutput').scrollTop = $('#terminalOutput').scrollHeight;
}

function stopTerminal() {
  if (terminalAbortController) {
    const controller = terminalAbortController;
    terminalAbortController = null;
    controller.abort();
  }
  const status = $('#terminalStatus');
  if (status) status.textContent = 'Disconnected';
  const button = $('#terminalToggle');
  if (button) button.textContent = 'Start stream';
}

async function startTerminal() {
  if (terminalAbortController) return stopTerminal();
  const controller = new AbortController();
  terminalAbortController = controller;
  $('#terminalStatus').textContent = 'Connecting…';
  $('#terminalStatus').classList.remove('error');
  $('#terminalToggle').textContent = 'Stop stream';
  try {
    const response = await fetch('/api/terminal/stream', {
      headers: {Authorization: `Bearer ${API.token}`},
      signal: controller.signal
    });
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch (_) {}
      throw Error(body.error || `HTTP ${response.status}`);
    }
    $('#terminalStatus').textContent = 'Connected';
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffered = '';
    while (terminalAbortController === controller) {
      const {value, done} = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, {stream: true});
      const blocks = buffered.split(/\r?\n\r?\n/);
      buffered = blocks.pop() || '';
      for (const block of blocks) {
        const dataLine = block.split(/\r?\n/).find(line => line.startsWith('data:'));
        if (!dataLine) continue;
        try { appendTerminalEvent(JSON.parse(dataLine.slice(5).trim())); }
        catch (_) { /* Ignore malformed event frames. */ }
      }
    }
  } catch (error) {
    if (error.name !== 'AbortError' && terminalAbortController === controller) {
      $('#terminalStatus').textContent = error.message;
      $('#terminalStatus').classList.add('error');
    }
  } finally {
    if (terminalAbortController === controller) {
      terminalAbortController = null;
      $('#terminalToggle').textContent = 'Start stream';
      if ($('#terminalStatus').textContent === 'Connected') $('#terminalStatus').textContent = 'Disconnected';
    }
  }
}
$('#terminalToggle').onclick = startTerminal;

boot();