/* 簽單台：所有業務資料以伺服器為準，不使用展示資料。 */
'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const BASE_PATH = $('meta[name="app-base-path"]')?.content || '';
const appUrl = path => `${BASE_PATH}${path}`;
const h = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n = value => Number(value || 0);
const number = value => new Intl.NumberFormat('zh-TW', {maximumFractionDigits:6}).format(n(value));
const money = value => `¥${number(value)} 萬`;
const rounded = value => new Intl.NumberFormat('zh-TW', {maximumFractionDigits:2}).format(n(value));
const computedMoney = value => `¥${rounded(value)} 萬`;
const pct = value => `${(n(value) * 100).toFixed(1)}%`;
const width = value => `${Math.max(0, Math.min(100, n(value)))}%`;
const shortDate = value => (value || '').slice(0, 10);
const fileSize = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
const stageTag = c => `<span class="tag ${c.stage === 5 ? 'tag-accent' : 'tag-outline'}">${h(c.stage_name)}${c.stage === 1 ? ` · 面聊 ${c.meetings} 次` : ''}</span>`;
const statusTag = ct => `<span class="tag ${ct.status === 'signed' ? 'tag-accent' : 'tag-outline'}">${ct.status === 'lost' ? '合同流失' : ct.status === 'signed' ? '簽約完畢' : '簽約中'}</span>`;
const button = (label, action, id = '', cls = 'btn-secondary', extras = '') => `<button type="button" class="btn ${cls}" data-action="${action}" data-id="${h(id)}" ${extras}>${label}</button>`;
const empty = (title, description = '', action = '') => `<div class="empty"><strong>${h(title)}</strong><p>${h(description)}</p>${action}</div>`;
const kpi = (label, value, sub = '', accent = false) => `<div class="kpi"><span class="kpi-label">${h(label)}</span><strong class="kpi-value ${String(value).length > 10 ? 'kpi-value-small' : ''} ${accent ? 'accent' : ''}">${h(value)}</strong><span class="kpi-sub">${h(sub)}</span></div>`;
const customerLink = id => `#/customer/${id}`;

let data, year = new Date().getFullYear(), modal = null, busy = false;
let customerFilter = 0, customerSearch = '', leadFilter = 'open', contractFilter = '', contractSearch = '';
let detailFollowups = [], targetDraft = null, toastTimer, refreshSequence = 0, previousFocus;
let authInfo = null, authRedirecting = false, peopleData = null;
const isAdmin = () => authInfo?.user?.role === 'admin';
const scopeLabel = () => isAdmin() ? '全部團隊' : '我的資料';

function returnToLogin(url) {
  if (authRedirecting) return;
  authRedirecting = true;
  const loginPath = appUrl('/login');
  const destination = url === loginPath || url?.startsWith(loginPath + '?') ? url : loginPath + '?reason=expired';
  const next = encodeURIComponent('/' + location.hash);
  targetDraft = null; modal = null; data = undefined;
  $('#modal-root').innerHTML = '';
  document.body.classList.remove('modal-open');
  $('#app').textContent = '正在返回登入頁…';
  location.replace(`${destination}${destination.includes('?') ? '&' : '?'}next=${next}`);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && !['GET','HEAD'].includes(options.method.toUpperCase()) && authInfo?.csrf_token) {
    headers.set('X-CSRF-Token', authInfo.csrf_token);
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(appUrl(`/api/v1${path}`), {...options, headers, cache:'no-store'});
  const result = await response.json().catch(() => ({}));
  if (response.status === 401) {
    returnToLogin('/login?reason=expired');
    throw new Error('登入狀態已失效，請重新登入');
  }
  if (response.status === 403 && result.approval_required) {
    authRedirecting = true;
    targetDraft = null; modal = null; data = undefined; peopleData = null;
    $('#modal-root').innerHTML = '';
    $('#app').textContent = '正在返回身分分配頁…';
    location.replace(appUrl('/waiting'));
    throw new Error('請等待管理員分配身分');
  }
  if (!response.ok) throw new Error(result.error || `連線失敗（${response.status}），請稍後再試`);
  return result;
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('visible'), 3800);
}

function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  if (parts[0] === 'lost' || (parts[0] === 'leads' && parts[1] === 'lost')) {
    return {page:'leads', lostTab:true};
  }
  return {page:parts[0] || 'dash', id:Number(parts[1])};
}

function customer(id) { return data.customers.find(c => c.id === Number(id)); }
function isLostCustomer(c) { return c?.stage === 0 && Boolean(c.lost_at); }
function contract(id) { return data.contracts.find(ct => ct.id === Number(id)); }
function options(items, value, label = 'name') {
  return items.map(item => `<option value="${h(item.id)}" ${String(item.id) === String(value) ? 'selected' : ''}>${h(item[label])}</option>`).join('');
}
function repsOptions(value) {
  const available = data.reps.filter(r => r.selectable || r.id === Number(value));
  return `<option value="">請選擇業務員</option>${available.map(r => `<option value="${r.id}" ${r.id === Number(value) ? 'selected' : ''}>${h(r.name)}${r.selectable ? '' : '（歷史負責人）'} · #${r.id}</option>`).join('')}`;
}

async function reload({close = false} = {}) {
  const sequence = ++refreshSequence;
  const identity = await api('/auth/me');
  if (sequence !== refreshSequence) return;
  authInfo = identity;
  const result = await api(`/state?year=${year}`);
  if (sequence !== refreshSequence) return;
  data = result;
  peopleData = isAdmin() ? await api('/admin/users') : null;
  if (sequence !== refreshSequence) return;
  if (route().page === 'customer' && customer(route().id)) {
    detailFollowups = await api(`/customers/${route().id}/followups`);
    if (sequence !== refreshSequence) return;
  }
  if (close) modal = null;
  render();
}

function openModal(value) {
  previousFocus = document.activeElement;
  modal = value;
  renderModal();
  setTimeout(() => $('#modal-root [autofocus], #modal-root input:not([type=hidden]), #modal-root select, #modal-root .close-btn')?.focus(), 0);
}
function closeModal() {
  if (busy) return;
  modal = null;
  renderModal();
  previousFocus?.isConnected && previousFocus.focus();
}

