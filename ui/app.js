
/* =========================== Tab 切换 =========================== */
const PANEL_NAMES = ['history', 'query', 'account', 'notify', 'schedule'];
let schedTimer = null;

function switchTab(name){
  PANEL_NAMES.forEach(function(k){
    const panel = document.getElementById('tab-' + k);
    if (panel) {
      panel.classList.toggle('on', k === name);
    }
    const btn = document.querySelector('.nav button[data-tab="' + k + '"]');
    if (btn) btn.classList.toggle('on', k === name);
  });

  if (name === 'query') loadQuery();
  if (name === 'notify') { loadPush(); loadPushPreview(); loadPushRecent(); }
  if (name === 'schedule') { loadSchedule(); startSchedPoll(); loadThresholds(); loadAutostart(); loadRunLog(); } else { stopSchedPoll(); }
  if (name === 'account') { loadDevice(); loadAccounts(); refreshNotice(); }
  if (name === 'history') { loadHistory(); loadLive(false); }
}

function startSchedPoll(){
  stopSchedPoll();
  schedTimer = setInterval(loadSchedule, 3000);
}
function stopSchedPoll(){
  if (schedTimer) { clearInterval(schedTimer); schedTimer = null; }
}

document.querySelectorAll('.nav button[data-tab]').forEach(function(btn){
  btn.addEventListener('click', function(){ switchTab(this.dataset.tab); });
});

document.addEventListener('click', function(e){
  /* 点击保持高亮：点卡片/折叠框加 .active（蓝描边常驻），点其他区域清除。
     details 折叠框同样适用（dev-card 也是 .card）。
     【死命令：同一事件只绑一次】原独立监听已并入本处理函数 */
  const card = e.target.closest('.card');
  document.querySelectorAll('.card.active').forEach(function(c){
    if (c !== card) c.classList.remove('active');
  });
  if (card) card.classList.add('active');

  const link = e.target.closest('[data-goto]');
  if (!link) return;
  e.preventDefault();
  switchTab(link.dataset.goto);
});

/* =========================== 通用提示 =========================== */
function toast(text, isErr){
  const el = document.getElementById('toast');
  el.textContent = text;
  el.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(function(){ el.className = ''; }, 2800);
}

async function postJSON(url, payload){
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {})
  });
  return r.json();
}

/* =========================== 秒开缓存（localStorage） ===========================
   机制：每次成功拉到数据就落一份到 localStorage；页面一打开先用缓存把 KPI /
   报表 / 实时面板画出来（绝不让用户看 0/0 白屏），真实请求随后在后台静默刷新。
   key 带版本号，结构变更时旧缓存整体失效。 */
const CACHE_V = 'rl-cache-v1';

function cacheGet(key){
  try {
    const raw = localStorage.getItem(CACHE_V + ':' + key);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

function cacheSet(key, val){
  try { localStorage.setItem(CACHE_V + ':' + key, JSON.stringify(val)); } catch (e) {}
}

/* KPI 静默刷新态计数：history 与 live 两路并发，最后一个到手才摘掉转圈 */
let kpiBusyCount = 0;
function kpiBusy(on){
  kpiBusyCount = Math.max(0, kpiBusyCount + (on ? 1 : -1));
  ['k-today', 'k-resin', 'k-cred'].forEach(function(id){
    const el = document.getElementById(id);
    if (el) el.classList.toggle('refreshing', kpiBusyCount > 0);
  });
}

/* 秒开：用上次缓存先把界面画出来。缓存结构异常就当没有，走正常加载。 */
function renderCachedData(){
  const ch = cacheGet('history');
  if (ch && ch.has_data) {
    try {
      renderHistory(ch);
      renderKpiToday(ch);
    } catch (e) { console.warn('缓存报表渲染失败，走正常加载', e); }
  }
  const cl = cacheGet('live');
  if (cl && cl.ok !== false && (cl.games || []).length) {
    try { renderLive(cl); } catch (e) { console.warn('缓存实时面板渲染失败', e); }
  }
}

function escHtml(v){
  return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

/* =========================== 账号绑定 =========================== */
let sid = null, timer = null;

/* 选择卡视觉与 input 状态严格同步：.on = 开启 */
function syncSelCard(box){
  const card = box.closest('.sel-card');
  if (card) card.classList.toggle('on', box.checked);
}

function toggleAll(group, checked){
  document.querySelectorAll('input[name="' + group + '"]').forEach(function(i){
    i.checked = checked;
    syncSelCard(i);
  });
  updateLoginButtons();
  pushEnabled(group,
    [...document.querySelectorAll('input[name="' + group + '"]')].map(function(i){ return i.value; }),
    checked);
}

/* 标题行右侧的「全选」次级按钮：未全开 → 全开；已全开 → 取消全选 */
function toggleAllCards(){
  const boxes = [...document.querySelectorAll('.sel-card input')];
  const target = !boxes.every(function(b){ return b.checked; });
  ['m', 's'].forEach(function(g){
    const gb = [...document.querySelectorAll('input[name="' + g + '"]')];
    if (gb.some(function(b){ return b.checked !== target; })) toggleAll(g, target);
  });
}

function pushEnabled(group, games, enabled){
  postJSON('/api/accounts/enabled', { games: games, enabled: enabled })
    .then(function(d){
      if (!d || !d.ok) { toast((d && d.message) || '设置失败', true); loadAccounts(); return; }
      toast(d.message || (enabled ? '已开启提醒' : '已关闭提醒'));
    })
    .catch(function(){ toast('无法连接本地服务', true); });
}

/* 「全选」按钮文案随状态变化 */
function updateLoginButtons(){
  const boxes = [...document.querySelectorAll('.sel-card input')];
  const allOn = boxes.length > 0 && boxes.every(function(b){ return b.checked; });
  const btn = document.getElementById('btn-selall');
  if (btn) btn.textContent = allOn ? '取消全选' : '全选';
}

/* 从后端拉取真实的巡检开关与凭证状态，覆盖页面默认值 */
function loadAccounts(){
  fetch('/api/accounts').then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok) return;
    /* 存一份映射，扫码选择弹窗按它显示游戏名与凭证状态 */
    window.__accounts = {};
    (d.items || []).forEach(function(it){ window.__accounts[it.id] = it; });
    (d.items || []).forEach(function(it){
      const box = document.querySelector(
        'input[name="m"][value="' + it.id + '"],input[name="s"][value="' + it.id + '"]');
      if (!box) return;
      box.checked = !!it.enabled;
      syncSelCard(box);
    });
    renderCredHealth(d);
    renderKpiCred(d);
    renderCapsuleDots(d);
    updateLoginButtons();
  }).catch(function(){});
}

/* 胶囊黄点：该平台任一游戏未绑定凭证 → 点亮 */
function renderCapsuleDots(d){
  ['m', 's'].forEach(function(g){
    const dot = document.getElementById('cap-dot-' + g);
    if (!dot) return;
    const missing = (d.items || []).some(function(it){
      const box = document.querySelector('input[name="' + g + '"][value="' + it.id + '"]');
      return box && !it.has_credential;
    });
    dot.classList.toggle('on', missing);
  });
}

/* 凭证健康：紧凑列表。绿点 = 已绑定（含从未更新）· 黄点 = 超过 7 天未更新 · 红点 = 未绑定
   色彩纪律：健康一律绿点灰字，只有「过期/未绑定」才给警示色 */
