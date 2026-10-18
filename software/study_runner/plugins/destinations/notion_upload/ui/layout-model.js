/**
 * The Notion layout as the configurator edits it - plain data, no DOM.
 *
 * Mirrors layout.py: the same defaults, so what the configurator shows is
 * what an upload does. `sources` is the answer of the plugin's
 * `describe_layout_sources` action: the study's cards, the participant
 * columns and the values a card can provide.
 */
export const LAYOUT_VERSION = 1;
export const SESSION_COLUMNS = ['session_count', 'first_session', 'last_session'];
export const DEFAULT_MISSING = { number: 9999, text: 'n.a.' };
const DEFAULT_VALUES = [
  { source: 'answer', answers: true, comparison: false },
  { source: 'duration_seconds', answers: true, comparison: false },
];
const ANSWER_SOURCES = new Set(['answer', 'duration_seconds']);

/** The stored layout with every default filled in, like effective_layout(). */
export function normalizeLayout(raw, sources = {}) {
  const value = raw && typeof raw === 'object' && Object.keys(raw).length ? raw : {};
  const stored = (sources.participant_columns || []).filter((column) => column.available).map((column) => column.key);
  const columns = Array.isArray(value.participants?.columns)
    ? [...value.participants.columns]
    : [...SESSION_COLUMNS, ...stored.filter((key) => !SESSION_COLUMNS.includes(key))];
  const values = Array.isArray(value.values) ? value.values : DEFAULT_VALUES;
  return {
    version: LAYOUT_VERSION,
    participants: { columns },
    answers_per_participant: value.answers_per_participant === true,
    values: values.map((item) => ({
      source: String(item.source),
      answers: item.answers !== false,
      comparison: item.comparison === true,
    })),
    cards: { ...(value.cards || {}) },
    card_comparison: value.card_comparison === true,
    missing: { ...DEFAULT_MISSING, ...(value.missing || {}) },
  };
}

/** What is saved: the comparison exists exactly when a value is compared. */
export function serializeLayout(layout) {
  return {
    version: LAYOUT_VERSION,
    participants: { columns: [...layout.participants.columns] },
    answers_per_participant: layout.answers_per_participant,
    values: layout.values.map(({ source, answers, comparison }) => ({ source, answers, comparison })),
    cards: { ...layout.cards },
    card_comparison: layout.values.some((item) => item.comparison),
    missing: { number: Number(layout.missing.number), text: String(layout.missing.text) },
  };
}

export function cardIncluded(layout, card) {
  return Object.hasOwn(layout.cards, card.card_id) ? layout.cards[card.card_id] === true : card.default_included;
}

/** Only differences from a card's default are stored. */
export function setCardIncluded(layout, card, included) {
  const cards = { ...layout.cards };
  if (included === card.default_included) delete cards[card.card_id];
  else cards[card.card_id] = included;
  return { ...layout, cards };
}

export function toggleColumn(layout, key, on) {
  const columns = layout.participants.columns.filter((item) => item !== key);
  if (on) columns.push(key);
  return { ...layout, participants: { columns } };
}

export function setValueFlag(layout, source, flag, on) {
  return { ...layout, values: layout.values.map((item) => (item.source === source ? { ...item, [flag]: on } : item)) };
}

export function addValue(layout, source) {
  if (layout.values.some((item) => item.source === source)) return layout;
  return { ...layout, values: [...layout.values, { source, answers: true, comparison: false }] };
}

/** Answer and duration stay listed (switch them off instead); sensor values can go. */
export function removeValue(layout, source) {
  if (ANSWER_SOURCES.has(source)) return layout;
  return { ...layout, values: layout.values.filter((item) => item.source !== source) };
}

export function isRemovableValue(source) {
  return !ANSWER_SOURCES.has(source);
}

export function valueLabel(source, sources = {}) {
  return (sources.values || []).find((item) => item.source === source)?.label || source;
}

/**
 * The Notion structure the layout creates, as a tree for the preview.
 * Column lists use the names Notion will show.
 */
export function notionTree(layout, sources = {}, labels = {}) {
  const text = (key, fallback) => labels[key] || fallback;
  const columnLabel = (key) => (sources.participant_columns || []).find((column) => column.key === key)?.label || key;
  const cardValues = (place) => layout.values.filter((item) => item[place]).map((item) => (
    item.source === 'answer' ? text('answer', 'Answer') : valueLabel(item.source, sources)
  ));
  const cards = (sources.cards || []).filter((card) => cardIncluded(layout, card));
  const participants = {
    kind: 'database',
    title: 'Participants',
    columns: ['Participant ID', ...layout.participants.columns.map(columnLabel)],
    children: [],
  };
  if (layout.answers_per_participant) {
    participants.children.push({
      kind: 'database',
      title: text('answersTitle', 'Answers (in every participant page)'),
      columns: ['Card', 'Session start', 'Status', ...cardValues('answers')],
      note: text('answersRows', '{count} cards per session').replace('{count}', String(cards.length)),
      children: [],
    });
  }
  const tree = [participants];
  if (layout.values.some((item) => item.comparison)) {
    tree.push({
      kind: 'page',
      title: 'Card comparison',
      columns: [],
      children: cards.map((card) => ({
        kind: 'database',
        title: card.label,
        columns: ['Participant ID', 'Participant ↗', 'Session start', 'Status', ...cardValues('comparison')],
        children: [],
      })),
    });
  }
  return tree;
}

/** One line for the study settings panel. */
export function summaryText(settings = {}, labels = {}) {
  const text = (key, fallback) => labels[key] || fallback;
  if (!String(settings.parent_page_id || '').trim()) return '';
  const layout = normalizeLayout(settings.notion_layout);
  const parts = [text('participants', 'Participants table')];
  if (layout.answers_per_participant) parts.push(text('answers', 'answers per participant'));
  if (layout.values.some((item) => item.comparison)) parts.push(text('comparison', 'one comparison table per card'));
  return parts.join(' · ');
}

/** A layout as a file another study can start from (no Notion ids, no key). */
export function exportLayout(layout, studyId) {
  return { format: 'study-runner-notion-layout', version: 1, study_id: studyId, layout: serializeLayout(layout) };
}

/** Card switches only mean something in the study they were made in. */
export function importLayout(document, studyId) {
  if (document?.format !== 'study-runner-notion-layout' || document.version !== 1 || typeof document.layout !== 'object') {
    const error = new Error('This file is not a Study Runner Notion layout.');
    error.code = 'invalidFile';
    throw error;
  }
  const layout = { ...document.layout };
  if (document.study_id !== studyId) layout.cards = {};
  return layout;
}