function render() {
  const r = route();
  let active = r.page;
  if (r.page === 'customer') active = isLostCustomer(customer(r.id)) ? 'leads' : 'customers';
  const links = [['dash','總覽'],['customers','客戶'],['leads','線索池'],['contracts','簽單／合同']];
  if (isAdmin()) links.push(['quota','額度與指標'],['people','人員與身分']);
  let content;
  switch (r.page) {
    case 'customers': content = renderCustomers(); break;
    case 'customer': content = renderDetail(r.id); break;
    case 'leads': content = renderLeads(); break;
    case 'contracts': content = renderContracts(); break;
    case 'quota': content = isAdmin() ? renderQuota() : empty('需要管理員身分','個人簽約統計請查看總覽。'); break;
    case 'people': content = renderPeople(); break;
    default: content = renderDashboard();
  }
  $('#app').innerHTML = `<div class="app-layout"><nav class="sidebar" aria-label="主要功能"><a class="brand" href="#/dash">簽單台<span>.</span></a><div class="nav-links">${links.map(([page,label]) => `<a href="#/${page}" class="nav-link ${active === page ? 'active' : ''}" ${active === page ? 'aria-current="page"' : ''}>${label}</a>`).join('')}</div><div class="sidebar-bottom"><span class="small signed-in-name">已登入 · ${h(authInfo?.user?.name || '')}</span><span class="tiny muted">${h(authInfo?.user?.role_label || '')} · ${scopeLabel()}</span>${isAdmin() ? `<a class="btn btn-ghost" href="${appUrl('/api/v1/backup')}" download>下載完整備份</a>` : ''}${button('重新整理','refresh','','btn-ghost')}${authInfo?.auth_required ? button('登出系統','logout','','btn-ghost') : ''}<span class="tiny muted">資料日期 ${h(data.today)}</span></div></nav><main class="main">${content}</main></div>`;
  if (r.page === 'customers') renderCustomerRows();
  if (r.page === 'contracts') renderContractRows();
  if (r.page === 'quota' && isAdmin()) renderTargetPanel();
  renderModal();
}

function header(title, sub, actions = '') {
  return `<header class="page-header"><div class="grow"><div class="eyebrow">${h(sub)}</div><h1>${h(title)}</h1></div>${actions}</header>`;
}

function renderPeople() {
  if (!isAdmin() || !peopleData) return empty('需要管理員身分','只有管理員可以分配人員身分。');
  const pending = peopleData.users.filter(u => u.role === 'pending').length;
  const roles = [['admin','管理員：查看與管理全體資料'],['sales','業務員：可被分配案件']];
  const legacyOptions = peopleData.legacy_reps.map(r => `<option value="${r.id}">${h(r.name)} · ${r.customers} 位客戶／線索 · ${r.contracts} 份合同</option>`).join('');
  return `<section class="page">${header('人員與身分',`${pending} 人待分配 · ${peopleData.users.length} 位已登入使用者`)}<div class="notice">首次登入身分：${peopleData.initial_roles.map(u => `${h(u.name)}（${h(u.role_label)}）`).join('、')}。其餘人員先用飛書登入，才會出現在此名單；分配為業務員後，自動使用飛書姓名建立業務員資料。</div><div class="account-list">${peopleData.users.map(u => `<article class="account-card ${u.role === 'pending' ? 'pending' : ''}"><div class="row between"><div><strong>${h(u.name)}</strong><span class="small muted"> · 帳號 #${u.id}${u.id === authInfo.user.id ? ' · 你自己' : ''}</span></div><span class="tag ${u.role === 'pending' ? 'tag-accent' : 'tag-outline'}">${h(u.role_label)}</span></div>${u.active ? `<form class="account-role-form account-controls" data-user-id="${u.id}" data-version="${u.role_version}"><fieldset class="role-choices"><legend>系統身分（可複選）</legend>${roles.map(([value,label]) => `<label class="check-line"><input type="checkbox" name="roles" value="${value}" ${u.roles.includes(value) ? 'checked' : ''}><span>${label}</span></label>`).join('')}<small class="muted">全部取消：退回等待分配。</small></fieldset>${!u.rep_id && legacyOptions ? `<div class="field legacy-binding" ${u.roles.includes('sales') ? '' : 'hidden'}><label for="legacy-${u.id}">原有業務資料</label><select class="input" name="legacy_rep_id" id="legacy-${u.id}"><option value="">建立新的業務員資料</option>${legacyOptions}</select></div>` : `<div class="small muted">${u.rep_id ? '已綁定業務員資料；改回業務員時會沿用。' : '分配後立即生效，對方不必重新掃碼。'}</div>`}<button class="btn btn-primary" type="submit">${u.role === 'pending' ? '確認分配' : '儲存身分'}</button></form>` : '<p class="small muted">此帳號已停用。</p>'}</article>`).join('')}</div>${peopleData.legacy_reps.length ? `<div class="notice">有 ${peopleData.legacy_reps.length} 份舊業務員資料尚未綁定。分配業務員身分時，可選擇承接正確的舊資料；原客戶和合同會保留。</div>` : ''}</section>`;
}

function renderProgress(target) {
  return `<div class="progress-grid"><span class="muted">時間進度</span><div class="progress-track"><div class="progress-fill neutral" style="width:${width(target.time_progress * 100)}"></div></div><span class="right mono">${pct(target.time_progress)}</span><span class="muted">完成進度</span><div class="progress-track"><div class="progress-fill" style="width:${width(n(target.done_progress) * 100)}"></div></div><span class="right accent mono">${target.done_progress == null ? '未設定' : pct(target.done_progress)}</span></div>`;
}

function timeline(items, links = false) {
  if (!items.length) return `<div class="empty compact">尚無跟進紀錄</div>`;
  return items.map(f => `<${links ? 'a' : 'div'} ${links ? `href="${customerLink(f.customer_id)}"` : ''} class="timeline-item"><span class="muted mono">${h(f.occurred_on)}</span><div class="stack" style="gap:4px"><div><strong>${links ? h(f.company) + ' · ' : ''}${h(f.kind)}</strong> <span class="muted">${h(f.rep_name)}</span></div><p>${h(f.note)}</p></div></${links ? 'a' : 'div'}>`).join('');
}

function renderDashboard() {
  const s = data.summary, t = data.targets;
  const groups = [1,2,3,4,5].map(stage => {
    const items = data.customers.filter(c => c.stage === stage);
    return {stage, count:items.length, amount:items.reduce((sum,c) => sum + n(stage === 5 ? c.signed_amount : c.pending_amount), 0), items};
  });
  const max = Math.max(1, ...groups.map(g => g.amount));
  return `<section class="page">${header('業務總覽', `${year} 年 · ${scopeLabel()}`, '<a class="btn btn-primary" href="#/leads">前往線索池</a>')}<div class="kpis">${kpi('待簽業務總額',money(s.pending_amount),'客戶預估額或簽約中合同，避免重複計算')}${kpi('本年已簽業務總額',money(t.signed_amount),isAdmin() ? (t.done_progress == null ? '尚未設定年度指標' : `完成年度指標 ${pct(t.done_progress)}`) : '本人負責客戶的簽約金額',true)}${kpi('報價及合同階段', `${data.customers.filter(c => c.stage >= 2 && c.stage <= 4).length} 家`,'客戶可同時有多份合同')}${kpi('待分配線索',`${s.open_leads} 條`,`預估 ${money(s.open_leads_amount)}`)}</div>${isAdmin() ? `<div class="stack" style="gap:16px"><div class="row section-title"><h3 class="grow">年度指標進度</h3><a href="#/quota" class="small muted">年度指標 ${money(t.annual_target)} · 查看月度明細 →</a></div>${renderProgress(t)}</div>` : ''}<div class="two-col"><div><h3 class="section-title">階段漏斗</h3>${groups.map(g => `<a class="funnel-row" href="#/customers" data-stage-link="${g.stage}"><span>${h(data.stages[g.stage])}</span><div class="funnel-bar"><div class="funnel-fill" style="width:${width(g.amount / max * 100)}"></div></div><span class="right mono">${g.count} 家 · ${money(g.amount)}</span></a>`).join('')}<p class="small muted" style="margin-top:12px">依客戶目前階段顯示；年度業績依每份已簽合同計算。</p></div><div><h3 class="section-title">最近跟進</h3>${timeline(data.recent, true)}</div></div>${!data.customers.length ? empty('從第一條線索開始','先登記客戶基本資料，再分配負責業務員。','<a class="btn btn-primary" href="#/leads">登記第一條線索</a>') : ''}</section>`;
}

