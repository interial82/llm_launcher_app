/* ── LLM Launcher PWA ── */
'use strict';
const LS_URL = 'llm.url', LS_KEY = 'llm.key', LS_OPTS = 'llm.opts', LS_CHAT = 'llm.chat_opts';
const LS_CHAT_HIST = 'llm.chat_hist';
const $ = id => document.getElementById(id);
const state = {
  server: localStorage.getItem(LS_URL) || '',
  key: localStorage.getItem(LS_KEY) || '',
};
// 서버 주소가 저장되어 있지 않으면, 이 페이지를 서빙한 서버(같은 기원)를 기본 서버로 사용.
// (아이폰 등에서 http://<PC>:8080 으로 직접 접속해도 설정을 안 했더라도 상태/모델/기동이 바로 동작)
if (!state.server && /^https?:$/.test(location.protocol)) state.server = location.origin;
let selectedModel = null, currentDir = '', currentParent = null, mmprojHint = null, modelFiles = [];
let chat = [], attached = null, genCtrl = null, sseLog = null, lastStatus = null;
let hist = { gpu: [], gpumem: [], sysmem: [], ctx: [] };
const HIST_MAX = 60;

// ── 컴프레셔 상태 ──
let compressor = {
  enabled: true,           // 자동 컴프레셔 활성화 여부 (로컬 토글 — 서버 이벤트 수신 여부)
  threshold: 75,           // 자동 트리거 임계값 (%) — 서버 설정(/api/status)과 동기
  keep_last: 6,            // 최근 유지 메시지 수 — 서버 설정과 동기
  compressing: false,      // 컴프레싱 진행 중인지
  lastCompressCount: 0,    // 마지막 압축 시 메시지 수 (연속 트리거 방지)
};

