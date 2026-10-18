/**
 * Completed-session browser: the hub list plus the full-detail panel.
 *
 * The detail panel reuses the session-timeline renderer for the sensor
 * lanes and answer markers; this module owns the data fetching, the answer
 * and file lists, and the marker popover.
 */
import { getJson, postJson } from '../shared/api-client.js';
import { t } from '../shared/i18n.js';
import { byId, escapeHtml, formatDateTime, formatFileSize, setHidden, setText } from '../shared/dom-utils.js';
import { copyText } from '../shared/clipboard.js';
import { createModal } from '../shared/modal.js';
import { methodsText, softwareNote, softwareRows } from '../shared/software-provenance-view-model.js';
import { bindTimelineMarkers, renderSessionTimeline, updateStreamPoints } from './session-timeline.js';
import { renderSessionProgressRail } from './session-progress-rail.js';
import { buildMarkers } from '../shared/timeline-view-model.js';
import {
  confirmDegradedFinalization,
  continueFinalization,
  openFinalizationFolder,
  republishFinalizationStep,
  retryFinalizationStep,
} from './finalization-actions.js';

const MAX_HUB_ITEMS = 25;
const HUB_REFRESH_INTERVAL_MS = 10000;

let callbacks = {};
let initialized = false;
let currentMarkers = [];
let currentSession = null;
let refreshTimer = null;
let finalizationTimer = null;
const FINALIZATION_REFRESH_MS = 3000;

export function initializeSessionsBrowser(options = {}) {
  callbacks = options;
  if (initialized) return;
  initialized = true;

  byId('btn-session-withdraw')?.addEventListener('click', () => {
    if (currentSession) void openWithdrawalModal(currentSession);
  });
  byId('btn-session-copy-methods')?.addEventListener('click', async () => {
    const text = methodsText(currentSession?.software, t);
    if (!text) return;
    await copyText(text);
    callbacks.showToast?.(t('sessions.software.copied', 'Methods text copied: {text}').replace('{text}', text), 'success');
  });
  refreshTimer = window.setInterval(() => {
    if (!document.hidden && byId('view-hub')?.classList.contains('active')) {
      void loadCompletedSessions();
    }
  }, HUB_REFRESH_INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      void loadCompletedSessions();
    }
  });
}

export async function loadCompletedSessions() {
  const listEl = byId('hub-sessions-list');
  if (!listEl) return;
  try {
    const sessions = await getJson('/api/admin/sessions');
    renderHubList(listEl, Array.isArray(sessions) ? sessions : []);
  } catch (error) {
    console.error('[sessions] Could not load completed sessions:', error);
    listEl.innerHTML = `
      <div class="hub-recent-item empty">
        <i class="iconoir-warning-triangle"></i>
        <div>${escapeHtml(t('sessions.loadFailed', 'Could not load completed studies'))}</div>
      </div>`;
  }
}

function renderHubList(listEl, sessions) {
  if (!sessions.length) {
    listEl.innerHTML = `
      <div class="hub-recent-item empty">
        <i class="iconoir-clock"></i>
        <div>${escapeHtml(t('sessions.empty', 'No completed studies yet.'))}</div>
      </div>`;
    return;
  }

  const shown = sessions.slice(0, MAX_HUB_ITEMS);
  listEl.innerHTML = shown.map((session) => `
    <div class="hub-recent-item is-clickable" data-study-id="${escapeHtml(session.study_id)}" data-participant-id="${escapeHtml(session.participant_id)}" data-session-id="${escapeHtml(session.session_id)}" data-session-folder="${escapeHtml(session.session_folder)}">
      <i class="iconoir-check-circle"></i>
      <div class="hub-recent-item-body">
        <div class="hub-recent-item-title">${escapeHtml(session.study_id)} <span class="hub-recent-item-sub">/ ${escapeHtml(session.participant_id)}</span></div>
        <div class="hub-recent-item-meta">${escapeHtml(formatDateTime(session.saved_at))} &middot; ${escapeHtml(String(session.answers_count))} ${escapeHtml(t('sessions.answersLabel', 'Answers'))}${sessionTags(session).map((tag) => ` &middot; ${escapeHtml(tag)}`).join('')}</div>
      </div>
    </div>
  `).join('') + (sessions.length > MAX_HUB_ITEMS
    ? `<div class="hub-recent-item empty">${escapeHtml(t('sessions.moreHint', '{count} more not shown').replace('{count}', String(sessions.length - MAX_HUB_ITEMS)))}</div>`
    : '');

  listEl.querySelectorAll('.hub-recent-item[data-study-id]').forEach((item) => {
    item.addEventListener('click', () => void openSessionDetail(
      item.dataset.studyId,
      item.dataset.participantId,
      item.dataset.sessionId,
      item.dataset.sessionFolder,
    ));
  });
}