function renderCustomers() {
  const items = data.customers.filter(c => c.stage > 0);
  return `<section class="page">${header('客戶',`${scopeLabel()} · ${items.length} 家`,`<input type="search" id="customer-search" class="input search" aria-label="搜尋客戶" placeholder="搜尋客戶／聯絡人／業務員" value="${h(customerSearch)}">`)}<div class="tabs">${['全部',...data.stages.slice(1)].map((name,index) => `<button class="tab ${customerFilter === index ? 'active' : ''}" data-action="customer-filter" data-id="${index}">${h(name)} ${index ? items.filter(c => c.stage === index).length : items.length}</button>`).join('')}</div><div id="customer-results"></div></section>`;
}
function renderCustomerRows() {
  const q = customerSearch.trim().toLocaleLowerCase();
  const rows = data.customers.filter(c => c.stage > 0 && (!customerFilter || c.stage === customerFilter) && (!q || `${c.company}${c.contact}${c.rep_name}${c.phone}`.toLocaleLowerCase().includes(q)));
  $('#customer-results').innerHTML = rows.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>客戶</th><th>聯絡人</th><th>負責業務員</th><th>階段</th><th class="right">面聊</th><th class="right">待簽／已簽</th><th>下次跟進</th></tr></thead><tbody>${rows.map(c => `<tr class="clickable" data-action="customer" data-id="${c.id}"><td class="company"><a href="${customerLink(c.id)}">${h(c.company)}</a></td><td>${h(c.contact || '—')}</td><td>${h(c.rep_name)}</td><td>${stageTag(c)}</td><td class="right mono">${c.meetings} 次</td><td class="right mono">${money(c.pending_amount)}<div class="small accent">已簽 ${money(c.signed_amount)}</div></td><td class="small muted mono">${h(c.next_followup || '未設定')}</td></tr>`).join('')}</tbody></table></div>` : empty('目前沒有符合的客戶',data.summary.customers ? '請調整搜尋或階段篩選。' : '在線索池分配負責業務員後，客戶會出現在這裡。','<a href="#/leads" class="btn btn-secondary">前往線索池</a>');
}

function renderDetail(id) {
  const c = customer(id);
  if (!c || (!c.stage && !c.lost_at)) return empty('找不到這位客戶','線索需要先分配負責業務員。','<a href="#/leads" class="btn btn-secondary">前往線索池</a>');
  const contracts = data.contracts.filter(ct => ct.customer_id === id);
  const returned = c.stage === 0;
  const canLose = c.stage > 0 && !contracts.some(ct => ct.status === 'signed');
  const controls = returned ? button('重新分配','assign',id,'btn-primary') : `${button('新增跟進','followup',id)}${c.stage === 1 ? button('記錄面聊','meeting',id) : ''}${c.stage <= 3 ? button(`進入${h(data.stages[c.stage + 1])}`,'advance',id,'btn-primary') : button('新增合同','new-contract',id,'btn-primary')}${canLose ? button('客戶流失／移入流失池','lose',id,'btn-ghost') : ''}`;
  const fields = [['聯絡人',c.contact],['電話',c.phone],['地區',c.region],['行業',c.industry],['來源',c.source],['負責業務員',c.rep_name],['登記日期',shortDate(c.created_at)],['預估金額',money(c.estimate)],['面聊次數',`${c.meetings} 次`],['下次跟進',c.next_followup],['需求描述',c.description]];
  return `<section class="page"><a href="${returned ? '#/leads/lost' : '#/customers'}" class="small accent">← ${returned ? '返回流失池' : '返回客戶列表'}</a>${header(c.company,returned ? '客戶流失 · 由管理員重新分配' : `${c.stage_name} · ${c.rep_name} 負責`,`${button('修改資料','edit-customer',id)}${controls}`)}${returned ? `<div class="notice">已移入流失池，由管理員重新分配。<br>原因：${h(c.loss_reason)}</div>` : ''}<div class="steps">${data.stages.slice(1).map((name,index) => `<div class="step ${index + 1 === c.stage ? 'current' : index + 1 < c.stage ? 'passed' : ''}"><small>0${index + 1}</small><span>${h(name)}</span>${index === 0 ? `<small class="meeting-note">面聊 ${c.meetings} 次</small>` : ''}</div>`).join('')}</div><div class="two-col"><div><h3 class="section-title">客戶資訊</h3>${fields.map(([key,value]) => `<div class="detail-field"><span class="muted">${h(key)}</span><span>${h(value || '—')}</span></div>`).join('')}</div><div><h3 class="section-title">跟進紀錄</h3>${timeline(detailFollowups)}</div></div><div><div class="row section-title"><h3 class="grow">合同 · ${contracts.length} 份</h3>${returned ? '' : button('新增合同','new-contract',id,'btn-primary')}</div><p class="small muted" style="margin-bottom:14px">合同分別記錄狀態和金額。有簽約中的合同時，客戶顯示合同簽約；有效合同全部簽完後顯示簽約完畢；流失合同僅保留歷史，不參與階段及金額計算。</p>${contracts.length ? contractTable(contracts) : empty('尚未建立合同','進入合同階段後，可在這裡新增第一份合同。')}</div></section>`;
}

function renderLeads() {
  const leads = data.customers.filter(c => !isLostCustomer(c));
  const lostCustomers = isAdmin() ? data.customers.filter(isLostCustomer) : [];
  const selected = isAdmin() && route().lostTab ? 'lost' : leadFilter;
  const tabs = [['open','待分配',data.summary.open_leads],['assigned','已分配',data.summary.customers],['all','全部',leads.length]];
  if (isAdmin()) tabs.push(['lost','流失池',lostCustomers.length]);
  const rows = leads.filter(c => leadFilter === 'all' || (leadFilter === 'open' ? c.stage === 0 : c.stage > 0));
  return `<section class="page">${header('線索池',`${data.summary.open_leads} 條待分配 · 分配後進入「案前接觸」`,button('＋ 登記新線索','new-lead','','btn-primary'))}<div class="flow"><div><b>01</b><h4>登記客戶資訊</h4><p>填寫公司、聯絡方式、預估金額與需求。</p></div><div><b>02</b><h4>分配負責業務員</h4><p>線索先留在池中，再選擇由誰負責跟進。</p></div><div><b>03</b><h4>持續跟進與簽約</h4><p>記錄面聊與報價，上傳已簽附件後完成簽約。</p></div></div><div class="tabs">${tabs.map(([value,label,count]) => `<button class="tab ${selected === value ? 'active' : ''}" data-action="lead-filter" data-id="${value}" aria-pressed="${selected === value}">${label} ${count}</button>`).join('')}</div>${selected === 'lost' ? renderLostCustomers(lostCustomers) : rows.length ? `<div class="cards">${rows.map(c => `<article class="card"><div class="row between"><span class="tag ${c.stage ? 'tag-outline' : 'tag-accent'}">${c.stage ? '已分配' : '待分配'}</span><span class="tiny muted mono">${h(shortDate(c.created_at))}</span></div><h3>${h(c.company)}</h3><p class="description">${h(c.description || '尚未填寫需求描述')}</p><div class="card-info">${[['聯絡人',c.contact],['電話',c.phone],['地區',c.region],['行業',c.industry],['來源',c.source],['登記人',c.registered_by_name],['預估金額',money(c.estimate)]].map(([key,value]) => `<span>${key}</span><span>${h(value || '—')}</span>`).join('')}</div><div class="card-actions">${c.stage ? `<div class="small grow">${h(c.rep_name)} 負責</div><a href="${customerLink(c.id)}" class="btn btn-secondary">查看客戶 →</a>` : `${button('分配業務員','assign',c.id,'btn-primary')}${button('修改','edit-customer',c.id)}${c.can_delete ? button('刪除','delete-lead',c.id,'btn-ghost') : ''}`}</div></article>`).join('')}</div>` : empty(leadFilter === 'open' ? '目前沒有待分配線索' : '目前沒有線索','登記後會以卡片呈現，資料會保存在系統中。',button('＋ 登記新線索','new-lead','','btn-primary'))}</section>`;
}

function renderLostCustomers(customers) {
  const rows = [...customers].sort((a,b) => b.lost_at.localeCompare(a.lost_at) || b.id - a.id);
  const cards = rows.map(c => `<article class="card">
    <div class="row between"><span class="tag tag-outline">客戶流失</span><span class="tiny muted mono">${h(shortDate(c.lost_at))}</span></div>
    <h3>${h(c.company)}</h3>
    <p class="description">${h(c.loss_reason)}</p>
    <div class="card-info"><span>聯絡人</span><span>${h(c.contact || '—')}</span><span>電話</span><span>${h(c.phone || '—')}</span></div>
    <div class="card-actions"><a href="${customerLink(c.id)}" class="btn btn-secondary">查看歷史紀錄</a>${button('重新分配','assign',c.id,'btn-primary')}</div>
  </article>`).join('');
  return cards ? `<div class="cards">${cards}</div>` : empty('目前沒有流失客戶','標記流失的客戶會集中顯示在這裡。');
}

function contractTable(rows) {
  return `<div class="table-wrap"><table class="table" style="min-width:870px"><thead><tr><th>合同編號</th><th>客戶／合同</th><th>負責業務員</th><th>狀態</th><th class="right">合同金額</th><th>簽約日期</th><th>已簽附件</th></tr></thead><tbody>${rows.map(ct => `<tr class="clickable" data-action="viewer" data-id="${ct.id}"><td class="small muted mono">${h(ct.number)}</td><td><button class="btn btn-ghost" data-action="viewer" data-id="${ct.id}" style="padding:0;text-align:left;color:inherit">${h(ct.company)}</button><div class="small muted">${h(ct.title)}</div></td><td>${h(ct.rep_name)}</td><td>${statusTag(ct)}</td><td class="right mono">${money(ct.amount)}</td><td class="mono small">${h(ct.signed_at || '—')}</td><td class="small ${ct.attachments.some(a => a.kind === 'signed') ? '' : 'accent'}">${ct.attachments.some(a => a.kind === 'signed') ? '已上傳' : '未上傳'} →</td></tr>`).join('')}</tbody></table></div>`;
}
function renderContracts() {
  const signed = data.contracts.filter(ct => ct.status === 'signed'), signing = data.contracts.filter(ct => ct.status === 'signing');
  return `<section class="page">${header('簽單／合同','全部團隊 · 同一客戶可以有多份合同',button('＋ 新增合同','new-contract','','btn-primary'))}<div class="kpis" style="grid-template-columns:repeat(3,minmax(0,1fr))">${kpi('合同總數',`${data.contracts.length} 份`)}${kpi('簽約完畢金額',money(signed.reduce((sum,ct) => sum+n(ct.amount),0)),`${signed.length} 份 · 所有年度`,true)}${kpi('簽約中金額',money(signing.reduce((sum,ct) => sum+n(ct.amount),0)),`${signing.length} 份`)}</div><div class="row between"><div class="tabs">${[['','全部'],['signing','簽約中'],['signed','簽約完畢'],['lost','合同流失']].map(([value,label]) => `<button class="tab ${contractFilter === value ? 'active' : ''}" data-action="contract-filter" data-id="${value}">${label}</button>`).join('')}</div><input id="contract-search" type="search" class="input search" aria-label="搜尋合同" placeholder="搜尋合同編號／客戶／合同名稱" value="${h(contractSearch)}"></div><div id="contract-results"></div></section>`;
}
function renderContractRows() {
  const q = contractSearch.trim().toLocaleLowerCase();
  const rows = data.contracts.filter(ct => (!contractFilter || ct.status === contractFilter) && (!q || `${ct.number}${ct.company}${ct.title}${ct.rep_name}`.toLocaleLowerCase().includes(q)));
  $('#contract-results').innerHTML = rows.length ? contractTable(rows) : empty('目前沒有符合的合同','請先分配客戶，再建立合同；上傳已簽附件後即可完成簽約。');
}

function yearSelect() {
  const current = Number(data.today.slice(0,4));
  const years = [...new Set([...Array.from({length:8},(_,i) => current - 5 + i),year])].sort((a,b) => b-a);
  return `<select id="year-select" class="input year-select" aria-label="統計年度">${years.map(y => `<option value="${y}" ${y === year ? 'selected' : ''}>${y} 年</option>`).join('')}</select>`;
}
function renderQuota() {
  return `<section class="page">${header('額度與指標',`截至 ${data.today} · 金額單位：萬元`,yearSelect())}<div class="stack gap24"><div class="row between section-title"><h3>年度業務指標 vs 實際完成</h3><form id="target-form" class="row target-form"><label for="target-input" class="small muted">年度指標（萬元）</label><input id="target-input" name="amount" type="number" min="0" step="0.000001" required class="input target-input" value="${h(targetDraft ?? data.targets.annual_target)}">${button('儲存指標','save-target','','btn-primary')}</form></div><div id="target-status" class="target-status">${targetDraft == null ? '指標按年度保存；輸入金額後即時試算，按「儲存指標」保存。' : '目前是試算結果，尚未儲存。'}</div><div id="target-panel" class="stack gap24"></div></div><h3 class="section-title">業務池：待簽 vs 已簽</h3>${renderPool()}<div><h3 class="section-title">業務員金額統計</h3><p class="small muted" style="margin-bottom:12px">待簽為目前業務池；已簽為所有年度，與上方業務池一致。</p>${renderPerformance()}</div></section>`;
}
function calculatedTarget() {
  const t = {...data.targets};
  if (targetDraft == null) return t;
  t.annual_target = Math.max(0,n(targetDraft));
  t.monthly_plan = t.annual_target / 12;
  t.expected_amount = t.annual_target * t.time_progress;
  t.gap = n(t.signed_amount) - t.expected_amount;
  t.done_progress = t.annual_target ? n(t.signed_amount)/t.annual_target : null;
  t.months = t.months.map(m => ({...m, plan:t.monthly_plan, difference:n(m.actual)-t.monthly_plan}));
  return t;
}
function renderTargetPanel() {
  const t = calculatedTarget();
  const max = Math.max(1,n(t.monthly_plan),...t.months.map(m => n(m.actual)))*1.15;
  const month = Number(data.today.slice(5,7)), currentYear = Number(data.today.slice(0,4));
  $('#target-panel').innerHTML = `<div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">${kpi('年度業務指標',money(t.annual_target))}${kpi('月度計畫（均攤）',computedMoney(t.monthly_plan))}${kpi('截至今日應完成',computedMoney(t.expected_amount))}${kpi('年度累計已簽',money(t.signed_amount),'按實際簽約日期歸屬',true)}${kpi(n(t.gap)>=0 ? '超前進度' : '落後進度',computedMoney(Math.abs(n(t.gap))))}</div>${renderProgress(t)}<div class="chart-scroll"><div class="month-chart">${t.months.map(m => `<div class="month-cell"><div class="month-plot"><div class="plan-line" style="bottom:${width(n(m.plan)/max*100)}"></div><div class="month-bar ${year===currentYear && m.month===month ? 'current' : ''}" style="height:${width(n(m.actual)/max*100)}"></div></div><div class="month-label">${m.month} 月</div><div class="month-value">${number(m.actual)}</div><div class="month-diff ${n(m.difference)<0 ? 'accent' : ''}">${n(m.difference)>=0 ? '+' : ''}${rounded(m.difference)}</div></div>`).join('')}</div></div><div class="row small muted" style="gap:22px"><span><i class="swatch" style="background:#444141"></i> 當月實際簽約</span><span>┄ 月度計畫</span><span>第三行為與計畫的差額</span></div>`;
}
function renderPool() {
  const p = n(data.summary.pending_amount), s = n(data.summary.signed_amount), total = p+s;
  return `<div class="stack" style="gap:18px"><div class="pool-track"><div style="width:${width(total ? p/total*100 : 0)};background:#444141"></div><div style="width:${width(total ? s/total*100 : 0)};background:var(--color-accent)"></div></div><div class="pool-legend"><div><i class="swatch" style="background:#444141"></i><span class="small muted">待簽業務總額</span><h2>${money(p)}</h2></div><div><i class="swatch"></i><span class="small muted">已簽業務總額（所有年度）</span><h2>${money(s)}</h2></div></div><p class="small muted">未建合同的已分配客戶計入預估額；建立合同後改以簽約中合同計算，線索池未分配金額不計入。</p></div>`;
}
function renderPerformance() {
  if (!data.reps.length) return empty('尚未設定業務員','在線索分配時新增業務員姓名，就會開始統計。',button('管理業務員','reps','','btn-secondary'));
  const rows = [...data.performance].sort((a,b) => n(b.pending)+n(b.signed)-n(a.pending)-n(a.signed));
  const max = Math.max(1,...rows.map(r => n(r.pending)+n(r.signed)));
  return `<div class="performance-row performance-head"><span>業務員</span><span>已簽 ■ ／待簽 □</span><span class="right">已簽</span><span class="right">待簽</span></div>${rows.map(r => `<div class="performance-row"><strong>${h(r.name)}</strong><div class="performance-bars"><div style="width:${width(n(r.signed)/max*100)};background:var(--color-accent)"></div><div style="width:${width(n(r.pending)/max*100)};background:var(--light)"></div></div><span class="right accent mono">${money(r.signed)}</span><span class="right mono">${money(r.pending)}</span></div>`).join('')}`;
}

