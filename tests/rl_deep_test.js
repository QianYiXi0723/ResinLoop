/* 深度自动化测试（Node 侧）：测试 2 倒计时防节流 + 测试 4 事件监听防重复绑定
   + 圆点状态运行时断言。直接从 login_ui.py 提取 <script>，不起浏览器。 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const Real = Date;

let failed = 0;
function check(name, ok, detail) {
  if (!ok) failed++;
  console.log((ok ? 'ok  : ' : 'FAIL: ') + name + (detail !== undefined ? '  | ' + JSON.stringify(detail) : ''));
}
function section(t) { console.log('== ' + t + ' =='); }

const jsSrc = fs.readFileSync(require('path').join(__dirname, '..', 'ui', 'app.js'), 'utf8');
if (!jsSrc) { console.error('FAIL: 无法提取 <script>'); process.exit(1); }

/* ================= 测试 2：JS 倒计时逻辑（Mock Date 全路径） ================= */
section('测试 2：JS 倒计时逻辑（防后台节流，Mock Date）');
const hbStart = jsSrc.indexOf('/* =========================== 心跳');
const hbEnd = jsSrc.indexOf('/* 体力临满');
const hb = jsSrc.slice(hbStart, hbEnd);
check('T2.0 静态扫描：心跳段无递减计数器（timeLeft / -= 1 零命中）',
  !/timeLeft|-=\s*1/.test(hb));

const box = { now: new Real(2026, 9, 29, 21, 0, 0).getTime() };  // 今天 21:00:00
function makeMockDate() {
  function M(...args) {
    if (args.length === 0) return new Real(box.now);
    return new Real(...args);
  }
  M.now = () => box.now;
  M.prototype = Real.prototype;
  return M;
}
const els2 = {};
const doc2 = { getElementById(id) {
  if (!els2[id]) els2[id] = { id, textContent: '', value: '', title: '', checked: false, dataset: {}, style: {} };
  return els2[id];
} };
const ctx2 = { console, Date: makeMockDate(), document: doc2 };
vm.createContext(ctx2);
vm.runInContext(hb + '\nthis.__api = { refresh: tickHeartbeat, setTimes: function(t){ hbState.times = t; },'
  + ' setRunning: function(r){ hbState.running = r; }, computeNextRun: computeNextRun,'
  + ' diffToNext: function(){ var n = computeNextRun(new Date()); return n ? n.getTime() - Date.now() : null; },'
  + ' nextTarget: function(){ var n = computeNextRun(new Date()); return n ? n.getTime() : null; } };', ctx2);
const api2 = ctx2.__api;

api2.setTimes(['22:00']);           // 目标 22:00:00，当前 mock 在 21:00:00
api2.setRunning(true);              // 守护进程运行中（否则显示函数走「已暂停」分支）
api2.refresh();
const diff0 = api2.diffToNext();    // diff 在 VM 内用 mock 时钟计算，避免混入宿主真实时钟
check('T2.1 21:00:00 时目标解析为 22:00，剩余恰 60 分钟（3600000ms）',
  els2['k-next'].textContent === '22:00' && diff0 === 3600000,
  { value: els2['k-next'].textContent, sub: els2['k-next-sub'].textContent, diffMs: diff0 });
check('T2.2 21:00 显示 "1 小时 0 分后"', els2['k-next-sub'].textContent === '1 小时 0 分后',
  els2['k-next-sub'].textContent);

/* 模拟浏览器切后台 3 分钟：mock 时钟直接 +180000ms，期间一次 tick 都没跑
   （节流场景）。若是 timeLeft -= 1 计数器，此刻仍会显示 60 分钟 → 必 FAIL */
box.now += 180000;                  // 21:03:00
api2.refresh();
const diff1 = api2.diffToNext();
check('T2.3 跳 180 秒后（零 tick）剩余恰 57 分钟 = 3420000ms，而非 59分57秒',
  els2['k-next-sub'].textContent === '57 分钟后' && diff1 === 3420000,
  { sub: els2['k-next-sub'].textContent, diffMs: diff1 });
const tgt1 = api2.nextTarget();
check('T2.4 目标时刻未被改动（同一 22:00，证明是 target - now 而非递减）',
  tgt1 === new Real(2026, 9, 29, 22, 0, 0).getTime(),
  { targetMs: tgt1, expectMs: new Real(2026, 9, 29, 22, 0, 0).getTime() });
box.now += 60000;                   // 21:04:00 → 56 分钟
api2.refresh();
check('T2.5 再走 1 分钟 → "56 分钟后"（随真实时刻线性推进）',
  els2['k-next-sub'].textContent === '56 分钟后', els2['k-next-sub'].textContent);