export async function openSessionDetail(studyId, participantId, sessionId, sessionFolder) {
  callbacks.switchView?.('view-session-detail');
  setText('session-detail-title', t('sessions.detailLoading', 'Loading ...'));
  setText('session-detail-subtitle', '');
  hidePopover();

  try {
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    if (sessionFolder) params.set('session_folder', sessionFolder);
    const query = params.size ? `?${params.toString()}` : '';
    const session = await getJson(`/api/admin/sessions/${encodeURIComponent(studyId)}/${encodeURIComponent(participantId)}${query}`);
    currentSession = session;
    renderSessionSummary(session);
    void renderProgressRail(session);
    renderAnswerList(session);
    renderFileList(session);
    await renderTimeline(session);
  } catch (error) {
    console.error('[sessions] Could not load session detail:', error);
    setText('session-detail-title', t('sessions.loadFailed', 'Could not load completed studies'));
    callbacks.showToast?.(t('sessions.loadFailed', 'Could not load completed studies'), 'error');
  }
}

function renderSessionSummary(session) {
  setText('session-detail-title', `${session.study_id} / ${session.participant_id}`);
  setText('session-detail-subtitle', formatDateTime(session.saved_at));

  setText('session-answers-value', String(session.answers_count ?? 0));
  setText('session-answers-hint', sessionTags(session).join(' · '));

  const result = session.result || {};
  const durationSeconds = _durationSeconds(result.timestamp_start, result.timestamp_end);
  setText('session-duration-value', durationSeconds != null ? formatDuration(durationSeconds) : '-');
  setText('session-duration-hint', '');

  const streams = Array.isArray(session.streams) ? session.streams : (Array.isArray(session.sidecars) ? session.sidecars : []);
  setText('session-signals-value', streams.length ? String(streams.length) : '-');
  setText(
    'session-signals-hint',
    streams.length
      ? streams.map((stream) => stream.stream_name || stream.sensor).join(', ')
      : t('sessions.noSignals', 'No sensor recordings for this session.'),
  );

  const files = Array.isArray(session.files) ? session.files : [];
  setText('session-files-value', String(files.length));
  setText('session-files-hint', '');

  renderLifecycleBadge(session);
  renderQualitySummary(session);
  renderSoftware(session);
}

// Study Runner and plugin versions, for the methods section.
function renderSoftware(session) {
  const list = byId('session-software-list');
  if (list) {
    list.innerHTML = softwareRows(session.software, t)
      .map((row) => `<dt>${escapeHtml(row.label)}</dt><dd>${escapeHtml(row.value)}</dd>`)
      .join('');
  }
  const note = softwareNote(session.software, t);
  setText('session-software-note', note);
  setHidden('session-software-note', !note);
  const button = byId('btn-session-copy-methods');
  if (button) {
    const available = Boolean(methodsText(session.software, t));
    button.disabled = !available;
    button.title = available ? '' : t('sessions.software.noVersion', 'The Study Runner version was not recorded for this session.');
  }
}

// Package A1/A2: session.lifecycle (5a) and session.quality_summary
// (session_quality_summary.py, reading 5c/5h's journals) existed with no
// reader in the UI at all. This is that reader - deliberately three things
// (a badge, a health word, a short finding list), never a chart or raw
// event dump. Detail lives in quality.jsonl itself; see
// docs/how-recording-quality-works.md.
const LIFECYCLE_LABEL_KEYS = {
  IDLE: 'sessions.lifecycle.idle',
  PREFLIGHT: 'sessions.lifecycle.preflight',
  RECORDING: 'sessions.lifecycle.recording',
  FINALIZING: 'sessions.lifecycle.finalizing',
  SEALED: 'sessions.lifecycle.sealed',
  WITHDRAWN: 'sessions.lifecycle.withdrawn',
  FAILED: 'sessions.lifecycle.failed',
};