function field(label, name, value = '', {type='text',required=false,full=false,placeholder='',step='',max='',min=''} = {}) {
  return `<div class="field ${full ? 'full' : ''}"><label for="field-${h(name)}">${h(label)}${required ? ' *' : ''}</label><input id="field-${h(name)}" class="input" name="${h(name)}" type="${type}" value="${h(value)}" ${required ? 'required' : ''} ${type==='text' ? `maxlength="${name==='phone' ? 80 : 200}"` : ''} ${step ? `step="${step}"` : ''} ${max ? `max="${max}"` : ''} ${min !== '' ? `min="${min}"` : ''} placeholder="${h(placeholder)}"></div>`;
}
function selectField(label, name, inner, full = false) {
  return `<div class="field ${full ? 'full' : ''}"><label for="field-${name}">${label} *</label><select id="field-${name}" class="input" name="${name}" required>${inner}</select></div>`;
}
function textareaField(label, name, value = '') {
  return `<div class="field full"><label for="field-${name}">${h(label)}</label><textarea id="field-${name}" class="input" name="${name}" maxlength="10000">${h(value)}</textarea></div>`;
}
function formShell(title, content, submit = '儲存', extra = '') {
  return `<div class="dialog-header"><h2 class="dialog-title" id="dialog-title">${title}</h2><button class="close-btn" data-action="close" aria-label="關閉">×</button></div><form id="modal-form" class="stack gap24">${content}<div class="form-error" id="form-error" role="alert"></div><div class="dialog-actions">${extra}${button('取消','close')}<button class="btn btn-primary" type="submit">${submit}</button></div></form>`;
}
function leadForm(c) {
  const sources = ['轉介紹','老客戶','展會','官網','電話外呼','其他'];
  if (c?.source && !sources.includes(c.source)) sources.unshift(c.source);
  return formShell(c ? '修改客戶資料' : '登記新線索', `<div class="form-grid">${field('公司名稱','company',c?.company || '',{required:true,full:true})}${field('聯絡人','contact',c?.contact)}${field('聯絡電話','phone',c?.phone)}${field('地區','region',c?.region)}${field('行業','industry',c?.industry)}${field('預估金額（萬元）','estimate',c?.estimate || '0',{type:'number',min:'0',step:'0.000001'})}<div class="field"><label for="field-source">線索來源</label><select id="field-source" class="input" name="source">${sources.map(s => `<option ${c?.source===s ? 'selected' : ''}>${h(s)}</option>`).join('')}</select></div>${c?.stage ? selectField('負責業務員','rep_id',repsOptions(c.rep_id)) : ''}${c?.stage ? field('下次跟進','next_followup',c.next_followup,{type:'date'}) : ''}${textareaField('需求描述','description',c?.description)}</div>${!c ? '<p class="small muted">登記後先進入線索池，稍後再分配負責業務員。</p>' : ''}`,c ? '儲存修改' : '提交到線索池');
}

