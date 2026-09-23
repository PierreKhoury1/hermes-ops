/* Atlas Workspace: talk to Atlas -> the workspace opens -> Atlas assigns the team in front of you -> the structure
   morphs as you keep talking -> build -> give the team a job and watch every agent work, output streaming live.
   Everything on screen comes from real events (design turns, blueprint diffs, the run's SSE feed). */
'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const api = async (p, opt) => { const r = await fetch('/api' + p, opt ? {headers:{'Content-Type':'application/json'}, ...opt, body: opt.body ? JSON.stringify(opt.body) : undefined} : undefined); if (r.status === 401) { location.href = '/login?next=/desk/workspace'; return null; } try { return await r.json(); } catch (_) { return {error: 'bad response ' + r.status}; } };
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('on'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('on'), 2800); }

const PALETTE = ['#7c3aed', '#db2777', '#1f9d63', '#b45309', '#0e7490', '#6d28d9', '#ea580c', '#15803d', '#a21caf', '#0369a1'];
const W = {
  phase: 'meet',            // meet -> design -> run
  sid: null, mode: 'demo', tier: 'free', liveReason: '',
  bp: null,                 // current blueprint (design phase)
  agents: new Map(),        // id -> {id,name,role,goal,instructions,tools,engine,reports_to,members,color}
  order: [],                // draw order
  pos: new Map(),           // id -> {x,y,w,h}
  deskId: null, deskName: '',
  run: null,                // {id, es, inst: Map(instId -> state), active}
  sel: null, busy: false, edgeAnim: null,
  cams: new Map(),          // name -> {name, id, source, sample, journal, alerts, el, seenTs, lastEv}
  camPoll: null, evSince: 0,
};

/* ------------------------------------------------------------------ boot */
window.addEventListener('DOMContentLoaded', boot);
async function boot(){
  const cfg = await api('/config') || {};
  W.mode = cfg.mode || 'demo'; W.liveReason = cfg.live_reason || '';
  const q = new URLSearchParams(location.search);
  if (q.get('desk') && cfg.desk) {                                   // existing desk: open straight into run mode
    W.deskId = cfg.desk.id; W.deskName = cfg.business && cfg.business.name || cfg.desk.name;
    W.tier = cfg.desk.tier || 'free'; $('#tier-pill').textContent = W.tier === 'free' ? 'free models' : 'paid models';
    loadAgentsFromConfig(cfg.agents || []);
    $('#bz-name').textContent = W.deskName;
    await openWorkspace();
    await spawnAll();
    const cams = await api('/cameras') || {};
    const list = (cams.cameras || []).map(c => ({name: c.name, id: c.id, source: (c.config || {}).source || '', journal: ['1','true','on','yes'].includes(String((c.config || {}).journal || '').toLowerCase()), alerts: !(c.rule && c.rule.alerts === false)}));
    if (list.length) await applyCams(list, true);
    enterRunMode();
    if (W.cams.size) { startCamPoll(); setSugg(CAM_QUESTIONS); }
    addMsg('a', W.cams.size
      ? `This is the ${W.deskName} desk: ${W.agents.size} agent${W.agents.size === 1 ? '' : 's'} and ${W.cams.size} camera${W.cams.size === 1 ? '' : 's'} keeping a journal. Ask me anything about what the cameras saw, give the team a job in the bar above, or tell me what to change.`
      : `This is the ${W.deskName} desk. Give the team a job in the bar above and watch them work.`);
    tutStart(false, W.cams.size ? 'watching' : 'built');
    return;
  }
  const s = await api('/design/start', {method: 'POST', body: {tier: W.tier}});
  if (!s || s.error) { addMsg('a', (s && s.error) || 'Atlas is not available right now.'); return; }
  W.sid = s.sid; W.mode = s.mode;
  sessionStorage.setItem('ws_sid', s.sid);
  const greet = (s.transcript && s.transcript[0] && s.transcript[0].text) || 'Tell me about your business.';
  await typeMsg(greet);
  setSugg(s.suggestions || []);
  loadMyDesks();
  if (W.liveReason) addMsg('s', 'no model key: ' + W.liveReason + ' (running the scripted designer)');
  tutStart(false, 'meet');
}

function loadAgentsFromConfig(list){
  W.agents.clear(); W.order = [];
  list.forEach((a, i) => { if (a.id === 'atlas') return;
    W.agents.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [], tools: (a.tools || []).filter(t => !['delegate','list_agents','finish'].includes(t)),
      engine: a.engine || 'atlas', reports_to: a.reports_to || 'atlas', members: a.members || [], color: a.color || PALETTE[i % PALETTE.length]}); });
  W.order = orderIds();
}

/* ------------------------------------------------------------------ chat */
function addMsg(role, text, cls){ const d = document.createElement('div'); d.className = 'm ' + role + (cls ? ' ' + cls : ''); d.textContent = text; $('#msgs').appendChild(d); $('#msgs').scrollTop = 1e9; return d; }
async function typeMsg(text){
  const d = addMsg('a', ''); const cur = document.createElement('i'); cur.className = 'cur'; d.appendChild(cur);
  $('#orbstate').textContent = 'speaking'; W.typing = true;
  const t0 = performance.now(); let shown = 0;                      // time-based: ~260 chars/s even when timers are throttled
  while (shown < text.length) { if (!W.typing) break; const want = Math.min(text.length, Math.max(shown + 1, Math.round((performance.now() - t0) * 0.26)));
    d.insertBefore(document.createTextNode(text.slice(shown, want)), cur); shown = want; $('#msgs').scrollTop = 1e9; await sleep(16); }
  if (shown < text.length) d.insertBefore(document.createTextNode(text.slice(shown)), cur);
  W.typing = false; cur.remove(); $('#orbstate').textContent = 'listening'; return d;
}
function setSugg(list){ $('#sugg').innerHTML = (list || []).map(s => `<button onclick="send(${JSON.stringify(s).replace(/"/g, '&quot;')})">${esc(s)}</button>`).join(''); }
function sayKey(ev){ if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(); } }