function renderLifecycleBadge(session) {
  const badge = byId('session-lifecycle-badge');
  if (!badge) return;
  const lifecycle = String(session.lifecycle || '');
  let labelKey = LIFECYCLE_LABEL_KEYS[lifecycle];
  // WITHDRAWN covers two very different events -- a participant's consent
  // withdrawal (destructive, everything deleted) and an admin's abort of a
  // stuck/misbehaving session (nothing deleted). Same lifecycle state, same
  // tombstone file, so the distinction lives only in withdrawal_kind
  // (sessions_index_service.py) -- never conflate the two labels here.
  let modifier = lifecycle.toLowerCase();
  if (lifecycle === 'WITHDRAWN' && session.withdrawal_kind === 'admin_abort') {
    labelKey = 'sessions.lifecycle.aborted';
    modifier = 'aborted';
  }
  // SEALED is the ordinary, expected outcome for a completed session; a
  // badge that fires on every session is one nobody reads. Only the
  // exceptional states earn a badge here.
  if (!labelKey || lifecycle === 'SEALED') {
    setHidden('session-lifecycle-badge', true);
    return;
  }
  badge.className = `status-pill status-pill--${modifier}`;
  badge.textContent = t(labelKey, lifecycle);
  setHidden('session-lifecycle-badge', false);
}

const HEALTH_LABEL_KEYS = {
  clean: 'sessions.quality.clean',
  warnings: 'sessions.quality.warnings',
  attention: 'sessions.quality.attention',
  unknown: 'sessions.quality.unknown',
};

function renderQualitySummary(session) {
  const card = byId('session-quality-card');
  const summary = session.quality_summary;
  if (!card || !summary) {
    setHidden('session-quality-card', true);
    return;
  }
  setHidden('session-quality-card', false);

  const health = String(summary.recording_health || 'unknown');
  const pill = byId('session-quality-value');
  if (pill) {
    pill.className = `status-pill status-pill--${health}`;
    pill.textContent = t(HEALTH_LABEL_KEYS[health] || HEALTH_LABEL_KEYS.unknown, health);
  }

  const list = byId('session-quality-findings');
  if (list) {
    const findings = Array.isArray(summary.findings) ? summary.findings : [];
    list.innerHTML = findings
      .map((finding) => `<li>${escapeHtml(formatQualityFinding(finding))}</li>`)
      .join('');
  }
}

// One finding -> one sentence. Kept next to the pill/badge rendering above
// rather than in a separate module: this is the only place a finding's
// shape (session_quality_summary.py's "kind" + details) is interpreted.
function formatQualityFinding(finding) {
  const streamKey = String(finding.stream_key || '');
  switch (finding.kind) {
    case 'gap':
      return t('sessions.quality.finding.gap', '{count} gap(s) in {stream}')
        .replace('{count}', String(finding.count))
        .replace('{stream}', streamKey);
    case 'timestamp_regression':
      return t('sessions.quality.finding.timestampRegression', '{count} timestamp regression(s) in {stream}')
        .replace('{count}', String(finding.count))
        .replace('{stream}', streamKey);
    case 'clock_jump':
      return t('sessions.quality.finding.clockJump', 'System clock jumped by up to {seconds}s ({count}x)')
        .replace('{seconds}', String(finding.max_drift_seconds))
        .replace('{count}', String(finding.count));
    case 'ingest_backlog':
      return t('sessions.quality.finding.ingestBacklog', 'Computer briefly fell behind {stream} ({count}x)')
        .replace('{stream}', streamKey)
        .replace('{count}', String(finding.count));
    case 'unconfirmed_tail':
      return t('sessions.quality.finding.unconfirmedTail', 'The last moments of {stream} before a restart were not confirmed as saved')
        .replace('{stream}', streamKey);
    default:
      return `${finding.kind}: ${streamKey}`;
  }
}