function renderModal() {
  const root = $('#modal-root');
  document.body.classList.toggle('modal-open', !!modal);
  if (!modal) { root.innerHTML = ''; return; }
  let html, wide = false;
  const c = customer(modal.id), ct = contract(modal.id);
  switch (modal.type) {
    case 'lead': html = leadForm(null); break;
    case 'customer':
      if (!c) { modal=null; renderModal(); return; }
      html = leadForm(c); break;
    case 'lose':
      html = formShell('客戶流失／移入流失池',`<p>${h(c?.company)}</p><div class="notice">確認後解除目前負責人並移入流失池，由管理員管理及重新分配。未簽合同將標記為流失、不再計入待簽金額；歷史資料與附件保留。</div><div class="field"><label for="loss-reason">流失原因（必填）</label><textarea id="loss-reason" name="reason" class="input" required maxlength="2000" rows="4"></textarea></div>`,'確認移入流失池'); break;
    case 'assign':
      html = formShell('分配負責業務員',`<p>${h(c?.company)}</p>${selectField('負責業務員','rep_id',repsOptions())}<p class="small muted">${data.reps.some(r => r.selectable) ? '名單來自已通過身分分配的飛書帳號。' : '尚無可分配的業務員，請先由管理員分配身分。'}${isAdmin() ? ' <a class="accent" href="#/people">前往人員與身分</a>' : ''}</p><p class="small muted">確認後進入「案前接觸」，流失退回的客戶重新分配後，從案前接觸開始跟進。</p>`,'確認分配'); break;
    case 'followup':
      html = formShell(modal.meeting ? '記錄一次面聊' : '新增跟進紀錄',`<p>${h(c?.company)}</p><div class="form-grid">${selectField('跟進類型','kind',['面聊','電話／微信溝通','報價溝通','合同溝通','其他'].map(kind => `<option ${kind === (modal.meeting ? '面聊' : '電話／微信溝通') ? 'selected' : ''}>${kind}</option>`).join(''))}${field('發生日期','occurred_on',data.today,{type:'date',required:true,max:data.today})}${textareaField('溝通內容','note')}</div><p class="small muted">面聊次數依已儲存的面聊紀錄自動計算。</p>`); break;
    case 'contract': {
      const existing = modal.edit ? ct : null;
      const eligible = data.customers.filter(c => c.stage > 0);
      if (!eligible.length) { html = `<h2 class="dialog-title" id="dialog-title">請先分配一位客戶</h2><p class="muted">在線索池分配業務員後，即可新增合同。</p><div class="dialog-actions">${button('關閉','close')}<a class="btn btn-primary" href="#/leads">前往線索池</a></div>`; break; }
      const selectedCustomer = existing ? customer(existing.customer_id) : customer(modal.customerId);
      html = formShell(existing ? '修改合同' : '新增合同',`<div class="form-grid">${existing ? `<div class="notice full">${h(existing.number)} · ${h(existing.company)}</div>` : selectField('客戶','customer_id',`<option value="">請選擇客戶</option>${options(eligible,modal.customerId,'company')}`,true)}${field('合同名稱','title',existing?.title || '',{required:true,full:true})}${field('合同金額（萬元）','amount',existing?.amount || '',{type:'number',required:true,min:'0.000001',step:'0.000001'})}${selectField('合同負責業務員','rep_id',repsOptions(existing?.rep_id || selectedCustomer?.rep_id))}${existing?.status==='signed' ? field('實際簽約日期','signed_at',existing.signed_at,{type:'date',required:true,max:data.today}) : ''}</div><p class="small muted">${existing?.status==='signed' ? '修改金額或日期後，相關業績統計會同步更新。' : '新增後狀態為「簽約中」，上傳已簽合同附件後才能完成簽約。'}</p>`,existing ? '儲存修改' : '建立合同'); break;
    }
    case 'complete':
      html = formShell('確認完成簽約',`<div class="notice">${h(ct?.number)} · ${h(ct?.company)}<br>${h(ct?.title)} · ${money(ct?.amount)}</div>${field('實際簽約日期','signed_at',data.today,{type:'date',required:true,max:data.today})}<p class="small muted">金額計入所填日期的月份及年度；上傳日期不影響業績歸屬。</p><label class="check-line"><input name="confirmed" type="checkbox" required><span>確認上傳的是已簽合同，金額及簽約日期正確。</span></label>`,'完成簽約',button('返回合同','viewer',modal.id)); break;
    case 'viewer':
      if (!ct) { modal=null; renderModal(); return; }
      html = renderViewer(ct); wide=true; break;
    default: return;
  }
  root.innerHTML = `<div class="dialog-backdrop"><section class="dialog ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="dialog-title">${html}</section></div>`;
}

