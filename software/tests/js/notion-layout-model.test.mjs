import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  addValue,
  cardIncluded,
  exportLayout,
  importLayout,
  normalizeLayout,
  notionTree,
  removeValue,
  serializeLayout,
  setCardIncluded,
  setValueFlag,
  settingsAfterCreate,
  summaryText,
} from '../../study_runner/plugins/destinations/notion_upload/ui/layout-model.js';

const SOURCES = {
  participant_columns: [
    { key: 'session_count', label: 'Sessions', available: true },
    { key: 'age_group', label: 'Age Group', available: true },
    { key: 'gender', label: 'Gender', available: false },
  ],
  cards: [
    { card_id: 'c1', label: '02 · Warm?', type: 'slider', default_included: true },
    { card_id: 'c2', label: '03 · Read', type: 'info', default_included: false },
  ],
  values: [{ source: 'stream.eda_signal.channel.eda.mean', label: 'eda_signal · eda · mean' }],
};

test('an empty layout has the same defaults as layout.py', () => {
  const layout = normalizeLayout({}, SOURCES);
  assert.deepEqual(layout.participants.columns, ['session_count', 'first_session', 'last_session', 'age_group']);
  assert.equal(layout.answers_per_participant, false);
  assert.deepEqual(layout.values.map((item) => [item.source, item.comparison]), [['answer', false], ['duration_seconds', false]]);
  assert.deepEqual(layout.missing, { number: 9999, text: 'n.a.' });
});

test('the card comparison exists exactly when a value is compared', () => {
  let layout = normalizeLayout({}, SOURCES);
  assert.equal(serializeLayout(layout).card_comparison, false);
  assert.equal(notionTree(layout, SOURCES).length, 1);
  layout = setValueFlag(layout, 'answer', 'comparison', true);
  assert.equal(serializeLayout(layout).card_comparison, true);
  const [, comparison] = notionTree(layout, SOURCES);
  assert.deepEqual(comparison.children.map((node) => node.title), ['02 · Warm?']);
});

test('answers tables appear inside the participants table', () => {
  const layout = { ...normalizeLayout({}, SOURCES), answers_per_participant: true };
  const [participants] = notionTree(layout, SOURCES);
  assert.equal(participants.children.length, 1);
  assert.ok(participants.children[0].columns.includes('Status'));
});

test('only differences from a card default are stored', () => {
  let layout = normalizeLayout({}, SOURCES);
  layout = setCardIncluded(layout, SOURCES.cards[1], true);
  assert.deepEqual(layout.cards, { c2: true });
  assert.equal(cardIncluded(layout, SOURCES.cards[1]), true);
  layout = setCardIncluded(layout, SOURCES.cards[1], false);
  assert.deepEqual(layout.cards, {});
});

test('sensor values can be added and removed, answer and duration only switched off', () => {
  let layout = addValue(normalizeLayout({}, SOURCES), 'stream.eda_signal.channel.eda.mean');
  assert.equal(layout.values.length, 3);
  assert.equal(addValue(layout, 'stream.eda_signal.channel.eda.mean').values.length, 3);
  layout = removeValue(removeValue(layout, 'answer'), 'stream.eda_signal.channel.eda.mean');
  assert.deepEqual(layout.values.map((item) => item.source), ['answer', 'duration_seconds']);
});

test('an exported layout keeps its card switches only in its own study', () => {
  const layout = setCardIncluded(normalizeLayout({}, SOURCES), SOURCES.cards[1], true);
  const file = exportLayout(layout, 'study-a');
  assert.deepEqual(importLayout(file, 'study-a').cards, { c2: true });
  assert.deepEqual(importLayout(file, 'study-b').cards, {});
  assert.throws(() => importLayout({ format: 'other' }, 'study-a'), /not a Study Runner Notion layout/);
});

test('creating tables always saves the layout they were created for', () => {
  const layout = setValueFlag(normalizeLayout({}, SOURCES), 'answer', 'comparison', true);
  // No new ids (the tables' parents were known): the layout is still saved.
  assert.deepEqual(settingsAfterCreate({ ok: true, applied: true, settings_updates: {} }, layout), {
    notion_layout: serializeLayout(layout),
  });
  assert.deepEqual(
    settingsAfterCreate({ ok: true, applied: true, settings_updates: { comparison_page_id: 'p1' } }, layout),
    { comparison_page_id: 'p1', notion_layout: serializeLayout(layout) },
  );
  assert.equal(settingsAfterCreate({ ok: true, applied: false }, layout), null);
  assert.equal(settingsAfterCreate({ ok: false, error: 'x' }, layout), null);
});

test('the summary names what the study creates', () => {
  assert.equal(summaryText({}), '');
  assert.equal(summaryText({ parent_page_id: 'p' }), 'Participants table');
  const layout = { version: 1, answers_per_participant: true, values: [{ source: 'answer', comparison: true }] };
  assert.equal(summaryText({ parent_page_id: 'p', notion_layout: layout }), 'Participants table · answers per participant · one comparison table per card');
});