// Package A3: withdrawal is irreversible and deletes a participant's
// recorded data, so the UI's own "type the session id" step is a genuine
// safeguard against a misclick - not a substitute for the server-side
// confirm_session_id check (apps/server/routes/sessions.py), which is the
// actual validated boundary. Built with createModal() directly (like
// plugin-console.js/upload-monitor.js) rather than confirmWithModal(),
// which only offers a plain yes/no and cannot gate a button on typed input.
async function openWithdrawalModal(session) {
  const sessionId = String(session.session_id || '');
  const modal = createModal({
    title: t('sessions.withdraw.title', 'Withdraw consent'),
    closeLabel: t('common.cancel', 'Cancel'),
  });

  modal.body.innerHTML = `
    <p class="settings-hint">${escapeHtml(
      t(
        'sessions.withdraw.warning',
        'This permanently deletes every recording, answer, and file for this session. It cannot be undone.',
      ),
    )}</p>
    <p class="settings-hint">${escapeHtml(
      t('sessions.withdraw.typeToConfirm', 'Type the session id "{id}" to confirm.').replace('{id}', sessionId),
    )}</p>
    <div class="field">
      <input type="text" id="withdraw-confirm-input" autocomplete="off" spellcheck="false">
    </div>
    <p class="settings-hint" id="withdraw-error" hidden></p>
    <div class="dashboard-actions confirm-modal-actions">
      <button type="button" class="btn-secondary" data-withdraw-cancel>${escapeHtml(t('common.cancel', 'Cancel'))}</button>
      <button type="button" class="btn-primary btn-primary--danger" data-withdraw-confirm disabled>
        ${escapeHtml(t('sessions.withdraw.confirmButton', 'Delete this session'))}
      </button>
    </div>
  `;

  const input = modal.body.querySelector('#withdraw-confirm-input');
  const confirmButton = modal.body.querySelector('[data-withdraw-confirm]');
  const errorText = modal.body.querySelector('#withdraw-error');

  input.addEventListener('input', () => {
    confirmButton.disabled = input.value !== sessionId;
  });
  modal.body.querySelector('[data-withdraw-cancel]').addEventListener('click', () => modal.destroy());

  confirmButton.addEventListener('click', async () => {
    confirmButton.disabled = true;
    setHidden('withdraw-error', true);
    try {
      const params = new URLSearchParams();
      if (session.session_id) params.set('session_id', session.session_id);
      if (session.session_folder) params.set('session_folder', session.session_folder);
      const result = await postJson(
        `/api/admin/sessions/${encodeURIComponent(session.study_id)}/${encodeURIComponent(session.participant_id)}/withdraw?${params.toString()}`,
        { confirm_session_id: input.value },
      );
      modal.destroy();
      const published = Array.isArray(result.already_published) ? result.already_published : [];
      if (published.length) {
        // Never summarized away: these destinations already have a copy
        // this server cannot delete, and the operator must act on them.
        const names = published.map((entry) => entry.kind || entry.job_id).join(', ');
        callbacks.showToast?.(
          t('sessions.withdraw.donePublished', 'Session withdrawn. Already uploaded to: {destinations} - remove it there manually.')
            .replace('{destinations}', names),
          'warning',
        );
      } else {
        callbacks.showToast?.(t('sessions.withdraw.done', 'Session withdrawn.'), 'success');
      }
      callbacks.switchView?.('view-hub');
      await loadCompletedSessions();
    } catch (error) {
      console.error('[sessions] Withdrawal failed:', error);
      errorText.textContent = error.message || t('sessions.withdraw.failed', 'Withdrawal failed.');
      setHidden('withdraw-error', false);
      confirmButton.disabled = input.value !== sessionId;
    }
  });

  modal.open();
  input.focus();
}

function renderAnswerList(session) {
  const container = byId('session-answer-list');
  if (!container) return;
  const entries = Array.isArray(session.result?.answer_details) ? session.result.answer_details : [];
  if (!entries.length) {
    container.innerHTML = `<p class="settings-hint">${escapeHtml(t('sessions.noAnswers', 'No answers were recorded.'))}</p>`;
    return;
  }

  container.innerHTML = entries.map((entry) => `
    <div class="session-answer-row">
      <div class="session-answer-num">${escapeHtml(String(entry.question_number ?? ''))}</div>
      <div class="session-answer-body">
        <div class="session-answer-prompt">${escapeHtml(entry.question_prompt || entry.question_type || '')}</div>
        <div class="session-answer-value">${entry.skipped
          ? `<em>${escapeHtml(t('sessions.skipped', 'skipped'))}</em>`
          : escapeHtml(formatAnswerValue(entry.answer))}</div>
      </div>
    </div>
  `).join('');
}

