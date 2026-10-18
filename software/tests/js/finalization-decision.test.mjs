import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderOperatorDecision, renderStep } from '../../study_runner/apps/ui/scripts/admin/session-progress-rail.js';

test('only quality warnings offer to continue processing with a reason', () => {
  const html = renderOperatorDecision({
    status: 'attention_required',
    can_continue_with_warnings: true,
    quality_acceptance: {
      step: 'validate_sources',
      issues: [{
        code: 'insufficient_time_coverage',
        source_key: 'brainbit',
        message: "stream 'study_runner.brainbit.bands' starts 52 ms after the session start marker",
      }],
    },
  });
  assert.match(html, /data-confirm-degraded/);
  assert.match(html, /Continue with warning/);
  assert.match(html, /data-degraded-reason/);
  assert.match(html, /starts 52 ms after the session start marker/);
  assert.match(html, /Processing continues/);
});

test('a blocking problem explains that nothing further is derived', () => {
  const html = renderOperatorDecision({ status: 'attention_required', can_continue_with_warnings: false });
  assert.match(html, /cannot be processed further/);
  assert.match(html, /Confirm degraded completion/);
  assert.doesNotMatch(html, /Continue with warning/);
});

test('a session stopped by an earlier version can be processed further', () => {
  const html = renderOperatorDecision({
    status: 'completed_degraded',
    can_continue_processing: true,
    degraded_confirmation: { reason: 'to short?' },
  });
  assert.match(html, /data-continue-processing/);
  assert.match(html, /to short\?/);
  assert.doesNotMatch(html, /data-degraded-reason/);
});

test('finished sessions offer no decision', () => {
  assert.equal(renderOperatorDecision({ status: 'completed' }), '');
  assert.equal(renderOperatorDecision({ status: 'completed_degraded', can_continue_processing: false }), '');
  assert.equal(renderOperatorDecision(null), '');
});

test('a finished upload offers to upload again, other steps do not', () => {
  const upload = renderStep({ key: 'publish_notion', label: 'Notion', status: 'done', republishable: true });
  assert.match(upload, /data-republish-step="publish_notion"/);
  assert.match(upload, /Upload again/);
  const merge = renderStep({ key: 'merge_xdf', label: 'Merge', status: 'done', republishable: false });
  assert.doesNotMatch(merge, /data-republish-step/);
});