function renderViewer(ct) {
  const files = ct.attachments;
  const file = files.find(a => a.id === modal.fileId) || files[0];
  if (file) modal.fileId = file.id;
  const original = files.some(a => a.kind === 'signed');
  let preview;
  if (!file) preview = '<div class="preview"><span class="muted small">尚未上傳附件</span></div>';
  else if (file.mime === 'application/pdf') preview = `<div class="preview"><iframe src="${h(file.url)}#toolbar=1" title="${h(file.name)}"></iframe></div>`;
  else if (file.mime.startsWith('image/')) preview = `<div class="preview"><img src="${h(file.url)}" alt="${h(file.name)}"></div>`;
  else preview = '<div class="preview"><div class="stack" style="padding:24px;text-align:center"><strong>此格式請下載後開啟</strong><span class="small muted">PDF 與圖片可直接在此預覽；Word 文件可下載檢視。</span></div></div>';
  return `<div class="dialog-header"><div class="stack" style="gap:6px"><span class="small muted mono">${h(ct.number)} · ${h(ct.rep_name)} 負責</span><h2 id="dialog-title" class="dialog-title">${h(ct.company)}</h2><span>${h(ct.title)}</span><div class="row small">${statusTag(ct)}<span>合同金額 ${money(ct.amount)}</span><span>簽約日期 ${h(ct.signed_at || '—')}</span></div></div><button class="close-btn" data-action="close" aria-label="關閉">×</button></div><div class="row">${ct.status === 'lost' ? '' : button('修改合同','edit-contract',ct.id)}<a href="${customerLink(ct.customer_id)}" class="btn btn-secondary">客戶詳情</a>${ct.status==='signing' ? button('完成簽約','complete',ct.id,'btn-primary',original ? '' : 'disabled title="請先上傳已簽合同附件"') : ''}${ct.status==='signing' ? button('刪除合同','delete-contract',ct.id,'btn-ghost') : ''}</div>${ct.status==='signing' && !original ? '<div class="notice accent">尚未上傳已簽合同。請選擇附件類型「已簽合同」並上傳，再手動確認完成簽約。</div>' : ''}<div class="file-layout"><div class="stack"><div class="file-list">${files.map(a => `<button type="button" class="file-item ${a.id===file?.id ? 'active' : ''}" data-action="file" data-id="${h(a.id)}"><span class="file-name">${h(a.name)}</span><small>${h(a.kind_label)} · ${fileSize(a.size)} · ${shortDate(a.created_at)}</small></button>`).join('') || '<p class="muted small" style="padding:16px 0">沒有附件</p>'}</div>${ct.status === 'lost' ? `<div class="notice">合同已流失，僅供查閱。<br>原因：${h(ct.loss_reason)}</div>` : `<form id="upload-form" class="upload-box"><strong>上傳附件</strong><label for="upload-kind">附件類型</label><select class="input" name="kind" id="upload-kind">${[['signed','已簽合同'],['draft','合同草案'],['quote','報價單'],['other','其他附件']].map(([value,label]) => `<option value="${value}">${label}</option>`).join('')}</select><label for="upload-file">選擇檔案</label><input id="upload-file" name="file" type="file" required accept=".pdf,.png,.jpg,.jpeg,.doc,.docx"><span class="tiny muted">PDF／圖片／Word · 單檔上限 20 MB</span><div id="form-error" class="form-error" role="alert"></div><button class="btn btn-primary" type="submit">上傳附件</button></form>`}</div><div class="stack"><div class="row"><strong class="grow small">${h(file?.name || '文件預覽')}</strong></div>${file ? `<div class="file-actions">${file.mime.startsWith('image/') || file.mime==='application/pdf' ? `<a class="btn btn-secondary" href="${h(file.url)}" target="_blank" rel="noopener">新視窗開啟</a>` : ''}<a class="btn btn-primary" href="${h(file.url)}?download=1" download>下載</a>${ct.status === 'lost' ? '' : button('刪除此附件','delete-file',file.id,'btn-ghost')}</div>` : ''}${preview}</div></div>`;
}

