/* 功能② 前端单元测试：staminaEtaHtml 体力预计满员时间
 * 从 rl_script.js 抽取纯函数，MockDate 固定时刻跑边界用例。
 * 运行前提：先从 login_ui.py 的 HTML_PAGE 提取 <script> 存为 rl_script.js。
 */
'use strict';
const fs = require('fs');
const src = fs.readFileSync('rl_script.js', 'utf8');

const constM = src.match(/const RECOVERY_RATE_PER_MIN = \{[^}]*\};/);
if (!constM) { console.error('FAIL: RECOVERY_RATE_PER_MIN 未找到'); process.exit(1); }
const fnM = src.match(/function staminaEtaHtml[\s\S]*?\n\}/);
if (!fnM) { console.error('FAIL: staminaEtaHtml 未找到'); process.exit(1); }

/* 固定「现在」= 2026-10-29 18:00:00 本地时间（MockDate 模式，禁止真实时钟漂移） */
const FIXED = new Date(2026, 9, 29, 18, 0, 0).getTime();
class MockDate extends Date {
  constructor(...args) { super(args.length === 0 ? FIXED : args[0]); }
  static now() { return FIXED; }
}

const make = new Function('Date',
  constM[0] + '\n' + fnM[0] + '\nreturn { eta: staminaEtaHtml, RATES: RECOVERY_RATE_PER_MIN };');
const api = make(MockDate);
const eta = api.eta;

let pass = 0, fail = 0;
function check(name, actual, expect) {
  if (actual === expect) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + '\n  期望: ' + expect + '\n  实际: ' + actual); }
}
function has(name, actual, needle) {
  if (String(actual).indexOf(needle) >= 0) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + '\n  应包含: ' + needle + '\n  实际: ' + actual); }
}
function lacks(name, actual, needle) {
  if (String(actual).indexOf(needle) < 0) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + '\n  不应包含: ' + needle + '\n  实际: ' + actual); }
}

/* T1 已满 → 「已满」，无橙色 */
check('T1 已满', eta('zzz', { current: 200, maximum: 200 }), '<span class="eta">已满</span>');

/* T2 常数表速率：原神 150/160，10 点 × 8 分 = 80 分钟 → 19:20，无橙色 */
check('T2 原神速率表 19:20',
  eta('genshin', { current: 150, maximum: 160 }),
  '<span class="eta">预计 19:20 满</span>');

/* T3 不足 1 小时 → eta-soon 橙色：星铁 238/240，2×6=12 分钟 → 18:12 */
has('T3 橙色类', eta('starrail', { current: 238, maximum: 240 }), 'eta-soon');
has('T3 时刻 18:12', eta('starrail', { current: 238, maximum: 240 }), '预计 18:12 满');

/* T4 API 真值优先于速率表：原神 recover=4900 秒 → 19:21（速率表会算出 19:20） */
check('T4 API 恢复字段优先',
  eta('genshin', { current: 150, maximum: 160, recover: 4900 }),
  '<span class="eta">预计 19:21 满</span>');

/* T5 超过 24 小时 → 「X 天后满」：100000 秒 = 1.157 天 → 1.2 */
check('T5 跨天显示',
  eta('genshin', { current: 20, maximum: 160, recover: 100000 }),
  '<span class="eta">预计 1.2 天后满</span>');

/* T6 数据缺失 → 空串 */
check('T6a resource 为空', eta('genshin', null), '');
check('T6b maximum=0', eta('genshin', { current: 5, maximum: 0 }), '');
check('T6c current 缺失', eta('genshin', { maximum: 160 }), '');

/* T7 recover=0（接口给了但为 0）且未满 → 回落常数表 */
check('T7 recover=0 回落速率表',
  eta('genshin', { current: 150, maximum: 160, recover: 0 }),
  '<span class="eta">预计 19:20 满</span>');

/* T8 恰好 1 小时（3600 秒）不算「快满」→ 无橙色：绝区零 190/200，10×6=60 分钟 */
lacks('T8 3600 秒无橙色', eta('zzz', { current: 190, maximum: 200 }), 'eta-soon');
has('T8 时刻 19:00', eta('zzz', { current: 190, maximum: 200 }), '预计 19:00 满');

/* T9 溢出边界（current > maximum）也显示已满 */
check('T9 超上限视为已满', eta('arknights', { current: 140, maximum: 135 }), '<span class="eta">已满</span>');

/* T10 速率表内容正确性（用户可审计） */
check('T10a 原神 8 分/点', api.RATES.genshin, 8);
check('T10b 五款齐全', Object.keys(api.RATES).sort().join(','),
  'arknights,endfield,genshin,starrail,zzz');

console.log('\n=== rl_test_eta.js: ' + pass + ' PASS / ' + fail + ' FAIL ===');
process.exit(fail ? 1 : 0);
