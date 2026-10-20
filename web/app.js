'use strict';
const $ = (id) => document.getElementById(id);
const state = {token: '', criteria: null, catalog: [], savedSources: [], report: null, filter: 'error', busy: false, timer: null, ready: false, aiReady: false, formReportId: null, desiredSet: null,
  catalogLoading: false, catalogReady: false, catalogRequest: 0, polling: false, connectionLost: false, initializing: true};
const labels = {error: '오류', warning: '검토 필요', needs_source: '자료 부족'};
const categories = {'2': '학습 목표', '3': '학년·어휘', '4': '지문 원문', '6': '문항·정답'};
const stageLabels = {pending: '대기', running: '진행 중', complete: '완료', failed: '실패', skipped: '실행 안 함', cancelled: '중단'};
const statusLabels = {running: '진행 중', cancelling: '중단 중', complete: '검토 완료', partial: '일부 미완료', failed: '실행 실패', cancelled: '중단됨', interrupted: '이전 실행 중단'};
const PAGE_SIZE = 60;
Object.assign(state, {visibleLimit: PAGE_SIZE, issueSignature: null, issueIndex: [], resultSignature: null, coverageSignature: null, searchTimer: null});

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function error(message) {
  $('error-banner').textContent = message || '';
  $('error-banner').hidden = !message;
}
async function api(path, data, signal) {
  const options = data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-Token': state.token}, body: JSON.stringify(data)};
  options.signal = signal;
  const res = await fetch(path, options);
  if (res.status === 401) {
    clearTimeout(state.timer);
    location.assign('/login');
    throw new Error('다시 로그인하세요.');
  }
  let json;
  try { json = await res.json(); } catch { throw new Error('프로그램 서버의 응답을 읽지 못했습니다. 실행 터미널을 확인하세요.'); }
  if (!res.ok) throw new Error(json.error || '요청을 처리하지 못했습니다.');
  return json;
}
function selectedGrades() {
  return [...document.querySelectorAll('input[name="grade"]:checked')].map(el => Number(el.value));
}
function sourceNote() {
  const saved = state.savedSources.find(s => s.id === $('source-select').value);
  $('source-note').classList.toggle('snapshot', Boolean(saved));
  $('source-note').textContent = saved ? `저장 데이터 · ${new Date(saved.fetched_at).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul'})}. 현재 시트와 다를 수 있습니다. 링크를 넣고 ‘시트 연결’을 누르면 최신 시트로 전환합니다.` : '링크로 볼 수 있는 시트를 매번 새로 읽습니다.';
}
function invalidateCatalog() {
  state.catalogRequest++;
  state.catalogController?.abort();
  state.catalog = [];
  state.catalogReady = false;
  state.catalogLoading = false;
  state.sourceDirty = true;
  state.desiredSet = null;
  $('set-select').value = '';
  populateSets();
  $('catalog-info').textContent = '링크 입력 후 시트 연결을 눌러 주세요.';
  $('refresh').disabled = state.busy;
  setBusy(state.busy);
}
async function connectSource() {
  if (state.busy) return;
  const url = $('source-url').value.trim();
  if (!/^https:\/\/docs\.google\.com\/spreadsheets\/d\/[A-Za-z0-9_-]{20,100}(?:\/[^\s]*)?$/.test(url)) {
    invalidateCatalog();
    return error('Google Sheets의 전체 링크를 입력해 주세요.');
  }
  $('source-url').value = url;
  $('source-select').value = '';
  state.desiredSet = null;
  $('set-select').value = '';
  sourceNote();
  error('');
  await refreshCatalog();
}
function populateSets() {
  const prev = state.desiredSet || $('set-select').value;
  const grades = selectedGrades();
  const items = state.catalog.filter(s => grades.includes(s.grade) && ($('include-sample').checked || !s.sample));
  $('set-select').replaceChildren(new Option('선택 학년의 전체 세트', ''));
  for (const item of items) $('set-select').add(new Option(`${item.grade}학년 · ${item.id} · ${item.title}`, item.id));
  if (items.some(s => s.id === prev)) { $('set-select').value = prev; state.desiredSet = null; }
  const absent = grades.filter(g => !items.some(s => s.grade === g));
  $('catalog-info').textContent = `${items.length}개 세트 선택 가능${absent.length ? ` · ${absent.join('·')}학년 데이터 없음` : ''}`;
}
async function refreshCatalog() {
  const request = ++state.catalogRequest;
  state.sourceDirty = false;
  state.desiredSet ||= $('set-select').value || null;
  state.catalogController?.abort();
  state.catalogController = new AbortController();
  state.catalogLoading = true;
  state.catalogReady = false;
  state.catalog = [];
  populateSets();
  setBusy(state.busy);
  $('refresh').disabled = true;
  $('catalog-info').textContent = '최신 목록을 불러오는 중입니다.';
  try {
    const source = $('source-select').value;
    const params = source ? `saved_source=${encodeURIComponent(source)}` : `sheet_url=${encodeURIComponent($('source-url').value)}`;
    const data = await api(`/api/catalog?${params}`, undefined, state.catalogController.signal);
    if (request !== state.catalogRequest) return;
    state.catalog = data.sets;
    state.catalogReady = true;
    if (source && data.source_url) $('source-url').value = data.source_url;
    populateSets();
    $('catalog-info').title = `${data.source_mode === 'snapshot' ? '저장 시각' : '목록 확인'}: ${data.fetched_at}`;
  } catch (e) {
    if (request !== state.catalogRequest || e.name === 'AbortError') return;
    $('catalog-info').textContent = '목록 읽기 실패 · 새로고침으로 다시 시도';
    error(e.message);
  } finally {
    if (request === state.catalogRequest) {
      state.catalogLoading = false;
      setBusy(state.busy);
      $('refresh').disabled = state.busy;
    }
  }
}
function setBusy(busy) {
  state.busy = busy;
  $('settings').disabled = busy;
  $('start').disabled = busy || !state.ready || !state.catalogReady || state.catalogLoading || state.connectionLost;
  $('start').firstElementChild.textContent = busy ? '검토 진행 중' : '오류 검토 시작';
  $('cancel').hidden = !busy;
  $('cancel').disabled = false;
}
function safeLink(link, url) {
  // URLs are built by the server. Keep a second boundary against stored report injection.
  link.removeAttribute('href');
  if (/^https:\/\/docs\.google\.com\/spreadsheets\/d\/[\w-]+\/edit(?:#.*)?$/.test(url)) {
    link.href = url; link.target = '_blank'; link.rel = 'noreferrer';
  }
}
function issueCard(issue) {
  const card = node('article', 'issue');
  card.dataset.issueKey = [issue.set_id,issue.sheet,issue.row,issue.kind,issue.title].join('|');
  const top = node('div', 'issue-top');
  top.append(node('span', `badge ${issue.severity}`, labels[issue.severity] || '확인 필요'), node('span', 'badge', categories[issue.category] || '연결 확인'), node('span', 'badge', issue.kind === 'ai' ? 'AI 검토' : '형식 검사'));
  if (issue.check) top.append(node('span', 'badge', issue.check));
  top.append(node('span', 'issue-meta', issue.set_id));
  card.append(top, node('h3', '', issue.title), node('p', '', issue.reason));
  if (issue.related_findings?.length) {
    const related = node('details');
    related.append(node('summary', '', `같은 원인 세부 항목 ${issue.related_findings.length}건 보기`));
    related.addEventListener('toggle', () => {
      if (!related.open || related.dataset.loaded) return;
      related.dataset.loaded = 'true';
      for (const item of issue.related_findings) {
        const line = node('p');
        const link = node('a', 'location', `${item.sheet} ${item.row}행 · ${item.title}`);
        safeLink(link, item.url);
        line.append(link, node('span', '', ` · ${item.reason}`));
        related.append(line);
      }
    });
    card.append(related);
  }
  if (issue.assessment_note) card.append(node('p', 'suggestion', `판정 유의  ·  ${issue.assessment_note}`));
  if (issue.quote) card.append(node('blockquote', '', issue.quote));
  card.append(node('p', 'suggestion', `수정 제안  ·  ${issue.suggestion}`));
  if (issue.evidence) {
    const details = node('details'); details.append(node('summary', '', '판단 근거 보기'), node('p', '', issue.evidence)); card.append(details);
  }
  const location = node('a', 'location', `${issue.sheet} · ${issue.row}행 · ${issue.field}  ↗`);
  safeLink(location, issue.url); card.append(location);
  return card;
}
function renderResults() {
  const report = state.report;
  if (!report) return;
  const query = $('search').value.trim().toLocaleLowerCase();
  if (state.indexedIssues !== report.issues) {
    state.indexedIssues = report.issues;
    state.issueSignature = JSON.stringify([report.id, report.issues]);
    const order = {error: 0, warning: 1, needs_source: 2};
    state.issueIndex = [...report.issues].sort((a, b) => order[a.severity] - order[b.severity] || a.set_id.localeCompare(b.set_id) || a.row - b.row)
      .map(issue => ({issue, searchText: [issue.set_id, issue.title, issue.reason, issue.quote, issue.suggestion, issue.check, issue.evidence, categories[issue.category], JSON.stringify(issue.related_findings || [])].join(' ').toLocaleLowerCase()}));
  }
  const signature = JSON.stringify([state.issueSignature, state.filter, query, state.visibleLimit, report.status]);
  if (state.resultSignature === signature) return;
  state.resultSignature = signature;
  const issues = state.issueIndex.filter(({issue, searchText}) => (state.filter === 'all' || issue.severity === state.filter) && (!query || searchText.includes(query))).map(({issue}) => issue);
  const fragment = document.createDocumentFragment();
  const opened = new Set([...$('results').querySelectorAll('.issue details[open]')].map(d => d.closest('.issue').dataset.issueKey));
  for (const issue of issues.slice(0, state.visibleLimit)) {
    const card = issueCard(issue);
    if (opened.has(card.dataset.issueKey) && card.querySelector('details')) card.querySelector('details').open = true;
    fragment.append(card);
  }
  if (issues.length > state.visibleLimit) {
    const more = node('button', 'more-results', `결과 더 보기 · ${state.visibleLimit}/${issues.length}건 표시`);
    more.type = 'button';
    more.addEventListener('click', () => { state.visibleLimit += PAGE_SIZE; renderResults(); });
    fragment.append(more);
  }
  if (!issues.length) {
    let text = '현재 필터에 해당하는 항목이 없습니다.';
    if (state.filter === 'all' && !query) {
      text = ['running', 'cancelling'].includes(report.status) ? '검토 중입니다. 결과가 준비되면 여기에 표시됩니다.' : report.status === 'complete' ? '이번 검토에서 발견된 항목이 없습니다.' : '발견된 항목이 없습니다. 미완료 범위는 아래 세트별 상태에서 확인하세요.';
    }
    fragment.append(node('div', 'no-results', text));
  }
  $('results').replaceChildren(fragment);
}
function renderCoverage(report) {
  $('coverage-panel').hidden = false;
  const signature = JSON.stringify([report.id, report.sets, report.empty_grades, report.skipped]);
  if (state.coverageSignature === signature) return;
  state.coverageSignature = signature;
  const table = node('table', 'coverage-table');
  const head = node('tr');
  for (const title of ['세트', '판정', '난이도', '완료 상태']) head.append(node('th', '', title));
  table.append(head);
  for (const item of report.sets) {
    const row = node('tr');
    const summary = item.review_summary;
    const passes = item.ai_passes;
    const passText = passes ? ` · 정순 ${stageLabels[passes.forward?.status] || '대기'} · 역순 ${stageLabels[passes.reverse?.status] || '대기'}` : '';
    row.append(node('td', '', item.id), node('td', '', summary?.verdict || '새 기준 미검토'), node('td', '', summary?.difficulty || '미판정'), node('td', '', `형식 ${stageLabels[item.rule_status] || '대기'} · AI ${stageLabels[item.ai_status] || '대기'}${passText}`));
    table.append(row);
    if (summary) {
      const tr = node('tr'), td = node('td', 'set-summary');
      td.colSpan = 4;
      td.append(node('p', '', `${summary.vocabulary_text}${summary.partial ? ' · 일부 자료 기준' : ''}`));
      td.append(node('p', '', Object.entries(summary.sections).map(([tab, verdict]) => `${tab}: ${verdict}`).join(' / ')));
      td.append(node('p', '', Object.entries(summary.checks).map(([check, value]) => `${check} ${value}`).join(' · ')));
      tr.append(td); table.append(tr);
    }
    if (item.ai_error || item.ai_summary) {
      const tr = node('tr'), td = node('td', '', item.ai_error || item.ai_summary);
      td.colSpan = 4; tr.append(td); table.append(tr);
    }
  }
  const missing = report.empty_grades?.length ? `${report.empty_grades.join('·')}학년은 데이터가 없어 건너뛰었습니다. ` : '';
  const note = node('p', 'coverage-note', `${missing}형식 검사만 실행한 항목은 의미 검토 전이므로 확인 필요로 표시합니다. 오류 건수는 발견 항목 수이며 같은 셀이 중복 집계될 수 있습니다. ${report.skipped?.length || 0}개 세트가 선택 조건에 따라 제외되었습니다.`);
  const parts = [table, note];
  if (report.skipped?.length) {
    const details = node('details'); details.append(node('summary', '', '제외된 세트 보기'));
    for (const s of report.skipped) details.append(node('p', 'coverage-note', `${s.id} · ${s.reason}`));
    parts.push(details);
  }
  $('coverage-content').replaceChildren(...parts);
}
function renderReport(report) {
  if (state.formReportId !== report.id) {
    const oldSource = `${$('source-select').value}|${$('source-url').value}`;
    state.formReportId = report.id;
    state.visibleLimit = PAGE_SIZE;
    const options = report.options;
    document.querySelectorAll('input[name="grade"]').forEach(el => { el.checked = options.grades.includes(Number(el.value)); });
    $('source-select').value = options.saved_source || '';
    $('source-url').value = options.saved_source ? report.source_url : (options.sheet_url || report.source_url);
    $('include-sample').checked = Boolean(options.include_sample);
    const canRestoreAI = state.aiReady || ['running', 'cancelling'].includes(report.status);
    document.querySelector(`input[name="mode"][value="${options.ai && canRestoreAI ? 'ai' : 'rules'}"]`).checked = true;
    state.desiredSet = options.set_id || null;
    sourceNote(); populateSets();
    if (!state.initializing && oldSource !== `${$('source-select').value}|${$('source-url').value}`) refreshCatalog();
  }
  state.report = report;
  const oldCriteria = !state.criteria || report.criteria_sha256 !== state.criteria.sha256;
  const oldEngine = state.engineVersion && report.engine_version !== state.engineVersion;
  $('criteria-notice').hidden = !oldCriteria && !oldEngine;
  $('criteria-notice').textContent = oldCriteria ? `이전 기준 결과입니다${report.criteria_version ? ` (${report.criteria_version})` : ''}. 추가 기준을 적용하려면 오류 검토 시작을 다시 눌러 주세요.` : oldEngine ? `이전 프로그램 버전의 보존된 결과입니다 (${report.engine_version || '버전 미기록'}). 현재 버전은 ${state.engineVersion}이며, 이 결과를 새 검수 결과로 바꾸지 않았습니다.` : '';
  const busy = ['running', 'cancelling'].includes(report.status);
  setBusy(busy);
  $('progress-panel').hidden = false;
  $('progress-label').textContent = report.message;
  $('progress-count').textContent = report.total_sets ? `${report.completed_sets} / ${report.total_sets} 세트` : '';
  $('progress-fill').style.width = `${report.total_sets ? Math.max(3, report.completed_sets / report.total_sets * 100) : 3}%`;
  const aiDone = report.sets.filter(s => s.ai_status === 'complete').length;
  const ruleDone = report.sets.filter(s => s.rule_status === 'complete').length;
  $('progress-note').textContent = report.options.ai ? `형식 검사 ${ruleDone}/${report.total_sets} · AI 검토 ${aiDone}/${report.total_sets} · 세트별 완료 상태는 아래에서 확인할 수 있습니다.` : '빠른 형식 검사 결과입니다. 의미·학년 적합성에 대한 AI 검토는 실행하지 않았습니다.';
  if (report.sets.some(s => s.ai_passes)) {
    const forward = report.sets.filter(s => s.ai_passes?.forward?.status === 'complete').length;
    const reverse = report.sets.filter(s => s.ai_passes?.reverse?.status === 'complete').length;
    $('progress-note').textContent += ` 정순 ${forward}/${report.total_sets} · 역순 ${reverse}/${report.total_sets}. 오류를 먼저 표시하며 확인 제안은 별도 필터에서 볼 수 있습니다.`;
  }
  if (report.ai_source_run_id) $('progress-note').textContent += ' 전체 AI 검수를 마친 같은 입력에 추가 형식·심각도 검사를 적용했습니다. 이 추가 단계에서는 AI를 재호출하지 않았습니다.';
  if (report.postprocess_note) $('progress-note').textContent += ` ${report.postprocess_note}`;
  $('count-sets').textContent = report.total_sets || 0;
  $('count-complete').textContent = `${ruleDone}개 형식 검사 · ${aiDone}개 AI 완료`;
  $('count-errors').textContent = report.issues.filter(i => i.severity === 'error').length;
  $('count-warnings').textContent = report.issues.filter(i => i.severity === 'warning').length;
  $('count-sources').textContent = report.issues.filter(i => i.severity === 'needs_source').length;
  $('issue-count').textContent = report.issues.length;
  const date = new Date(report.started_at).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  $('run-info').textContent = `${date} (한국 시간) · ${statusLabels[report.status] || report.status} · ${oldCriteria ? '이전 기준' : `기준 ${report.criteria_version}`}${report.fetched_at ? (report.source_mode === 'snapshot' ? ` · 저장 데이터 ${new Date(report.fetched_at).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul'})}` : ' · 실행 시점 최신 시트 기준') : ''}`;
  safeLink($('sheet-link'), report.source_url);
  for (const [id, ext] of [['export-md', 'md'], ['export-json', 'json']]) {
    const link = $(id);
    const canDownload = report.total_sets > 0 && (ruleDone > 0 || !busy);
    link.classList.toggle('disabled', !canDownload);
    link.setAttribute('aria-disabled', String(!canDownload));
    if (canDownload) link.href = `/api/report/${report.id}.${ext}`; else link.removeAttribute('href');
  }
  if (report.error) error(report.error);
  renderResults(); renderCoverage(report);
}
function schedulePoll(delay = 1000) {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, delay);
}
async function poll() {
  if (state.polling) return;
  state.polling = true;
  clearTimeout(state.timer);
  try {
    const report = state.report;
    const cursor = Number.isInteger(report?.revision) ? `?after_id=${encodeURIComponent(report.id)}&after_revision=${report.revision}` : '';
    const data = await api(`/api/status${cursor}`);
    const recovered = state.connectionLost;
    state.connectionLost = false;
    if (recovered) error('');
    if (data.job) renderReport(data.job);
    else if (state.busy && !data.unchanged) {
      // The server restarted; restore its partial report and a new request token.
      await init();
      return;
    }
    setBusy(state.busy);
  } catch (e) {
    error('서버 연결이 끊겼습니다. 실행 터미널을 확인하고 이 화면을 새로고침하세요.');
    state.connectionLost = true;
    setBusy(state.busy);
  } finally {
    state.polling = false;
    schedulePoll(document.hidden ? 15000 : state.busy || state.connectionLost ? 1000 : 5000);
  }
}
$('review-form').addEventListener('submit', async event => {
  event.preventDefault(); error('');
  if (state.busy || !state.catalogReady || state.connectionLost) return;
  const grades = selectedGrades();
  if (!grades.length) return error('대상 학년을 하나 이상 선택하세요.');
  const ai = document.querySelector('input[name="mode"]:checked').value === 'ai';
  if (ai && !state.aiReady) return error(state.aiProvider === 'openai'
    ? '서버에 OpenAI API 키를 설정해야 합니다. 빠른 형식 검사는 사용할 수 있습니다.'
    : 'AI 검토를 시작하려면 Codex 로그인이 필요합니다. 빠른 형식 검사는 바로 실행할 수 있습니다.');
  setBusy(true);
  try {
    await api('/api/review', {grades, set_id: $('set-select').value || null, ai, include_sample: $('include-sample').checked, sheet_url: $('source-url').value, saved_source: $('source-select').value || null});
    state.filter = 'error'; $('search').value = '';
    document.querySelectorAll('[data-filter]').forEach(el => el.classList.toggle('active', el.dataset.filter === 'error'));
    $('progress-panel').hidden = false;
    await poll();
  } catch (e) { setBusy(false); error(e.message); await poll(); }
});
$('cancel').addEventListener('click', async () => {
  $('cancel').disabled = true;
  try { await api('/api/cancel', {id: state.report?.id}); await poll(); } catch (e) { error(e.message); await poll(); $('cancel').disabled = false; }
});
$('refresh').addEventListener('click', () => { error(''); state.sourceDirty ? connectSource() : refreshCatalog(); });
$('connect-source').addEventListener('click', connectSource);
$('source-select').addEventListener('change', () => {
  state.desiredSet = null;
  $('set-select').value = '';
  sourceNote();
  error(''); refreshCatalog();
});
$('source-url').addEventListener('input', () => { error(''); invalidateCatalog(); });
$('source-url').addEventListener('keydown', event => {
  if (event.key === 'Enter') { event.preventDefault(); connectSource(); }
});
document.querySelectorAll('input[name="grade"], #include-sample').forEach(el => el.addEventListener('change', populateSets));
document.querySelectorAll('[data-filter]').forEach(el => el.addEventListener('click', () => {
  state.filter = el.dataset.filter;
  state.visibleLimit = PAGE_SIZE;
  document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active', b === el));
  renderResults();
}));
$('search').addEventListener('input', () => {
  clearTimeout(state.searchTimer);
  state.visibleLimit = PAGE_SIZE;
  state.searchTimer = setTimeout(renderResults, 100);
});

async function init() {
  state.initializing = true;
  try {
    const data = await api('/api/bootstrap');
    state.token = data.token; state.aiReady = data.ai.ready; state.ready = true;
    state.aiProvider = data.ai.provider || 'codex';
    state.criteria = data.criteria || null;
    state.engineVersion = data.engine_version;
    $('criteria-version').textContent = data.criteria?.version || '서버 업데이트 필요';
    $('criteria-text').textContent = data.criteria?.text || '프로그램을 다시 실행해 최신 기준을 불러오세요.';
    state.savedSources = data.saved_sources || [];
    $('source-select').replaceChildren(new Option('실시간 Google 시트', ''));
    for (const source of state.savedSources) $('source-select').add(new Option(source.label, source.id));
    $('ai-info').textContent = data.ai.message;
    $('ai-info').classList.toggle('unavailable', !data.ai.ready);
    safeLink($('sheet-link'), data.source_url);
    if (!data.ai.ready) document.querySelector('input[name="mode"][value="rules"]').checked = true;
    setBusy(false);
    if (data.last_report) renderReport(data.last_report);
    const dataNow = await api('/api/status');
    if (dataNow.job) renderReport(dataNow.job);
    await refreshCatalog();
  } catch (e) { error(e.message); }
  finally { state.initializing = false; schedulePoll(state.busy ? 1000 : 5000); }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) schedulePoll(0); });
$('logout')?.addEventListener('click', async () => {
  try { await api('/api/logout', {}); location.replace('/login'); }
  catch (e) { error(e.message); }
});
init();
