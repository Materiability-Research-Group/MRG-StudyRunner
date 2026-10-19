const STEP_COMPLETE = new Set(['done', 'skipped']);

/** The steps a UI shows: the server hides those that can never happen here. */
export function visibleFinalizationSteps(job) {
  return (Array.isArray(job?.steps) ? job.steps : []).filter((step) => !step?.hidden);
}

export function finalizationProgress(job) {
  const steps = visibleFinalizationSteps(job);
  const total = steps.length;
  const done = steps.filter((step) => STEP_COMPLETE.has(step?.status)).length;
  return {
    done,
    total,
    percent: total ? Math.round((done / total) * 100) : 0,
  };
}

function hasUnseenUploadFailure(job) {
  return job?.status === 'completed'
    && Array.isArray(job.upload_failures) && job.upload_failures.length > 0
    && !job.attention_acknowledged_at;
}

// The admin clicked this notice and nothing new has happened since.
function isNoticeSeen(job) {
  return Boolean(job?.acknowledged_signature) && job.acknowledged_signature === job.notice_signature;
}

function isFinalizationActive(job) {
  if (isNoticeSeen(job)) return false;
  if (hasUnseenUploadFailure(job)) return true;
  if (!job?.job_id || job.status === 'completed') return false;
  // An acknowledged attention job stays attention_required, but no longer needs the widget.
  if (job.status === 'attention_required' && job.attention_acknowledged_at) return false;
  if (job.status === 'completed_degraded') {
    return (Array.isArray(job.steps) ? job.steps : []).some((step) => (
      (step?.phase === 'publication' || String(step?.key || '').startsWith('publish_'))
      && !STEP_COMPLETE.has(step?.status)
    ));
  }
  return true;
}

export function pickFinalizationFocus(jobs) {
  const active = (Array.isArray(jobs) ? jobs : []).filter(isFinalizationActive);
  const priority = { attention_required: 0, completed_degraded: 1, running: 2, queued: 3 };
  const rank = (job) => (hasUnseenUploadFailure(job) ? 0.5 : priority[job.status] ?? 9);
  return active.sort((left, right) => {
    const statusOrder = rank(left) - rank(right);
    if (statusOrder) return statusOrder;
    return Number(right.created_epoch || 0) - Number(left.created_epoch || 0);
  })[0] || null;
}

/**
 * The upload-queue session the corner notice should point at, if any.
 *
 * This is the fallback for uploads the finalization service does not
 * cover. A session is skipped when:
 * - the server marks it `orphaned` (its finalization job, and with it the
 *   session folder, is gone - linking there would only show "not found");
 * - every job is done or cancelled (nothing left to do);
 * - the operator already looked at it in exactly this state
 *   (`acknowledged_signature`, journaled server-side so it survives a
 *   restart).
 */
export function pickUploadFocus(uploadSessions, localSessions = []) {
  const localById = new Map((Array.isArray(localSessions) ? localSessions : [])
    .map((session) => [session.session_id, session]));
  const session = (Array.isArray(uploadSessions) ? uploadSessions : []).find((candidate) => {
    if (candidate?.orphaned) return false;
    const jobs = Array.isArray(candidate?.jobs) ? candidate.jobs : [];
    const open = jobs.filter((job) => !['done', 'cancelled'].includes(job.status));
    if (!open.length) return false;
    return open.some((job) => job.acknowledged_signature !== job.notice_signature);
  });
  if (!session) return null;
  return { ...session, session_path: session.session_path || localById.get(session.session_id)?.session_path || '' };
}

export function finalizationSessionKey(job) {
  return `finalization:${job?.job_id || ''}`;
}

/** Resolve known translations while allowing arbitrary future worker steps. */
export function finalizationStepLabel(step, translate = (_key, fallback) => fallback) {
  const key = String(step?.key || '').trim();
  const fallback = String(step?.label || humanize(key) || key);
  if (!key) return fallback;
  const translationSuffix = key.replace(/_([a-z0-9])/g, (_match, letter) => letter.toUpperCase());
  return translate(`finalization.step.${translationSuffix}`, fallback);
}

function humanize(value) {
  return String(value || '')
    .replace(/[._-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

// Each problem is shown once: as its failed step, or else once in the warnings.
export function distinctFinalizationWarnings(warnings, problemMessages = []) {
  const shown = new Set(problemMessages);
  return [...new Set((Array.isArray(warnings) ? warnings : []).filter(Boolean).map(String))]
    .filter((warning) => !shown.has(warning));
}