function url(p) { return state.server.replace(/\/+$/, '') + p; }
function headers() {
  const h = { 'Content-Type': 'application/json' };
  if (state.key) h['X-Api-Key'] = state.key;
  return h;
}
async function api(p, o = {}) {
  const r = await fetch(url(p), { ...o, headers: headers() });
  if (r.status === 401) throw new Error('API 키가 일치하지 않습니다 (401)');
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && (j.error.message || j.error)) || ('HTTP ' + r.status));
  return j;
}
let toastTimer = null;
function toast(msg, ms = 2600) {
  let t = $('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}
function hostOf(u) { try { return new URL(u).host; } catch (e) { return u; } }
function setConn(ok, text, tone) {
  const el = $('connState');
  el.textContent = text;
  el.className = 'conn ' + (tone || (ok ? 'ok' : (text ? 'err' : '')));
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtTok(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}
function fmtMb(mb) { return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB'; }
function clampNum(v, dflt, min, max) {
  const n = parseFloat(v);
  if (isNaN(n)) return dflt;
  return Math.max(min, Math.min(max, n));
}
function renderMd(text) {
  let e = escapeHtml(text);
  e = e.replace(/```([\s\S]*?)```/g, (_, c) => '<pre>' + c.replace(/^\n/, '') + '</pre>');
  e = e.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  e = e.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  e = e.replace(/\n/g, '<br>');
  return e;
}
function stateOf(s) { return s.process === 'running' ? (s.model_state || 'loading') : 'off'; }

// ── 탭 전환 ──
document.querySelectorAll('.tabbar button').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tabbar button').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    b.classList.add('active');
    $('tab-' + b.dataset.tab).classList.add('active');
    if (b.dataset.tab === 'dash') drawSpark();
  });
});
// ── 설정 모달 ──
function openSettings() {
  $('setUrl').value = state.server;
  $('setKey').value = state.key;
  $('setMsg').textContent = '';
  $('settingsOverlay').hidden = false;
  refreshIps();
}
function closeSettings() { $('settingsOverlay').hidden = true; }
async function refreshIps() {
  const box = $('setIps');
  box.innerHTML = '<div class="muted small">IP 목록을 불러오는 중…</div>';
  try {
    const info = await api('/api/lan-info');
    box.innerHTML = '';
    for (const it of info.ips) {
      const row = document.createElement('div'); row.className = 'iprow';
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = it.tailscale ? 'Tailscale' : 'LAN';
      const ip = document.createElement('span'); ip.textContent = it.ip + ':' + info.port; ip.style.flex = '1';
      const btn = document.createElement('button'); btn.textContent = '사용';
      btn.onclick = () => { $('setUrl').value = 'http://' + it.ip + ':' + info.port; };
      row.append(tag, ip, btn); box.appendChild(row);
    }
  } catch (e) {
    box.innerHTML = '<div class="muted small">IP 목록을 불러올 수 없습니다: ' + escapeHtml(e.message) +
      '<br>주소를 직접 입력하세요. (Tailscale: 100.x.y.z)</div>';
  }
}
$('btnSettings').addEventListener('click', openSettings);
$('btnCloseSet').addEventListener('click', closeSettings);
$('settingsOverlay').addEventListener('click', e => { if (e.target === $('settingsOverlay')) closeSettings(); });
$('btnSaveSet').addEventListener('click', () => {
  state.server = $('setUrl').value.trim().replace(/\/+$/, '');
  state.key = $('setKey').value.trim();
  localStorage.setItem(LS_URL, state.server);
  localStorage.setItem(LS_KEY, state.key);
  closeSettings();
  toast('설정이 저장되었습니다');
  connectLogStream(); connectEvents(); pollStatus();
  if (state.server) loadDir('');
});
$('btnTestSet').addEventListener('click', async () => {
  const u = $('setUrl').value.trim().replace(/\/+$/, '');
  const k = $('setKey').value.trim();
  const msg = $('setMsg'); msg.textContent = '테스트 중…';
  try {
    const r = await fetch(u + '/api/health', { headers: k ? { 'X-Api-Key': k } : {} });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || ('HTTP ' + r.status));
    msg.textContent = '✅ 연결 성공 (모델: ' + (j.model_state || '미기동') + ')';
  } catch (e) {
    msg.textContent = '❌ 연결 실패: ' + e.message;
  }
});
// ── 대시보드 폴링 ──
let connOk = false;
async function pollStatus() {
  if (!state.server) return;
  if (!connOk) setConn(false, '연결 확인 중… ' + hostOf(state.server), 'wait');
  try {
    const s = await api('/api/status');
    lastStatus = s;
    loadCompressorSettings(); // 서버 설정 동기화
    connOk = true;
    const st = stateOf(s);
    setConn(true, '연결됨');
    const badge = $('stateBadge');
    badge.textContent = st === 'loaded' ? '실행 중' : st === 'loading' ? '로딩 중' : st === 'sleeping' ? '수면(대기)' : '정지됨';
    badge.className = 'badge ' + (st === 'loaded' ? 'on' : st);
    $('modelName').textContent = s.model || '모델 없음';
    const g = s.gpu;
    $('barGpu').style.width = (g && g.util != null ? g.util : 0) + '%';
    $('valGpu').textContent = g && g.util != null ? Math.round(g.util) + '%' : '—';
    $('barGpuRam').style.width = (g && g.mem_pct != null ? g.mem_pct : 0) + '%';
    $('valGpuRam').textContent = g && g.mem_used_mb != null
      ? (g.mem_used_mb / 1024).toFixed(1) + '/' + (g.mem_total_mb / 1024).toFixed(1) + ' GB' : '—';
    $('barSysRam').style.width = (s.sys_ram != null ? s.sys_ram : 0) + '%';
    $('valSysRam').textContent = s.sys_ram != null ? Math.round(s.sys_ram) + '%' : '—';
    const c = s.context || {};
    const cpct = c.max ? 100 * c.used / c.max : 0;
    $('barCtx').style.width = Math.min(100, cpct) + '%';
    $('valCtx').textContent = (c.used != null && c.max != null) ? fmtTok(c.used) + ' / ' + fmtTok(c.max) : '—';
    // ── 자동 압축: 서버가 임계값 초과를 감지해 'compress' 이벤트를 발행 → connectEvents() ──
    const tok = s.tokens || {};
    $('tokenStats').textContent = fmtTok((tok.input || 0) + (tok.output || 0)) + ' tokens';
    $('ttlInfo').textContent = (st === 'off') ? ''
      : (s.ttl_min > 0 ? ('TTL 자동 언로드 ' + s.ttl_min + '분 · ') : 'TTL 해제 · ')
      + (c.used != null && c.max != null ? ('컨텍스트 ' + fmtTok(c.used) + '/' + fmtTok(c.max)) : '');
    const push = (a, v) => { a.push(v); if (a.length > HIST_MAX) a.shift(); };
    push(hist.gpu, g && g.util != null ? g.util : 0);
    push(hist.gpumem, g && g.mem_pct != null ? g.mem_pct : 0);
    push(hist.sysmem, s.sys_ram != null ? s.sys_ram : 0);
    push(hist.ctx, Math.min(100, cpct));
    if ($('tab-dash').classList.contains('active')) drawSpark();
  } catch (e) {
    connOk = false;
    // 저장된 서버 주소에 접속이 안 되면, 이 페이지를 서빙 중인 현재 주소(같은 기원)로 자동 전환.
    // (예: 저장된 IP가 바뀌어서 죽은 경우 — 페이지가 열렸으니 현재 주소는 반드시 살아있음)
    if (/^https?:$/.test(location.protocol) && state.server !== location.origin) {
      state.server = location.origin;
      toast('저장된 주소에 접속할 수 없어 현재 주소 ' + hostOf(location.origin) + '으로 전환했습니다', 4000);
      loadDir('');
      loadPresets();
      connectEvents();
      pollStatus();
      return;
    }
    setConn(false, '연결 끊김: ' + e.message + ' (' + hostOf(state.server) + ')');
  }
}
function drawSpark() {
  const cv = $('spark');
  const ctx = cv.getContext('2d');
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W; cv.height = H;
  ctx.clearRect(0, 0, W, H);
  const series = [[hist.gpu, '#4fd1c5'], [hist.gpumem, '#58a6ff'], [hist.sysmem, '#f2a33c'], [hist.ctx, '#56d364']];
  for (const [arr, color] of series) {
    if (arr.length < 2) continue;
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    for (let i = 0; i < arr.length; i++) {
      const x = (i / (HIST_MAX - 1)) * W;
      const y = H - (arr[i] / 100) * (H - 6) - 3;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}
// ── 컨텍스트 컴프레셔 ──
async function compressChat(auto = false, reason = 'auto') {
  if (compressor.compressing) {
    if (!auto) toast('컴프레셔가 실행 중입니다…');
    return;
  }
  if (chat.length < 3) {
    if (!auto) toast('압축할 메시지가 부족합니다 (최소 3개 필요)');
    return;
  }
  if (!state.server) {
    if (!auto) toast('서버가 연결되지 않았습니다');
    return;
  }
  
  compressor.compressing = true;
  const btnCompress = $('btnCompress');
  if (btnCompress) {
    btnCompress.disabled = true;
    btnCompress.textContent = '⏳ 요약 중…';
  }
  
  if (auto) {
    toast(`🗜 자동 컴프레셔 작동 (${compressor.threshold}% 초과)`);
  }
  
  try {
    const result = await api('/api/compress', {
      method: 'POST',
      body: JSON.stringify({
        messages: chat,
        keep_last: compressor.keep_last || 6,
      }),
    });
    
    if (result.ok) {
      // 압축된 메시지로 chat 배열 교체
      chat = result.compressed;
      compressor.lastCompressCount = chat.length;
      persistChat();
      
      // 채팅 UI 새로 그리기 (system = 압축 요약)
      const msgsEl = $('chatMsgs');
      msgsEl.innerHTML = '';
      for (const msg of chat) {
        const aEl = document.createElement('div');
        const cls = msg.role === 'user' ? 'user' : 'model';
        aEl.className = 'msg-item ' + cls;
        msgsEl.appendChild(aEl);
        if (msg.role === 'system') {
          aEl.innerHTML = '<span class="reasoning">🗜 ' + escapeHtml(msg.content || '') + '</span>';
        } else {
          renderMsgEl(aEl, msg);
        }
      }
      msgsEl.scrollTop = msgsEl.scrollHeight;
      
      const saved = result.original_count - result.compressed_count;
      toast(`✅ 압축 완료: ${result.original_count}개 → ${result.compressed_count}개 (${saved}개 요약됨)`, 3000);
    } else {
      if (!auto) toast(`❌ 압축 실패: ${result.error}`);
    }
  } catch (e) {
    if (!auto) toast(`❌ 압축 오류: ${e.message}`);
  } finally {
    compressor.compressing = false;
    if (btnCompress) {
      btnCompress.disabled = false;
      btnCompress.textContent = '🗜 압축';
    }
  }
}

// ── 설정에서 컴프레셔 옵션 로드 (비율/유지 개수 동기화 — 자동 토글은 로컬 저장) ──
function loadCompressorSettings() {
  try {
    if (lastStatus && lastStatus.compressor) {
      compressor.threshold = lastStatus.compressor.auto_trigger_pct || 75;
      compressor.keep_last = lastStatus.compressor.keep_last_msgs || 6;
      const pctEl = $('optCompressPct'), keepEl = $('optCompressKeep');
      if (pctEl) pctEl.value = compressor.threshold;
      if (keepEl) keepEl.value = compressor.keep_last;
    }
  } catch (e) { /* 무시 */ }
}
// ── 모델 탭: 디렉터리 탐색 ──
async function loadDir(dir) {
  const listEl = $('dirList');
  listEl.innerHTML = '<div class="muted small">불러오는 중…</div>';
  try {
    const r = await api('/api/models' + (dir ? '?dir=' + encodeURIComponent(dir) : ''));
    currentDir = r.dir;
    currentParent = r.parent || null;
    mmprojHint = r.mmproj_hint || null;
    $('mmprojInfo').textContent = r.mmproj_hint
      ? ('🔍 mmproj 자동 적용: ' + r.mmproj_hint.split(/[\\/]/).pop()) : '';
    $('modelsMsg').textContent = '';
    $('modelsMsg').className = 'msg';
    listEl.innerHTML = '';
    if (r.parent) {
      const up = document.createElement('button');
      up.textContent = '⬆ ' + (r.parent.split(/[\\/]/).filter(Boolean).pop() || '부모');
      up.onclick = () => loadDir(r.parent);
      listEl.appendChild(up);
    }
    for (const d of (r.dirs || [])) {
      const b = document.createElement('button');
      b.textContent = d + '/';
      b.onclick = () => loadDir(currentDir ? currentDir + '/' + d : d);
      listEl.appendChild(b);
    }
    $('dirInput').value = r.dir || '';
    if (r.error) $('modelsMsg').textContent = '⚠ ' + r.error;
    modelFiles = r.files || [];
    renderModels(modelFiles);
  } catch (e) {
    listEl.innerHTML = '';
    $('modelsMsg').textContent = '❌ ' + e.message;
    $('modelsMsg').className = 'msg err';
  }
}
function renderModels(files) {
  const box = $('modelList');
  box.innerHTML = '';
  const mains = files.filter(f => !f.is_mmproj);
  if (!mains.length) { box.innerHTML = '<div class="muted small">GGUF 모델이 없습니다.</div>'; return; }
  for (const f of mains) {
    const item = document.createElement('div');
    item.className = 'model-item';
    if (selectedModel && selectedModel.path === f.path) item.classList.add('selected');
    const name = document.createElement('span'); name.className = 'fname'; name.textContent = f.name;
    const size = document.createElement('span'); size.className = 'fsize'; size.textContent = fmtMb(f.size_mb);
    item.append(name, size);
    item.onclick = () => { selectedModel = { name: f.name, path: f.path, size_mb: f.size_mb, layer_count: f.layer_count }; if (f.layer_count) $('optNgl').value = String(f.layer_count); renderModels(files); };
    box.appendChild(item);
  }
}
// ── 프리셋(설정 저장): /api/presets — 선택 시 저장된 옵션 일괄 적용 ──
let presetList = [], presetSig = null;
function selectedPreset() {
  const sel = $('optPreset');
  if (!sel || !sel.value) return null;
  return presetList.find(p => p.name === sel.value) || null;
}
async function loadPresets() {
  const sel = $('optPreset');
  if (!sel || !state.server) return;
  try {
    const r = await api('/api/presets');
    const list = Array.isArray(r.presets) ? r.presets : [];
    presetList = list;
    // 프리셋 목록이 실제로 바뀌었을 때만 재구성 (열려있는 드롭다운을 방해하지 않도록)
    const sig = list.map(p => p.name + '|' + (p.model || '')).join('§');
    if (sig === presetSig) return;
    presetSig = sig;
    const cur = sel.value;
    sel.innerHTML = '';
    const def = document.createElement('option');
    def.value = ''; def.textContent = '— 적용 안 함 —';
    sel.appendChild(def);
    for (const p of list) {
      const o = document.createElement('option');
      o.value = p.name; o.textContent = p.name;
      sel.appendChild(o);
    }
    if (list.some(p => p.name === cur)) sel.value = cur;
  } catch (e) { /* 무시 — 다음 폴링에 프리셋 목록 갱신 */ }
}
function presetSummary(p) {
  const bits = [];
  if (p.model) bits.push(p.model.split(/[\\/]/).pop());
  if (p.ngl) bits.push('NGL ' + p.ngl);
  if (p.ctx) bits.push(p.ctx);
  if (p.mtp) bits.push('MTP' + (p.mtp_max && String(p.mtp_max) !== '0' ? '×' + p.mtp_max : ''));
  if (p.fa) bits.push('FA on');
  if (p.n) bits.push('N ' + p.n);
  if (p.mmproj) bits.push('mmproj');
  if (p.server_exe) bits.push('exe ' + p.server_exe.split(/[\\/]/).pop());
  if (p.compressor) bits.push(p.compressor.enabled
    ? '압축 ' + (p.compressor.auto_trigger_pct || 75) + '%/유지 ' + (p.compressor.keep_last_msgs || 6)
    : '압축 off');
  return bits.join(' · ');
}
function applyPresetToUi(name) {
  const info = $('presetInfo');
  const p = presetList.find(x => x.name === name);
  if (!p) { if (info) info.textContent = ''; return; }
  if (p.model) {
    const f = modelFiles.find(f => f.path === p.model);
    if (f) {
      selectedModel = { name: f.name, path: f.path, size_mb: f.size_mb, layer_count: f.layer_count };
      renderModels(modelFiles);
    } else {
      // 모델 목록에 없어도 경로 자체를 지정해 기동 페이로드로 바로 사용
      selectedModel = { name: p.model.split(/[\\/]/).pop(), path: p.model, size_mb: null };
    }
  }
  // NGL: 프리셋 값 적용 후, 모델 계층 수가 있으면 그것으로 덮어씀
  if (p.ngl) $('optNgl').value = p.ngl;
  if (selectedModel && selectedModel.layer_count) $('optNgl').value = String(selectedModel.layer_count);
  if (p.ctx) $('optCtx').value = p.ctx;
  if (typeof p.fa === 'boolean') $('optFa').checked = p.fa;
  if (p.ctk) $('optCtk').value = p.ctk;
  if (p.ctv) $('optCtv').value = p.ctv;
  if (p.np) $('optNp').value = String(p.np);
  if (typeof p.mtp === 'boolean') $('optMtp').checked = p.mtp;
  if (p.mtp_max != null && String(p.mtp_max) !== '') $('optMtpMax').value = String(p.mtp_max);
  // 컴프레셔 옵션 (자동/트리거비율/유지개수) — 적용 후 서버에 즉시 동기화
  if (p.compressor) {
    if (typeof p.compressor.enabled === 'boolean') $('optCompressAuto').checked = p.compressor.enabled;
    if (p.compressor.auto_trigger_pct != null) $('optCompressPct').value = String(p.compressor.auto_trigger_pct);
    if (p.compressor.keep_last_msgs != null) $('optCompressKeep').value = String(p.compressor.keep_last_msgs);
    compressor.enabled = $('optCompressAuto').checked;
    compressor.threshold = parseInt($('optCompressPct').value, 10) || 75;
    compressor.keep_last = parseInt($('optCompressKeep').value, 10) || 6;
    try { localStorage.setItem('llm.compressor_enabled', compressor.enabled); } catch (e) { /* 무시 */ }
    saveCompressorOpts();
  }
  if (info) info.textContent = '💾 ' + presetSummary(p);
  toast('프리셋 "' + name + '" 적용됨');
}
$('optPreset').addEventListener('change', () => {
  const name = $('optPreset').value;
  if (!name) { const i = $('presetInfo'); if (i) i.textContent = ''; return; }
  applyPresetToUi(name);
});
function getLaunchPayload() {
  const preset = selectedPreset();
  const payload = {
    model: selectedModel ? selectedModel.path : '',
    ngl: $('optNgl').value || '999',
    ctx: $('optCtx').value || '128K',
    fa: $('optFa').checked,
    ctk: $('optCtk').value || 'q8_0',
    ctv: $('optCtv').value || 'q8_0',
    np: String($('optNp').value || '1'),
    mtp: $('optMtp').checked,
    mtp_max: parseInt($('optMtpMax').value || '0', 10),
    vision: $('optVision').checked,
    ttl_min: parseInt($('optTtl').value || '0', 10),
  };
  // mmproj/n/exe 경로는 웹 UI에 별도 옵션 항목이 없어 선택된 프리셋에서 가져옴
  if (preset) {
    if (preset.mmproj) payload.mmproj = preset.mmproj;
    if (preset.n) payload.n = String(preset.n);
    if (preset.server_exe) payload.server_exe = preset.server_exe;
  }
  return payload;
}
function saveOpts() {
  try {
    localStorage.setItem(LS_OPTS, JSON.stringify({
      ngl: $('optNgl').value, ctx: $('optCtx').value, threads: $('optNp').value,
      fa: $('optFa').checked, vision: $('optVision').checked, mtp: $('optMtp').checked,
      mtp_max: $('optMtpMax').value, ttl: $('optTtl').value,
    }));
  } catch (e) { /* 무시 — 기동 자체는 계속 진행 */ }
}
function restoreOpts() {
  try {
    const o = JSON.parse(localStorage.getItem(LS_OPTS) || '{}');
    if (o.ngl) $('optNgl').value = o.ngl;
    if (o.ctx) $('optCtx').value = o.ctx;
    if (o.threads) $('optNp').value = o.threads;
    if (typeof o.fa === 'boolean') $('optFa').checked = o.fa;
    if (typeof o.vision === 'boolean') $('optVision').checked = o.vision;
    if (typeof o.mtp === 'boolean') $('optMtp').checked = o.mtp;
    if (o.mtp_max) $('optMtpMax').value = o.mtp_max;
    if (o.ttl) $('optTtl').value = o.ttl;
  } catch (e) { /* 무시 */ }
}
function showLaunchMsg(text, cls) {
  const m = $('modelsMsg');
  if (m) { m.textContent = text; m.className = 'msg' + (cls ? ' ' + cls : ''); }
}
async function doLaunch() {
  // 모델 선택 없으면 서버의 마지막 기동 설정(last_launch) 사용 — 대시보드 "기동 (마지막 설정)" 버튼용
  let btns = [], oldText = [];
  try {
    const last = lastStatus && lastStatus.last_launch;
    if (!selectedModel && !(last && last.model)) { toast('모델을 먼저 선택하세요'); return; }
    if (selectedModel) saveOpts();
    showLaunchMsg('기동 요청 중…');
    btns = [$('btnLaunch'), $('btnDashLaunch')].filter(Boolean);
    oldText = btns.map(b => b.textContent);
    btns.forEach(b => { b.disabled = true; b.textContent = '⏳ 요청 중…'; });
    const payload = selectedModel ? getLaunchPayload() : { model: '' };
    const r = await api('/api/launch', { method: 'POST', body: JSON.stringify(payload) });
    showLaunchMsg('✅ ' + (r.message || '기동 요청됨'), 'ok');
    toast('모델 기동 요청됨');
  } catch (e) {
    // 버튼이 "반응 없이 죽는" 일이 없도록 모든 예외를 화면에 표시
    showLaunchMsg('❌ ' + e.message, 'err');
    toast('❌ 기동 오류: ' + e.message, 4000);
  } finally {
    btns.forEach((b, i) => { b.disabled = false; b.textContent = oldText[i]; });
  }
}
async function doStop() {
  const btns = [$('btnStop'), $('btnDashStop')];
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/api/stop', { method: 'POST' });
    toast(r.message || '중지 요청됨');
  } catch (e) { toast('❌ ' + e.message); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}
$('btnLaunch').addEventListener('click', doLaunch);
$('btnDashLaunch').addEventListener('click', doLaunch);
$('btnStop').addEventListener('click', doStop);
$('btnDashStop').addEventListener('click', doStop);
$('btnDirGo').addEventListener('click', () => loadDir($('dirInput').value.trim()));
$('btnDirUp').addEventListener('click', () => { if (currentParent) loadDir(currentParent); });
$('dirInput').addEventListener('keydown', e => { if (e.key === 'Enter') loadDir($('dirInput').value.trim()); });
// ── 로그 (SSE + 폴백) ──
const MAX_LOG = 400;
function addLogLine(ts, line) {
  const view = $('logView');
  const el = document.createElement('div');
  el.className = 'logline';
  let t = '';
  try { t = new Date(ts).toTimeString().slice(0, 8); } catch (e) { /* 무시 */ }
  el.innerHTML = (t ? '<span class="ts">' + t + '</span>' : '') + escapeHtml(line);
  if (/error|fail/i.test(line)) el.classList.add('err');
  else if (/warn/i.test(line)) el.classList.add('warn');
  else if (/ready|launch|loaded|started/i.test(line)) el.classList.add('ok');
  view.appendChild(el);
  while (view.children.length > MAX_LOG) view.removeChild(view.firstChild);
  view.scrollTop = view.scrollHeight;
}
function setLogMode(sse) { $('logState').textContent = sse ? '라이브 (SSE)' : '폴백 (폴링)'; }
function connectLogStream() {
  if (sseLog) { sseLog.close(); sseLog = null; }
  if (!state.server) return;
  const es = new EventSource(url('/api/logs/stream'));
  es.onopen = () => setLogMode(true);
  es.onmessage = ev => {
    try {
      const d = JSON.parse(ev.data);
      if (d.type === 'init') for (const [seq, ts, l] of d.lines) addLogLine(ts, l);
      else if (d.type === 'log') addLogLine(d.ts, d.line);
    } catch (e) { /* 무시 */ }
  };
  es.onerror = () => { setLogMode(false); es.close(); sseLog = null; };
  sseLog = es;
}
// ── 서버 제어 이벤트 (SSE): 컨텍스트 비율 임계값 초과 시 'compress' 수신 ──
let sseEvents = null;
function connectEvents() {
  if (sseEvents) { sseEvents.close(); sseEvents = null; }
  if (!state.server) return;
  const es = new EventSource(url('/api/events'));
  es.addEventListener('compress', ev => {
    let d = {};
    try { d = JSON.parse(ev.data || '{}'); } catch (e) { /* 무시 */ }
    if (!compressor.enabled) return;
    if (compressor.compressing) return;
    if (chat.length <= (compressor.keep_last || 6) + 1) return;
    compressChat(true, d.reason || 'auto');
  });
  sseEvents = es;
}
async function pollLogsOnce() {
  if (sseLog || !state.server) return;
  try {
    const r = await api('/api/logs?lines=200');
    const view = $('logView');
    view.innerHTML = '';
    for (const l of (r.lines || [])) addLogLine(l.ts, l.line);
  } catch (e) { /* 무시 */ }
}
$('btnClearLogs').addEventListener('click', () => { $('logView').innerHTML = ''; });
// ── 채팅 ──
function addMsgEl(role) {
  const msgs = $('chatMsgs');
  const el = document.createElement('div');
  el.className = 'msg-item ' + role;
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}
function msgToContent(m) {
  if (m._attach) {
    return [
      { type: 'text', text: m.content || '' },
      { type: 'image_url', image_url: { url: m._attach } },
    ];
  }
  return m.content;
}
function renderMsgEl(el, m) {
  let html = '';
  if (m._attach) html += '<span class="attach-tag">📎 ' + escapeHtml(m._attachName || '첨부') + '</span><br>';
  if (m.reasoning) html += '<span class="reasoning">💭 ' + escapeHtml(m.reasoning) + '</span>';
  html += renderMd(m._display != null ? m._display : m.content);
  el.innerHTML = html;
}
function addChatMsg(role, content, opts = {}) {
  const m = { role, content, reasoning: opts.reasoning || null, _attach: opts.attach || null, _attachName: opts.attachName || null };
  if (role !== 'error') {
    // error 메시지는 표시 전용 — chat 배열에 넣으면 API 요청에 role:'error'가
    // 포함돼 llama-server가 400을 반환한다 (영속화 대상에서도 제외)
    chat.push(m);
    persistChat();
  }
  const el = addMsgEl(role);
  renderMsgEl(el, m);
  return m;
}
// ── 채팅 기록 영속화 (PWA 재로드/브라우저 종료 시 컨텍스트 보존) ──
// 서버는 무상태(매 요청 전체 히스토리 전송)이므로 클라이언트 히스토리가 살아
// 있어야 재로드 후에도 동일한 컨텍스트가 유지된다. 이미지 dataUrl은 용량이
// 커서 저장하지 않고(텍스트 + 첨부 파일명만 보존), 최대 200개 메시지만 남긴다.
function persistChat() {
  // localStorage 쿤타 초과 시 작은 크기로 재시도 (200→100→50)
  for (const limit of [200, 100, 50]) {
    try {
      const slim = chat.slice(-limit).map(m => ({
        role: m.role, content: m.content, reasoning: m.reasoning || null,
        _attachName: m._attachName || null,
      }));
      localStorage.setItem(LS_CHAT_HIST, JSON.stringify(slim));
      return;
    } catch (e) { /* 쿼터 초과 — 더 작게 재시도 */ }
  }
}
function restoreChat() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LS_CHAT_HIST) || 'null'); } catch (e) { return; }
  if (!Array.isArray(saved) || !saved.length) return;
  const msgsEl = $('chatMsgs');
  let n = 0;
  for (const m of saved) {
    if (!m || (m.role !== 'user' && m.role !== 'assistant' && m.role !== 'system')) continue;
    if (m.role === 'assistant' && !m.content && !m.reasoning) continue;  // 생성 중 닫힌 빈 메시지
    const el = addMsgEl(m.role === 'user' ? 'user' : 'model');
    let html = '';
    if (m._attachName) html += '<span class="attach-tag">📎 ' + escapeHtml(m._attachName) + '</span><br>';
    if (m.reasoning) html += '<span class="reasoning">💭 ' + escapeHtml(m.reasoning) + '</span>';
    if (m.role === 'system') html += '<span class="reasoning">🗜 ' + escapeHtml(m.content || '') + '</span>';
    else html += renderMd(m.content || '');
    el.innerHTML = html;
    chat.push({ role: m.role, content: m.content, reasoning: m.reasoning || null, _attachName: m._attachName || null });
    n++;
  }
  msgsEl.scrollTop = msgsEl.scrollHeight;
  if (n) toast('이전 대화 ' + n + '건을 복원했습니다 (이미지 첨부는 재전송되지 않음)', 3000);
}
async function sendChat() {
  const input = $('chatInput');
  const text = input.value.trim();
  if ((!text && !attached) || genCtrl) return;
  if (lastStatus && stateOf(lastStatus) === 'off') {
    addChatMsg('error', '모델이 기동되어 있지 않습니다. 모델 탭에서 먼저 기동하세요.');
    return;
  }
  const m = addChatMsg('user', text, { attach: attached ? attached.dataUrl : null, attachName: attached ? attached.name : null });
  m._display = text;
  input.value = '';
  autoGrow();
  clearAttach();
  const aEl = addMsgEl('assistant typing');
  aEl.textContent = '';
  const acc = { content: '', reasoning: '' };
  const am = { role: 'assistant', content: '', reasoning: null, _display: '' };
  chat.push(am);
  genCtrl = new AbortController();
  $('btnSend').disabled = true;
  $('btnStopGen').hidden = false;
  $('btnAttach').disabled = true;
  try {
    const body = {
      model: (lastStatus && lastStatus.model) || 'local-model',
      // 유효한 역할+내용만 요청에 포함 (error/빈 메시지 제외 — llama-server 400 방지)
      messages: chat
        .filter(c => c.role !== 'error' && (c.content || c._attach))
        .map(c => ({ role: c.role, content: msgToContent(c) })),
      stream: true,
      max_tokens: parseInt($('maxTokens').value || '4096', 10),
      temperature: clampNum($('optTemp').value, 0.7, 0, 2),
      top_p: clampNum($('optTopP').value, 0.95, 0, 1),
      reasoning_effort: $('reasoningEffort').value || undefined,
    };
    const ctKwargs = { enable_thinking: $('optThinking').checked };
    if (body.reasoning_effort) ctKwargs.reasoning_effort = body.reasoning_effort;
    body.chat_template_kwargs = ctKwargs;
    const r = await fetch(url('/v1/chat/completions'), {
      method: 'POST', headers: headers(), body: JSON.stringify(body), signal: genCtrl.signal,
    });
    if (!r.ok || !r.body) {
      const j = await r.json().catch(() => ({}));
      throw new Error((j.error && (j.error.message || j.error)) || ('HTTP ' + r.status));
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n');
      buf = parts.pop();
      for (const line of parts) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const data = t.slice(5).trim();
        if (data === '[DONE]') continue;
        try {
          const j = JSON.parse(data);
          if (j.error) throw new Error(j.error.message || '서버 오류');
          const d = j.choices && j.choices[0] && j.choices[0].delta;
          if (!d) continue;
          if (d.content) acc.content += d.content;
          const rk = d.reasoning || d.reasoning_content;
          if (typeof rk === 'string' && rk) acc.reasoning += rk;
          am._display = acc.content;
          renderMsgEl(aEl, am);
          const msgs = $('chatMsgs');
          msgs.scrollTop = msgs.scrollHeight;
        } catch (e) {
          if (e.message && e.message.indexOf('JSON') === -1) throw e;
        }
      }
    }
    am.content = acc.content || '(빈 응답)';
    am.reasoning = acc.reasoning || null;
    renderMsgEl(aEl, am);
  } catch (e) {
    if (e.name === 'AbortError') {
      am.content = acc.content;
      am.reasoning = acc.reasoning || null;
      if (!acc.content && !acc.reasoning) {
        // 아무 내용도 없는 중단된 메시지인 경우 요청/영속화 대상에서 제거
        const i = chat.indexOf(am);
        if (i >= 0) chat.splice(i, 1);
      }
      am._display = (acc.content || '') + ' ⏹ 중지됨';
      renderMsgEl(aEl, am);
    } else {
      aEl.className = 'msg-item error';
      aEl.textContent = '❌ ' + e.message;
    }
  } finally {
    aEl.classList.remove('typing');
    genCtrl = null;
    $('btnSend').disabled = false;
    $('btnStopGen').hidden = true;
    $('btnAttach').disabled = false;
    $('chatInput').focus();
    persistChat();
  }
}
$('btnSend').addEventListener('click', sendChat);
$('chatInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
function autoGrow() {
  const t = $('chatInput');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 120) + 'px';
}
$('chatInput').addEventListener('input', autoGrow);
$('btnStopGen').addEventListener('click', () => { if (genCtrl) genCtrl.abort(); });
$('btnClearChat').addEventListener('click', () => { chat = []; $('chatMsgs').innerHTML = ''; clearAttach(); try { localStorage.removeItem(LS_CHAT_HIST); } catch (e) { /* 무시 */ } });
function saveChatOpts() {
  try {
    localStorage.setItem(LS_CHAT, JSON.stringify({
      thinking: $('optThinking').checked, temp: $('optTemp').value, top_p: $('optTopP').value,
    }));
  } catch (e) { /* 무시 */ }
}
function restoreChatOpts() {
  try {
    const o = JSON.parse(localStorage.getItem(LS_CHAT) || '{}');
    if (typeof o.thinking === 'boolean') $('optThinking').checked = o.thinking;
    if (o.temp) $('optTemp').value = o.temp;
    if (o.top_p) $('optTopP').value = o.top_p;
  } catch (e) { /* 무시 */ }
}
['optThinking', 'optTemp', 'optTopP'].forEach(id => $(id).addEventListener('change', saveChatOpts));

