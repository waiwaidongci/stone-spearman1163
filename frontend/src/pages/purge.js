import {DOMAIN} from '../constants/domain.js';
import {store,load,loadRows} from '../stores/research.js';
import {purges} from '../api/purge.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {bindForm} from '../hooks/forms.js';
import {esc} from '../utils/format.js';
const LABELS={accepted:'已受理',purging_db:'清理数据库',db_purged:'数据库已清',purging_files:'清理结果文件',files_purged:'文件已清',verifying:'核验中',completed:'已完成',failed:'核验未通过'};
export async function render(){
  const isAdmin=localStorage.getItem('research-role')==='admin';
  await loadRows();
  const tasks=isAdmin?await purges.list():[];
  return `<div class="eyebrow">06 / DATA WITHDRAWAL</div><h1>测值授权撤回与数据清除</h1>
  <p>按${DOMAIN.study_label}和${DOMAIN.row_label}编号受理撤回。受理即阻断相关研究的读取、复核与下载；随后永久清除观测、历次冻结测值、衍生结果与结果文件，只保留不含测值的执行凭据。</p>
  <div class="method-note">同批未涉及的试件与无关研究照常可用；已撤回编号不能再录入、编辑或用新研究重新冻结。全部存储核验为空后才显示完成。</div>
  ${isAdmin?`<div class="toolbar">${studySelect()}</div>
  <h2>选择撤回的${DOMAIN.row_label}</h2>
  ${store.rows.length?`<form id="purge-form"><div class="table-wrap"><table><thead><tr><th>选择</th><th>源记录</th>${DOMAIN.fields.map(f=>`<th>${esc(f[1])}</th>`).join('')}</tr></thead><tbody>${store.rows.map(row=>`<tr><td><input type="checkbox" name="keys" value="${esc(row.source_key)}"></td><td>${esc(row.source_key)}</td>${DOMAIN.fields.map(f=>`<td>${esc(row.values[f[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
  <div class="inline-form"><label>或输入编号（逗号/换行分隔）<input name="keys_text" placeholder="FIELD-001, FIELD-002"></label><button type="submit">受理撤回并执行清除 →</button></div></form>`:'<p>该批次当前没有可清除的观测。</p>'}
  <h2>清除任务与受影响研究</h2>
  ${tasks.length?tasks.map(task=>`<section class="worksheet"><h3>#${task.id} · ${esc(task.study_name)} <span class="status">${esc(LABELS[task.status]||task.status)}</span></h3>
  <p class="checksum">范围 ${task.specimen_keys.length} 个${DOMAIN.row_label} · 受理 ${esc(task.created_at)} · ${esc(task.created_by)}${task.completed_at?' · 完成 '+esc(task.completed_at):''}</p>
  <p>${esc(task.progress)}</p>
  <p><small>已移除观测 ${task.removed_observations} · 已清除研究 ${task.cleared_runs}/${task.affected_run_ids.length} · 已删结果文件 ${task.removed_files} · 尝试 ${task.attempts} 次</small></p>
  ${task.affected_runs.length?table(['受影响研究','当前状态'],task.affected_runs.map(r=>[`#${r.id} ${esc(r.name)}`,status(r.status==='purged'?'purged':r.status)])):'<p>没有冻结过这些试件的研究。</p>'}
  ${task.status==='failed'?`<p class="error">失败原因：${esc(task.error)}</p><button class="text" data-retry="${task.id}">重试同一任务（不会新建任务）</button>`:''}
  ${task.status!=='completed'?`<p><small>进度每 3 秒自动刷新。</small></p>`:`<p><small>执行凭据：${task.specimen_keys.map(esc).join('、')} 不含任何测值。</small></p>`}
  </section>`).join(''):'<p>尚无清除任务。</p>'}`:'<p>只有负责人可以发起与查看数据清除。</p>'}
`;
}
export function bind(refresh){
  const sel=document.getElementById('study-select');
  if(sel)sel.onchange=()=>{store.studyId=+sel.value;refresh();};
  bindForm('purge-form',async data=>{
    const text=(data.keys_text||'').split(/[,，\s]+/).filter(Boolean);
    const picked=[...document.querySelectorAll('input[name=keys]:checked')].map(x=>x.value);
    const keys=[...new Set([...picked,...text])];
    if(!keys.length)throw new Error('请选择或输入至少一个试件编号');
    const task=await purges.create(store.studyId,keys);
    document.getElementById('notice').textContent=task.reused?'该撤回首已受理，沿用原任务 #'+task.id:'已受理，相关研究读取已阻断';
    await load();refresh();
  });
  document.querySelectorAll('[data-retry]').forEach(el=>el.onclick=async()=>{try{await purges.retry(+el.dataset.retry);await load();refresh();}catch(e){document.getElementById('notice').textContent=e.message;}});
}