function errorMessage(error) {
  const el = $('#form-error');
  if (el) { el.textContent = error.message || '操作失敗，請稍後再試'; el.scrollIntoView({block:'nearest'}); }
  else toast(error.message || '操作失敗，請稍後再試');
}
async function mutate(work, {message='已儲存', close=true} = {}) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('#modal-root button, #modal-root input, #modal-root select, #modal-root textarea').forEach(el => el.disabled = true);
  let committed = false;
  try {
    await work();
    committed = true;
    await reload({close});
    toast(message);
  } catch (error) {
    if (authRedirecting) return;
    if (committed) {
      if (close) { modal=null; renderModal(); }
      toast('資料已儲存，但畫面更新失敗；請按重新整理查看。');
    } else errorMessage(error);
  } finally {
    busy = false;
    document.querySelectorAll('#modal-root button, #modal-root input, #modal-root select, #modal-root textarea').forEach(el => el.disabled = false);
    if (modal?.type==='viewer') {
      const ct = contract(modal.id);
      const btn = $('#modal-root [data-action="complete"]');
      if (btn && ct) btn.disabled = !ct.attachments.some(a => a.kind === 'signed');
    }
  }
}

document.addEventListener('click', async event => {
  const stageLink = event.target.closest('[data-stage-link]');
  if (stageLink) { customerFilter=Number(stageLink.dataset.stageLink); customerSearch=''; }
  const el = event.target.closest('[data-action]');
  if (!el || !data) return;
  if (el.disabled) return;
  const action = el.dataset.action, id = el.dataset.id;
  if (busy && !['close'].includes(action)) return;
  switch(action) {
    case 'logout':
      try {
        const result = await api('/auth/logout',{method:'POST',body:{}});
        returnToLogin(result.login_url);
      } catch(error) { if (!authRedirecting) errorMessage(error); }
      break;
    case 'close': closeModal(); break;
    case 'refresh':
      try { await reload(); toast('資料已更新'); } catch(error) { errorMessage(error); } break;
    case 'customer': location.hash=customerLink(id); break;
    case 'customer-filter': customerFilter=Number(id); render(); break;
    case 'contract-filter': contractFilter=id; render(); break;
    case 'lead-filter':
      if (id === 'lost') {
        location.hash = '#/leads/lost';
      } else {
        leadFilter = id;
        if (route().lostTab) location.hash = '#/leads';
        else render();
      }
      break;
    case 'new-lead': openModal({type:'lead'}); break;
    case 'edit-customer': openModal({type:'customer',id:Number(id)}); break;
    case 'lose': openModal({type:'lose',id:Number(id)}); break;
    case 'assign': openModal({type:'assign',id:Number(id)}); break;
    case 'followup': case 'meeting': openModal({type:'followup',id:Number(id),meeting:action==='meeting'}); break;
    case 'new-contract': openModal({type:'contract',customerId:Number(id)||null}); break;
    case 'edit-contract': openModal({type:'contract',id:Number(id),edit:true}); break;
    case 'viewer': openModal({type:'viewer',id:Number(id)}); break;
    case 'complete': openModal({type:'complete',id:Number(id)}); break;
    case 'file': modal.fileId=id; renderModal(); break;
    case 'advance': {
      const c = customer(id);
      await mutate(() => api(`/customers/${id}/advance`,{method:'POST',body:{version:c.version}}),{message:'已推進階段',close:false}); break;
    }
    case 'delete-lead': {
      const c = customer(id);
      if (!confirm(`確定刪除「${c.company}」這條尚未分配的線索？`)) return;
      await mutate(() => api(`/leads/${id}`,{method:'DELETE',body:{version:c.version}}),{message:'線索已刪除'}); break;
    }
    case 'delete-contract': {
      const ct = contract(id);
      if (!confirm(`確定刪除簽約中的合同「${ct.title}」？`)) return;
      await mutate(() => api(`/contracts/${id}`,{method:'DELETE',body:{version:ct.version}}),{message:'合同已刪除'}); break;
    }
    case 'delete-file':
      if (!confirm('確定移除此附件？已簽合同必須保留至少一份已簽附件。')) return;
      await mutate(() => api(`/attachments/${id}`,{method:'DELETE'}),{message:'附件已移除',close:false}); break;
    case 'save-target': await saveTarget(); break;
  }
});