async function send(text){
  text = (text || $('#say').value).trim(); if (!text || W.busy) return;
  $('#say').value = ''; W.typing = false;
  if (/^switch to the paid model/i.test(text) && W.sid) { await setTier('balanced'); return send(W.lastSaid || ''); }
  if (W.phase === 'run' && W.cams.size && (!W.sid || looksLikeQuestion(text))) return askCams(text);
  W.lastSaid = text;
  if (W.phase === 'run' && !W.sid) { $('#job').value = text; return deploy(); }
  addMsg('u', text); setSugg([]);
  W.busy = true; $('#send').disabled = true; $('#orb').classList.add('busy'); $('#orbstate').textContent = 'thinking';
  const d = addMsg('a', ''); const cur = document.createElement('i'); cur.className = 'cur'; d.appendChild(cur);
  let result = null;
  try {
    const r = await fetch(`/api/design/${W.sid}/say`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      let i; while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          let ev; try { ev = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (ev.t === 'tok') { d.insertBefore(document.createTextNode(ev.d), cur); $('#msgs').scrollTop = 1e9; }
          else if (ev.t === 'status') { $('#orbstate').textContent = ev.d.toLowerCase(); }
          else if (ev.t === 'done') result = ev;
          else if (ev.t === 'error') result = {error: ev.error};
        }
      }
    }
  } catch (e) { result = {error: String(e)}; }
  cur.remove();
  W.busy = false; $('#send').disabled = false; $('#orb').classList.remove('busy'); $('#orbstate').textContent = 'listening';
  if (!result || result.error) { d.textContent = (result && result.error) || 'Atlas did not answer. Try again.'; return; }
  const last = (result.transcript || []).filter(m => m.role === 'assistant').pop();
  if (!d.textContent.trim()) d.textContent = (last && last.text) || result.text || 'Atlas did not answer. Try again.';
  setSugg(result.suggestions || []);
  if (result.blueprint && (result.blueprint.agents || []).length) await applyBlueprint(result.blueprint);
  $('#build-btn').disabled = !(W.bp && W.bp.agents && W.bp.agents.length);
  $('#build-hint').textContent = result.ready ? 'Atlas thinks the team is ready. Build it, or keep refining.' : (W.bp ? 'Keep talking to reshape the team, or build it now.' : 'Atlas needs a first draft of the team before you can build.');
  if (result.ready) tutHook('ready');
}

/* ------------------------------------------------------------------ workspace open + team diff/animation */
async function openWorkspace(){
  if (W.phase !== 'meet') return;
  W.phase = 'design';
  $('#phase-label').textContent = 'designing the team';
  const ws = $('#ws'); ws.classList.remove('meet'); ws.classList.add('open');
  await sleep(950);                                                  // grid + orb transitions
  layoutAll(false);
}

function orderIds(){
  const tops = [...W.agents.values()].filter(a => a.reports_to === 'atlas' || !W.agents.has(a.reports_to));
  const out = [];
  tops.forEach(t => { out.push(t.id); (t.members || []).forEach(m => { if (W.agents.has(m)) out.push(m); }); });
  W.agents.forEach(a => { if (!out.includes(a.id)) out.push(a.id); });
  return out;
}

