import { articlePayload, charCount, copyArticle, mergeServerArticle, newBlockId } from './article.js';

const API = window.MOA_API || 'http://localhost:8000';
const $ = id => document.getElementById(id);
const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const storageGet = key => { try { return localStorage.getItem(key); } catch { return null; } };
const storageSet = (key, value) => { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* 저장소를 못 써도 동작 */ } };

let token = storageGet('moa:token');
let authMode = 'login';
let session = null;            // 서버의 세션 상태 (public_view)
let article = copyArticle({}); // 화면의 글
let busy = false;              // 답변 보내는 중
let drafting = null;           // 글 갱신 요청 중이면 { editedIds, deletedIds, titleEdited }
let saveTimer = null;
let profilePoll = null;
let memos = [];

// ================================================================ API
class ApiError extends Error {}
async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(API + path, { method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(`서버에 연결할 수 없어요. API 서버(${API})가 켜져 있는지 확인해 주세요.`);
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && token) { showAuth(); throw new ApiError('다시 로그인해 주세요.'); }
  if (!res.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했어요.');
  return data;
}
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { $('toast').hidden = true; }, 4200); }

// ================================================================ 로그인 / 회원가입
function setAuthMode(mode) {
  authMode = mode; const signup = mode === 'signup';
  $('signup-fields').hidden = !signup; $('blog-name').required = signup; $('blog-url').required = signup;
  $('auth-password').autocomplete = signup ? 'new-password' : 'current-password';
  $('auth-title').textContent = signup ? '나만의 글쓰기를 시작해요.' : '다시 만나서 반가워요.';
  $('auth-description').textContent = signup ? '계정과 블로그 정보를 입력해주세요. 블로그 글로 내 문체를 배워요.' : '이메일로 로그인하고 글쓰기를 이어가세요.';
  $('auth-submit').firstChild.textContent = signup ? '회원가입하기 ' : '로그인하기 ';
  $('auth-switch-copy').textContent = signup ? '이미 계정이 있으신가요?' : '아직 계정이 없으신가요?';
  $('auth-toggle').textContent = signup ? '로그인' : '회원가입'; $('auth-error').hidden = true;
}
function authError(text) { $('auth-error').textContent = text; $('auth-error').hidden = false; }
async function finishAuth(data) { token = data.token; storageSet('moa:token', token); await enterWorkspace(); }

$('auth-toggle').addEventListener('click', () => setAuthMode(authMode === 'login' ? 'signup' : 'login'));
$('auth-form').addEventListener('submit', async event => {
  event.preventDefault(); $('auth-error').hidden = true; $('auth-submit').disabled = true;
  const email = $('auth-email').value.trim(); const password = $('auth-password').value;
  try {
    const data = authMode === 'signup'
      ? await api('/api/auth/signup', { method: 'POST', body: { email, password, blog_name: $('blog-name').value.trim(), blog_url: $('blog-url').value.trim() } })
      : await api('/api/auth/login', { method: 'POST', body: { email, password } });
    await finishAuth(data);
  } catch (e) { authError(e.message); } finally { $('auth-submit').disabled = false; }
});
$('demo-login').addEventListener('click', async () => {
  try { await finishAuth(await api('/api/auth/demo', { method: 'POST' })); } catch (e) { authError(e.message); }
});
$('logout').addEventListener('click', async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* 이미 만료 */ } showAuth(); });

function showAuth() {
  token = null; storageSet('moa:token', null); session = null; clearInterval(profilePoll);
  closePanels(); $('app-shell').hidden = true; $('auth-screen').hidden = false; $('auth-password').value = ''; setAuthMode('login');
}

async function enterWorkspace() {
  const me = await api('/api/me');
  $('auth-screen').hidden = true; $('app-shell').hidden = false; $('mode-badge').hidden = !me.demo_mode;
  renderProfile(me.profile);
  loadMemos();
  const { sessions } = await api('/api/sessions');
  if (sessions.length) await openSession(sessions[0].id); else await startSession();
}