// ── 컴프레셔 UI 이벤트 ──
$('btnCompress').addEventListener('click', () => compressChat(false));
$('btnCompressNow').addEventListener('click', () => compressChat(false));
['optCompressAuto', 'optCompressPct', 'optCompressKeep'].forEach(id => $(id).addEventListener('change', () => {
  if (id === 'optCompressAuto') {
    compressor.enabled = $('optCompressAuto').checked;
    try { localStorage.setItem('llm.compressor_enabled', compressor.enabled); } catch (e) { /* 무시 */ }
  }
  saveCompressorOpts();
}));
function saveCompressorOpts() {
  try {
    api('/api/compressor', {
      method: 'POST',
      body: JSON.stringify({
        enabled: $('optCompressAuto').checked,
        auto_trigger_pct: clampNum($('optCompressPct').value, 75, 10, 99),
        keep_last_msgs: clampNum($('optCompressKeep').value, 6, 1, 50),
      }),
    });
  } catch (e) { /* 무시 */ }
}
function restoreCompressorOpts() {
  try {
    const saved = localStorage.getItem('llm.compressor_enabled');
    if (saved !== null) $('optCompressAuto').checked = saved === 'true';
  } catch (e) { /* 무시 */ }
  compressor.enabled = $('optCompressAuto').checked;
}