function relTime(text){
  // "2026-10-27 22:00:33" -> "3天前"
  const m = String(text || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return String(text || '');
  const t = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  const diff = Date.now() - t.getTime();
  if (diff < 0) return m[2] + '-' + m[3];
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return min + '分钟前';
  const h = Math.floor(min / 60);
  if (h < 24) return h + '小时前';
  const d = Math.floor(h / 24);
  if (d < 30) return d + '天前';
  return m[2] + '-' + m[3];
}

function credAgeDays(text){
  const m = String(text || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return null;
  const t = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).getTime();
  return (Date.now() - t) / 86400000;
}

function renderCredHealth(d){
  const box = document.getElementById('cred-health');
  const badge = document.getElementById('cred-health-badge');
  if (!box) return;
  const items = (d && d.items) || [];
  if (!items.length) {
    box.innerHTML = '<div class="emptybox">config.json 里没有账号节点。</div>';
    if (badge) badge.textContent = '-';
    return;
  }
  const bound = items.filter(function(i){ return i.has_credential; }).length;
  if (badge) {
    badge.textContent = bound + '/' + items.length + ' 已绑定';
    badge.className = 'badge' + (bound === items.length ? '' : ' warn');
  }
  box.innerHTML = '<div class="cred-list">' +
    items.map(function(it){
      const age = it.has_credential ? credAgeDays(it.credential_updated_at) : null;
      let st, stText, timeTxt;
      if (!it.has_credential) {
        st = 'err'; stText = '未绑定'; timeTxt = '—';
      } else if (age == null) {
        st = 'ok'; stText = '已绑定'; timeTxt = '从未更新';
      } else if (age > 7) {
        st = 'warn'; stText = '超过 7 天未更新'; timeTxt = relTime(it.credential_updated_at);
      } else {
        st = 'ok'; stText = '已绑定'; timeTxt = relTime(it.credential_updated_at);
      }
      return '<div class="cred-row' + (st === 'err' ? ' bad' : '') +
             (st === 'warn' ? ' stale' : '') + '">' +
        selIconHtml(it.id, it.name || it.id) +
        '<div class="cred-info"><b>' + escHtml(it.name || it.id) + '</b>' +
        '<small>' + stText +
          (it.nickname ? ' · ' + escHtml(it.nickname) : '') + '</small></div>' +
        '<span class="cred-dot ' + st + '"></span>' +
        '<span class="cred-time">' + escHtml(timeTxt) + '</span>' +
        '</div>';
    }).join('') + '</div>';
}

/* 选择卡/凭证行的游戏图标：五款游戏都有官方图（终末地自 v1.1.8 起用 App Store 图） */
function selIconHtml(gid, name){
  const base = GAME_ICON[gid];
  if (!base) {
    return '<span class="sel-ico ph">' + escHtml(String(name || gid || '?').slice(0, 1)) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="sel-ico" alt="" src="' + base + '.png' + v +
         '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

/* 把静态占位图替换成官方图标（GAME_ICON 定义后调用一次） */
function fillSelIcons(){
  document.querySelectorAll('.sel-card').forEach(function(card){
    const ph = card.querySelector('.sel-ico.ph');
    if (!ph) return;
    const input = card.querySelector('input');
    if (!input) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = selIconHtml(input.value, ph.textContent);
    const node = tmp.firstChild;
    if (node) ph.replaceWith(node);
  });
}

document.querySelectorAll('.sel-card input').forEach(function(box){
  syncSelCard(box);
  box.addEventListener('change', function(){
    syncSelCard(box);
    updateLoginButtons();
    pushEnabled(box.name, [box.value], box.checked);
  });
});

function badgeFor(state){
  return {
    creating:['准备中',''], waiting:['等待扫码','warn'], scanned:['已扫码','warn'],
    writing:['写入中',''], done:['完成','ok'], error:['失败','err']
  }[state] || [state,''];
}

function setBusy(busy){
  ['cap-m', 'cap-s'].forEach(function(id){
    const b = document.getElementById(id);
    if (b) b.disabled = busy;
  });
}

function render(d){
  const stage = document.getElementById('stage');
  stage.hidden = false;
  const pair = badgeFor(d.state);
  const badge = document.getElementById('badge');
  badge.textContent = pair[0];
  badge.className = 'badge ' + pair[1];
  document.getElementById('msg').textContent = d.message || '';

  const box = document.getElementById('qrbox');
  if (d.qr) {
    if (!box.querySelector('img')) box.innerHTML = '<img alt="登录二维码">';
    box.querySelector('img').src = 'data:image/png;base64,' + d.qr;
  } else if (d.state === 'creating' || d.state === 'writing') {
    box.innerHTML = '<span class="ph"><span class="spin"></span>请稍候…</span>';
  }

  const kv = document.getElementById('detail');
  const items = [];
  if (d.detail) {
    if (d.detail.ltuid) items.push('米游社 ID：<code>' + d.detail.ltuid + '</code>');
    if (d.detail.games) items.push('已更新：' + d.detail.games.join('、'));
    if (d.detail.note) items.push(d.detail.note);
  }
  if (d.results && d.results.length && (d.results.length > 1 || d.state === 'error')) {
    const rows = d.results.map(function(r){
      return '<div><span class="' + (r.ok ? 'i-ok' : 'i-no') + '">' +
             (r.ok ? '✓' : '✗') + '</span>' + r.label +
             (r.note ? ' — ' + r.note : '') + '</div>';
    }).join('');
    items.push('<div class="res">' + rows + '</div>');
  }
  kv.innerHTML = items.join('<br>');
  kv.hidden = items.length === 0;
}

async function start(provider, targets){
  reset();
  setBusy(true);
  document.getElementById('modal-title').textContent =
    provider === 'mihoyo' ? '米游社扫码登录' : '森空岛扫码登录';
  const url = '/api/start?provider=' + encodeURIComponent(provider)
            + '&targets=' + encodeURIComponent(targets.join(','));
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (!d.ok) { showError(d.message); setBusy(false); return; }
    sid = d.sid;
    poll();
  } catch (e) {
    showError('无法连接本地服务：' + e);
    setBusy(false);
  }
}

/* ---------- 两步式扫码：第一步先选游戏，第二步才生成二维码 ---------- */
const PROVIDER_OF = { m: 'mihoyo', s: 'skland' };
const PROVIDER_LABEL = { m: '米游社', s: '森空岛' };
let pickGroup = null;

/* 点胶囊 → 打开选择弹窗（默认勾选已绑定凭证的游戏，未绑定不勾） */
function openPickModal(group){
  pickGroup = group;
  const accs = window.__accounts || {};
  const boxes = [...document.querySelectorAll('input[name="' + group + '"]')];
  document.getElementById('pick-list').innerHTML = boxes.map(function(b){
    const it = accs[b.value] || {};
    const name = it.name || b.value;
    const bound = it.has_credential === true;
    const known = it.has_credential === true || it.has_credential === false;
    const stateText = bound ? '已绑定凭证' : (known ? '未绑定' : '凭证状态未知');
    return '<label class="pick-row">' +
      '<input type="checkbox" value="' + escHtml(b.value) + '"' + (bound ? ' checked' : '') + '>' +
      selIconHtml(b.value, name) +
      '<span class="pick-name">' + escHtml(name) +
        '<small>' + stateText + '</small></span>' +
      '</label>';
  }).join('');
  document.getElementById('pick-title').textContent =
    '选择要登录的游戏 · ' + (PROVIDER_LABEL[group] || '');
  document.getElementById('pick-note').textContent = group === 's'
    ? '森空岛勾选多款时将依次弹出二维码，逐款扫码确认即可。'
    : '一次扫码，登录凭证将写入所有勾选的游戏。';
  updatePickNext();
  document.getElementById('pick-modal').hidden = false;
}

/* 取消/关闭：只收起弹窗，此时还没创建任何扫码会话，无副作用 */
function closePick(){
  document.getElementById('pick-modal').hidden = true;
  pickGroup = null;
}

function pickedGames(){
  return [...document.querySelectorAll('#pick-list input:checked')].map(function(i){
    return i.value;
  });
}

function updatePickNext(){
  const btn = document.getElementById('btn-pick-next');
  if (btn) btn.disabled = pickedGames().length === 0;
}

/* 第二步：拿勾选结果去申请二维码（进入原有的扫码浮层流程） */
function confirmPick(){
  const targets = pickedGames();
  if (!targets.length || !pickGroup) return;
  const group = pickGroup;
  closePick();
  start(PROVIDER_OF[group], targets);
}

function startMihoyo(){
  openPickModal('m');
}

function startSkland(){
  openPickModal('s');
}

function showError(msg){
  const stage = document.getElementById('stage');
  stage.hidden = false;
  const badge = document.getElementById('badge');
  badge.textContent = '失败'; badge.className = 'badge err';
  document.getElementById('msg').textContent = msg;
  document.getElementById('qrbox').innerHTML = '<span class="ph">未能创建二维码</span>';
}

function poll(){
  fetch('/api/state?sid=' + encodeURIComponent(sid))
    .then(function(r){ return r.json(); })
    .then(function(d){
      render(d);
      if (d.state === 'done' || d.state === 'error') { setBusy(false); loadAccounts(); return; }
      timer = setTimeout(poll, 1000);
    })
    .catch(function(){ timer = setTimeout(poll, 2000); });
}

function reset(){
  if (timer) { clearTimeout(timer); timer = null; }
  sid = null;
  setBusy(false);
  document.getElementById('stage').hidden = true;
  document.getElementById('qrbox').innerHTML = '<span class="ph">二维码准备中…</span>';
  document.getElementById('detail').hidden = true;
}

function refreshNotice(){
  fetch('/api/env').then(function(r){ return r.json(); }).then(function(d){
    const box = document.getElementById('notice');
    if (!box) return;
    if (d.configured) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    box.innerHTML =
      '<b>尚未配置推送通道</b>，手机收不到提醒。去 <a data-goto="notify">「通知绑定」</a> 填写。';
  }).catch(function(){});
}

/* ---------- 设备伪装 ---------- */
function renderDevice(d){
  if (!d.ok) { toast(d.message || '读取设备档案失败', true); return; }
  const p = d.profile || {};
  document.getElementById('dev-badge').textContent = d.custom ? '自定义档案' : '默认档案';
  document.getElementById('dev-badge').className = 'badge ' + (d.custom ? 'ok' : '');
  document.getElementById('dev-current').innerHTML =
    '当前设备：<b>' + (p.brand || '?') + ' ' + (p.model || '?') + '</b>' +
    ' · Android ' + (p.os_version || '?') +
    ' · OAID <code>' + (p.oaid || '?') + '</code>' +
    ' · 指纹缓存' + (d.cache_exists ? '存在' : '<b>已清空</b>');

  const apk = d.apk || {};
  const qrBox = document.getElementById('apk-qr');
  const meta = document.getElementById('apk-meta');
  if (apk.available && apk.qr) {
    qrBox.innerHTML = '<img alt="APK 下载二维码">';
    qrBox.querySelector('img').src = 'data:image/png;base64,' + apk.qr;
    let shown = apk.url;
    try { shown = decodeURIComponent(apk.url); } catch (e) {}
    meta.innerHTML = apk.name + '（' + apk.size_text + '）<br>' + shown +
      (apk.warning ? '<br><span style="color:var(--warn)">⚠ ' + apk.warning + '</span>' : '');
  } else {
    qrBox.innerHTML =
      '<div style="padding:10px 8px;text-align:center">' +
      '<div style="font-size:11px;color:var(--muted);line-height:1.5">二维码不可用</div>' +
      '<button class="btn sm" id="btn-apk-retry" style="margin-top:6px">换个端口重试</button>' +
      '</div>';
    meta.innerHTML = '<span style="color:var(--warn)">' + (apk.message || '未找到 APK') + '</span>';
    const btn = document.getElementById('btn-apk-retry');
    if (btn) btn.addEventListener('click', restartApkService);
  }
}

async function restartApkService(){
  const btn = document.getElementById('btn-apk-retry');
  if (btn) { btn.disabled = true; btn.textContent = '重试中…'; }
  try {
    const d = await postJSON('/api/apk/restart', {});
    const box = document.getElementById('apk-qr');
    const meta = document.getElementById('apk-meta');
    if (d.available && d.qr) {
      box.innerHTML = '<img alt="APK 下载二维码">';
      box.querySelector('img').src = 'data:image/png;base64,' + d.qr;
      let shown = d.url;
      try { shown = decodeURIComponent(d.url); } catch (e) {}
      meta.innerHTML = d.name + '（' + d.size_text + '）<br>' + shown;
      toast('下载服务已重启，端口 ' + d.port);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '换个端口重试'; }
      meta.innerHTML = '<span style="color:var(--warn)">' + (d.message || '仍然不可用') + '</span>';
      toast('仍然不可用：' + (d.message || ''), true);
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '换个端口重试'; }
    toast('重启失败：' + e.message, true);
  }
}

function loadDevice(){
  fetch('/api/device').then(function(r){ return r.json(); })
    .then(renderDevice)
    .catch(function(){ toast('无法读取设备档案', true); });
}

/* =========================== 体力溢出阈值 =========================== */
function renderThresholds(d){
  if (!d || !d.ok) { return; }
  const values = d.values || {};
  const defs = d.defaults || {};
  ['genshin','starrail','zzz','arknights','endfield'].forEach(function(gid){
    const box = document.getElementById('resin-' + gid);
    if (!box) return;
    box.value = (values[gid] != null) ? values[gid] : '';
    box.placeholder = (defs[gid] != null) ? String(defs[gid]) : '0';
  });

  const enabled = d.enabled !== false;
  const box = document.getElementById('resin-toggle');
  if (box) box.checked = enabled;

  const item = document.getElementById('acc-resource');
  if (item) item.classList.toggle('is-off', !enabled);

  const hint = document.getElementById('resin-hint');
  if (hint) hint.textContent = '留空用默认值 · 0 = 关闭该游戏';
  refreshSetSummaries();
}

function loadThresholds(){
  fetch('/api/settings').then(function(r){ return r.json(); })
    .then(renderThresholds)
    .catch(function(){
      const hint = document.getElementById('resin-hint');
      if (hint) hint.textContent = '读取失败，请刷新重试';
    });
}

function resinPayload(){
  const box = document.getElementById('resin-toggle');
  return {
    genshin:   document.getElementById('resin-genshin').value,
    starrail:  document.getElementById('resin-starrail').value,
    zzz:       document.getElementById('resin-zzz').value,
    arknights: document.getElementById('resin-arknights').value,
    endfield:  document.getElementById('resin-endfield').value,
    enabled:   box ? box.checked : true
  };
}

async function saveThresholds(){
  const btn = document.getElementById('btn-resin-save');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/settings', resinPayload());
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderThresholds(d);
    toast(d.message || '已保存');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function toggleResourceAlert(){
  const box = document.getElementById('resin-toggle');
  if (!box) return;
  refreshSetSummaries();          // 开关一动，摘要立即跟随（死命令：同一事件只绑一次）
  const want = box.checked;
  box.disabled = true;
  try {
    // 顺带把输入框里的当前值一起提交，避免开关切换把用户刚改的数字丢掉
    const payload = resinPayload();
    payload.enabled = want;
    const d = await postJSON('/api/settings', payload);
    if (!d.ok) {
      box.checked = !want;
      toast(d.message || '设置失败', true);
      return;
    }
    renderThresholds(d);
    toast(want ? '已开启体力溢出提醒' : '已关闭体力溢出提醒（阈值已保留）');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

document.getElementById('btn-resin-save').addEventListener('click', saveThresholds);

async function saveDevice(){
  const btn = document.getElementById('btn-dev-save');
  const text = document.getElementById('dev-text').value;
  if (!text.trim()) { toast('请先粘贴设备信息', true); return; }
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/device', { text: text });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderDevice(d);
    document.getElementById('dev-text').value = '';
    toast(d.message || '设备指纹已替换，请重新扫码');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function resetDevice(){
  if (!confirm('恢复内置默认设备档案？恢复后需要重新扫码。')) return;
  try {
    const d = await postJSON('/api/device/reset', {});
    if (!d.ok) { toast(d.message || '恢复失败', true); return; }
    renderDevice(d);
    toast(d.message || '已恢复默认');
  } catch (e) {
    toast('恢复失败：' + e, true);
  }
}

/* =========================== 通知绑定 =========================== */
const CHANNELS = ['serverchan', 'qmsg'];

function hintText(it){
  if (!it.has_key && !it.enabled) return '未配置';
  if (!it.has_key) return '已启用，但没有 Key';
  if (!it.in_channels) return 'Key 已保存，未启用';
  return '已配置并生效';
}

/* 渠道列表名字右侧的状态指示：Key 输入框有值 → 绿点「已配置」，无值 → 灰点「未配置」。
   数据源是输入框本身：输入时实时变化，服务端数据落框后也走这里统一刷新。 */
function updateChState(name){
  const box = document.getElementById(name + '-key');
  const dotEl = document.getElementById('ch-dot-' + name);
  const stateEl = document.getElementById('ch-state-' + name);
  if (!box || !dotEl || !stateEl) return;
  const has = String(box.value || '').trim().length > 0;
  dotEl.className = 'ch-dot' + (has ? ' ok' : '');
  stateEl.textContent = has ? '已配置' : '未配置';
}

function renderPush(d){
  CHANNELS.forEach(function(name){
    const it = (d.items || {})[name] || {};
    const box = document.getElementById(name + '-key');
    const en = document.getElementById(name + '-enabled');
    const hint = document.getElementById(name + '-hint');
    if (!box || !en || !hint) return;
    box.dataset.masked = it.masked || '';
    box.value = it.masked || '';
    box.placeholder = it.has_key ? ''
      : (name === 'serverchan' ? 'SCT 开头的 SendKey' : 'Qmsg 酱的 key');
    en.checked = !!it.enabled;
    hint.innerHTML = hintText(it);
  });
  const an = document.getElementById('always-notify');
  if (an) an.checked = !!d.always_notify;

  // 左栏渠道列表：名字右侧指示器 = 输入框是否有值（配置明细仍在编辑区 hint 里）
  CHANNELS.forEach(function(name){ updateChState(name); });
  refreshNotice();
}

function loadPush(){
  fetch('/api/push').then(function(r){ return r.json(); }).then(function(d){
    if (!d.ok) { toast(d.message || '读取配置失败', true); return; }
    renderPush(d);
  }).catch(function(){ toast('无法连接本地服务', true); });
}

/* ---------- 三区结构：渠道列表切换 + 最近推送记录 ---------- */
const CHANNEL_NAMES = {
  serverchan: '💬 Server酱（微信）',
  qmsg: '🐧 Qmsg 酱（QQ）'
};
let selChannel = 'serverchan';

function selectChannel(name){
  if (!CHANNEL_NAMES[name]) return;
  selChannel = name;
  document.querySelectorAll('.ch-item').forEach(function(el){
    el.classList.toggle('on', el.dataset.ch === name);
  });
  document.getElementById('ed-serverchan').hidden = name !== 'serverchan';
  document.getElementById('ed-qmsg').hidden = name !== 'qmsg';
  // 重建标题 + (?) 提示（textContent 会把图标一起清掉）
  document.getElementById('ed-title').innerHTML =
    escHtml(CHANNEL_NAMES[name]) +
    ' <span class="q" data-tip="Key 由后端打码显示：不修改输入框 = 保持原值；清空后保存 = 删除该 Key"></span>';
}

document.querySelectorAll('.ch-item').forEach(function(el){
  el.addEventListener('click', function(){ selectChannel(this.dataset.ch); });
});

/* 最近推送记录：读 data/push_history.json（notifier 每次真实发送——成功/失败——
   都落一条结构化记录）。不再解析纯文本日志：console 与 daemon 的日志分家
   （desktop.log / dailycheck.log），按文件筛正则永远可能漏。 */
const PUSH_LABELS = { serverchan: 'Server酱', qmsg: 'Qmsg 酱', bark: 'Bark' };
function loadPushRecent(){
  const box = document.getElementById('pv-recent');
  if (!box) return;
  fetch('/api/push_history').then(function(r){ return r.json(); })
    .then(function(d){
      const items = (d && d.items) || [];
      if (!d || d.ok === false || !items.length) {
        box.innerHTML = '<div>暂无数据。</div>';
        return;
      }
      box.innerHTML = items.map(function(it){
        const ch = PUSH_LABELS[it.channel] || it.channel || '未知渠道';
        const ok = !!it.ok;
        return '<div><span class="' + (ok ? 'i-ok' : 'i-no') + '">' + (ok ? '✓' : '✗') +
          '</span>' + escHtml(String(it.ts || '').replace(/^\d{4}-/, '')) + ' · ' +
          escHtml(ch) + ' · ' + escHtml(it.message || (ok ? '推送成功' : '推送失败')) + '</div>';
      }).join('');
    })
    .catch(function(){ box.innerHTML = '<div>读取推送历史失败，请稍后重试。</div>'; });
}

function collectPush(){
  const out = {};
  CHANNELS.forEach(function(name){
    const box = document.getElementById(name + '-key');
    const en = document.getElementById(name + '-enabled');
    out[name] = { enabled: en.checked, key: box.value };
  });
  const an = document.getElementById('always-notify');
  if (an) out.always_notify = an.checked;
  return out;
}

async function savePush(){
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '保存中…';
  try {
    const d = await postJSON('/api/push', collectPush());
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderPush(d);
    document.getElementById('test-result').hidden = true;
    toast(d.message || '保存成功');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function testPush(){
  const btn = document.getElementById('btn-test');
  const box = document.getElementById('test-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '发送中…';
  box.hidden = true;
  try {
    const d = await postJSON('/api/test_push', collectPush());
    if (!d.ok) { toast(d.message || '发送失败', true); return; }
    const rows = (d.results || []).map(function(x){
      return '<div><span class="' + (x.ok ? 'i-ok' : 'i-no') + '">' +
             (x.ok ? '✓' : '✗') + '</span>' + x.label + ' — ' + x.message + '</div>';
    }).join('');
    box.innerHTML = '<div class="res">' + rows + '</div>';
    box.hidden = false;
    const allOk = (d.results || []).every(function(x){ return x.ok; });
    toast(allOk ? '测试消息已发出' : '部分通道发送失败', !allOk);
  } catch (e) {
    toast('发送失败：' + e, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

CHANNELS.forEach(function(name){
  const box = document.getElementById(name + '-key');
  if (!box) return;
  /* 死命令：只允许 addEventListener。Tab 切换只切 display 不重建 DOM，
     这段在页面加载时只执行一次；dataset 闸门兜底，重复调用不会叠加监听器 */
  if (box.dataset.chBound) return;
  box.dataset.chBound = '1';
  box.addEventListener('focus', function(){
    if (box.value && box.value === box.dataset.masked) box.select();
  });
  box.addEventListener('input', function(){ updateChState(name); });
  box.addEventListener('change', function(){ updateChState(name); });
});

document.getElementById('btn-save').addEventListener('click', savePush);
document.getElementById('btn-test').addEventListener('click', testPush);

/* =========================== 定时与状态 =========================== */
function renderDaemon(st){
  const dot = document.getElementById('d-dot');
  const badge = document.getElementById('d-state');
  const running = !!st.running;
  /* 心跳点两态：绿点呼吸 = 运行，灰点静止 = 停止（底色交给 .dot/.dot.on，不再写内联） */
  dot.className = 'dot' + (running ? ' on' : '');
  badge.textContent = running ? '运行中' : '已停止';
  badge.className = 'badge' + (running ? '' : ' err');

  hbState.running = running;
  hbState.startedAtRaw = String(st.started_at || '');
  hbState.startedAt = parseStamp(hbState.startedAtRaw);
  hbState.times = Array.isArray(st.check_times) ? st.check_times.slice() : [];

  document.getElementById('d-pid').textContent = st.pid || '-';
  const heart = document.getElementById('d-heart');
  if (heart) {
    heart.textContent = (st.heartbeat_age != null)
      ? Math.round(st.heartbeat_age) + ' 秒前' : '-';
  }

  const last = st.last_run || {};
  document.getElementById('d-last').textContent =
    last.at ? (last.exit === 0 ? '成功' : '退出码 ' + last.exit) + ' (' + shortStamp(last.at) + ')'
            : '还没跑过';
  document.getElementById('btn-daemon-start').disabled = running;
  document.getElementById('btn-daemon-stop').disabled = !running;

  /* 心跳显示统一入口：按状态起/停 timer，并立即刷新一次
     （从暂停恢复为运行时马上出数，不等下一个 30 秒 tick） */
  if (running) startHeartbeat(); else stopHeartbeat();
  tickHeartbeat();
}

function shortStamp(text){
  // "2026-10-27 01:23:43" -> "10-27 01:23"
  const m = String(text || '').match(/^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? (m[1] + ' ' + m[2]) : String(text || '');
}

function renderSchedule(d){
  if (!d.ok) { toast(d.message || '读取失败', true); return; }
  const times = d.times || [];
  const box = document.getElementById('d-times');
  if (!times.length) {
    box.innerHTML = '<span class="chip empty">还没有时间点</span>';
  } else {
    box.innerHTML = times.map(function(t){
      return '<span class="chip">' + t +
             '<button title="删除" data-remove="' + t + '">×</button></span>';
    }).join('');
  }
  renderRuler(times);
  document.getElementById('d-count').textContent = times.length + ' 个';
  /* 下次提醒：守护进程停止时与 KPI 保持一致显示「已暂停」，不再给一个够不着的时刻 */
  document.getElementById('d-next').textContent =
    (d.daemon && d.daemon.running) ? (d.next_run || '-') : '已暂停';

  // 参与检查的账号：只给数量，完整清单走 title（来自后端，不写死）
  const gamesBox = document.getElementById('d-games');
  if (gamesBox) {
    const infos = d.accounts_info || [];
    if (!infos.length) {
      gamesBox.textContent = '无';
      gamesBox.title = 'config.json 里 accounts 的 enabled 全为 false';
    } else {
      gamesBox.textContent = infos.length + ' 个';
      gamesBox.title = infos.map(function(it){
        return (it.name || it.id) + (it.nickname ? '（' + it.nickname + '）' : '');
      }).join('\n');
    }
  }

  renderCatchUp(d);
  renderDaemon(d.daemon || {});
  renderActivitySwitch(d.activity_alert_enabled, d.activity_alert_days);
}

/* 活动结束提醒：config.app.activity_alert_enabled / activity_alert_days */
function renderActivitySwitch(enabled, days){
  const on = enabled !== false;
  const box = document.getElementById('activity-toggle');
  if (box) box.checked = on;
  const item = document.getElementById('acc-activity');
  if (item) item.classList.toggle('is-off', !on);
  const num = parseInt(days, 10);
  const input = document.getElementById('activity-days');
  if (input && document.activeElement !== input && !isNaN(num)) input.value = num;
  refreshSetSummaries();
}

async function toggleActivityAlert(){
  const box = document.getElementById('activity-toggle');
  if (!box) return;
  refreshSetSummaries();          // 开关一动，摘要立即跟随（死命令：同一事件只绑一次）
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/activity_alert', { enabled: want });
    if (!d.ok) { box.checked = !want; toast(d.message || '设置失败', true); return; }
    renderActivitySwitch(d.activity_alert_enabled);
    toast(want ? '已开启活动结束提醒' : '已关闭活动结束提醒');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

/* 补发：开关 + 窗口数值分别对应 config.app.catch_up_enabled / catch_up_minutes */
let lastCatchUp = { enabled: true, minutes: 180 };

function renderCatchUp(d){
  d = d || {};
  const enabled = d.catch_up_enabled !== false;
  const num = parseInt(d.catch_up_minutes, 10);
  const minutes = isNaN(num) ? 180 : num;
  lastCatchUp = { enabled: enabled, minutes: minutes };

  const input = document.getElementById('catch-up-minutes');
  if (input && document.activeElement !== input) input.value = minutes;

  const box = document.getElementById('catch-up-toggle');
  if (box) box.checked = enabled;

  const item = document.getElementById('acc-catchup');
  if (item) item.classList.toggle('is-off', !enabled);
  refreshSetSummaries();
}

async function toggleCatchUp(){
  const box = document.getElementById('catch-up-toggle');
  if (!box) return;
  refreshSetSummaries();          // 开关一动，摘要立即跟随（死命令：同一事件只绑一次）
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/catch_up',
      { minutes: lastCatchUp.minutes, enabled: want });
    if (!d.ok) {
      box.checked = !want;
      toast(d.message || '设置失败', true);
      return;
    }
    renderCatchUp(d);
    toast(want ? '已开启睡过头补发' : '已关闭睡过头补发（窗口数值已保留）');
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

async function saveCatchUp(){
  const btn = document.getElementById('btn-catch-up');
  const input = document.getElementById('catch-up-minutes');
  const raw = parseInt(input.value, 10);
  let minutes = isNaN(raw) ? 0 : raw;
  if (minutes < 0) minutes = 0;
  if (minutes > 720) minutes = 720;
  btn.disabled = true;
  try {
    const d = await postJSON('/api/catch_up',
      { minutes: minutes, enabled: lastCatchUp.enabled });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    renderCatchUp(d);
    toast(d.message || '已保存');
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
  }
}

function loadSchedule(){
  fetch('/api/schedule').then(function(r){ return r.json(); })
    .then(renderSchedule)
    .catch(function(){});
}

/* ========================= 开机自启 ========================= */
function renderAutostart(d){
  const box = document.getElementById('autostart-toggle');
  const badge = document.getElementById('autostart-state');
  const hint = document.getElementById('autostart-hint');
  const supported = !!(d && d.supported);

  if (box) {
    box.checked = !!(d && d.enabled);
    box.disabled = !supported;
  }
  if (badge) {
    if (!supported) {
      badge.textContent = '不支持';
      badge.className = 'badge err';
    } else if (d.stale) {
      badge.textContent = '路径已失效';
      badge.className = 'badge err';
    } else if (d.enabled) {
      badge.textContent = '已开启';
      badge.className = 'badge';
    } else {
      badge.textContent = '未开启';
      badge.className = 'badge';
    }
  }
  if (hint) {
    if (!supported) {
      hint.textContent = (d && d.message) || '仅支持 Windows';
    } else if (d.stale) {
      hint.innerHTML = '注册表路径已失效：把开关关掉再打开一次即可刷新。';
    } else if (d.enabled) {
      hint.innerHTML = '下次登录 Windows 自动在托盘启动。';
    } else {
      hint.innerHTML = '勾选后下次登录 Windows 自动在托盘启动。';
    }
  }
  refreshSetSummaries();
}

function loadAutostart(){
  fetch('/api/autostart').then(function(r){ return r.json(); })
    .then(renderAutostart)
    .catch(function(){
      const hint = document.getElementById('autostart-hint');
      if (hint) hint.textContent = '读取自启状态失败，刷新页面重试';
    });
}

async function toggleAutostart(){
  const box = document.getElementById('autostart-toggle');
  if (!box) return;
  refreshSetSummaries();          // 开关一动，摘要立即跟随（死命令：同一事件只绑一次）
  const want = box.checked;
  box.disabled = true;
  try {
    const d = await postJSON('/api/autostart', { enable: want });
    if (!d.ok) {
      box.checked = !want;          // 失败就把开关拨回去，别让界面撒谎
      toast(d.message || '设置失败', true);
      renderAutostart(d);
      return;
    }
    renderAutostart(d);
    toast(d.message || (want ? '已开启开机自启' : '已关闭开机自启'));
  } catch (e) {
    box.checked = !want;
    toast('设置失败：' + e, true);
  } finally {
    box.disabled = false;
  }
}

document.getElementById('autostart-toggle')
  .addEventListener('change', toggleAutostart);
document.getElementById('catch-up-toggle')
  .addEventListener('change', toggleCatchUp);

/* 活动提醒提前天数：单独保存，不碰总开关 */
async function saveActivityDays(){
  const input = document.getElementById('activity-days');
  const btn = document.getElementById('btn-activity-days');
  if (!input || !btn) return;
  const raw = input.value;
  const num = parseInt(raw, 10);
  if (isNaN(num) || num < 1 || num > 30) {
    toast('请填 1–30 之间的整数', true);
    return;
  }
  btn.disabled = true;
  try {
    const d = await postJSON('/api/activity_alert', { days: num });
    if (!d.ok) { toast(d.message || '保存失败', true); return; }
    if (document.activeElement !== input) input.value = d.activity_alert_days;
    refreshSetSummaries();
    toast(d.message || ('活动结束前 ' + d.activity_alert_days + ' 天开始提醒'));
  } catch (e) {
    toast('保存失败：' + e, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('btn-activity-days')
  .addEventListener('click', saveActivityDays);
document.getElementById('resin-toggle')
  .addEventListener('change', toggleResourceAlert);
document.getElementById('activity-toggle')
  .addEventListener('change', toggleActivityAlert);

/* ========================= 折叠面板参数摘要 =========================
   每个设置行标题右侧的 11px 摘要，数据永远取当前 DOM（开关 + 输入框）。
   三类时机会刷新：
   ① 用户改动：下方 input/change 监听（一次性绑定，dataset 闸门防叠加）
   ② 后端数据渲染完：renderAutostart / renderThresholds / renderCatchUp /
      renderActivitySwitch 末尾各调一次
   ③ 页面初始化：模块末尾立即调一次，未拿到后端数据时诚实显示「读取中…」 */
function setSum(id, text, full){
  const el = document.getElementById('sum-' + id);
  if (!el) return;
  el.textContent = text;
  el.title = full || text;   // 紧凑文案/截断时，悬停看全量
}

function refreshSetSummaries(){
  /* 睡过头补发：关 → 已关闭；开 → 窗口 X 分钟（0 = 不补发） */
  const cuBox = document.getElementById('catch-up-toggle');
  const cuMin = document.getElementById('catch-up-minutes');
  const cuLoaded = cuMin && String(cuMin.value).trim() !== '';
  if (!cuLoaded) setSum('catchup', '读取中…');
  else if (cuBox && !cuBox.checked) setSum('catchup', '已关闭');
  else {
    const v = parseInt(cuMin.value, 10);
    setSum('catchup', !isNaN(v) && v > 0 ? '窗口 ' + v + ' 分钟' : '不补发');
  }

  /* 体力溢出阈值：可见文字保持紧凑（用户点名：全量参数收进 title 悬浮看） */
  const rsBox = document.getElementById('resin-toggle');
  const rsInputs = ['genshin', 'starrail', 'zzz', 'arknights', 'endfield']
    .map(function(g){ return document.getElementById('resin-' + g); });
  const rsLoaded = rsInputs.some(function(inp){ return inp && String(inp.value).trim() !== ''; });
  if (!rsLoaded) setSum('resin', '读取中…');
  else if (rsBox && !rsBox.checked) setSum('resin', '已关闭');
  else {
    let zeros = 0;
    const parts = [['genshin','原神'],['starrail','星铁'],['zzz','绝区零'],
                   ['arknights','方舟'],['endfield','终末地']].map(function(r){
      const inp = document.getElementById('resin-' + r[0]);
      const v = inp ? parseInt(inp.value, 10) : NaN;
      if (!isNaN(v) && v === 0) zeros++;
      return r[1] + ' ' + (isNaN(v) ? '–' : (v === 0 ? '关' : v));
    });
    setSum('resin', '阈值已配置' + (zeros ? ' · ' + zeros + ' 款关闭' : ''), parts.join(' · '));
  }

  /* 活动结束提醒：关 → 已关闭；开 → 提前 X 天 */
  const acBox = document.getElementById('activity-toggle');
  const acDays = document.getElementById('activity-days');
  const acLoaded = acDays && String(acDays.value).trim() !== '';
  if (!acLoaded) setSum('activity', '读取中…');
  else if (acBox && !acBox.checked) setSum('activity', '已关闭');
  else {
    const v = parseInt(acDays.value, 10);
    setSum('activity', !isNaN(v) && v > 0 ? '提前 ' + v + ' 天' : '未设置天数');
  }
}

/* 摘要跟随所有改动：7 个数字输入（input+change）。
   4 个开关的 change 已由既有启停处理函数持有（toggleAutostart 等），
   按死命令「同一事件只绑一次」，摘要刷新并入了那些函数的开头，这里不再绑。
   页面 DOM 是静态的、Tab 切换只切 display 不重建，这里只会执行一次；
   dataset 闸门再兜底，重复调用不会叠加监听器 */
(function bindSetSummaries(){
  ['catch-up-minutes', 'activity-days', 'resin-genshin', 'resin-starrail',
   'resin-zzz', 'resin-arknights', 'resin-endfield'].forEach(function(id){
    const el = document.getElementById(id);
    if (!el || el.dataset.sumBound) return;
    el.dataset.sumBound = '1';
    el.addEventListener('input', refreshSetSummaries);
    el.addEventListener('change', refreshSetSummaries);
  });
  /* 首屏：立即按当前 DOM 画一版（此刻多半是「读取中…」，
     后端数据到手后由 render*() 末尾的刷新接管） */
  refreshSetSummaries();
})();
document.getElementById('btn-live-refresh')
  .addEventListener('click', function(){ loadLive(true); });
document.getElementById('btn-act-refresh')
  .addEventListener('click', refreshActivities);
document.getElementById('btn-push-refresh')
  .addEventListener('click', loadPushRecent);
/* 活动卡片是动态渲染的，删除按钮与抽屉把手用事件委托挂在容器上 */
document.getElementById('act-list').addEventListener('click', function(e){
  if (!e.target.closest) return;
  const handle = e.target.closest('.act-drawer-handle');
  if (handle) {                                   // 「已完成」抽屉：点击展开 / 再点收起
    const drawer = handle.closest('.act-drawer');
    if (drawer) drawer.classList.toggle('open');
    return;
  }
  const btn = e.target.closest('.act-del');
  if (btn) { e.preventDefault(); deleteActivity(btn); }
});

/* 折叠面板标题栏里的开关 / 小按钮：只做自己的事，不连带展开或收起面板。 */
document.querySelectorAll('.set-head .toggle, .set-head .mini, .set-head .btn')
  .forEach(function(el){
    el.addEventListener('click', function(e){ e.stopPropagation(); });
  });

async function saveTimes(times){
  try {
    const d = await postJSON('/api/schedule', { times: times });
    if (!d.ok) { toast(d.message || '保存失败', true); return false; }
    renderSchedule(d);
    toast(d.message || '已保存');
    return true;
  } catch (e) {
    toast('保存失败：' + e, true);
    return false;
  }
}

async function addTime(){
  const input = document.getElementById('d-new-time');
  const value = (input.value || '').trim();
  if (!value) { toast('请先选择时间', true); return; }
  const current = currentTimes();
  if (current.indexOf(value) >= 0) { toast('这个时间点已经有了', true); return; }
  await saveTimes(current.concat([value]));
}

function currentTimes(){
  return [...document.querySelectorAll('#d-times .chip')]
    .map(function(el){ return el.textContent.replace('×','').trim(); })
    .filter(function(t){ return /^\d{1,2}:\d{2}$/.test(t); });
}

document.getElementById('d-times').addEventListener('click', async function(e){
  const btn = e.target.closest('[data-remove]');
  if (!btn) return;
  const target = btn.dataset.remove;
  const current = currentTimes();
  if (current.length <= 1) { toast('至少要保留一个时间点', true); return; }
  await saveTimes(current.filter(function(t){ return t !== target; }));
});

document.getElementById('btn-add-time').addEventListener('click', addTime);
document.getElementById('btn-catch-up').addEventListener('click', saveCatchUp);

async function controlDaemon(action){
  const btn = document.getElementById(action === 'start' ? 'btn-daemon-start' : 'btn-daemon-stop');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = action === 'start' ? '启动中…' : '停止中…';
  /* 死命令：点停止 = 前端 heartbeatTimer 立刻停 + 后端 /api/daemon/stop 真停进程，
     两件事必须同时发生；若后端失败，finally 的 loadSchedule 会把状态纠回来 */
  if (action === 'stop') stopHeartbeat();
  try {
    const d = await postJSON('/api/daemon/' + action, {});
    if (d.daemon) renderDaemon(d.daemon);
    const okMsg = action === 'stop'
      ? '已停止本次运行（开机自启设置不变）'
      : (d.message || '已启动守护进程');
    toast(d.ok ? okMsg : (d.message || '操作失败'), !d.ok);
  } catch (e) {
    toast('操作失败：' + e, true);
  } finally {
    btn.textContent = old;
    loadSchedule();
  }
}

document.getElementById('btn-daemon-start').addEventListener('click', function(){ controlDaemon('start'); });
document.getElementById('btn-daemon-stop').addEventListener('click', function(){ controlDaemon('stop'); });
document.getElementById('btn-dev-save').addEventListener('click', saveDevice);
document.getElementById('btn-dev-reset').addEventListener('click', resetDevice);


/* ========================= 信息查询 ========================= */
let liveLoaded = false;    // 本次会话已经发起过实时请求
let liveRendered = false;  // 页面上已有可展示的数据（缓存或实时），占位符不再覆盖

function loadQuery(){
  loadActivities();
  if (!liveLoaded) loadLive(false);
}

/* 把 /api/live 结果整理成 gid → {finished, tasks, 体力信息} 的映射。
   只有查询成功（g.ok）的游戏才进入映射 —— 它的完成状态有"发言权"，
   KPI 与迷你卡以它为准；查询失败的没有记录，回落到巡检历史。 */
function buildLiveMap(d){
  const map = {};
  (d.games || []).forEach(function(g){
    if (!g.ok) return;
    map[g.game] = {
      finished: !!g.finished,
      tasks: (g.tasks || []).map(function(t){
        return { name: t.name, done: !!t.done, progress: t.progress || '' };
      }),
    };
    if (g.resource && g.resource.percent != null) {
      map[g.game].name = g.resource.name;
      map[g.game].current = g.resource.current;
      map[g.game].maximum = g.resource.maximum;
      map[g.game].percent = g.resource.percent;
      map[g.game].recover = g.resource.recover;
    }
  });
  return map;
}

/* 任务明细 → "委托 0/4 · 实训 0/500" 式进度串；无进度语义的任务用 ✓/✗ */
function taskProgressHtml(tasks){
  const parts = (tasks || []).map(function(t){
    const label = escHtml(t.name);
    if (t.progress) {
      const hit = /(\d+)\s*\/\s*(\d+)/.exec(t.progress);
      const txt = hit ? (parseInt(hit[1], 10) + '/' + parseInt(hit[2], 10)) : escHtml(t.progress);
      return '<span class="tp-item' + (t.done ? '' : ' tp-miss') + '">' + label +
             ' <b class="mono">' + txt + '</b></span>';
    }
    return '<span class="tp-item' + (t.done ? '' : ' tp-miss') + '">' + label +
           ' <b>' + (t.done ? '✓' : '✗') + '</b></span>';
  });
  return parts.length ? '<div class="gcard-tasks">' + parts.join('<i class="tp-sep">·</i>') + '</div>' : '';
}

/* ---------- 体力预计满员时间（事前规划） ----------
   数据优先级：API 恢复字段（resource.recover = 后端统一化的距满秒数，
   原神/崩铁/终末地有）> RECOVERY_RATE_PER_MIN 常数表（方舟等接口不给恢复字段的走这里）。
   输出静态文案「预计 22:45 满」：目标时刻由查询数据推出，不引入新 timer（倒计时纪律），
   不写 localStorage；页面常驻时以「信息查询」最新一次结果为准。
   已满 → 「已满」；剩余不足 1 小时 → .eta-soon 橙色；数据缺失 → 空串。 */
const RECOVERY_RATE_PER_MIN = {
  genshin: 8, starrail: 6, zzz: 6, arknights: 6, endfield: 6
};
function staminaEtaHtml(gid, resource){
  if (!resource || resource.current == null || resource.maximum == null) return '';
  const cur = Number(resource.current), max = Number(resource.maximum);
  if (!(max > 0)) return '';
  if (cur >= max) return '<span class="eta">已满</span>';
  let seconds = null;
  if (resource.recover != null && Number(resource.recover) > 0) {
    seconds = Number(resource.recover);            // API 真值优先
  } else {
    const rate = RECOVERY_RATE_PER_MIN[gid];
    if (rate > 0) seconds = (max - cur) * rate * 60;
  }
  if (!seconds || seconds <= 0) return '';
  const pad = function(n){ return (n < 10 ? '0' : '') + n; };
  let text;
  if (seconds >= 86400) {
    text = '预计 ' + (Math.round(seconds / 8640) / 10) + ' 天后满';
  } else {
    const target = new Date(Date.now() + seconds * 1000);
    text = '预计 ' + pad(target.getHours()) + ':' + pad(target.getMinutes()) + ' 满';
  }
  return '<span class="eta' + (seconds < 3600 ? ' eta-soon' : '') + '">' + text + '</span>';
}

function renderLive(d){
  const box = document.getElementById('live-grid');
  const hint = document.getElementById('live-hint');
  if (!box) return;
  if (!d || d.ok === false) {
    /* 已有数据在展示时，失败的刷新静默忽略，不把页面打成空态 */
    if (!liveRendered) {
      box.innerHTML = '<div class="emptybox">' + escHtml((d && d.message) || '查询失败，请重试') + '</div>';
      if (hint) hint.textContent = '';
    }
    return;
  }
  liveRendered = true;
  window.__liveMap = Object.assign(window.__liveMap || {}, buildLiveMap(d));
  const games = d.games || [];
  if (!games.length) {
    box.innerHTML = '<div class="emptybox">没有启用的账号。</div>';
  } else {
    box.innerHTML = '<table class="tbl"><thead><tr>' +
      '<th>游戏</th><th>日常</th><th>体力</th><th>进度</th></tr></thead><tbody>' +
      games.map(function(g){
        const name = escHtml(g.game_name || g.game);
        if (!g.ok) {
          return '<tr><td>' + gameIconHtml(g.game) + name + '</td>' +
            '<td><span class="badge err">失败</span></td>' +
            '<td class="wrap err-t">' + escHtml(g.error || '未知错误') + '</td>' +
            '<td>—</td></tr>';
        }
        const r = g.resource;
        const st = g.finished
          ? '<span class="dim">已完成</span>'
          : '<span class="badge warn">未完成</span>';
        if (!r || r.current == null) {
          return '<tr><td>' + gameIconHtml(g.game) + name + '</td><td>' + st + '</td>' +
            '<td class="dim">—</td><td class="dim">暂不支持</td></tr>';
        }
        const pct = (r.percent == null) ? 0 : r.percent;
        const max = (r.maximum == null) ? '?' : r.maximum;
        return '<tr><td>' + gameIconHtml(g.game) + name + '</td><td>' + st + '</td>' +
          '<td class="mono">' + r.current + ' <span class="dim">/ ' + max +
          ' · ' + escHtml(r.name) + '</span></td>' +
          '<td><span class="bar"><i class="' + (pct >= 85 ? 'hot' : '') +
          '" style="width:' + pct + '%"></i><i class="progress-glow' +
          (pct >= 85 ? ' hot' : '') + '" style="left:' + pct + '%"></i></span><span class="dim">' + pct + '%</span>' +
          staminaEtaHtml(g.game, r) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }
  if (hint) {
    hint.textContent = '查询时间 ' + (d.generated_at || '-') +
      (d.cached ? (d.stale ? '（历史缓存 · 点「刷新」获取实时）' : '（45 秒内缓存）') : '');
  }
  // 迷你卡的体力行用最新数据重绘（报表页当前可见时才有意义）
  const statsBox = document.getElementById('h-stats');
  if (lastHistory && lastHistory.has_data && statsBox && statsBox.querySelector('.gcard')) {
    statsBox.innerHTML = '<div class="gcards">' +
      (lastHistory.games || []).map(gameCardHtml).join('') + '</div>';
  }
  renderKpiResin(d);
  /* 实时结果一到手就重算「今日完成」KPI：live 说已完成的立刻计入，
     不等下一次巡检落盘 —— KPI 与表格永远对得上 */
  if (lastHistory) renderKpiToday(lastHistory);
}

function loadLive(force){
  const btn = document.getElementById('btn-live-refresh');
  const box = document.getElementById('live-grid');
  if (btn) { btn.disabled = true; btn.textContent = '查询中…'; }
  /* 已有缓存渲染的表格时不再打占位符 —— 静默刷新，数据到了平滑替换 */
  if (box && !liveRendered) box.innerHTML = '<div class="emptybox">正在查询，请稍候…</div>';
  kpiBusy(true);
  fetch('/api/live' + (force ? '?refresh=1' : ''))
    .then(function(r){ return r.json(); })
    .then(function(d){
      liveLoaded = true;
      renderLive(d);
      if (d && d.ok !== false) cacheSet('live', d);   // 落缓存，下次秒开
    })
    .catch(function(){
      if (box && !liveRendered) box.innerHTML = '<div class="emptybox">查询失败，请稍后重试。</div>';
    })
    .then(function(){
      kpiBusy(false);
      if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
    });
}

function renderActHint(d){
  const hint = document.getElementById('act-hint');
  if (!hint) return;
  d = d || {};
  const parts = [];
  if (d.last_error) {
    parts.push('上次拉取失败（沿用旧数据）：' + d.last_error);
  } else if (d.fetched_at) {
    parts.push('数据更新于 ' + d.fetched_at);
  }
  if (d.ignored_count > 0) {
    parts.push('已删除 ' + d.ignored_count + ' 个');
  }
  hint.innerHTML = escHtml(parts.join(' · ')) +
    (d.ignored_count > 0 ? ' <a class="act-restore" id="act-restore">恢复全部</a>' : '');
  const restore = document.getElementById('act-restore');
  if (restore) restore.addEventListener('click', restoreActivities);
}

/* 游戏头像：按 game_id 硬绑定官方图标。
   ICON_STAMP 由后端按图片 mtime 注入，换图后 URL 必变，不会被 WebView2 缓存坑。 */
const ICON_STAMP = '__ICON_STAMP__';
const GAME_ICON = {
  genshin: '/static/icons/genshin',
  starrail: '/static/icons/starrail',
  zzz: '/static/icons/zzz',
  arknights: '/static/icons/arknights',
  endfield:  '/static/icons/endfield'
};

/* 图标定义就绪后，把账号绑定页选择卡的静态占位换成官方图标 */
fillSelIcons();

function actIconHtml(it){
  const base = GAME_ICON[it.game];
  if (!base) {
    return '<span class="act-icon ph">' +
      escHtml(String(it.game_name || '?').slice(0, 1)) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="act-icon" width="24" height="24" alt="' +
    escHtml(it.game_name || '') + '" src="' + base + '.png' + v +
    '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

/* '2026-10-29 04:00:00' -> '10-29 04:00'（卡片头部精确到分） */
function shortRange(s){
  const m = String(s || '').match(/\d{4}-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/);
  return m ? m[1] + '-' + m[2] + ' ' + m[3] : '';
}

/* 最紧急 1 张的紧迫度标记（只改颜色不改尺寸）：剩余 ≤24h = critical（红边+呼吸）；≤72h（ending_soon）= warn（琥珀数字） */
function actUrgencyAttr(it, isHero){
  if (!isHero || it.not_started) return '';
  const hl = +it.hours_left || 0;
  if (it.ending_soon && hl > 0 && hl <= 24) return ' data-urgency="critical"';
  if (it.ending_soon) return ' data-urgency="warn"';
  return '';
}

/* isHero：最紧急的 1 张 → 2x2 英雄卡（hero class）+ 紧迫感描边 */
function actCardHtml(it, isHero){
  const id = escHtml(it.id || '');
  const st = shortRange(it.start), en = shortRange(it.end);
  const ended = it.status === '已完成';
  const timeCls = ended ? 't-end' : (it.not_started ? 't-up' : 't-on');
  const soon = !!it.ending_soon && !it.not_started;
  // reward 为空 = 接口没给奖励明细，被兜底规则保留下来的（别假装它有原石）
  const reward = it.reward
    ? '<span class="act-reward" title="这个活动给抽卡资源">' + escHtml(it.reward) + '</span>'
    : '';
  const cdCls = ended ? 'ended' : (soon ? 'soon' : '');
  const prog = actProgHtml(it);
  return '<div class="card acard' + (isHero ? ' hero' : '') + '"' +
    actUrgencyAttr(it, isHero) + ' data-id="' + id + '">' +
    '<div class="acard-top"><span class="act-game">' + actIconHtml(it) +
      escHtml(it.game_name) + '</span>' +
      '<button type="button" class="act-del" data-del="' + id +
      '" title="删除，之后不再提醒">×</button></div>' +
    '<div class="acard-title" title="' + escHtml(it.title) + '">' + escHtml(it.title) + reward + '</div>' +
    '<div class="act-time ' + timeCls + '">始 ' + (st || '时间待定') +
      (en ? ' · 止 ' + en : '') + '</div>' +
    '<div class="act-cd ' + cdCls + '">' + escHtml(it.countdown || '') + '</div>' +
    prog +
    '</div>';
}

/* 36/36 这类文本进度 -> 进度条；解析不出就不画 */
function actProgHtml(it){
  const m = String(it.progress || '').match(/(\d+)\s*\/\s*(\d+)/);
  if (!m) return '';
  const a = +m[1], b = +m[2];
  if (!b) return '';
  const pct = Math.min(100, Math.round(a / b * 100));
  return '<div class="act-prog"><span class="sbar"><i style="width:' + pct + '%"></i></span>' +
    '<span class="mono">' + a + '/' + b + '</span></div>';
}

/* 找最紧急的 1 张（剩余小时最小且 >0），做 2x2 英雄卡 + data-urgency 标记 */
function pickHeroIndex(pool){
  if (!pool.length) return -1;
  let best = 0;
  for (let i = 1; i < pool.length; i++) {
    const a = +pool[i].hours_left || 0, b = +pool[best].hours_left || 0;
    if (a > 0 && (b <= 0 || a < b)) best = i;
  }
  return best;
}

function renderActivities(d){
  const list = document.getElementById('act-list');
  const uns = document.getElementById('act-unsupported');
  if (!list) return;
  d = d || {};
  const items = d.items || [];
  if (!items.length) {
    list.innerHTML = '<div class="act-empty">' +
      '<div class="act-empty-icon">' + (d.has_data ? '🌱' : '📭') + '</div>' +
      '<div>' + (d.has_data
        ? '近期无即将结束的活动，安心长草'
        : '还没有活动数据，点「刷新」拉取。') + '</div>' +
      '</div>';
  } else {
    // 后端已排好序；这里拆成 进行中 / 未开启 / 已完成（进抽屉）三段
    const ongoing = items.filter(function(x){ return x.group !== 'upcoming' && x.status !== '已完成'; });
    const upcoming = items.filter(function(x){ return x.group === 'upcoming'; });
    const finished = items.filter(function(x){ return x.status === '已完成'; });

    let html = '<div class="act-grid">';
    if (ongoing.length) {
      const heroIdx = pickHeroIndex(ongoing);
      html += ongoing.map(function(it, i){ return actCardHtml(it, i === heroIdx); }).join('');
    }
    if (upcoming.length) {
      html += upcoming.map(function(it){ return actCardHtml(it, !ongoing.length && it === upcoming[0]); }).join('');
    }
    html += '</div>';

    if (finished.length) {
      html += '<div class="act-drawer" id="act-drawer">' +
        '<button type="button" class="act-drawer-handle">已完成 · ' + finished.length + '</button>' +
        '<div class="act-drawer-body"><div class="act-grid">' +
        finished.map(function(it){ return actCardHtml(it, false); }).join('') +
        '</div></div></div>';
    }
    list.innerHTML = html;
  }
  renderActHint(d);
  if (uns) {
    const list2 = d.unsupported || [];
    uns.innerHTML = list2.length
      ? '<div class="au-title">暂不支持</div>' +
        list2.map(function(u){
          return '<span class="au-chip" title="' + escHtml(u.reason || '') + '">' +
            escHtml(u.game_name) + '</span>';
        }).join('')
      : '';
  }
}

/* 删除：立刻从界面消失，并写进 data/ignored_activities.json，之后永不提醒 */
async function deleteActivity(btn){
  const id = btn.getAttribute('data-del');
  const row = btn.closest('.acard');
  btn.disabled = true;
  try {
    const d = await postJSON('/api/activities/delete', { id: id });
    if (!d || d.ok === false) {
      toast((d && d.message) || '删除失败', true);
      btn.disabled = false;
      return;
    }
    if (row) row.remove();
    renderActivities(d);
    toast('已删除，之后不会再提醒');
  } catch (e) {
    btn.disabled = false;
    toast('删除失败：' + e, true);
  }
}

async function restoreActivities(){
  try {
    const d = await postJSON('/api/activities/restore', {});
    if (!d || d.ok === false) { toast('恢复失败', true); return; }
    renderActivities(d);
    toast('已恢复全部被删除的活动');
  } catch (e) {
    toast('恢复失败：' + e, true);
  }
}

function loadActivities(){
  fetch('/api/activities').then(function(r){ return r.json(); })
    .then(renderActivities)
    .catch(function(){
      const list = document.getElementById('act-list');
      if (list) list.innerHTML = '<div class="emptybox">读取活动缓存失败，请点「刷新」。</div>';
    });
}

async function refreshActivities(){
  const btn = document.getElementById('btn-act-refresh');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '刷新中…';
  try {
    const d = await postJSON('/api/activities/refresh', {});
    if (d.ok === false) { toast(d.message || '刷新失败', true); return; }
    renderActivities(d);
    toast('活动数据已更新（' + (d.items || []).length + ' 个进行中）');
  } catch (e) {
    toast('刷新失败：' + e, true);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

/* =========================== 每日报表 =========================== */
function renderHistoryEmpty(message, detail){
  // 空状态：统计表清空，表格位置放一句人话（绝不弹红色错误条）
  const statsBox = document.getElementById('h-stats');
  if (statsBox) statsBox.innerHTML = '';
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = '-';
  const tableBox = document.getElementById('h-table');
  if (!tableBox) return;
  tableBox.innerHTML =
    '<div class="emptybox">' + message +
    (detail ? '<br><span style="font-size:11px;color:var(--muted)">' + detail + '</span>' : '') +
    '</div>';
}

function gameBaseName(g){
  // 只取游戏基础名：去掉「 · 角色昵称」。
  var raw = String((g && (g.game_name || g.game)) || '');
  var cut = raw.indexOf('·');
  if (cut > 0) raw = raw.slice(0, cut);
  return raw.trim() || String((g && g.game) || '');
}

// 游戏识别色（BetterGI 式图标占位小圆点）
const GAME_DOT = {
  genshin:   '#e0a83c',
  starrail:  '#8f7ff0',
  zzz:       '#f0854b',
  arknights: '#4aa3e8',
  endfield:  '#dcc23c'
};
function gameDotColor(gid){
  return GAME_DOT[gid] || 'var(--brand)';
}

let lastHistory = null;

/* ---------- 本周汇总（报表页顶部横条） ----------
   数据源 = /api/history?days=7 的 days[].games{gid:{known,finished}}，
   客户端聚合，零新 API；口径与 daemon 周报（history.weekly_digest）一致：
   known = 当天查到数据，「漏」只数查过但没做完的天，整天没查过不算漏。 */
function weeklyStatsOf(d){
  const days = (d && d.days) || [];
  const tracked = (d && d.tracked) || [];
  const nameOf = {};
  (d.games || []).forEach(function(g){ nameOf[g.game] = gameBaseName(g); });
  return tracked.map(function(gid){
    let done = 0, known = 0;
    days.forEach(function(row){
      const c = (row.games || {})[gid];
      if (!c || !c.known) return;
      known++;
      if (c.finished) done++;
    });
    return { gid: gid, name: nameOf[gid] || gid, done: done, known: known, days: days.length };
  });
}

function renderWeeklyCard(d){
  const box = document.getElementById('week-body');
  const badge = document.getElementById('week-badge');
  if (!box) return;
  const stats = weeklyStatsOf(d);
  if (!stats.length || !stats[0].days) {
    box.innerHTML = '<div class="emptybox">暂无数据，等本周巡检跑起来。</div>';
    if (badge) badge.textContent = '-';
    return;
  }
  const totalDays = stats[0].days;
  let totalDone = 0;
  const chips = stats.map(function(s){
    totalDone += s.done;
    const missed = s.known - s.done;             // 查过但没做完（与周报同口径）
    let html = '<span class="week-chip"><b>' + escHtml(s.name) + '</b> <span class="mono">' +
      s.done + '/' + totalDays + '</span>';
    if (s.done >= totalDays) html += '<span class="dim"> 全勤</span>';
    else if (missed > 0) html += '<span class="miss"> 漏 ' + missed + '</span>';
    return html + '</span>';
  }).join('');
  box.innerHTML = '<div class="week-chips">' + chips + '</div>';
  if (badge) badge.textContent = totalDone + '/' + (stats.length * totalDays);
}

function renderHistory(d){
  d = d || {};
  lastHistory = d;
  renderWeeklyCard(d);   // 顶部本周汇总横条（空数据时自己画空态）
  const monthBadge = document.getElementById('h-month');
  if (monthBadge) monthBadge.textContent = d.month ? (d.month + ' 本月') : '-';

  if (!d.has_data) {
    renderHistoryEmpty('暂无历史记录，等待下一次定时检查写入。');
    return;
  }
  const games = d.games || [];
  if (!games.length) {
    renderHistoryEmpty('没有启用任何游戏账号。');
    return;
  }

  // --- 游戏概览：每款一张迷你卡 ---
  const statsBox = document.getElementById('h-stats');
  if (statsBox) {
    statsBox.innerHTML = '<div class="gcards">' +
      games.map(gameCardHtml).join('') + '</div>';
  }
  renderHeatmap(d);
}

function gameIconHtml(gid, cls){
  const klass = cls || 'gcard-ico';
  const base = GAME_ICON[gid];
  if (!base) {
    return '<span class="' + klass + ' ph">' + escHtml(String(gid).slice(0, 1).toUpperCase()) + '</span>';
  }
  const v = ICON_STAMP.indexOf('__') === 0 ? '' : '?v=' + ICON_STAMP;
  return '<img class="' + klass + '" width="40" height="40" alt="" src="' + base + '.png' + v +
         '" srcset="' + base + '@2x.png' + v + ' 2x">';
}

function todayCellFor(d, gid){
  const days = (d && d.days) || [];
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) return (days[i].games || {})[gid] || null;
  }
  return null;
}

function gameCardHtml(g){
  const gid = g.game;
  const today = todayCellFor(lastHistory, gid);
  const live = (window.__liveMap || {})[gid];

  // 今日状态：live 查询成功以 live 为准（与 KPI/表格同一套判定），
  // 没有实时数据时回落巡检历史。正常 = 灰字，异常 = 橙色徽章（色彩纪律）
  let todayHtml;
  if (live && live.finished != null) {
    todayHtml = live.finished ? '<span class="dim">✔ 今日已完成</span>'
                              : '<span class="badge warn">未完成</span>';
  } else if (!today || !today.known) {
    todayHtml = '<span class="dim">今日无数据</span>';
  } else if (today.finished) {
    todayHtml = '<span class="dim">✔ 今日已完成</span>';
  } else {
    todayHtml = '<span class="badge warn">未完成 ' + (today.unfinished_tasks || 0) + ' 项</span>';
  }

  // 体力：来自「信息查询」最近一次结果（未查过则提示）
  let stamina;
  if (live && live.percent != null) {
    const hot = live.percent >= 85;
    stamina = '<div class="gcard-stam"><span class="dim">' + escHtml(live.name || '体力') +
      '</span><span class="sbar"><i class="' + (hot ? 'hot' : '') +
      '" style="width:' + live.percent + '%"></i><i class="progress-glow' +
      (hot ? ' hot' : '') + '" style="left:' + live.percent + '%"></i></span>' +
      '<span class="mono">' + live.current + '/' + live.maximum + '</span>' +
      staminaEtaHtml(gid, live) + '</div>';
  } else {
    stamina = '<div class="gcard-stam"><span class="dim">体力</span>' +
      '<span class="dim">到「信息查询」查询后显示</span></div>';
  }

  const missed = Number(g.month_missed || 0);
  return '<div class="card gcard">' +
    '<div class="gcard-head">' + gameIconHtml(gid) +
      '<div class="gcard-name"><b>' + escHtml(gameBaseName(g)) + '</b>' +
        '<small>' + (today && today.last ? escHtml(shortStamp(today.last)) : '—') + '</small></div>' +
      '<div class="gcard-streak"><b class="value">' + Number(g.streak || 0) + '</b><small>连续天数</small></div>' +
    '</div>' +
    '<div class="gcard-today">' + todayHtml + '</div>' +
    /* 进度透明化：直接展示每条判定任务的进度（live 优先） */
    taskProgressHtml(live && live.tasks) +
    stamina +
    '<div class="gcard-foot">本月完成 <b>' + Number(g.month_done || 0) + '</b> 天' +
      (missed > 0 ? ' · <span class="err-t">漏 ' + missed + '</span>' : '') +
    '</div></div>';
}

/* GitHub 式热力图：行 = 游戏，列 = 日期；绿=完成 红=未完成 灰=无数据 */
function renderHeatmap(d){
  const box = document.getElementById('h-table');
  if (!box) return;
  const days = d.days || [];
  const tracked = d.tracked || [];
  if (!tracked.length) {
    box.innerHTML = '<div class="emptybox">没有启用任何游戏账号。</div>';
    return;
  }
  const nameOf = {};
  (d.games || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });

  let html = '<div class="heatmap">';
  html += '<div class="hm-row hm-head"><div class="hm-label"></div>' +
    days.map(function(row){
      return '<div class="hm-cell' + (row.is_today ? ' today' : '') + '">' +
        escHtml(String(row.label || '').replace(/^\d{4}-/, '')) + '</div>';
    }).join('') + '</div>';

  tracked.forEach(function(gid){
    html += '<div class="hm-row"><div class="hm-label">' +
      '<span class="gdot" style="background:' + gameDotColor(gid) + '"></span>' +
      escHtml(gameBaseName({ game: gid, game_name: nameOf[gid] })) + '</div>';
    days.forEach(function(row){
      const c = (row.games || {})[gid];
      let cls = 'na', mark = '';
      if (c && c.known) {
        cls = c.finished ? 'ok' : 'err';
        mark = c.finished ? '✓' : '✗';
      } else if (row.is_today) {
        /* 今天的定时巡检还没到：画"等待中"灰点，绝不提前画红叉 */
        cls = 'na wait';
        mark = '⋯';
      }
      const tip = (c && c.known)
        ? (row.label + ' · ' + (c.finished ? '已完成'
            : '未完成 ' + (c.unfinished_tasks || 0) + ' 项'))
        : (row.is_today ? (row.label + ' · 等待今日巡检')
                        : (row.label + ' · 无数据'));
      html += '<div class="hm-cell ' + cls + (row.is_today ? ' today' : '') +
        '" title="' + escHtml(tip) + '">' + mark + '</div>';
    });
    html += '</div>';
  });

  html += '</div>' +
    '<div class="hm-legend">' +
    '<span class="hm-cell ok">✓</span>完成' +
    '<span class="hm-cell err">✗</span>未完成' +
    '<span class="hm-cell na">⋯</span>等待中/无数据' +
    '</div>';
  box.innerHTML = html;
}

function loadHistory(){
  kpiBusy(true);
  fetch('/api/history?days=7')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(d){
      try {
        cacheSet('history', d);   // 落缓存，下次秒开
        renderHistory(d);
        renderKpiToday(d);
      } catch (e) {
        console.error('渲染历史报表失败', e);
        renderHistoryEmpty('报表渲染出错，请点「刷新」重试。');
      }
    })
    .catch(function(err){
      console.error('读取历史记录失败', err);
      /* 有缓存时静默失败：上次的数据还在页面上，不打断用户 */
      if (!lastHistory) {
        renderHistoryEmpty('暂无历史记录，等待下一次定时检查写入。',
                           err && err.message ? '（' + err.message + '）' : '');
      }
    })
    .then(function(){ kpiBusy(false); });
}

document.getElementById('btn-history-refresh').addEventListener('click', loadHistory);

/* ========================= 主题切换 ========================= */
/* auto = 跟随系统，light / dark = 手动固定；选择存在 localStorage，重启后保留。
   CSS 变量只认 html[data-theme="dark"|"light"] 两组 Token；
   auto 在这里解析成系统实际值后同样落到 data-theme 上，杜绝两边逻辑分叉。 */
const THEME_ORDER = ['auto', 'light', 'dark'];
const THEME_NAMES = { auto: '跟随系统', light: '浅色', dark: '深色' };
let themeCur = 'auto';
try { themeCur = localStorage.getItem('rl-theme') || 'auto'; } catch (e) {}
if (THEME_ORDER.indexOf(themeCur) < 0) themeCur = 'auto';

const systemDark = window.matchMedia('(prefers-color-scheme: dark)');

function applyTheme(){
  const mode = themeCur === 'auto' ? (systemDark.matches ? 'dark' : 'light') : themeCur;
  document.documentElement.dataset.theme = mode;
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = '主题：' + THEME_NAMES[themeCur];
}

applyTheme();
document.getElementById('theme-btn').addEventListener('click', function(){
  themeCur = THEME_ORDER[(THEME_ORDER.indexOf(themeCur) + 1) % THEME_ORDER.length];
  try { localStorage.setItem('rl-theme', themeCur); } catch (e) {}
  applyTheme();
});
/* 跟随系统时，系统深浅变化实时切换 */
if (systemDark.addEventListener) {
  systemDark.addEventListener('change', function(){ if (themeCur === 'auto') applyTheme(); });
}

/* ========================= 推送预览 ========================= */
function renderPushPreview(h){
  const titleEl = document.getElementById('pv-title');
  const bodyEl = document.getElementById('pv-body');
  const chEl = document.getElementById('pv-channel');
  if (!titleEl || !bodyEl) return;

  const channels = [];
  const sc = document.getElementById('serverchan-enabled');
  const qm = document.getElementById('qmsg-enabled');
  if (sc && sc.checked) channels.push('Server酱');
  if (qm && qm.checked) channels.push('Qmsg 酱');
  if (chEl) chEl.textContent = channels.length
    ? ('推送到 ' + channels.join(' + ')) : '未启用推送渠道';

  const days = (h && h.days) || [];
  let row = null;
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) { row = days[i]; break; }
  }
  if (!row && days.length) row = days[days.length - 1];

  if (!row) {
    titleEl.textContent = '游戏日常：等待第一次巡检';
    bodyEl.textContent = '还没有巡检数据。定时检查跑过一轮后，这里会按真实结果显示推送样式。';
    return;
  }

  const cells = row.games || {};
  const nameOf = {};
  ((h && h.games) || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });
  const tracked = (h && h.tracked) || [];
  const done = [], miss = [];
  let lastTime = '';
  tracked.forEach(function(gid){
    const c = cells[gid];
    if (!c || !c.known) return;
    if (c.last && c.last > lastTime) lastTime = c.last;
    if (c.finished) done.push(nameOf[gid] || gid);
    else miss.push((nameOf[gid] || gid) +
      (c.unfinished_tasks ? '（漏 ' + c.unfinished_tasks + ' 项）' : ''));
  });
  const total = done.length + miss.length;
  const allDone = total > 0 && !miss.length;
  titleEl.textContent = allDone
    ? ('游戏日常：' + total + '/' + total + ' 全部完成')
    : ('游戏日常：' + miss.length + '/' + (total || tracked.length) + ' 个未完成');

  const lines = [];
  lines.push((row.label || '') + (row.weekday ? ' ' + row.weekday : '') +
             (lastTime ? ' · ' + lastTime : ''));
  miss.forEach(function(m){ lines.push('[未完成] ' + m); });
  if (done.length) lines.push('[已完成] ' + done.join('、'));
  lines.push(allDone ? '今天的日常全部完成，可以放心摆烂了。'
                     : '别拖到 23:59，记得把日常清了。');
  bodyEl.textContent = lines.join('\n');
}

function loadPushPreview(){
  fetch('/api/history?days=1').then(function(r){ return r.json(); })
    .then(renderPushPreview)
    .catch(function(){
      const t = document.getElementById('pv-title');
      if (t) t.textContent = '游戏日常：预览不可用';
      const b = document.getElementById('pv-body');
      if (b) b.textContent = '读取巡检数据失败，请稍后重试。';
    });
}

/* ========================= 今日巡检动态 ========================= */
function renderRunLog(h){
  const box = document.getElementById('run-log');
  if (!box) return;
  const days = (h && h.days) || [];
  const nameOf = {};
  ((h && h.games) || []).forEach(function(g){ nameOf[g.game] = g.game_name || g.game; });
  const tracked = (h && h.tracked) || [];
  const rows = days.filter(function(row){
    const cells = row.games || {};
    return Object.keys(cells).some(function(k){ return cells[k] && cells[k].known; });
  }).slice(-3).reverse();

  if (!rows.length) {
    box.innerHTML = '<div class="emptybox">还没有巡检记录，等第一次定时检查跑完后这里会有动态。</div>';
    return;
  }
  box.innerHTML = rows.map(function(row){
    const cells = row.games || {};
    const done = [], miss = [];
    let lastTime = '';
    tracked.forEach(function(gid){
      const c = cells[gid];
      if (!c || !c.known) return;
      if (c.last && c.last > lastTime) lastTime = c.last;
      if (c.finished) done.push(nameOf[gid] || gid);
      else miss.push(nameOf[gid] || gid);
    });
    const summary = miss.length
      ? '<span class="pill no">' + miss.length + ' 款未完成</span>'
      : '<span class="dim">全部完成</span>';
    const detail = miss.length
      ? '<span class="log-detail">未完成：' + escHtml(miss.join('、')) + '</span>' : '';
    return '<div class="log-row">' +
      '<span class="log-date">' + escHtml(row.label || '') +
        ' <span class="dim">' + escHtml(row.weekday || '') +
        (row.is_today ? ' · 今天' : '') + '</span></span>' +
      '<span class="log-time">' + escHtml(lastTime || '--:--') + '</span>' +
      summary + detail +
      '</div>';
  }).join('');
}

function loadRunLog(){
  fetch('/api/history?days=7').then(function(r){ return r.json(); })
    .then(renderRunLog)
    .catch(function(){
      const box = document.getElementById('run-log');
      if (box) box.innerHTML = '<div class="emptybox">读取巡检记录失败，请点「刷新」重试。</div>';
    });
}

document.getElementById('btn-log-refresh')
  .addEventListener('click', loadRunLog);

/* ========================= 全局 Tooltip ========================= */
const tipBox = document.createElement('div');
tipBox.id = 'tip';
document.body.appendChild(tipBox);

document.addEventListener('mouseover', function(e){
  const el = e.target.closest ? e.target.closest('[data-tip]') : null;
  if (!el) return;
  const text = el.getAttribute('data-tip') || '';
  if (!text) return;
  tipBox.textContent = text;
  tipBox.classList.add('on');          // 先显示才能量出气泡尺寸
  const r = el.getBoundingClientRect();
  const tw = tipBox.offsetWidth, th = tipBox.offsetHeight;
  let x = r.left + r.width / 2 - tw / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
  let y = r.top - th - 8;              // 默认在图标上方，贴顶时翻到下方
  if (y < 8) y = r.bottom + 8;
  tipBox.style.left = Math.round(x) + 'px';
  tipBox.style.top = Math.round(y) + 'px';
});
document.addEventListener('mouseout', function(e){
  if (e.target.closest && e.target.closest('[data-tip]')) tipBox.classList.remove('on');
});
window.addEventListener('scroll', function(){ tipBox.classList.remove('on'); }, true);

/* ========================= KPI 摘要条 ========================= */
function kpiSet(id, text){
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* 紧迫感系统：把 warn/critical 写到 KPI 卡片的 data-urgency 上 */
function setUrgency(id, level){
  const el = document.getElementById(id);
  if (!el) return;
  const card = el.closest('.kpi');
  if (card) card.dataset.urgency = level;
}

/* KPI 智能吸顶：滚动容器是 .main（不是 window） */
const kpiBarEl = document.getElementById('kpi-bar');
const kpiMainEl = document.querySelector('.main');

function kpiUrgencyScore(card){
  const u = card.dataset.urgency;
  return u === 'critical' ? 2 : (u === 'warn' ? 1 : 0);
}

/* compact 态只保留最紧急的 2 个信号（不足 2 个紧急时用普通卡补位） */
function refreshKpiKeep(){
  if (!kpiBarEl) return;
  const cards = Array.prototype.slice.call(kpiBarEl.querySelectorAll('.kpi'));
  const sorted = cards.slice().sort(function(a, b){
    return kpiUrgencyScore(b) - kpiUrgencyScore(a);
  });
  sorted.forEach(function(c, i){ c.classList.toggle('keep', i < 2); });
}

function updateKpiCompact(){
  if (!kpiBarEl || !kpiMainEl) return;
  kpiBarEl.classList.toggle('compact', kpiMainEl.scrollTop > 80);
}
if (kpiMainEl) kpiMainEl.addEventListener('scroll', updateKpiCompact, {passive:true});
updateKpiCompact();

/* 今日完成：巡检历史 + 实时查询合并统计。
   数据源纪律：/api/live 查询成功的游戏以 live 为准（现场状态最权威）；
   live 没查到/查询失败的回落 /api/history 的粘性完成。两路永远不会打架。 */
function renderKpiToday(h){
  const el = document.getElementById('k-today');
  if (!el) return;
  const days = (h && h.days) || [];
  let row = null;
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].is_today) { row = days[i]; break; }
  }
  const tracked = (h && h.tracked) || [];
  const cells = (row && row.games) || {};
  const liveMap = window.__liveMap || {};
  let done = 0, known = 0;
  tracked.forEach(function(gid){
    const lv = liveMap[gid];
    if (lv && lv.finished != null) {      // live 说了算
      known++;
      if (lv.finished) done++;
      return;
    }
    const c = cells[gid];
    if (!c || !c.known) return;           // 两路都没有 → 不计入
    known++;
    if (c.finished) done++;
  });
  const total = tracked.length;
  if (!total) {
    el.textContent = '–';
    el.className = 'kpi-value value';
    setUrgency('k-today', '');
    kpiSet('k-today-sub', '没有启用任何游戏');
    return;
  }
  el.textContent = done + '/' + total;
  el.className = 'kpi-value value' + (known && done !== total ? ' warn' : '');
  /* 0 完成 = critical；部分完成 = warn；全部完成 = 无紧迫（紧迫度只看合并结果，不碰统计） */
  setUrgency('k-today', !known ? '' : (done === total ? '' : (done > 0 ? 'warn' : 'critical')));
  refreshKpiKeep();
  kpiSet('k-today-sub', !known ? '今日还没检查过，查询中…'
    : (done === total ? '今日全部完成' : (total - done) + ' 款未完成'));
}

/* 凭证健康：已绑定凭证的账号 / 启用账号 */
function renderKpiCred(d){
  const el = document.getElementById('k-cred');
  if (!el) return;
  const items = (d && d.items) || [];
  if (!items.length) {
    el.textContent = '–';
    el.className = 'kpi-value value';
    setUrgency('k-cred', '');
    kpiSet('k-cred-sub', '没有账号');
    return;
  }
  const bound = items.filter(function(i){ return i.has_credential; }).length;
  el.textContent = bound + '/' + items.length;
  el.className = 'kpi-value value' + (bound === items.length ? '' : ' warn');
  setUrgency('k-cred', bound === items.length ? '' : 'warn');
  refreshKpiKeep();
  kpiSet('k-cred-sub', bound === items.length ? '全部已绑定'
    : (items.length - bound) + ' 个未绑定');
}

/* =========================== 心跳：已运行 + 下次巡查 ===========================
   全局唯一的 heartbeatTimer（30 秒一拍），「已运行」和「下次巡查」都从这里刷。
   计算一律 target - Date.now()：起点只认 Python 端给的数据（pid 文件 started_at /
   config 的巡查时刻），严禁拿页面打开时刻之类伪造守护进程的数据。 */
let heartbeatTimer = null;
const PAGE_BOOT = Date.now();   // 仅用于「本次会话」兜底显示，与守护进程数据无关
const hbState = { running: false, startedAtRaw: '', startedAt: null, times: [] };

function parseStamp(text){
  const m = String(text || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
}

function fmtUptime(ms){
  const min = Math.floor(ms / 60000);
  if (min < 1) return '不到 1 分钟';
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
  if (d > 0) return d + ' 天 ' + h + ' 小时';
  if (h > 0) return h + ' 小时 ' + m + ' 分';
  return m + ' 分钟';
}

/* 最近的下一个巡查点：每次 tick 都重算；已过今日时刻自动指向明日同一时刻，
   返回值永远晚于 now（归零后下一拍自动切到下一个点，永不出现负数） */
function computeNextRun(now){
  let best = null;
  hbState.times.forEach(function(t){
    const m = String(t).trim().match(/^(\d{1,2}):(\d{1,2})$/);
    if (!m) return;
    const cand = new Date(now.getFullYear(), now.getMonth(), now.getDate(), +m[1], +m[2]);
    if (cand.getTime() <= now.getTime()) cand.setDate(cand.getDate() + 1);
    if (!best || cand.getTime() < best.getTime()) best = cand;
  });
  return best;
}

function renderUptime(){
  const el = document.getElementById('d-since');
  if (!el) return;
  if (!hbState.running) { el.textContent = '-'; el.title = ''; return; }
  if (hbState.startedAt) {
    el.textContent = '已运行 ' + fmtUptime(Date.now() - hbState.startedAt.getTime());
    el.title = '启动于 ' + hbState.startedAtRaw;
  } else {
    /* started_at 拿不到（老版 pid 文件只有 pid）：老实标注是控制台会话时长 */
    el.textContent = '本次会话：' + fmtUptime(Date.now() - PAGE_BOOT);
    el.title = '守护进程未上报启动时间，此处显示本控制台页面的打开时长';
  }
}

function renderNextRun(){
  const el = document.getElementById('k-next');
  const sub = document.getElementById('k-next-sub');
  if (!el || !sub) return;
  if (!hbState.running) {
    el.textContent = '已暂停';
    sub.textContent = '守护进程未运行';
    return;
  }
  const next = computeNextRun(new Date());
  if (!next) { el.textContent = '–'; sub.textContent = '未设置巡查时间点'; return; }
  const pad = function(n){ return (n < 10 ? '0' : '') + n; };
  el.textContent = pad(next.getHours()) + ':' + pad(next.getMinutes());
  const min = Math.round(Math.max(0, next.getTime() - Date.now()) / 60000);
  const h = Math.floor(min / 60), m = min % 60;
  sub.textContent = min === 0 ? '即将执行'
    : (h > 0 ? h + ' 小时 ' + m + ' 分后' : m + ' 分钟后');
}

function tickHeartbeat(){ renderUptime(); renderNextRun(); }

function startHeartbeat(){
  if (!heartbeatTimer) heartbeatTimer = setInterval(tickHeartbeat, 30000);
}
function stopHeartbeat(){
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
}

/* 体力临满：最近一次「信息查询」结果里 ≥85% 的游戏数 */
function renderKpiResin(d){
  const el = document.getElementById('k-resin');
  if (!el) return;
  if (!d || d.ok === false) return;
  const games = d.games || [];
  let hot = 0, withRes = 0;
  games.forEach(function(g){
    if (!g.ok || !g.resource || g.resource.percent == null) return;
    withRes++;
    if (g.resource.percent >= 85) hot++;
  });
  el.textContent = withRes ? String(hot) : '–';
  el.className = 'kpi-value value' + (hot ? ' warn' : '');
  setUrgency('k-resin', hot > 0 ? 'warn' : '');
  refreshKpiKeep();
  kpiSet('k-resin-sub', withRes
    ? (hot ? hot + ' 款即将溢出' : '体力都还充裕')
    : '到「信息查询」查询后显示');
}

/* ========================= 24 小时标尺 ========================= */
function renderRuler(times){
  const box = document.getElementById('d-ruler');
  if (!box) return;
  let html = '<div class="ruler-track"></div>';
  [0, 6, 12, 18, 24].forEach(function(h){
    html += '<div class="ruler-tick" style="left:' + (h / 24 * 100) + '%">' +
      (h < 10 ? '0' + h : h) + ':00</div>';
  });
  if (!times.length) {
    html += '<div class="ruler-empty">还没有时间点，在下面添加</div>';
  }
  times.forEach(function(t){
    const m = String(t).match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return;
    const pct = ((+m[1] * 60 + +m[2]) / 1440 * 100).toFixed(3);
    html += '<div class="ruler-dot" style="left:' + pct + '%" title="' + t + '">' +
            '<span>' + t + '</span></div>';
  });
  box.innerHTML = html;
}

/* ========================= 底部日志控制台 ========================= */
let consoleLoaded = false;

function renderConsole(d){
  const body = document.getElementById('console-body');
  const last = document.getElementById('console-last');
  if (!body) return;
  if (!d || d.ok === false) {
    body.innerHTML = '<span class="console-empty">' +
      escHtml((d && d.message) || '读取失败，请点「刷新」重试。') + '</span>';
    return;
  }
  const lines = d.lines || [];
  if (!lines.length) {
    body.innerHTML = '<span class="console-empty">还没有日志。</span>';
    if (last) last.textContent = '暂无日志';
    return;
  }
  body.innerHTML = lines.map(function(line){
    let cls = 'cline';
    if (/ERROR|CRITICAL/.test(line)) cls += ' c-err';
    else if (/WARNING/.test(line)) cls += ' c-warn';
    else if (/推送成功/.test(line)) cls += ' c-ok';
    const marked = escHtml(line).replace(
      /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/, '<span class="c-time">$1</span>');
    return '<div class="' + cls + '">' + marked + '</div>';
  }).join('');
  body.scrollTop = body.scrollHeight;
  if (last) last.textContent = lines[lines.length - 1].replace(/^\S+\s+/, '');
}

function loadConsole(){
  fetch('/api/logtail?n=200').then(function(r){ return r.json(); })
    .then(function(d){ consoleLoaded = true; renderConsole(d); })
    .catch(function(){
      const body = document.getElementById('console-body');
      if (body) body.innerHTML = '<span class="console-empty">读取日志失败，请点「刷新」。</span>';
    });
}

document.getElementById('log-console').addEventListener('toggle', function(){
  if (this.open && !consoleLoaded) loadConsole();
});
document.getElementById('btn-console-refresh').addEventListener('click', function(e){
  // 阻止 summary 的默认展开/收起行为，只刷新
  e.preventDefault();
  e.stopPropagation();
  loadConsole();
});

/* =========================== 启动 =========================== */
/* 扫码选择弹窗：勾选变化时同步「下一步」按钮可用性（至少勾一款） */
const pickList = document.getElementById('pick-list');
if (pickList) pickList.addEventListener('change', updatePickNext);
/* 秒开：先把 localStorage 里的上次数据画出来（KPI/报表/实时面板），
   下面的 switchTab 会发起真实请求做后台静默刷新，数据到手平滑替换 */
renderCachedData();
const hash = (location.hash || '').replace('#', '');
switchTab(PANEL_NAMES.indexOf(hash) >= 0 ? hash : 'history');
refreshNotice();
loadDevice();
loadAccounts();      // 用 config 里的真实巡检开关覆盖页面默认勾选
loadThresholds();
loadSchedule();      // 状态先拉一次，展开时不会闪空
loadAutostart();
