/* ============================================================
   定时与状态页 · 布局断言（直接粘进 DevTools Console 运行）
   先切到「定时与状态」页，再粘贴执行；返回 true = 全绿
   ============================================================ */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  /* ---------- 0. 元素存在性守卫：取不到就报错并终止，不抛 TypeError ---------- */
  const rulesCard = $('.card--rules');
  const timelineCard = $('.card--timeline');
  if (!rulesCard || !timelineCard) {
    console.error('[断言中止] 未找到 .card--rules 或 .card--timeline。'
      + '请确认当前在「定时与状态」页，且卡片 class 未被改动。');
    return false;
  }
  const panel = document.getElementById('tab-schedule');
  if (!panel || panel.getBoundingClientRect().height < 10) {
    console.error('[断言中止] #tab-schedule 不可见（还没切到「定时与状态」页），无法测量。');
    return false;
  }

  const out = [];
  const check = (name, pass, actual) => {
    out.push({ 检查项: name, 结果: pass ? 'PASS' : 'FAIL', 实测: String(actual) });
    return pass;
  };
  const px = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : NaN; };
  const tracks = (v) => String(v).trim().split(/\s+/).filter(Boolean);
  const wide = window.innerWidth >= 1100;
  let ok = true;

  try {
    /* ---------- 1. 卡片骨架：flex 列 + min-height:0 ---------- */
    const csCard = getComputedStyle(rulesCard);
    ok = check('.card 为 flex 列容器',
      csCard.display === 'flex' && csCard.flexDirection === 'column',
      csCard.display + ' / ' + csCard.flexDirection) && ok;
    ok = check('.card min-height:0',
      px(csCard.minHeight) === 0, csCard.minHeight) && ok;

    const bodyRules = $('.card--rules .card__body');
    const bodyTl = $('.card--timeline .card__body');
    if (!bodyRules || !bodyTl) {
      console.error('[断言中止] 缺少 .card__body（规则卡或时间轴卡）。');
      return false;
    }
    const csBody = getComputedStyle(bodyRules);
    ok = check('.card__body flex:1 1 auto',
      csBody.flexGrow === '1' && csBody.flexShrink === '1', csBody.flex) && ok;
    ok = check('.card__body min-height:0',
      px(csBody.minHeight) === 0, csBody.minHeight) && ok;

    /* ---------- 2. 根网格：两行两列 + 右卡跨两行 ---------- */
    const csPanel = getComputedStyle(panel);
    ok = check('#tab-schedule 是 grid',
      csPanel.display === 'grid', csPanel.display) && ok;
    ok = check('根网格两列（1.55fr / 1fr）',
      tracks(csPanel.gridTemplateColumns).length === 2, csPanel.gridTemplateColumns) && ok;
    ok = check('根网格两行（auto / minmax(0,1fr)）',
      tracks(csPanel.gridTemplateRows).length === 2, csPanel.gridTemplateRows) && ok;
    ok = check('align-items:stretch',
      csPanel.alignItems === 'stretch', csPanel.alignItems) && ok;

    const csTl = getComputedStyle(timelineCard);
    ok = check('时间轴跨两行 grid-row:1 / -1',
      csTl.gridRowStart === '1' && csTl.gridRowEnd === '-1',
      csTl.gridRowStart + ' / ' + csTl.gridRowEnd) && ok;

    /* ---------- 3. 底边齐平（仅 ≥1100px） ---------- */
    if (wide) {
      const b1 = rulesCard.getBoundingClientRect().bottom;
      const b2 = timelineCard.getBoundingClientRect().bottom;
      ok = check('左右卡片底边齐平（差 < 1px）',
        Math.abs(b1 - b2) < 1, 'Δ=' + Math.abs(b1 - b2).toFixed(3) + 'px') && ok;
    } else {
      out.push({ 检查项: '左右卡片底边齐平', 结果: 'SKIP', 实测: '窄屏单列不考核' });
    }

    /* ---------- 4. 无全页滚动（仅 ≥1100px） ---------- */
    if (wide) {
      ok = check('无全页滚动',
        document.body.scrollHeight <= window.innerHeight + 1,
        'body=' + document.body.scrollHeight + ' viewport=' + window.innerHeight) && ok;
      /* .main 是真正会滚的那个容器（body 被 .app overflow:hidden 锁死），
         它不滚才说明高度链真的没溢出 */
      const mainEl = $('.main');
      ok = check('主内容区无溢出滚动',
        !!mainEl && mainEl.scrollHeight <= mainEl.clientHeight + 1,
        mainEl ? mainEl.scrollHeight + ' / ' + mainEl.clientHeight : 'no .main') && ok;
    } else {
      out.push({ 检查项: '无全页滚动', 结果: 'SKIP', 实测: '窄屏单列允许整页滚' });
    }

    /* ---------- 5. 内部滚动区：overflow + 稳定滚动槽 + 横向不溢出 ---------- */
    [['规则卡', bodyRules], ['时间轴卡', bodyTl]].forEach(([label, el]) => {
      const cs = getComputedStyle(el);
      ok = check(label + ' 内部纵向滚动', cs.overflowY === 'auto', cs.overflowY) && ok;
      ok = check(label + ' scrollbar-gutter:stable',
        cs.scrollbarGutter === 'stable', cs.scrollbarGutter) && ok;
      ok = check(label + ' 横向不溢出',
        el.scrollWidth <= el.clientWidth + 1,
        el.scrollWidth + ' / ' + el.clientWidth) && ok;
    });

    /* ---------- 6. 时间轴：三列 / 76px / 连接线三件事 ---------- */
    const items = $$('.tl-item');
    ok = check('时间轴有节点', items.length >= 1, items.length + ' 项') && ok;
    if (items.length) {
      const csItem = getComputedStyle(items[0]);
      ok = check('时间轴三列 Grid',
        tracks(csItem.gridTemplateColumns).length === 3, csItem.gridTemplateColumns) && ok;
      ok = check('每项 min-height ≥ 76px',
        px(csItem.minHeight) >= 76, csItem.minHeight) && ok;
    }
    if (items.length >= 2) {
      const node = items[0].querySelector('.tl-node');
      const pre = node ? getComputedStyle(node, '::before') : null;
      ok = check('连接线存在（::before content 非 none）',
        !!pre && pre.content !== 'none' && pre.content !== 'normal',
        pre ? pre.content : 'no .tl-node') && ok;
      ok = check('连接线 bottom 为负值',
        !!pre && px(pre.bottom) < 0, pre ? pre.bottom : 'n/a') && ok;

      const lastNode = items[items.length - 1].querySelector('.tl-node');
      const preLast = lastNode ? getComputedStyle(lastNode, '::before') : null;
      ok = check('末项连接线隐藏',
        !!preLast && preLast.content === 'none',
        preLast ? preLast.content : 'no node') && ok;

      const prevNode = items[items.length - 2].querySelector('.tl-node');
      const prePrev = prevNode ? getComputedStyle(prevNode, '::before') : null;
      ok = check('倒数第二项连接线未被误伤',
        !!prePrev && prePrev.content !== 'none',
        prePrev ? prePrev.content : 'no node') && ok;
    } else {
      out.push({ 检查项: '时间轴连接线', 结果: 'SKIP', 实测: '不足 2 项，无法验证衔接' });
    }

    /* ---------- 7. 规则行三段式 ---------- */
    const head = $('.card--rules .set-head');
    if (head) {
      const lead = head.querySelector('.set-lead');
      const end = head.querySelector('.set-end');
      const csHead = getComputedStyle(head);
      const csLead = lead ? getComputedStyle(lead) : null;
      const csEnd = end ? getComputedStyle(end) : null;
      ok = check('规则行 align-items:center',
        csHead.alignItems === 'center', csHead.alignItems) && ok;
      ok = check('文本区 flex:1 且 min-width:0',
        !!csLead && csLead.flexGrow === '1' && px(csLead.minWidth) === 0,
        csLead ? csLead.flexGrow + ' / ' + csLead.minWidth : 'no .set-lead') && ok;
      ok = check('开关区 flex:0 0 auto',
        !!csEnd && csEnd.flexGrow === '0' && csEnd.flexShrink === '0',
        csEnd ? csEnd.flex : 'no .set-end') && ok;
      const listEl = $('.card--rules .rule-list');
      const csList = listEl ? getComputedStyle(listEl) : null;
      ok = check('列表限宽 720px',
        !!csList && px(csList.maxWidth) === 720,
        csList ? csList.maxWidth : 'no .rule-list') && ok;
    }
  } catch (err) {
    console.error('[断言异常]', err);
    return false;
  }

  console.table(out);
  const fail = out.filter(r => r.结果 === 'FAIL').length;
  const pass = out.filter(r => r.结果 === 'PASS').length;
  console.log('%c' + (fail === 0 ? '全部通过: ' + pass + ' 项' : '存在失败: ' + fail + ' 项 / 通过 ' + pass + ' 项'),
    'color:' + (fail === 0 ? '#0a7d43' : '#c0392b') + ';font-weight:700');
  return fail === 0;
})();