function clearAttach() { attached = null; $('attachRow').hidden = true; }
$('btnAttach').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', e => {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  if (!f.type || !f.type.startsWith('image/')) {
    toast('이미지 파일만 첨부할 수 있습니다 (JPG·PNG·WebP 등) — 영상은 비전 모델이 지원하지 않습니다');
    e.target.value = '';
    return;
  }
  if (f.size > 15 * 1024 * 1024) { toast('15MB 이하만 첨부할 수 있습니다'); return; }
  const rd = new FileReader();
  rd.onload = () => {
    attached = { name: f.name, dataUrl: rd.result };
    $('attachName').textContent = '📎 ' + f.name;
    $('attachRow').hidden = false;
  };
  rd.readAsDataURL(f);
  e.target.value = '';
});
$('btnRemoveAttach').addEventListener('click', clearAttach);

// ── 초기화 ──
restoreOpts();
restoreChatOpts();
restoreCompressorOpts();
restoreChat();

// PWA: 서비스 워커 등록 (오프라인/서버 연결 실패 폴백 + 앱 셸 캐시)
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => { /* 무시 */ });
}

connectLogStream();
connectEvents();
pollStatus();
if (state.server) loadDir('');
loadPresets();
setInterval(pollStatus, 3000);
setInterval(pollLogsOnce, 5000);
setInterval(loadPresets, 15000);