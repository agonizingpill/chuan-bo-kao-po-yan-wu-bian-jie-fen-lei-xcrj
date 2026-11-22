import {batchStates, decisions, itemStates, store, supervisor} from './store.js';
import {blank, clock, esc, field, form, journal, notes, pill, portDate, title} from './ui.js';

const score = value => value === null || value === undefined ? '—'
  : `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(4)}`;

function candidates() {
  const ready = store.data.standby_ready || [];
  const waiting = store.data.standby_waiting || [];
  return `<section class="panel">
    <div class="section-head"><h2>本轮待排案件</h2><span>${ready.length} 件已判优先</span></div>
    <div class="table-wrap"><table><thead><tr><th>次序</th><th>案件 / 申请</th><th>分数 f(x)</th><th>观测</th><th>类别</th></tr></thead><tbody>
      ${ready.map((item, index) => `<tr><td class="mono">${index + 1}</td><td><a href="#incidents/${esc(item.code)}">${esc(item.code)}</a><small>${esc(item.request_code)}</small></td><td class="mono">${score(item.score)}</td><td>${esc(portDate(item.observed_at))}</td><td>${esc(decisions[item.decision] || '—')}${item.planned ? ' · 已列入待排批次' : ''}</td></tr>`).join('')
        || '<tr><td colspan="5">暂无已判优先、尚未发令的案件</td></tr>'}
    </tbody></table></div>
    <div class="section-head"><h2>仍在复核，暂不排序</h2><span>${waiting.length} 件</span></div>
    ${waiting.length ? `<ul class="waiting-list">${waiting.map(item => `<li><a href="#incidents/${esc(item.code)}">${esc(item.code)}</a><span>${esc(item.request_code)} · ${esc(portDate(item.observed_at))}</span>${pill(item.state, {queued: '待计算', failed: '计算失败', margin_review: '待人工复核'})}</li>`).join('')}</ul>`
      : '<p class="muted pending-note">没有仍在计算或等待签署的案件。</p>'}
  </section>`;
}

function planTable(batch) {
  return `<div class="table-wrap"><table><thead><tr><th>次序</th><th>案件 / 申请</th><th>分数</th><th>所需时段</th><th>拖轮</th><th>预排结论</th></tr></thead><tbody>
    ${batch.items.map(item => `<tr class="${item.state === 'blocked' ? 'blocked-row' : ''}">
      <td class="mono">${item.rank}</td>
      <td><a href="#incidents/${esc(item.incident_code)}">${esc(item.incident_code)}</a><small>${esc(item.vessel || '')} ${esc(item.request_code || '')}</small></td>
      <td class="mono">${score(item.score)}</td>
      <td>${esc(clock(item.starts_at))}–${esc(clock(item.ends_at))}</td>
      <td class="mono">${esc(item.tug_code || '未排上')}</td>
      <td>${item.state === 'blocked' ? `<span class="block-reason">排不上：${esc(item.block_reason)}</span>` : `${pill(item.state, itemStates)}${batch.state === 'published' && item.hold_active ? ' <span class="muted">占用中</span>' : ''}`}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

function batchDetail(batch) {
  if (!batch) return blank('选择待命批次', '按分类分数预排拖轮，主管确认整批后才占用拖轮并下发指令。');
  const blocked = batch.items.filter(item => item.state === 'blocked');
  const scheduled = batch.items.length - blocked.length;
  const canConfirm = supervisor() && batch.state === 'planned' && scheduled > 0;
  return `<header class="subhead"><div><div class="reference mono">${esc(batch.code)} · ${esc(batch.created_by)}</div><h2>${esc(batch.note)}</h2></div>${pill(batch.state, batchStates)}</header>
    <dl class="measurements"><div><dt>预排时间</dt><dd>${esc(portDate(batch.planned_at))}</dd></div><div><dt>案件</dt><dd>${batch.items.length} 件 / ${blocked.length} 件排不上</dd></div><div><dt>发布</dt><dd>${esc(batch.published_by ? `${batch.published_by} ${portDate(batch.published_at)}` : '未发布')}</dd></div></dl>
    ${batch.state === 'returned' ? `<p class="warning" role="alert">整批已退回：${esc(batch.return_reason)}。本次一条指令都未发出，请重新预排后再次确认。</p>` : ''}
    ${batch.state === 'planned' && blocked.length ? `<p class="warning">有 ${blocked.length} 件排不上拖轮，确认后只对已排上的 ${scheduled} 件占用拖轮并发令；排不上的案件保持待发布，可调整后另行排入。</p>` : ''}
    ${batch.state === 'planned' ? '<p class="clearance">预排尚未占用拖轮。主管确认整批后才会写入待命占用并下发已排上的全部指令。</p>' : ''}
    ${planTable(batch)}
    ${supervisor() && batch.state === 'planned' ? `<div class="batch-actions">${canConfirm
      ? form('publishStandby', batch.code, '', '主管确认整批并发布', 'confirm')
      : form('publishStandby', batch.code, '<p class="muted">本批所有案件都排不上拖轮，需先重新预排或释放拖轮。</p>', '主管确认整批并发布', '', true)}</div>` : ''}
    ${supervisor() && ['planned', 'returned'].includes(batch.state) ? form('replanStandby', batch.code, '', batch.state === 'returned' ? '按当前拖轮重新预排' : '重新预排') : ''}
    <h3>预排与发布记录</h3>${journal(store.history)}`;
}

export function standbyPage(code) {
  const batches = store.data.standby || [];
  const active = batches.find(batch => batch.code === code) || batches[0];
  const ready = store.data.standby_ready || [];
  return `${title('待命整批', '已判优先的案件按分类分数从高到低预排，主管确认整批后才占用拖轮并发布指令。')}
  <div class="case-grid"><section class="panel case-inbox"><div class="section-head"><h2>待命批次</h2><span>${batches.length} 批</span></div>
    ${batches.map(batch => `<a class="queue-entry ${batch.code === active?.code ? 'selected-entry' : ''}" href="#standby/${esc(batch.code)}"><div><strong class="mono">${esc(batch.code)}</strong>${pill(batch.state, batchStates)}</div><small>${esc(batch.note)} · ${batch.items.length} 件</small></a>`).join('') || blank('尚无待命批次', '主管可在右侧登记本轮预排。')}
    ${supervisor() ? `<details class="paper-fold" ${batches.length ? '' : 'open'}><summary>登记待命预排</summary>${ready.length ? form('planStandby', '', `<div class="form-grid">${field('批次编号', 'code', 'text', '', 'maxlength="32" placeholder="SB-1121-1"')}${notes('批次说明', 'note', '例如早班争用待命拖轮，按分类分数排序。')}</div>`, '生成预排') : blank('没有可预排的案件', '等待案件完成分类并由主管签署优先处置。')}</details>` : ''}
  </section>
  <section class="panel case-detail">${batchDetail(active)}${candidates()}</section></div>`;
}