function renderFileList(session) {
  const container = byId('session-file-list');
  if (!container) return;
  const files = Array.isArray(session.files) ? session.files : [];
  if (!files.length) {
    container.innerHTML = `<p class="settings-hint">${escapeHtml(t('sessions.noFiles', 'No files were saved.'))}</p>`;
    return;
  }

  const groups = [
    ['raw', t('sessions.files.raw', 'Sensor recordings')],
    ['low_rate', t('sessions.files.lowRate', '1 Hz data')],
    ['other', t('sessions.files.other', 'Results and checks')],
  ];
  const fileGroup = (name) => name.startsWith('raw/plugins/') || name === 'derived/session.xdf' ? 'raw'
    : name.startsWith('raw/backup/') || name === 'session_1hz.csv' ? 'low_rate' : 'other';
  container.innerHTML = groups.map(([key, label]) => {
    const members = files.filter((file) => fileGroup(String(file.name || '')) === key);
    if (!members.length) return '';
    return `<section class="session-file-group"><h3>${escapeHtml(label)}</h3>${members.map((file) => `
      <div class="session-file-row">
        <i class="iconoir-page"></i>
        <span class="session-file-name">${escapeHtml(file.name)}</span>
        <span class="session-file-meta">${escapeHtml(formatFileSize(file.size))} &middot; ${escapeHtml(formatDateTime(file.modified_at))}</span>
      </div>`).join('')}</section>`;
  }).join('');
}

async function renderTimeline(session) {
  const container = byId('session-timeline');
  if (!container) return;
  hidePopover();

  const streams = (Array.isArray(session.streams) ? session.streams : (Array.isArray(session.sidecars) ? session.sidecars : []))
    .filter((stream) => stream.sensor && stream.sample_count > 0);

  const loaded = (await Promise.all(
    streams.map((stream) => fetchSignals(session, stream.sensor)),
  )).filter(Boolean);

  currentMarkers = buildMarkers(session.result?.answer_details);
  currentSession = session;

  renderSessionTimeline(
    container,
    { streams: loaded, markers: currentMarkers, labels: timelineLabels() },
    { onWindowChange: (window) => void refetchWindow(session, loaded, window) },
  );
  bindTimelineMarkers(container, currentMarkers, showPopover);
}

function timelineLabels() {
  return {
    nothingRecorded: t('sessions.timelineEmpty', 'Nothing was recorded for this session.'),
    chartLabel: t('sessions.timeline', 'Timeline'),
    markersLabel: t('sessions.answersTitle', 'Questions and answers'),
    zoomFull: t('sessions.timelineZoomFull', 'Whole session. Scroll to zoom, shift-scroll to pan.'),
    zoomPartial: t('sessions.timelineZoomPartial', 'Showing {shown}s of {total}s. Scroll to zoom.'),
  };
}

/**
 * One stream's samples, optionally clipped to a window.
 *
 * The descriptor travels with the points: the stream's own LSL header is what
 * decides how it is drawn, so it must reach the view model unchanged.
 */
async function fetchSignals(session, sensor, window = null) {
  const base = `/api/admin/sessions/${encodeURIComponent(session.study_id)}/${encodeURIComponent(session.participant_id)}/signals`;
  const params = new URLSearchParams({ sensor, session_folder: session.session_folder });
  if (window) {
    params.set('start', String(window.start));
    params.set('end', String(window.end));
  }
  try {
    return await getJson(`${base}?${params}`);
  } catch (error) {
    console.error(`[sessions] Could not load ${sensor} signals:`, error);
    return null;
  }
}

/** Refill the visible window at full resolution after a zoom settles. */
async function refetchWindow(session, streams, window) {
  await Promise.all(streams.map(async (stream) => {
    const refreshed = await fetchSignals(session, stream.stream_key || stream.sensor, window);
    if (refreshed) updateStreamPoints(stream.stream_key || stream.sensor, refreshed.points, refreshed.mode);
  }));
}


function showPopover(marker, node) {
  const popover = byId('session-timeline-popover');
  if (!popover) return;
  const entry = marker.entry || {};
  const answerLine = entry.skipped
    ? `<em>${escapeHtml(t('sessions.skipped', 'skipped'))}</em>`
    : escapeHtml(formatAnswerValue(entry.answer));

  popover.innerHTML = `
    <div class="timeline-popover-title">#${escapeHtml(String(entry.question_number ?? ''))} ${escapeHtml(entry.question_prompt || entry.question_type || '')}</div>
    <div class="timeline-popover-answer">${answerLine}</div>
    ${renderBiosignalSummary(entry.biosignal_interval)}
  `;
  popover.hidden = false;

  // Measured against the popover's own offset parent. Using any other element
  // put the box in a different coordinate space, which is how it used to
  // appear far away in the top left instead of under the marker.
  const container = popover.offsetParent || popover.parentElement;
  const containerRect = container?.getBoundingClientRect();
  const nodeRect = node.getBoundingClientRect();
  if (!containerRect) return;

  const left = nodeRect.left - containerRect.left - 80;
  const maxLeft = containerRect.width - popover.offsetWidth;
  popover.style.left = `${Math.max(0, Math.min(left, Math.max(0, maxLeft)))}px`;
  popover.style.top = `${nodeRect.bottom - containerRect.top + 8}px`;
}