document.addEventListener('input', event => {
  if (event.target.id === 'customer-search') { customerSearch=event.target.value; renderCustomerRows(); }
  if (event.target.id === 'contract-search') { contractSearch=event.target.value; renderContractRows(); }
  if (event.target.id === 'target-input') {
    targetDraft=event.target.value;
    $('#target-status').textContent='目前是試算結果，尚未儲存。';
    renderTargetPanel();
  }
});
document.addEventListener('change', async event => {
  if (event.target.id === 'year-select') {
    year=Number(event.target.value); targetDraft=null;
    try { await reload(); } catch(error) { errorMessage(error); }
  }
  if (event.target.name === 'customer_id' && modal?.type === 'contract' && !modal.edit) {
    const c=customer(event.target.value), repSelect=$('#field-rep_id');
    if (c && repSelect) repSelect.value=String(c.rep_id);
  }
});

async function saveTarget() {
  const form = $('#target-form');
  if (!form?.reportValidity()) return;
  const amount=$('#target-input').value;
  await mutate(async () => {
    await api(`/targets/${year}`,{method:'PUT',body:{amount}});
    targetDraft=null;
  },{message:'年度指標已儲存',close:false});
}

document.addEventListener('change', event => {
  if (event.target.name !== 'roles' || !event.target.closest('.account-role-form')) return;
  const legacy = event.target.closest('form').querySelector('.legacy-binding');
  if (legacy) legacy.hidden = !event.target.closest('form').querySelector('[name=roles][value=sales]').checked;
});

document.addEventListener('submit', async event => {
  if (!event.target.matches('.account-role-form')) return;
  event.preventDefault();
  if (busy) return;
  const form = event.target;
  const values = Object.fromEntries(new FormData(form));
  const selectedRoles = new FormData(form).getAll('roles');
  const payload = {roles:selectedRoles, version:Number(form.dataset.version)};
  if (selectedRoles.includes('sales') && values.legacy_rep_id) payload.legacy_rep_id = Number(values.legacy_rep_id);
  form.querySelectorAll('button,select,input').forEach(el => el.disabled = true);
  try {
    await mutate(() => api(`/admin/users/${form.dataset.userId}/role`, {method:'POST',body:payload}),
                 {message:'身分已更新，立即生效',close:false});
  } finally {
    if (form.isConnected) form.querySelectorAll('button,select,input').forEach(el => el.disabled = false);
  }
});

document.addEventListener('submit', async event => {
  if (!['modal-form','upload-form','target-form'].includes(event.target.id)) return;
  event.preventDefault();
  if (busy) return;
  if (event.target.id==='target-form') { await saveTarget(); return; }
  const values=Object.fromEntries(new FormData(event.target));
  if (event.target.id==='upload-form') {
    const payload=new FormData(event.target);
    await mutate(() => api(`/contracts/${modal.id}/attachments`,{method:'POST',body:payload}),{message:'附件已上傳',close:false});
    return;
  }
  if (!modal) return;
  const current={...modal};
  switch(current.type) {
    case 'lead':
      await mutate(async () => {
        await api('/leads',{method:'POST',body:values});
        leadFilter = 'open';
        if (route().lostTab) location.hash = '#/leads';
      },{message:'已登記到線索池'}); break;
    case 'customer':
      if ('rep_id' in values) values.rep_id=Number(values.rep_id);
      await mutate(() => api(`/customers/${current.id}`,{method:'PATCH',body:{...values,version:customer(current.id).version}}),{message:'客戶資料已更新'}); break;
    case 'lose':
      await mutate(async () => { await api(`/customers/${current.id}/lose`,{method:'POST',body:{reason:values.reason,version:customer(current.id).version}}); location.hash=isAdmin() ? '#/leads/lost' : '#/customers'; },{message:'客戶已移入流失池'}); break;
    case 'assign':
      await mutate(() => api(`/leads/${current.id}/assign`,{method:'POST',body:{rep_id:Number(values.rep_id)}}),{message:'已分配業務員，可至客戶列表跟進'}); break;
    case 'followup':
      await mutate(() => api(`/customers/${current.id}/followups`,{method:'POST',body:values}),{message:'跟進紀錄已儲存'}); break;
    case 'contract':
      values.rep_id=Number(values.rep_id);
      if (current.edit) {
        await mutate(() => api(`/contracts/${current.id}`,{method:'PATCH',body:{...values,version:contract(current.id).version}}),{message:'合同資料已更新'});
      } else {
        values.customer_id=Number(values.customer_id);
        await mutate(async () => { const result=await api('/contracts',{method:'POST',body:values}); modal={type:'viewer',id:result.id}; },{message:'合同已建立，請上傳附件',close:false});
      }
      break;
    case 'complete':
      await mutate(async () => {
        await api(`/contracts/${current.id}/complete`,{method:'POST',body:{signed_at:values.signed_at,version:contract(current.id).version}});
        modal={type:'viewer',id:current.id};
      },{message:'已完成簽約，業績已更新',close:false}); break;
  }
});

document.addEventListener('keydown', event => {
  if (!modal) return;
  if (event.key==='Escape') { event.preventDefault(); closeModal(); }
  if (event.key==='Tab') {
    const focusable=[...document.querySelectorAll('#modal-root button:not(:disabled),#modal-root a[href],#modal-root input:not(:disabled),#modal-root select:not(:disabled),#modal-root textarea:not(:disabled)')].filter(el => el.offsetParent !== null);
    const first=focusable[0], last=focusable[focusable.length-1];
    if (event.shiftKey && document.activeElement===first) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && document.activeElement===last) { event.preventDefault(); first?.focus(); }
  }
});
window.addEventListener('hashchange', async () => {
  modal=null; renderModal();
  if (!data) return;
  if (['customer','people'].includes(route().page)) {
    try { await reload(); } catch(error) { toast(error.message); }
  } else render();
  window.scrollTo(0,0);
});
window.addEventListener('beforeunload', event => {
  if (!authRedirecting && (targetDraft != null || busy)) { event.preventDefault(); event.returnValue=''; }
});
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });

async function start() {
  try { authInfo = await api('/auth/me'); await reload(); }
  catch(error) {
    if (authRedirecting) return;
    $('#app').innerHTML=`<div class="initial-loading"><h2>暫時無法載入資料</h2><p style="margin:16px 0">${h(error.message)}</p><button class="btn btn-primary" id="initial-retry">重新連線</button></div>`;
    $('#initial-retry').addEventListener('click',start);
  }
}
start();