async function applyBlueprint(bp){
  W.bp = bp;
  const incoming = new Map();
  (bp.agents || []).forEach((a, i) => incoming.set(a.id, {id: a.id, name: a.name, role: a.role || '', goal: a.goal || '', instructions: a.instructions || [],
    tools: a.tools || [], engine: a.engine || 'atlas', reports_to: a.reports_to || 'atlas', members: a.members || [], color: a.color || PALETTE[i % PALETTE.length]}));
  const business = bp.business || {};
  if (business.name) { W.deskName = business.name; $('#bz-name').textContent = business.name; }
  const first = W.phase === 'meet';
  if (first) { await openWorkspace(); }
  const added = [], removed = [], moved = [], changed = [];
  incoming.forEach((a, id) => { const old = W.agents.get(id); if (!old) added.push(a); else { if (old.reports_to !== a.reports_to) moved.push(a); else if (old.role !== a.role || old.name !== a.name || (old.tools || []).join() !== (a.tools || []).join()) changed.push(a); } });
  W.agents.forEach((a, id) => { if (!incoming.has(id)) removed.push(a); });
  // narrate what Atlas is doing to the team
  if (first && added.length) addMsg('s', 'Atlas is assembling your team…', 'assign');
  // update state
  W.agents = incoming;                                               // blueprint order, not first-seen order
  W.order = orderIds();
  // animate
  removed.forEach(a => { const el = nodeEl(a.id); if (el) { el.classList.add('gone'); setTimeout(() => el.remove(), 500); } removeEdge(a.id); addMsg('s', `− ${a.name} removed`, 'assign'); });
  layoutAll(true, added.map(a => a.id));
  for (const a of added) {
    const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? W.agents.get(a.reports_to) : null;
    addMsg('s', `+ ${a.name} — ${a.role}${lead ? `  (in ${lead.name}'s team)` : ''}`, 'assign');
    await spawn(a.id, lead ? lead.id : 'atlas');
    await sleep(160);
  }
  moved.forEach(a => { const to = a.reports_to === 'atlas' ? 'Atlas' : (W.agents.get(a.reports_to) || {}).name || a.reports_to; addMsg('s', `↳ ${a.name} now reports to ${to}`, 'assign'); removeEdge(a.id); drawEdge(a.id, true); });
  changed.forEach(a => { const el = nodeEl(a.id); if (el) { renderNodeInner(el, a); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); } });
  await applyCams(bp.cameras || []);
  if (first) { tutHook('team'); }
  if (W.sel && !W.agents.has(W.sel)) closeInsp();
  else if (W.sel) inspect(W.sel);
}

async function spawnAll(){
  layoutAll(false);
  for (const id of W.order) { const a = W.agents.get(id); const lead = a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? a.reports_to : 'atlas'; await spawn(id, lead); await sleep(140); }
}

/* ---- layout: Atlas col 0, top-level col 1, members col 2; vertical stack centred */
const NW = 236, NW_ATLAS = 214, COL = 300, ROW_MAX = 118, GAP_MAX = 26;
function layoutAll(animate, freshIds){
  const c = $('#canvas'); const H0 = c.clientHeight || 600, W0 = c.clientWidth || 900;
  const tops = W.order.filter(id => { const a = W.agents.get(id); return a && (a.reports_to === 'atlas' || !W.agents.has(a.reports_to)); });
  const blocks = tops.map(id => ({id, members: (W.agents.get(id).members || []).filter(m => W.agents.has(m))}));
  const rowsN = blocks.reduce((n, b) => n + Math.max(1, b.members.length), 0);
  const tight = rowsN * ROW_MAX + Math.max(0, blocks.length - 1) * GAP_MAX > H0 - 40;      // a tall team: close the gaps before shrinking
  const ROW = tight ? 100 : ROW_MAX, GAP = tight ? 6 : GAP_MAX;
  const heights = blocks.map(b => Math.max(1, b.members.length) * ROW);
  const total = heights.reduce((s, h) => s + h, 0) + Math.max(0, blocks.length - 1) * GAP;
  const hasMembers = blocks.some(b => b.members.length);
  const needW = NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30) + 48;
  const k = W.k = Math.max(.5, Math.min(1, (H0 - 40) / Math.max(1, total), W0 / needW));
  const H = H0 / k, Wd = W0 / k;
  ['#nodes', '#edges'].forEach(q => { const st = $(q).style; st.inset = 'auto'; st.left = st.top = '0'; st.width = Wd + 'px'; st.height = H + 'px'; st.transformOrigin = '0 0'; st.transform = k < 1 ? `scale(${k})` : ''; });
  const x0 = Math.max(24, Math.round((Wd - (NW_ATLAS + COL + (hasMembers ? COL : 0) + (NW - 30))) / 2));
  let y = Math.max(20, Math.round((H - total) / 2));
  const atlasY = Math.max(20, Math.round(H / 2 - 48));
  setPos('atlas', x0, atlasY, NW_ATLAS, animate);
  blocks.forEach((b, i) => {
    const blockH = heights[i]; const yT = y + Math.round(blockH / 2 - ROW / 2);
    setPos(b.id, x0 + COL, yT, NW, animate && !(freshIds || []).includes(b.id));
    b.members.forEach((m, j) => setPos(m, x0 + COL * 2, y + j * ROW, NW, animate && !(freshIds || []).includes(m)));
    y += blockH + GAP;
  });
  ensureAtlasNode();
  $('#empty').classList.toggle('hide', W.agents.size > 0);
  redrawEdges(); if (animate) animateEdges(900);
}
function setPos(id, x, y, w, animate){ W.pos.set(id, {x, y, w}); const el = nodeEl(id); if (el && !el.classList.contains('spawn')) { if (!animate) el.style.transition = 'none'; el.style.transform = `translate(${x}px,${y}px)`; if (!animate) { void el.offsetWidth; el.style.transition = ''; } } }
function nodeEl(id){ return document.getElementById('n-' + id.replace(/[^a-zA-Z0-9_#-]/g, '-')); }
function ensureAtlasNode(){
  let el = nodeEl('atlas'); const p = W.pos.get('atlas');
  if (!el) { el = document.createElement('div'); el.className = 'node atlas'; el.id = 'n-atlas'; el.onclick = () => inspect('atlas');
    el.innerHTML = `<div class="nm">Atlas <span class="tag lead">lead</span></div><div class="role">Orchestrator — briefs, reviews, approves</div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
    el.style.transition = 'none'; el.style.transform = `translate(${p.x}px,${p.y}px)`; $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = ''; }
  else el.style.transform = `translate(${p.x}px,${p.y}px)`;
}
function renderNodeInner(el, a){
  const isLead = (a.members || []).length > 0; const member = a.reports_to && a.reports_to !== 'atlas' && W.agents.has(a.reports_to);
  el.style.setProperty('--c', a.color);
  el.querySelector('.nm').innerHTML = `${esc(a.name)} <span class="tag ${isLead ? 'lead' : ''}">${isLead ? 'sub-team lead' : (member ? '↳ ' + esc((W.agents.get(a.reports_to) || {}).name || a.reports_to) : (a.engine === 'hermes_agent' ? 'hermes' : 'idle'))}</span>`;
  el.querySelector('.role').textContent = a.role;
  if (!el.querySelector('.chips').children.length || !W.run) el.querySelector('.chips').innerHTML = (a.tools || []).slice(0, 5).map(t => `<span>${esc(t)}</span>`).join('');
}
async function spawn(id, fromId){
  const a = W.agents.get(id); if (!a) return; if (nodeEl(id)) { renderNodeInner(nodeEl(id), a); return; }
  const from = W.pos.get(fromId) || W.pos.get('atlas') || {x: 40, y: 40, w: 200}; const to = W.pos.get(id) || from;
  const el = document.createElement('div'); el.className = 'node spawn'; el.id = 'n-' + id; el.onclick = () => inspect(id);
  el.innerHTML = `<div class="nm"></div><div class="role"></div><div class="asg"></div><div class="chips"></div><div class="out"></div><div class="ft"></div>`;
  renderNodeInner(el, a);
  el.style.transition = 'none'; el.style.transform = `translate(${from.x + from.w / 2 - NW / 2}px,${from.y + 20}px) scale(.25)`;
  $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = '';
  await sleep(20);
  el.classList.remove('spawn'); el.style.transform = `translate(${to.x}px,${to.y}px)`;
  const fromEl = nodeEl(fromId); if (fromEl) { fromEl.classList.remove('flash'); void fromEl.offsetWidth; fromEl.classList.add('flash'); }
  animateEdges(850); setTimeout(() => drawEdge(id, true), 120);
}

/* ---- edges (parent right-middle -> child left-middle) */
function edgeD(fromId, toId){
  const f = W.pos.get(fromId), t = W.pos.get(toId); if (!f || !t) return '';
  const fe = nodeEl(fromId), te = nodeEl(toId);
  const fh = fe ? fe.offsetHeight : 90, th = te ? te.offsetHeight : 90;
  const x1 = f.x + f.w, y1 = f.y + fh / 2, x2 = t.x, y2 = t.y + th / 2, mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}
function parentOf(id){ const a = W.agents.get(id); if (!a) return 'atlas'; return a.reports_to !== 'atlas' && W.agents.has(a.reports_to) ? a.reports_to : 'atlas'; }
function drawEdge(id, animate){
  const svg = $('#edges'); let p = svg.querySelector(`[data-e="${id}"]`);
  if (!p) { p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.dataset.e = id; p.setAttribute('pathLength', '1'); svg.appendChild(p); }
  p.setAttribute('d', edgeD(parentOf(id), id));
  if (animate) { p.classList.remove('draw'); void p.getBoundingClientRect(); p.classList.add('draw'); }
}
function removeEdge(id){ const p = $('#edges').querySelector(`[data-e="${id}"]`); if (p) p.remove(); }
function redrawEdges(){
  const svg = $('#edges'); const c = $('#canvas'); const k = W.k || 1; svg.setAttribute('viewBox', `0 0 ${c.clientWidth / k} ${c.clientHeight / k}`);
  W.agents.forEach((a, id) => { if (nodeEl(id)) { const p = svg.querySelector(`[data-e="${id}"]`); if (p) p.setAttribute('d', edgeD(parentOf(id), id)); else drawEdge(id, false); } });
  if (W.run) W.run.inst.forEach((s, inst) => { if (s.ghost) { const p = svg.querySelector(`[data-e="${inst}"]`); if (p) p.setAttribute('d', edgeD(s.parent, inst)); } });
}
function animateEdges(ms){ const end = performance.now() + ms; cancelAnimationFrame(W.edgeAnim); const step = () => { redrawEdges(); if (performance.now() < end) W.edgeAnim = requestAnimationFrame(step); }; W.edgeAnim = requestAnimationFrame(step); }
window.addEventListener('resize', () => { if (W.phase !== 'meet') layoutAll(false); });

/* ------------------------------------------------------------------ inspector */
function inspect(id){
  W.sel = id; document.querySelectorAll('.node.sel').forEach(n => n.classList.remove('sel')); const el = nodeEl(id); if (el) el.classList.add('sel');
  const a = id === 'atlas' ? {name: 'Atlas', role: 'Orchestrator', goal: 'Briefs the team, runs leads in parallel, reviews every result, and holds anything outbound for your approval.', instructions: [], tools: ['delegate', 'queue_action', 'crm_update', 'save_deliverable'], engine: 'atlas', reports_to: ''} : W.agents.get(id);
  if (!a) return;
  $('#insp-name').textContent = a.name;
  const st = W.run && [...W.run.inst.values()].filter(s => s.base === id);
  const out = st && st.length ? st.map(s => `<h4>${esc(s.inst)} · ${esc(s.status)}${s.assignment ? ' — ' + esc(s.assignment) : ''}</h4><pre id="live-${esc(s.inst)}">${esc(s.text || '(waiting…)')}</pre>`).join('') : '';
  const ask = id === 'atlas' ? [] : [`Give ${a.name} web access`, `Make ${a.name} lead a pod of 3`, `Merge ${a.name} into another role`, `Remove ${a.name}`, `Rewrite ${a.name}'s standing orders to be stricter`];
  $('#insp-body').innerHTML = `
    ${out}
    <h4>Role</h4><div>${esc(a.role)}</div>
    ${a.goal ? `<h4>Goal</h4><div>${esc(a.goal)}</div>` : ''}
    ${(a.instructions || []).length ? `<h4>Standing orders</h4><ul>${a.instructions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    <h4>Tools</h4><div class="chips">${(a.tools || []).map(t => `<span>${esc(t)}</span>`).join('') || '<span class="hint">none</span>'}</div>
    <h4>Engine · reports to</h4><div>${esc(a.engine || 'atlas')} · ${esc(a.reports_to === 'atlas' || !a.reports_to ? 'Atlas' : ((W.agents.get(a.reports_to) || {}).name || a.reports_to))}${(a.members || []).length ? ` · leads ${a.members.map(m => esc((W.agents.get(m) || {}).name || m)).join(', ')}` : ''}</div>
    ${W.phase === 'design' && ask.length ? `<h4>Ask Atlas to…</h4><div class="ask">${ask.map(s => `<button onclick="closeInsp();send(${JSON.stringify(s).replace(/"/g, '&quot;')})">${esc(s)}</button>`).join('')}</div>` : ''}`;
  $('#insp').classList.add('on'); $('#apdrawer').classList.remove('on');
}
function closeInsp(){ W.sel = null; $('#insp').classList.remove('on'); document.querySelectorAll('.node.sel').forEach(n => n.classList.remove('sel')); }

/* ------------------------------------------------------------------ build */
async function buildDesk(){
  if (!W.bp || !(W.bp.agents || []).length) return;
  $('#build-btn').disabled = true; $('#build-hint').textContent = 'Building the desk…';
  const r = await api(`/design/${W.sid}/build`, {method: 'POST', body: {blueprint: W.bp, tier: W.tier, name: W.deskName || 'New desk'}});
  if (!r || r.error) { $('#build-hint').textContent = (r && r.error) || 'build failed'; $('#build-btn').disabled = false; return; }
  W.deskId = r.desk.id; W.deskName = r.desk.business_name || r.desk.name; $('#bz-name').textContent = W.deskName;
  (r.cameras || []).forEach(c => { const k = W.cams.get(c.name); if (k) { k.id = c.id; k.journal = c.journal; setCamState(k, 'starting…', ''); } });
  const camLine = (r.cameras || []).length ? ` ${r.cameras.length} camera${r.cameras.length === 1 ? ' is' : 's are'} now watching and writing the journal. Ask me anything about what they see.` : '';
  const missing = (r.cameras_missing || []).length ? ` Still needed: the stream address for ${r.cameras_missing.join(', ')} (add it on the Cameras page of the full dashboard).` : '';
  addMsg('a', `Built. ${W.agents.size} specialist${W.agents.size === 1 ? '' : 's'} on the desk, every outbound message waits for your approval.${camLine}${missing} Give the team a job in the bar above, or keep telling me what to change.`);
  toast('Desk built');
  enterRunMode();
  if ((r.cameras || []).length) { startCamPoll(); setSugg(CAM_QUESTIONS); tutHook('watching'); } else tutHook('built');
}
function enterRunMode(){
  W.phase = 'run'; $('#ws').classList.add('run');
  $('#jobbar').classList.remove('hide'); $('#phase-label').textContent = 'ready — give the team a job';
  $('#chat-foot').classList.add('hide');
  $('#say').placeholder = W.sid ? 'Ask Atlas to change the team, or type a job here…' : 'Give the team a job…';
  document.querySelectorAll('.node .nm .tag').forEach(t => { if (!t.classList.contains('lead') && !t.textContent.startsWith('↳')) t.textContent = 'idle'; });
  loadApprovals();
  setTimeout(() => $('#job').focus(), 300);
}

/* ------------------------------------------------------------------ run: real events, live output */
async function deploy(){
  const task = $('#job').value.trim(); if (!task) { $('#job').focus(); return toast('Type the job first'); }
  if (W.run && W.run.active) return toast('A run is already going — wait for it to finish');
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});   // this tab's desk, even with other tabs open
  const r = await api('/runs', {method: 'POST', body: {task, mode: 'auto'}});
  if (!r || r.error) return toast((r && (r.message || r.error)) || 'could not start');
  $('#job').value = '';
  addMsg('u', task); addMsg('s', `run ${r.run_id} started`, 'assign');
  $('#summary').classList.add('hide'); $('#feed').innerHTML = '';
  document.querySelectorAll('.node.ghost').forEach(n => n.remove()); $('#edges').querySelectorAll('[data-e*="#"]').forEach(p => p.remove());
  document.querySelectorAll('.node').forEach(n => { n.classList.remove('busy', 'done', 'error'); n.querySelector('.out').textContent = ''; n.querySelector('.asg').textContent = ''; n.querySelector('.ft').textContent = ''; const t = n.querySelector('.nm .tag'); if (t && !t.classList.contains('lead') && !t.textContent.startsWith('↳')) { t.textContent = 'idle'; t.className = 'tag'; } });
  W.agents.forEach(a => { const el = nodeEl(a.id); if (el) el.querySelector('.chips').innerHTML = ''; });
  const atlas = nodeEl('atlas'); if (atlas) atlas.querySelector('.chips').innerHTML = '';
  W.run = {id: r.run_id, inst: new Map(), active: true, tin: 0, tout: 0};
  $('#phase-label').textContent = 'team working…';
  const es = new EventSource(`/api/stream?run=${encodeURIComponent(r.run_id)}&since=0`);
  W.run.es = es;
  es.onmessage = m => { try { onRunEvent(JSON.parse(m.data)); } catch (_) {} };
  es.onerror = () => { if (W.run && !W.run.active) es.close(); };
  tutHook('running');
}
function instState(e){
  const inst = (e.data && e.data.inst) || e.agent; const base = inst.split('#')[0];
  let s = W.run.inst.get(inst);
  if (!s) { s = {inst, base, text: '', status: 'idle', chips: [], parent: (e.data && e.data.parent) || 'atlas', ghost: inst !== base, turns: 0}; W.run.inst.set(inst, s); if (s.ghost) makeGhost(s); }
  return s;
}
function makeGhost(s){
  const baseEl = nodeEl(s.base); const bp = W.pos.get(s.base); if (!baseEl || !bp) return;
  const n = parseInt(s.inst.split('#')[1] || '2', 10); const x = bp.x + 22 * (n - 1), y = bp.y + 34 * (n - 1);
  W.pos.set(s.inst, {x, y, w: bp.w});
  const el = baseEl.cloneNode(true); el.id = 'n-' + s.inst.replace(/[^a-zA-Z0-9_#-]/g, '-'); el.classList.add('ghost', 'spawn'); el.classList.remove('sel', 'busy', 'done');
  el.onclick = () => inspect(s.base); el.querySelector('.nm').innerHTML = `${esc((W.agents.get(s.base) || {}).name || s.base)} <span class="tag">#${n}</span>`; el.querySelector('.out').textContent = ''; el.querySelector('.chips').innerHTML = ''; el.querySelector('.asg').textContent = '';
  el.style.transition = 'none'; el.style.transform = `translate(${bp.x}px,${bp.y}px) scale(.6)`; $('#nodes').appendChild(el); void el.offsetWidth; el.style.transition = '';
  requestAnimationFrame(() => { el.classList.remove('spawn'); el.style.transform = `translate(${x}px,${y}px)`; });
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.dataset.e = s.inst; p.setAttribute('pathLength', '1'); p.setAttribute('d', edgeD(s.parent, s.inst)); p.classList.add('draw'); $('#edges').appendChild(p);
  animateEdges(700);
}
function elFor(s){ return s.ghost ? nodeEl(s.inst) : nodeEl(s.base); }
function setTag(el, text, cls){ const t = el && el.querySelector('.nm .tag'); if (!t || t.classList.contains('lead') || t.textContent.startsWith('↳')) return; t.textContent = text; t.className = 'tag ' + (cls || ''); }
function feed(e, cls){ const f = $('#feed'); const d = document.createElement('div'); d.className = cls || e.kind; const t = new Date((e.ts || Date.now() / 1000) * 1000).toLocaleTimeString('en-GB'); d.innerHTML = `<span class="t">${t}</span><span class="a">${esc((e.data && e.data.inst) || e.agent)}</span><span class="k">${esc(e.kind)}</span><span class="x">${esc(e.text || '')}</span>`; f.appendChild(d); f.scrollTop = 1e9; while (f.children.length > 120) f.firstChild.remove(); }
function onRunEvent(e){
  if (!W.run || e.run_id !== W.run.id) return;
  if (typeof e.data === 'string') { try { e.data = JSON.parse(e.data || '{}'); } catch (_) { e.data = {}; } }
  e.data = e.data || {};
  if (e.kind === 'usage') { W.run.tin = e.data.tokens_in; W.run.tout = e.data.tokens_out; $('#phase-label').textContent = `team working… ${(W.run.tin || 0).toLocaleString()} tokens in`; return; }
  if (e.kind === 'token') { if (e.data.thinking) return; const s = instState(e); s.text += e.text; if (s.text.length > 6000) s.text = s.text.slice(-6000); const el = elFor(s); if (el) { const o = el.querySelector('.out'); o.textContent = s.text.slice(-420); } const live = document.getElementById('live-' + s.inst); if (live) { live.textContent = s.text; live.scrollTop = 1e9; } if (s.status !== 'writing') { s.status = 'writing'; setTag(el, 'writing', 'busy'); } return; }
  if (e.agent !== 'system' && e.agent !== 'owner') {
    const s = instState(e); const el = elFor(s);
    if (e.kind === 'agent_start') { s.status = 'thinking'; s.assignment = e.data.assignment || ''; s.text = ''; if (el) { el.classList.add('busy'); el.classList.remove('done', 'error'); el.querySelector('.asg').textContent = s.assignment; el.querySelector('.out').textContent = ''; setTag(el, 'thinking', 'busy'); } edgeState(s.inst === 'atlas' ? null : (s.ghost ? s.inst : s.base), 'on'); if (s.base !== 'atlas') addMsg('s', `→ ${(W.agents.get(s.base) || {}).name || s.base}: ${s.assignment.slice(0, 90)}`, 'assign'); }
    else if (e.kind === 'log') { s.turns++; s.status = 'thinking'; if (el) { setTag(el, `turn ${s.turns}`, 'busy'); el.querySelector('.ft').textContent = `${s.turns} turn${s.turns === 1 ? '' : 's'}`; } }
    else if (e.kind === 'tool') { const name = e.text.split(/[( →:]/)[0]; s.chips.push(name); if (el) { const c = el.querySelector('.chips'); c.querySelectorAll('.hot').forEach(x => x.classList.remove('hot')); const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = name; c.appendChild(sp); while (c.children.length > 7) c.firstChild.remove(); setTag(el, name, 'busy'); } if (name === 'delegate') { const m = e.text.match(/delegate → ([\w#-]+)/); if (m) edgeState(m[1], 'on'); } }
    else if (e.kind === 'agent_end') { s.status = 'done'; if (el) { el.classList.remove('busy'); el.classList.add('done'); setTag(el, 'done ✓', 'done'); } edgeState(s.ghost ? s.inst : s.base, 'done'); }
    else if (e.kind === 'error') { s.status = 'error'; if (el) { el.classList.remove('busy'); el.classList.add('error'); setTag(el, 'error', ''); } }
    else if (e.kind === 'approval') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = '⏸ ' + (e.data.action_kind || 'approval'); el.querySelector('.chips').appendChild(sp); } loadApprovals(); toast('Waiting for your approval: ' + (e.data.action_kind || 'an outbound action')); tutHook('approval'); }
    else if (e.kind === 'policy') { if (el) { const sp = document.createElement('span'); sp.className = 'hot'; sp.textContent = 'policy ✋'; el.querySelector('.chips').appendChild(sp); } }
    if (W.sel === s.base && ['agent_start', 'agent_end', 'tool', 'log'].includes(e.kind)) inspect(s.base);
  }
  if (e.kind === 'done') {
    W.run.active = false; if (W.run.es) W.run.es.close();
    document.querySelectorAll('.node.busy').forEach(n => { n.classList.remove('busy'); n.classList.add('done'); setTag(n, 'done ✓', 'done'); });
    $('#edges').querySelectorAll('path.on').forEach(p => { p.classList.remove('on'); p.classList.add('done'); });
    $('#phase-label').textContent = (e.data.status || 'done') + ' — give the team another job';
    $('#summary-text').textContent = (e.text || '').trim(); $('#summary').classList.remove('hide');
    addMsg('a', `Done. ${(e.text || '').split('---').pop().trim() || 'Run finished.'}`);
    loadApprovals(); tutHook('done');
  }
  if (!['token', 'usage', 'log'].includes(e.kind)) feed(e);
}
function edgeState(id, state){ if (!id) return; const p = $('#edges').querySelector(`[data-e="${id}"]`); if (!p) return; p.classList.remove('draw', 'on', 'done'); p.classList.add(state); }

/* ------------------------------------------------------------------ approvals */
async function loadApprovals(){
  if (!W.deskId && W.phase !== 'run') return;
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const list = await api('/actions?status=pending') || [];
  const n = Array.isArray(list) ? list.length : 0;
  $('#ap-n').textContent = n; $('#ap-n').classList.toggle('hide', !n);
  $('#ap-body').innerHTML = n ? list.map(a => `<div class="ap" id="ap-${a.id}"><div class="k">${esc(a.kind)} → ${esc(a.to)} · queued by ${esc(a.by || 'atlas')}</div>
      ${a.flags ? `<div class="flag">⚠ ${esc(a.flags)}</div>` : ''}
      ${a.reason ? `<div class="hint">${esc(a.reason)}</div>` : ''}
      <input id="ap-s-${a.id}" value="${esc(a.subject || '')}" placeholder="Subject"><textarea id="ap-b-${a.id}">${esc(a.body || '')}</textarea>
      <div class="row"><button class="btn p sm" onclick="decide(${a.id},'approved')">✓ Approve &amp; send</button><button class="btn sm d" onclick="decide(${a.id},'rejected')">✕ Reject</button><input id="ap-n-${a.id}" placeholder="note for the agents (optional)" style="flex:1"></div></div>`).join('')
    : '<div class="hint">Nothing waiting. When an agent wants to send something, it lands here first.</div>';
}
async function decide(id, status){
  const r = await api(`/actions/${id}/decide`, {method: 'POST', body: {status, note: $(`#ap-n-${id}`).value, body: $(`#ap-b-${id}`).value, subject: $(`#ap-s-${id}`).value}});
  if (!r || r.error) return toast((r && r.error) || 'failed');
  toast(status === 'approved' ? (W.mode === 'demo' ? 'Approved (simulated send in demo mode)' : 'Approved and sent') : 'Rejected — the agents will see your note next run');
  loadApprovals();
}
function toggleApprovals(force){ const d = $('#apdrawer'); const on = typeof force === 'boolean' ? force : !d.classList.contains('on'); d.classList.toggle('on', on); if (on) { $('#insp').classList.remove('on'); loadApprovals(); } }

/* ------------------------------------------------------------------ tutorial (coach marks driven by what actually happens) */
const TUT = {steps: [], i: -1, auto: true, on: false};
const TUT_STEPS = {
  meet: [
    {t: '#orbwrap', h: 'This is Atlas', p: 'Atlas runs your desk. It designs the team, briefs every agent, checks their work and never sends anything without you.'},
    {t: '#composer', h: 'Tell it about your business', p: 'One or two sentences: what you do, and the job that eats your time. Try a suggestion chip if you want a quick start. Atlas will open the workspace and assemble the team as you talk.', end: true},
  ],
  team: [
    {t: '#nodes', h: 'Atlas assigned your team', p: 'Each card is an agent with its own role, standing orders and tools. Sub-team leads run their own members. Click any card to read its brief.'},
    {t: '#composer', h: 'Reshape it by talking', p: 'Say things like "add a QA reviewer", "split research into a pod of 3", "the writer should report to the researcher". The structure morphs live.'},
    {t: '#build-btn', h: 'Build when it looks right', p: 'Building creates the desk with this team. You can still change everything later.', end: true},
  ],
  built: [
    {t: '#jobbar', h: 'Give the team a job', p: 'Paste a customer enquiry, ask for a report, a comparison, a chase list — anything. Atlas briefs the team and you watch them work in real time.', end: true},
  ],
  running: [
    {t: '#nodes', h: 'Watch them work', p: 'Cards light up as agents start, tool calls appear as chips, and each agent\'s output streams onto its card. Click a card to read the full live output.', end: true},
  ],
  cameras: [
    {t: '#camstrip', h: 'Cameras join the desk', p: 'Each tile is a camera. With the journal on, it writes a detailed note whenever something changes, and a summary every 15 minutes. After you build, the live picture and the latest note show here.', end: true},
  ],
  watching: [
    {t: '#camstrip', h: 'The cameras are documenting', p: 'Every few seconds each camera looks, compares with its last note and writes down what changed: who arrived or left, what they wear and carry, how long they waited.'},
    {t: '#composer', h: 'Ask anything', p: 'Type a question like "Was any bag left unattended?" or "How long did the guest at reception wait?". Atlas answers from the journal with times, and shows the frames it used.'},
    {t: '#journal-link', h: 'Read the whole day', p: 'The journal is also a readable page, one per day, filterable by camera.'},
    {t: '#jobbar', h: 'Give the team a job', p: 'Ask for a report or a summary, paste an enquiry, anything. You watch every agent work, and nothing leaves without your approval.', end: true},
  ],
  approval: [
    {t: '#approvals-btn', h: 'Nothing goes out without you', p: 'An agent wants to send something. Open Approvals, edit the text if you like, then approve or reject with a note — the note tunes the agents next time.', end: true},
  ],
  done: [
    {t: '#summary', h: 'The run is done', p: 'The summary is verified by the desk (what was queued, sent, saved), not claimed by the model. Give the team another job, or tell Atlas what to change in the team.', end: true},
  ],
};
function tutStart(force, phase){
  if (localStorage.getItem('ws_tut_done') && !force) { TUT.auto = false; return; }
  TUT.auto = true; const key = phase || (W.phase === 'run' ? 'built' : (W.agents.size ? 'team' : 'meet'));
  tutShow(TUT_STEPS[key] || TUT_STEPS.meet);
}
function tutHook(ev){ if (!TUT.auto || TUT.on) return; const steps = TUT_STEPS[ev]; if (steps) setTimeout(() => tutShow(steps), ev === 'team' ? 900 : 500); }
function tutShow(steps){ TUT.steps = steps; TUT.i = -1; TUT.on = true; $('#coach').classList.remove('hide'); tutNext(); }
function tutNext(){
  document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target'));
  TUT.i++; const s = TUT.steps[TUT.i];
  if (!s) { tutEnd(); return; }
  const target = $(s.t); const box = $('#coach-box');
  $('#coach-step').textContent = `${TUT.i + 1} / ${TUT.steps.length}`; $('#coach-title').textContent = s.h; $('#coach-text').textContent = s.p;
  $('#coach-next').textContent = TUT.i === TUT.steps.length - 1 ? (s.end ? 'Got it' : 'Done') : 'Next';
  box.className = '';
  if (target) { target.classList.add('coach-target'); const r = target.getBoundingClientRect(); const below = r.bottom + 220 < innerHeight; box.style.left = Math.max(12, Math.min(innerWidth - 360, r.left)) + 'px'; box.style.top = (below ? r.bottom + 14 : r.top - 14) + 'px'; if (!below) { box.classList.add('below'); box.style.transform = 'translateY(-100%)'; } else box.style.transform = ''; }
  else { box.classList.add('noarrow'); box.style.left = '50%'; box.style.top = '40%'; box.style.transform = 'translate(-50%,-50%)'; }
}
function tutEnd(){ TUT.on = false; $('#coach').classList.add('hide'); document.querySelectorAll('.coach-target').forEach(e => e.classList.remove('coach-target')); if (TUT.steps === TUT_STEPS.done || TUT.steps === TUT_STEPS.approval) localStorage.setItem('ws_tut_done', '1'); }
function tutSkip(){ TUT.auto = false; localStorage.setItem('ws_tut_done', '1'); tutEnd(); }


/* ------------------------------------------------------------------ cameras: tiles, live picture, journal, questions */
const CAM_QUESTIONS = ['What happened in the last 10 minutes?', 'Was anything left behind?', 'Who waited the longest?', 'Describe everyone who came in'];
function camKind(src){ src = String(src || ''); if (!src) return 'needs a stream address'; if (src.startsWith('sample:') || /AtlasDemo[\\/]videos/i.test(src)) return 'sample footage'; if (/^rtsps?:/i.test(src)) return 'RTSP camera'; if (/^https?:/i.test(src)) return 'snapshot URL'; if (/^\d+$/.test(src)) return 'webcam'; if (/\.(mp4|mov|avi|mkv|webm)$/i.test(src)) return 'recording'; return 'camera'; }
function setCamState(k, text, cls){ if (!k.el) return; const t = k.el.querySelector('.cn .tag'); t.textContent = text; t.className = 'tag ' + (cls || ''); }
async function applyCams(list, instant){
  const incoming = new Map((list || []).map(c => [c.name, c]));
  const first = !W.cams.size && incoming.size;
  W.cams.forEach((k, name) => { if (!incoming.has(name)) { if (k.el) { k.el.classList.add('spawn'); setTimeout(() => k.el.remove(), 500); } W.cams.delete(name); addMsg('s', `− camera ${name} removed`, 'assign'); } });
  $('#camstrip').classList.toggle('hide', !incoming.size);
  $('#ws').classList.toggle('hascams', incoming.size > 0);
  if (first) { layoutAll(true); animateEdges(700); }
  for (const [name, c] of incoming) {
    const k = W.cams.get(name);
    if (k) { Object.assign(k, {source: c.source, journal: c.journal !== false, alerts: !!c.alerts, id: c.id || k.id}); k.el.querySelector('.kind').textContent = camKind(c.source); continue; }
    const nk = {name, id: c.id || null, source: c.source || '', journal: c.journal !== false, alerts: !!c.alerts, el: null, seenTs: 0};
    const el = document.createElement('div'); el.className = 'cam' + (instant ? '' : ' spawn');
    el.innerHTML = `<div class="pic"><span class="kind">${esc(camKind(c.source))}</span></div><div class="cb"><div class="cn">${esc(name)}<span class="tag ${nk.journal ? 'on' : ''}">${nk.journal ? 'journal on' : 'rules only'}</span></div><div class="note">${esc(c.notes || c.focus || 'waiting for the first note')}</div></div>`;
    el.onclick = () => openLive(name);
    $('#cams').appendChild(el); nk.el = el; W.cams.set(name, nk);
    if (!instant) { addMsg('s', `+ camera ${name}: ${camKind(c.source)}${nk.journal ? ', keeping a journal' : ''}`, 'assign'); void el.offsetWidth; await sleep(30); el.classList.remove('spawn'); await sleep(170); }
  }
  if (first && !instant) tutHook('cameras');
}
function startCamPoll(){ clearInterval(W.camPoll); W.evSince = Date.now() / 1000 - 5; pollCams(); W.camPoll = setInterval(pollCams, 4000); }
async function pollCams(){
  if (!W.cams.size) return;
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const r = await api('/cameras'); if (!r || !r.cameras) return;
  for (const c of r.cameras) {
    const k = W.cams.get(c.name); if (!k) continue; k.id = c.id;
    const s = c.seen || {}; const le = c.last_event || {};
    const ts = s.ts || le.ts || 0;
    if (ts && ts !== k.seenTs) {
      k.seenTs = ts;
      const pic = k.el.querySelector('.pic'); let img = pic.querySelector('img');
      const src = `/api/cameras/${c.id}/frame.jpg?t=${Math.round(ts * 1000)}`;
      const pre = new Image(); pre.onload = () => { if (!img) { img = document.createElement('img'); pic.appendChild(img); if (!pic.querySelector('.live')) { const lv = document.createElement('span'); lv.className = 'live'; lv.textContent = 'live'; pic.appendChild(lv); } } img.src = src; }; pre.src = src;
      const counts = Object.entries(s.counts || le.counts || {}).map(([k2, v]) => `${v} ${k2}`).join(', ');
      setCamState(k, s.journal ? 'writing note' : (counts || 'watching'), s.journal ? 'on' : (k.journal ? 'on' : ''));
    }
    const note = s.journal || (le.source === 'journal' || le.source === 'digest' ? le.answer : '');
    if (note && note !== k.note) { k.note = note; const n = k.el.querySelector('.note'); n.textContent = note; n.classList.remove('new'); void n.offsetWidth; n.classList.add('new'); }
  }
  const ev = await api(`/vision/events?hours=1&limit=20`) || [];
  if (Array.isArray(ev)) ev.filter(e => e.ts > W.evSince && (e.source === 'journal' || e.source === 'digest')).sort((a, b) => a.ts - b.ts).forEach(e => {
    W.evSince = Math.max(W.evSince, e.ts);
    feed({ts: e.ts, agent: e.camera, kind: e.source === 'digest' ? 'summary' : 'journal', text: e.answer || e.reason || '', data: {}}, e.source);
    const k = W.cams.get(e.camera); if (k && k.el) { k.el.classList.remove('flash'); void k.el.offsetWidth; k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 900); }
  });
}
function looksLikeQuestion(t){ return /\?\s*$/.test(t) || /^(who|what|when|where|why|how|was|were|did|does|do|is|are|has|have|had|any|anyone|anything|show|describe|tell me|list|summar|count)\b/i.test(t.trim()); }
async function askCams(text){
  addMsg('u', text); setSugg([]);
  W.busy = true; $('#send').disabled = true; $('#orb').classList.add('busy'); $('#orbstate').textContent = 'reading the journal';
  if (W.deskId) await api(`/desks/${W.deskId}/select`, {method: 'POST', body: {}});
  const r = await api('/vision/ask', {method: 'POST', body: {question: text, hours: 24}});
  W.busy = false; $('#send').disabled = false; $('#orb').classList.remove('busy'); $('#orbstate').textContent = 'listening';
  if (!r || r.error) { addMsg('a', (r && r.error) || 'I could not read the journal just now. Try again.'); return; }
  const d = await typeMsg(r.answer || 'Nothing in the journal answers that yet.');
  d.classList.add('cams');
  const ev = (r.evidence || []).filter(e => e.snapshot_url).slice(-6);
  if (ev.length) {
    const row = document.createElement('div'); row.className = 'evid';
    row.innerHTML = ev.map(e => `<a href="${esc(e.snapshot_url)}" target="_blank" rel="noopener" title="${esc(e.camera)} #${e.id}"><img src="${esc(e.snapshot_url)}" loading="lazy" alt=""><span>#${e.id} ${esc(e.camera)}</span></a>`).join('');
    d.appendChild(row); $('#msgs').scrollTop = 1e9;
    [...new Set(ev.map(e => e.camera))].forEach(n => { const k = W.cams.get(n); if (k && k.el) { k.el.classList.add('flash'); setTimeout(() => k.el.classList.remove('flash'), 1400); } });
  }
  const m = r.retrieval || {}; if (m.considered) addMsg('s', `searched ${m.considered} journal entries (${m.window || 'last 24h'}), ${m.grounding || ''}`);
  setSugg(CAM_QUESTIONS);
}

/* ------------------------------------------------------------------ front door: my desks, guide */
async function loadMyDesks(){
  const r = await api('/desks'); const list = (r && r.desks || []).filter(d => d.name && d.name !== 'My business').slice(0, 8);
  if (!list.length) return;
  const box = $('#mydesks');
  box.innerHTML = '<span>Or open one of your desks:</span>' + list.map(d => `<button onclick="openDesk(${d.id})">${esc(d.business_name || d.name)}</button>`).join('');
  box.classList.remove('hide');
}
async function openDesk(id){ await api(`/desks/${id}/select`, {method: 'POST', body: {}}); location.href = '/desk/workspace?desk=' + id; }
function openGuide(){ $('#guide').classList.remove('hide'); }
function closeGuide(){ $('#guide').classList.add('hide'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#guide').classList.contains('hide')) closeGuide(); });


/* ------------------------------------------------------------------ free / paid models (always the owner's click) */
async function setTier(tier){
  if (!W.sid) return toast(W.deskId ? 'This desk was built on ' + (W.tier === 'free' ? 'free' : 'paid') + ' models; change it in Desk setup' : 'Start a conversation first');
  const r = await api(`/design/${W.sid}/tier`, {method: 'POST', body: {tier}});
  if (!r || r.error) return toast((r && r.error) || 'could not switch');
  W.tier = r.tier; const lbl = W.tier === 'free' ? 'free models' : 'paid models (about 3p a message)'; $('#tier-btn').textContent = lbl; $('#tier-pill').textContent = lbl;
  addMsg('s', W.tier === 'free' ? 'switched to free models' : 'switched to paid models: Claude for design, a paid vision model for the cameras', 'assign');
}
function toggleTier(){ return setTier(W.tier === 'free' ? 'balanced' : 'free'); }


/* ------------------------------------------------------------------ live view of one camera (motion JPEG with detections) */
function openLive(name){
  const k = W.cams.get(name); if (!k) return;
  if (!k.id) return toast('This camera goes live once the desk is built');
  $('#live-name').textContent = name;
  $('#live-note').textContent = k.note || 'The first journal note appears here within a few seconds.';
  $('#live-journal').href = `/api/vision/journal?format=html&camera=${encodeURIComponent(name)}`;
  $('#live-img').src = `/api/cameras/${k.id}/live.mjpg?fps=12&t=${Date.now()}`;
  $('#liveview').classList.remove('hide'); W.liveCam = name;
}
function closeLive(){ $('#live-img').src = ''; $('#liveview').classList.add('hide'); W.liveCam = null; }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && W.liveCam) closeLive(); });
setInterval(() => { if (W.liveCam) { const k = W.cams.get(W.liveCam); if (k && k.note) $('#live-note').textContent = k.note; } }, 2000);
