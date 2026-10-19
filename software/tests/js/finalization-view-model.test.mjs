import assert from 'node:assert/strict';
import {
  distinctFinalizationWarnings,
  finalizationProgress,
  finalizationSessionKey,
  finalizationStepLabel,
  pickFinalizationFocus,
  pickUploadFocus,
  visibleFinalizationSteps,
} from '../../study_runner/apps/ui/scripts/shared/finalization-view-model.js';

const queued = {
  job_id: 'queued',
  status: 'queued',
  created_epoch: 3,
  steps: [
    { key: 'commit_submission', status: 'done' },
    { key: 'freeze_recording', status: 'pending' },
    { key: 'publish_notion', status: 'skipped' },
  ],
};
const attention = {
  job_id: 'attention',
  status: 'attention_required',
  created_epoch: 1,
  steps: [
    { key: 'commit_submission', status: 'done' },
    { key: 'validate_sources', status: 'failed' },
    { key: 'publish_nextcloud', status: 'retrying' },
  ],
};
const completed = { job_id: 'done', status: 'completed', created_epoch: 9, steps: [] };
const degradedPublishing = {
  job_id: 'degraded',
  status: 'completed_degraded',
  created_epoch: 2,
  steps: [{ key: 'publish_notion', status: 'retrying' }],
};

assert.deepEqual(finalizationProgress(queued), { done: 2, total: 3, percent: 67 });
// A step the server hides (no destination can release source files) is not shown or counted.
const withHiddenRelease = { ...queued, steps: [...queued.steps, { key: 'purge_local_sources', status: 'skipped', hidden: true }] };
assert.deepEqual(visibleFinalizationSteps(withHiddenRelease).map((step) => step.key), ['commit_submission', 'freeze_recording', 'publish_notion']);
assert.deepEqual(finalizationProgress(withHiddenRelease), { done: 2, total: 3, percent: 67 });
assert.equal(pickFinalizationFocus([completed, queued, attention]), attention);
assert.equal(pickFinalizationFocus([completed, degradedPublishing]), degradedPublishing);
assert.equal(finalizationSessionKey(attention), 'finalization:attention');

// A clicked notice stays quiet until its signature changes.
const seenRetrying = { ...queued, job_id: 'seen', status: 'running', notice_signature: 'active|publish_nextcloud:retrying', acknowledged_signature: 'active|publish_nextcloud:retrying' };
assert.equal(pickFinalizationFocus([seenRetrying]), null);
const newProblem = { ...seenRetrying, notice_signature: 'attention_required|validate_sources:failed' };
assert.equal(pickFinalizationFocus([newProblem]), newProblem);
assert.equal(
  finalizationStepLabel(
    { key: 'publish_future_archive', label: 'Publish research archive' },
    (key, fallback) => key === 'finalization.step.publishFutureArchive' ? 'Archiv publizieren' : fallback,
  ),
  'Archiv publizieren',
);
assert.equal(
  finalizationStepLabel({ key: 'validate_future_format' }),
  'Validate Future Format',
);

// An acknowledged attention job no longer takes the widget focus.
{
  const seen = { job_id: 'a', status: 'attention_required', created_epoch: 2 };
  const busy = { job_id: 'r', status: 'running', created_epoch: 1 };
  assert.equal(pickFinalizationFocus([seen, busy]).job_id, 'a');
  assert.equal(pickFinalizationFocus([{ ...seen, attention_acknowledged_at: '2026-09-23T10:00:00Z' }, busy]).job_id, 'r');
  assert.equal(pickFinalizationFocus([{ ...seen, attention_acknowledged_at: 'x' }]), null);
}

// Each finalization problem is listed once.
{
  const warnings = [
    'session_end_notice: brainbit: timed out',
    'session_end_notice: brainbit: timed out',
    'freeze_recording: worker stopped',
    '',
  ];
  assert.deepEqual(
    distinctFinalizationWarnings(warnings, ['freeze_recording: worker stopped']),
    ['session_end_notice: brainbit: timed out'],
  );
  assert.deepEqual(distinctFinalizationWarnings(undefined), []);
}

// The upload-queue fallback notice: never point at a vanished session, never
// at finished work, and stay quiet once seen in exactly this state.
{
  const failedJob = { job_id: 'j1', status: 'failed', notice_signature: 'failed:3', acknowledged_signature: '' };
  const fresh = { session_id: 's1', jobs: [failedJob] };
  assert.equal(pickUploadFocus([fresh])?.session_id, 's1');
  assert.equal(pickUploadFocus([{ ...fresh, orphaned: true }]), null);
  assert.equal(pickUploadFocus([{ session_id: 's2', jobs: [{ job_id: 'j2', status: 'cancelled' }, { job_id: 'j3', status: 'done' }] }]), null);
  const seen = { session_id: 's3', jobs: [{ ...failedJob, acknowledged_signature: 'failed:3' }] };
  assert.equal(pickUploadFocus([seen]), null);
  const changedSinceSeen = { session_id: 's4', jobs: [{ ...failedJob, notice_signature: 'failed:4', acknowledged_signature: 'failed:3' }] };
  assert.equal(pickUploadFocus([changedSinceSeen])?.session_id, 's4');
  assert.equal(
    pickUploadFocus([fresh], [{ session_id: 's1', session_path: 'study/p01/session-1' }]).session_path,
    'study/p01/session-1',
  );
}
