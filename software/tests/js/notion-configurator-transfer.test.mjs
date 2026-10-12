import assert from 'node:assert/strict';
import { appendPresetTarget, exportConfiguration, importConfiguration } from '../../study_runner/plugins/destinations/notion_upload/ui/configurator-transfer.js';

const mapping = { preset: 'custom', targets: [{
  id: 'cards', title: 'Cards', row_level: 'card', database_id: 'db-1',
  columns: { Answer: { type: 'rich_text', source: 'card.answer' } },
  relations: [{ target_id: 'people', property_name: 'Participant', linked_view: true }],
  comparison_views: true,
}] };
const file = exportConfiguration('study-a', 'parent-1', mapping);
assert.equal(file.version, 1);
assert.equal(JSON.stringify(file).includes('api_key'), false);
assert.deepEqual(importConfiguration(file, 'study-a', { keepIds: true }), {
  mapping, parentPageId: 'parent-1',
});
assert.equal(importConfiguration(file, 'study-a').mapping.targets[0].database_id, '');
assert.equal(importConfiguration(file, 'study-b').parentPageId, '');
assert.throws(() => importConfiguration(file, 'study-b', { keepIds: true }), /same study/);
assert.throws(() => importConfiguration({ ...file, version: 99 }, 'study-a'), /Invalid/);
assert.throws(() => importConfiguration({ ...file, api_key: 'secret' }, 'study-a'), /unsupported/);
assert.throws(() => importConfiguration({ ...file, export_mapping: { targets: [null] } }, 'study-a'), /Invalid/);
assert.equal(file.export_mapping.targets[0].database_id, 'db-1');

const withExisting = structuredClone(mapping);
const newId = appendPresetTarget(withExisting, { id: 'sessions', title: 'Sessions', database_id: '' }, 'new-2');
assert.equal(newId, 'new-2');
assert.equal(withExisting.targets.length, 2);
assert.equal(withExisting.targets[0].database_id, 'db-1');
assert.throws(() => appendPresetTarget(withExisting, { title: 'Duplicate' }, 'cards'), /unique/);