// ================================================================ 문체 프로필
function renderProfile(p) {
  clearInterval(profilePoll);
  const dot = $('profile-dot'); const label = $('profile-status'); const chip = $('profile-chip');
  dot.className = 'status-dot'; $('profile-refresh').disabled = false;
  if (!p) { label.textContent = '문체 프로필 없음'; chip.title = 'RE를 누르면 블로그 글로 문체 프로필을 만들어요.'; dot.classList.add('off'); return; }
  if (p.status === 'building') {
    label.textContent = '문체 분석 중…'; chip.title = '블로그 최신 글을 읽고 문체를 분석하고 있어요.'; dot.classList.add('pending'); $('profile-refresh').disabled = true;
    profilePoll = setInterval(async () => { try { renderProfile((await api('/api/profile')).profile); } catch { clearInterval(profilePoll); } }, 3000);
  } else if (p.status === 'ready') {
    label.textContent = `내 문체 · 글 ${p.post_count}개`; chip.title = p.features?.summary || '';
  } else {
    label.textContent = '문체 프로필 실패'; chip.title = p.error || ''; dot.classList.add('error');
  }
}
$('profile-refresh').addEventListener('click', async () => {
  try { renderProfile((await api('/api/profile/refresh', { method: 'POST' })).profile); toast('지금 블로그의 최신 글로 문체 프로필을 다시 만들고 있어요.'); } catch (e) { toast(e.message); }
});

// ================================================================ 세션
async function startSession(memoId = null) {
  setBusy(true);
  try { applyState((await api('/api/sessions', { method: 'POST', body: { memo_id: memoId } })).state, { replaceArticle: true }); }
  catch (e) { toast(e.message); setBusy(false); return; }
  setBusy(false);
  if (session.draft_stale) updateDraft();          // 메모로 시작하면 메모 내용으로 바로 첫 글
}
async function openSession(id) {
  applyState((await api(`/api/sessions/${id}`)).state, { replaceArticle: true });
  if (!session.draft_stale) return;
  const final = session.needs_final;
  await updateDraft();
  if (final) await updateMetrics();               // 최종 완성을 못 하고 나갔던 글
}
$('reset').addEventListener('click', async () => { await flushSave(); startSession(); });

function applyState(state, { replaceArticle = false } = {}) {
  session = state;
  if (replaceArticle) { article = copyArticle(state.article); renderArticle(); }
  renderChat(); renderControls(); renderMetrics();
}

// ================================================================ 대화
function addBubble(text, kind) {
  const row = element('div', `message ${kind}`);
  if (kind.startsWith('assistant')) row.append(element('div', 'avatar', '✳'));
  const bubble = element('div', 'bubble'); bubble.append(element('p', '', text)); row.append(bubble);
  $('messages').append(row); return bubble;
}
function assistantBubble(m) {
  const bubble = addBubble(m.content, 'assistant');
  const question = bubble.firstChild;
  if (m.empathy) bubble.prepend(element('p', 'empathy', m.empathy));
  if (m.reason) {
    const line = element('div', 'question-line'); question.replaceWith(line); line.append(question);
    const toggle = element('button', 'reason-toggle', '+'); toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-label', '이 질문을 한 이유 보기');
    const reason = element('p', 'question-reason'); reason.hidden = true;
    reason.append(element('span', 'reason-label', '질문 이유'), document.createTextNode(m.reason));
    toggle.addEventListener('click', () => {
      const open = reason.hidden; reason.hidden = !open;
      toggle.textContent = open ? '−' : '+'; toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? '질문 이유 닫기' : '이 질문을 한 이유 보기');
    });
    line.append(toggle); bubble.append(reason);
  }
  return bubble;
}
function renderChat(pendingText) {
  $('messages').replaceChildren();
  let lastAssistant = null;
  for (const m of session.conversation) {
    if (m.kind === 'memo') { const b = addBubble(m.content, 'user'); b.prepend(element('span', 'bubble-label', '메모에서 시작')); }
    else if (m.kind === 'skip') addBubble('이 질문은 넘어갈게요', 'user skipped');
    else if (m.role === 'assistant') lastAssistant = assistantBubble(m);
    else if (m.include === false) { const b = addBubble(m.content, 'user excluded'); b.prepend(element('span', 'bubble-label', '글에 반영 안 함')); }
    else addBubble(m.content, 'user');
  }
  if (session.needs_topic && !session.finished && lastAssistant && pendingText === undefined) {
    const topic = element('button', 'topic-button', '주제 추천 받기 →'); topic.type = 'button';
    topic.addEventListener('click', () => toast('주제 추천은 아직 준비 중이에요. 떠오르는 경험이 있으면 편하게 적어 주세요.'));
    lastAssistant.append(topic);
  }
  if (pendingText !== undefined) {
    if (pendingText) addBubble(pendingText.text, pendingText.include === false ? 'user excluded' : 'user');
    addBubble('생각하는 중…', 'assistant thinking');
  }
  $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
}
function renderControls() {
  const done = !!session?.finished;
  $('chat-input').disabled = busy || done; $('send').disabled = busy || done; $('send-exclude').disabled = busy || done;
  $('skip-question').disabled = busy || done; $('stop-questions').disabled = busy || done;
  $('chat-input').placeholder = done ? '질문이 끝났어요. 더 쓰고 싶으면 왼쪽 아래 "더 쓰러 가기"를 눌러주세요.' : '경험을 떠오르는 대로 자세히 들려주세요...';
  $('continue-writing').hidden = !done; $('continue-writing').disabled = busy;
}
function setBusy(value) { busy = value; renderControls(); }