/* ================= 测试 4：事件监听防重复绑定（全脚本 + 录制式 DOM 桩） ================= */
section('测试 4：事件监听防重复绑定（全脚本求值 + 两次 Tab 切换）');
const bindings = [];
let anon = 0;
function makeEl(id) {
  return {
    id, textContent: '', value: '', innerHTML: '', title: '', placeholder: '',
    checked: false, disabled: false, hidden: false, className: '',
    dataset: {}, style: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev) { bindings.push({ id, ev }); },
    removeEventListener() {}, setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {}, select() {}, click() {},
    insertAdjacentHTML() {}, appendChild() {}
  };
}
const els = {};
const doc = {
  getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(ev) { bindings.push({ id: 'document', ev }); },
  body: null, documentElement: null, activeElement: null,
  createElement() { return makeEl('anon-' + (anon++)); }
};
doc.body = makeEl('body'); doc.documentElement = makeEl('html');
const win = {
  addEventListener(ev) { bindings.push({ id: 'window', ev }); },
  removeEventListener() {}, matchMedia() { return { matches: true, addEventListener() {}, addListener() {} }; },
  scrollTo() {}, open() {}
};
const ctx = {
  console, window: win, document: doc,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  location: { hash: '', href: 'http://127.0.0.1:8765/' },
  navigator: { userAgent: 'rl-deep-test' },
  fetch() { return new Promise(function () {}); },          // 永不落定：网络路径全部挂起
  setTimeout() { return 0; }, setInterval() { return 0; },
  clearTimeout() {}, clearInterval() {}, requestAnimationFrame() {},
  alert() {}, confirm() { return false; }, prompt() { return null; }
};
vm.createContext(ctx);
let evalErr = null;
try { vm.runInContext(jsSrc, ctx); }
catch (e) { evalErr = e; }
check('T4.1 全脚本在 DOM 桩中求值无异常', evalErr === null, evalErr ? String(evalErr) : 'ok');

/* 首屏运行时证明（呼应测试 1 的静态断言）：IIFE 确实跑过、摘要确实被刷 */
check('T4.2 初始化后摘要刷新（自启摘要已按冗余清理需求删除，其余三格就绪）',
  els['sum-autostart'] === undefined &&
  els['sum-catchup'] && els['sum-catchup'].textContent === '读取中…',
  { sumAutostart: typeof els['sum-autostart'], catchup: els['sum-catchup'] && els['sum-catchup'].textContent });

function countMap() {
  const m = {};
  bindings.forEach(function (b) { const k = b.id + '|' + b.ev; m[k] = (m[k] || 0) + 1; });
  return m;
}
const before = countMap();
const dupInit = Object.keys(before).filter(function (k) { return before[k] !== 1; });
check('T4.3 初始化后每个 (元素,事件) 对恰好绑定 1 次（含 document/window）',
  dupInit.length === 0, { 总绑定数: bindings.length, 重复项: dupInit });
const spots = { 'autostart-toggle|change': 1, 'serverchan-key|input': 1, 'serverchan-key|change': 1,
  'btn-save|click': 1, 'window|scroll': 1, 'document|click': 1 };
const spotBad = Object.keys(spots).filter(function (k) { return before[k] !== spots[k]; });
check('T4.4 抽查：关键节点各事件绑定数恰为 1', spotBad.length === 0,
  { 抽查: before, 未达预期: spotBad });

/* 模拟连续两次 Tab 切换（switchTab 只切 display + 发 fetch，不重建 DOM） */
ctx.switchTab('notify');
ctx.switchTab('schedule');
ctx.switchTab('notify');
const after = countMap();
const grew = Object.keys(after).filter(function (k) { return after[k] !== before[k]; });
check('T4.5 两次 Tab 切换后绑定数零增长（无重复叠加）',
  bindings.length === Object.keys(before).reduce(function (a, k) { return a + before[k]; }, 0) && grew.length === 0,
  grew);
const st0 = jsSrc.indexOf('function switchTab');
const stSrc = jsSrc.slice(st0, jsSrc.indexOf('\nfunction ', st0 + 10));
check('T4.6 静态佐证：switchTab 函数体内无任何 addEventListener',
  stSrc.indexOf('addEventListener') === -1);

/* ================= 附加：圆点状态运行时断言（renderDaemon 真跑） ================= */
section('附加：圆点状态与暂停显示（renderDaemon 真实执行）');
ctx.renderDaemon({ running: true, pid: 1234, started_at: '2026-10-29 12:00:00',
  heartbeat_age: 3, check_times: ['22:00'], last_run: {} });
check('T5.1 运行中：d-dot className = "dot on"（绿点呼吸类）',
  els['d-dot'].className === 'dot on', els['d-dot'].className);
check('T5.2 运行中：徽章 = 运行中，且不含 err 红',
  els['d-state'].textContent === '运行中' && els['d-state'].className.indexOf('err') === -1,
  els['d-state'].textContent);
/* 状态条本身已有「运行」标签，d-since 只放时长（见 app.js renderDaemon 注释）。
   断言两头都查：确实算出了时长（数字开头），且没有重复的「已运行」前缀。 */
check('T5.3 已运行时长随真实 started_at 计算（纯时长，不重复「已运行」）',
  /^\d/.test(els['d-since'].textContent) &&
  els['d-since'].textContent.indexOf('已运行') < 0, els['d-since'].textContent);
ctx.renderDaemon({ running: false, pid: null, started_at: '',
  heartbeat_age: null, check_times: ['22:00'], last_run: {} });
check('T5.4 停止：d-dot className = "dot"（灰点静止，无 on/无红）',
  els['d-dot'].className === 'dot', els['d-dot'].className);
check('T5.5 停止：KPI 下次巡查 = 已暂停（不再给时刻）',
  els['k-next'].textContent === '已暂停', els['k-next'].textContent);

console.log(failed ? '\n[Node 侧结论] FAIL（' + failed + ' 项未过）' : '\n[Node 侧结论] 全部通过');
process.exit(failed ? 1 : 0);