function hidePopover() {
  setHidden('session-timeline-popover', true);
}

function renderBiosignalSummary(intervalSummary) {
  if (!intervalSummary || typeof intervalSummary !== 'object') return '';
  const rows = [];
  Object.entries(intervalSummary).forEach(([sensor, summary]) => {
    if (!summary || typeof summary !== 'object' || !summary.available) return;
    const numericFields = Object.entries(summary).filter(
      ([key, value]) => typeof value === 'number' && !['dropped_in_interval', 'max_gap_seconds'].includes(key),
    );
    if (!numericFields.length) return;
    const fieldsText = numericFields.map(([key, value]) => `${key}: ${Number(value.toFixed?.(2) ?? value)}`).join(', ');
    rows.push(`<div class="timeline-popover-signal"><strong>${escapeHtml(sensor)}</strong>: ${escapeHtml(fieldsText)}</div>`);
  });
  return rows.join('');
}

function formatAnswerValue(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (Array.isArray(value)) return value.join(', ');
  // e.g. { words: [...], pleasantness: 0.72 } -> "words: a, b · pleasantness: 0.72"
  if (typeof value === 'object') {
    return Object.entries(value)
      .map(([key, entryValue]) => `${key}: ${Array.isArray(entryValue) ? entryValue.join(', ') : entryValue}`)
      .join(' · ');
  }
  return String(value);
}

export function sessionTags(session) {
  const tags = [];
  if (['queued', 'running'].includes(session.finalization_status)) {
    tags.push(t('sessions.finalizingTag', 'finalizing'));
  }
  if (Array.isArray(session.upload_failures) && session.upload_failures.length) {
    tags.push(t('sessions.uploadFailedTag', 'upload failed'));
  }
  if (session.status === 'attention_required') {
    tags.push(t('sessions.attentionTag', 'attention required'));
  } else if (session.status === 'completed_degraded' || session.quality_status === 'degraded') {
    tags.push(t('sessions.degradedTag', 'quality warning'));
  }
  if (session.recovered) tags.push(t('sessions.recoveredTag', 'recovered'));
  return tags;
}

/**
 * The session's whole course, left to right: started, ended, then every step
 * after submit (validation, merge, uploads) with retry buttons. It stays
 * available for as long as the session exists, so a failed upload can be
 * fixed and retried later, and it is where the finalization notice leads.
 */
async function renderProgressRail(session) {
  window.clearTimeout(finalizationTimer);
  const body = byId('session-progress-rail');
  if (!body || !session) return;
  const jobId = session.finalization_job_id;
  let job = null;
  if (jobId) {
    try {
      job = (await getJson(`/api/finalization/${encodeURIComponent(jobId)}`, { timeoutMs: 3000 }))?.job || null;
    } catch (error) {
      console.error('[sessions] Could not load the finalization steps:', error);
    }
  }
  if (currentSession !== session) return; // another session was opened meanwhile
  const reload = () => renderProgressRail(currentSession);
  const options = { showToast: callbacks.showToast, onDone: reload };
  renderSessionProgressRail(body, session, job, {
    onRetry: (id, stepKey) => retryFinalizationStep(id, stepKey, options),
    onRepublish: (id, stepKey, label) => republishFinalizationStep(id, stepKey, label, options),
    onConfirmDegraded: (id, reason) => confirmDegradedFinalization(id, reason, options),
    onContinue: (id) => continueFinalization(id, options),
    onOpenFolder: (id) => openFinalizationFolder(id, options),
  });
  if (!job) return;
  const stillWorking = ['queued', 'running'].includes(job.status)
    || (job.steps || []).some((step) => ['running', 'retrying', 'pending'].includes(step.status) && String(step.key || '').startsWith('publish_'));
  if (stillWorking && byId('view-session-detail')?.classList.contains('active')) {
    finalizationTimer = window.setTimeout(() => void reload(), FINALIZATION_REFRESH_MS);
  }
}

function formatDuration(totalSeconds) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds % 60);
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function _durationSeconds(startIso, endIso) {
  if (!startIso || !endIso) return null;
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return (end - start) / 1000;
}