async function sendToLoop(body, pendingText) {
  if (busy || !session) return;
  setBusy(true); renderChat(pendingText);
  try {
    await flushSave();
    const data = await api(`/api/sessions/${session.session_id}/messages`, { method: 'POST', body });
    applyState(data.state);
  } catch (e) { renderChat(); toast(e.message); setBusy(false); return; }
  setBusy(false);
  await updateDraft();
  if (session.finished) await updateMetrics();
}
function sendAnswer(include) {
  const text = $('chat-input').value.trim(); if (!text) { $('chat-input').focus(); return; }
  $('chat-input').value = ''; sendToLoop({ message: text, include }, { text, include });
}
$('chat-form').addEventListener('submit', event => { event.preventDefault(); sendAnswer(true); });
$('send-exclude').addEventListener('click', () => sendAnswer(false));
$('chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); } });
$('skip-question').addEventListener('click', () => sendToLoop({ skip: true }, ''));
$('stop-questions').addEventListener('click', () => sendToLoop({ stop: true }, ''));
$('continue-writing').addEventListener('click', async () => {
  if (busy) return; setBusy(true); renderChat('');
  try { await flushSave(); applyState((await api(`/api/sessions/${session.session_id}/continue`, { method: 'POST' })).state); }
  catch (e) { renderChat(); toast(e.message); }
  setBusy(false); $('chat-input').focus();
});

// ================================================================ 실시간 글
async function updateDraft() {
  if (!session || drafting || !session.draft_stale) return;
  const sid = session.session_id;
  drafting = { editedIds: new Set(), deletedIds: new Set(), titleEdited: false };
  $('draft-status').textContent = session.needs_final ? '글을 마무리하는 중…' : '내 문체로 글 쓰는 중…';
  try {
    await flushSave();
    const { state } = await api(`/api/sessions/${sid}/draft`, { method: 'POST' });
    if (!session || session.session_id !== sid) return;
    const changes = drafting;
    article = mergeServerArticle(state.article, article, changes);
    session = { ...session, article: state.article, draft_stale: state.draft_stale, needs_final: state.needs_final, metrics: state.metrics, metrics_stale: state.metrics_stale };
    renderArticle(); renderMetrics();
    if (changes.editedIds.size || changes.deletedIds.size || changes.titleEdited) scheduleSave();
  } catch (e) { toast(e.message); }
  finally { drafting = null; $('draft-status').textContent = ''; }
}

function autosize(textarea) { textarea.style.height = 'auto'; textarea.style.height = `${textarea.scrollHeight}px`; }
function renderArticle() {
  $('article-title').value = article.title; autosize($('article-title'));
  $('article-title').classList.toggle('locked', article.title_locked);
  const list = $('blocks'); list.replaceChildren();
  article.blocks.forEach(block => list.append(blockElement(block)));
  $('blocks-empty').hidden = article.blocks.length > 0;
  list.querySelectorAll('textarea').forEach(autosize);
  updateCount();
}
function blockElement(block) {
  const wrap = element('div', `block${block.locked ? ' locked' : ''}`); wrap.dataset.id = block.id;
  const area = element('textarea', 'block-text'); area.value = block.text; area.rows = 1; area.spellcheck = false;
  area.setAttribute('aria-label', block.locked ? '내가 쓴 문단' : 'AI가 쓴 문단');
  const badge = element('span', 'lock-badge', '🔒 내가 쓴 문단'); badge.hidden = !block.locked;
  area.addEventListener('input', () => {
    block.text = area.value; autosize(area);
    if (!block.locked) { block.locked = true; wrap.classList.add('locked'); badge.hidden = false; area.setAttribute('aria-label', '내가 쓴 문단'); }
    drafting?.editedIds.add(block.id); updateCount(); scheduleSave();
  });
  area.addEventListener('blur', () => {
    if (block.text.trim()) return;
    article.blocks = article.blocks.filter(b => b !== block); drafting?.deletedIds.add(block.id);
    wrap.remove(); $('blocks-empty').hidden = article.blocks.length > 0; scheduleSave();
  });
  wrap.append(badge, area); return wrap;
}
$('add-block').addEventListener('click', () => {
  const block = { id: newBlockId(), text: '', locked: true };
  article.blocks.push(block); drafting?.editedIds.add(block.id);
  const node = blockElement(block); $('blocks').append(node); $('blocks-empty').hidden = true; node.querySelector('textarea').focus();
});
$('article-title').addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
$('article-title').addEventListener('input', () => {
  article.title = $('article-title').value.replace(/\n/g, ' '); autosize($('article-title')); article.title_locked = true; $('article-title').classList.add('locked');
  if (drafting) drafting.titleEdited = true; updateCount(); scheduleSave();
});
function updateCount() { $('word-count').textContent = `${charCount(article)}자`; }

function scheduleSave() { clearTimeout(saveTimer); $('editor-state').lastChild.textContent = ' 저장 중…'; saveTimer = setTimeout(saveArticle, 600); }
async function flushSave() { if (saveTimer) { clearTimeout(saveTimer); await saveArticle(); } }
async function saveArticle() {
  saveTimer = null; if (!session) return;
  try {
    const { state } = await api(`/api/sessions/${session.session_id}/article`, { method: 'PUT', body: articlePayload(article) });
    session = { ...session, article: state.article, metrics_stale: state.metrics_stale };
    $('editor-state').lastChild.textContent = ' 저장됨'; renderMetrics();
  } catch (e) { $('editor-state').lastChild.textContent = ' 저장 실패'; toast(e.message); }
}

// ================================================================ 지표
async function updateMetrics() {
  if (!session) return;
  $('metric-coverage').textContent = '지표 계산 중…'; $('metric-distinct').textContent = ''; $('metrics').hidden = false;
  try {
    await flushSave();
    const { state } = await api(`/api/sessions/${session.session_id}/metrics`, { method: 'POST' });
    session = { ...session, metrics: state.metrics, metrics_stale: state.metrics_stale };
  } catch (e) { toast(e.message); }
  renderMetrics();
}
function renderMetrics() {
  const m = session?.metrics;
  $('metrics').hidden = !m;
  if (!m) return;
  $('metric-coverage').textContent = `들어가야 할 내용 ${m.coverage ?? '–'}%`;
  $('metric-coverage').title = m.missing.length ? `아직 빠진 내용: ${m.missing.join(', ')}` : '다른 글들이 공통으로 다룬 내용을 모두 담았어요.';
  $('metric-distinct').textContent = `차별성 있는 내용 ${m.distinctiveness}%`;
  $('metric-distinct').title = '글의 문장 중, 다른 블로그 글에는 없는 나만의 경험·감정 문장의 비율이에요.';
  $('metrics-refresh').hidden = !session.metrics_stale;
}
$('metrics-refresh').addEventListener('click', updateMetrics);

// ================================================================ 메모
async function loadMemos() { try { memos = (await api('/api/memos')).memos; renderMemos(); } catch (e) { toast(e.message); } }
function renderMemos() {
  $('memo-list').replaceChildren();
  if (!memos.length) { $('memo-list').append(element('p', 'empty-state', '아직 적어둔 경험이 없어요.')); return; }
  for (const memo of memos) {
    const item = element('div', 'memo-item'); item.append(element('p', '', memo.text));
    const actions = element('div', 'memo-actions');
    const write = element('button', 'memo-write', '바로 작성하러 가기 ↗'); write.type = 'button';
    write.addEventListener('click', async () => { closePanels(); await flushSave(); startSession(memo.id); });
    const remove = element('button', 'memo-remove', '삭제'); remove.type = 'button';
    remove.addEventListener('click', async () => { try { await api(`/api/memos/${memo.id}`, { method: 'DELETE' }); memos = memos.filter(m => m.id !== memo.id); renderMemos(); } catch (e) { toast(e.message); } });
    actions.append(remove, write); item.append(actions); $('memo-list').append(item);
  }
}
$('memo-form').addEventListener('submit', async event => {
  event.preventDefault(); const text = $('memo-input').value.trim(); if (!text) return;
  try { const { memo } = await api('/api/memos', { method: 'POST', body: { text } }); memos.unshift(memo); $('memo-input').value = ''; renderMemos(); $('memo-input').focus(); }
  catch (e) { toast(e.message); }
});

// ================================================================ 패널
function setOverlay() { $('overlay').hidden = $('memo-drawer').hidden && $('draft-dialog').hidden; }
function closePanels() { $('memo-drawer').hidden = true; $('draft-dialog').hidden = true; setOverlay(); }
$('overlay').addEventListener('click', closePanels); $('memo-close').addEventListener('click', closePanels); $('dialog-close').addEventListener('click', closePanels);
document.addEventListener('keydown', event => { if (event.key === 'Escape') closePanels(); });
$('memo-open').addEventListener('click', () => { $('draft-dialog').hidden = true; $('memo-drawer').hidden = false; setOverlay(); $('memo-input').focus(); });
$('drafts-open').addEventListener('click', async () => {
  $('memo-drawer').hidden = true; $('draft-dialog').hidden = false; setOverlay();
  try {
    const { sessions } = await api('/api/sessions');
    $('draft-count').textContent = sessions.length; $('draft-list').replaceChildren();
    if (!sessions.length) $('draft-list').append(element('p', 'empty-state', '저장된 글이 아직 없어요.'));
    for (const s of sessions) {
      const row = element('div', 'draft-item'); const details = element('div');
      details.append(element('strong', '', s.title.trim() || '제목 없는 글'), element('small', '', new Date(s.updated_at).toLocaleString('ko-KR')));
      const load = element('button', 'draft-load', s.id === session?.session_id ? '지금 쓰는 글' : '불러오기'); load.type = 'button'; load.disabled = s.id === session?.session_id;
      load.addEventListener('click', async () => { await flushSave(); closePanels(); try { await openSession(s.id); } catch (e) { toast(e.message); } });
      row.append(details, load); $('draft-list').append(row);
    }
  } catch (e) { toast(e.message); }
});

// ================================================================ 화면 분할 바
const workspace = $('workspace'); const splitter = $('splitter');
function setSplit(percent) { const width = Math.min(70, Math.max(30, percent)); workspace.style.setProperty('--left-width', `${width}%`); splitter.setAttribute('aria-valuenow', Math.round(width)); }
let dragging = false;
splitter.addEventListener('pointerdown', event => { dragging = true; splitter.setPointerCapture(event.pointerId); document.body.classList.add('dragging'); });
splitter.addEventListener('pointermove', event => { if (!dragging) return; const rect = workspace.getBoundingClientRect(); setSplit((event.clientX - rect.left) / rect.width * 100); });
function endDrag() { dragging = false; document.body.classList.remove('dragging'); }
splitter.addEventListener('pointerup', endDrag); splitter.addEventListener('pointercancel', endDrag);
splitter.addEventListener('keydown', event => { const value = Number(splitter.getAttribute('aria-valuenow')); if (event.key === 'ArrowLeft') { setSplit(value - 5); event.preventDefault(); } if (event.key === 'ArrowRight') { setSplit(value + 5); event.preventDefault(); } });
window.addEventListener('resize', () => document.querySelectorAll('.block-text, .article-title').forEach(autosize));

// ================================================================ 시작
setAuthMode('login');
if (token) enterWorkspace().catch(() => showAuth());
