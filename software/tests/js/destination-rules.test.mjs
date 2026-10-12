import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  askedPayloadFields,
  buildActionPayload,
  nextDestinationStep,
  panelActions,
  splitDestinationFields,
} from '../../study_runner/apps/ui/scripts/settings/study/destination-rules.js';

const PLUGINS = new URL('../../study_runner/plugins/destinations/', import.meta.url);

// The catalog hands the browser normalized manifests, whose capabilities sit
// under capability_config; for these fields that is the raw "capabilities".
function manifest(folder) {
  const raw = JSON.parse(readFileSync(new URL(`${folder}/manifest.json`, PLUGINS), 'utf8'));
  return { ...raw, capability_config: raw.capabilities };
}

const plugin = {
  plugin_key: 'example',
  settings: {
    machine: { timeout_seconds: { type: 'number' } },
    study: {
      page: { type: 'string' },
      target_id: { type: 'string', read_only: true },
      tuning: { type: 'number', advanced: true },
      mapping: { type: 'object' },
    },
  },
  capability_config: {
    credentials: { config_field: 'api_key', per_study: true },
    readiness_requirements: { requires_secret: true, requires_settings: ['page', 'target_id'] },
    admin_actions: {
      actions: [
        {
          key: 'test',
          payload_schema: {
            api_key: { type: 'string' },
            study_id: { type: 'string' },
            page: { type: 'string' },
            target_id: { type: 'string' },
            timeout_seconds: { type: 'integer' },
            note: { type: 'string' },
          },
        },
        { key: 'browse', surface: 'configurator', payload_schema: { cursor: { type: 'string' } } },
      ],
    },
  },
};

test('the panel offers only panel actions', () => {
  assert.deepEqual(panelActions(plugin).map((action) => action.key), ['test']);
});

test('an action never asks for what study, credential or machine settings answer', () => {
  const [action] = panelActions(plugin);
  assert.deepEqual(askedPayloadFields(action, plugin).map(([name]) => name), ['note']);
});

test('read-only and advanced fields go to the details, object fields nowhere', () => {
  const { main, details } = splitDestinationFields(plugin);
  assert.deepEqual(main.map(([name]) => name), ['page']);
  assert.deepEqual(details.map(([name]) => name), ['target_id', 'tuning']);
});

test('the next step mirrors the study start check', () => {
  assert.deepEqual(nextDestinationStep(plugin, {}, { credentialConfigured: false }), { kind: 'credential' });
  // None of the required settings is set: the editable one is named.
  assert.deepEqual(nextDestinationStep(plugin, {}), { kind: 'setting', name: 'page' });
  // One of them is enough, as in study_readiness_service.py.
  assert.deepEqual(nextDestinationStep(plugin, { target_id: 'abc' }), { kind: 'ready' });
  assert.deepEqual(nextDestinationStep(plugin, { page: ' ' }), { kind: 'setting', name: 'page' });
});

test('the payload takes typed values first, then saved ones, never machine settings', () => {
  const [action] = panelActions(plugin);
  const payload = buildActionPayload(action, plugin, {
    studyId: 'study-1',
    inputs: { page: 'typed-page', note: '' },
    saved: { page: 'saved-page', target_id: 'saved-target', timeout_seconds: 99 },
    credentialValue: '',
  });
  assert.deepEqual(payload, { study_id: 'study-1', page: 'typed-page', target_id: 'saved-target' });
});

test('Notion and Nextcloud show only "Test connection", and it asks for nothing', () => {
  for (const folder of ['notion_upload', 'nextcloud_upload']) {
    const destination = manifest(folder);
    const actions = panelActions(destination);
    assert.deepEqual(actions.map((action) => action.key), ['test_connection'], folder);
    assert.deepEqual(askedPayloadFields(actions[0], destination), [], folder);
  }
});

test('Notion keeps its IDs out of the main area and asks only for the parent page', () => {
  const { main, details } = splitDestinationFields(manifest('notion_upload'));
  assert.deepEqual(main.map(([name]) => name), ['parent_page_id']);
  assert.deepEqual(details.map(([name]) => name), ['database_id', 'data_source_id', 'sessions_database_id']);
});
