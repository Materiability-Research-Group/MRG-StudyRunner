/**
 * The configurator's right-hand side: the Notion structure the layout will
 * create (updated on every change), what a check in Notion found, and what
 * one recorded session would write.
 */
import { escapeHtml } from '/static/scripts/shared/dom-utils.js';
import { t } from '/static/scripts/shared/i18n.js';
import { notionTree } from './layout-model.js';

const TREE_LABELS = () => ({
  answer: t('notionLayout.values.answer', 'Answer'),
  answersTitle: t('notionLayout.tree.answersTitle', 'Answers (in every participant page)'),
  answersRows: t('notionLayout.tree.answersRows', '{count} cards per session'),
});
const CHECK_STATUS = {
  ok: ['iconoir-check-circle', 'notionLayout.check.ok', 'ready'],
  create: ['iconoir-plus-circle', 'notionLayout.check.create', 'will be created'],
  add_columns: ['iconoir-plus-circle', 'notionLayout.check.addColumns', 'gets new columns: {columns}'],
  rename: ['iconoir-edit-pencil', 'notionLayout.check.rename', 'gets the card’s current name'],
};

export function renderTreePreview(layout, sources) {
  const nodes = notionTree(layout, sources, TREE_LABELS());
  return `
    <h3 class="notion-layout-aside-title">${escapeHtml(t('notionLayout.tree.title', 'What Notion will look like'))}</h3>
    <ul class="notion-tree" role="tree">
      <li class="notion-tree-node notion-tree-node--page" role="treeitem" aria-expanded="true">
        <span class="notion-tree-label"><i class="iconoir-page"></i> ${escapeHtml(t('notionLayout.tree.yourPage', 'Your Notion page'))}</span>
        <ul role="group">${nodes.map(renderNode).join('')}</ul>
      </li>
    </ul>`;
}

function renderNode(node) {
  const icon = node.kind === 'page' ? 'iconoir-page' : 'iconoir-table-rows';
  const columns = node.columns.length
    ? `<span class="notion-tree-columns">${node.columns.map((column) => `<span>${escapeHtml(column)}</span>`).join('')}</span>`
    : '';
  const note = node.note ? `<small class="notion-tree-note">${escapeHtml(node.note)}</small>` : '';
  const children = node.children.length ? `<ul role="group">${node.children.map(renderNode).join('')}</ul>` : '';
  return `
    <li class="notion-tree-node notion-tree-node--${escapeHtml(node.kind)}" role="treeitem">
      <span class="notion-tree-label"><i class="${icon}"></i> ${escapeHtml(node.title)}</span>
      ${columns}${note}${children}
    </li>`;
}

export function renderCheck(check) {
  if (!check) return '';
  if (!check.ok) {
    return `<div class="notion-layout-check is-error" role="alert"><i class="iconoir-warning-triangle"></i> ${escapeHtml(check.error || '')}</div>`;
  }
  const rows = (check.tables || []).map((table) => {
    const [icon, key, fallback] = CHECK_STATUS[table.status] || CHECK_STATUS.ok;
    const text = t(key, fallback).replace('{columns}', (table.columns || []).join(', '));
    return `<li><i class="${icon}"></i> <strong>${escapeHtml(table.table)}</strong> · ${escapeHtml(text)}</li>`;
  }).join('');
  const heading = check.applied
    ? t('notionLayout.check.applied', 'Created in Notion.')
    : t('notionLayout.check.found', 'Checked in Notion:');
  return `
    <div class="notion-layout-check" role="status">
      <p>${escapeHtml(heading)}</p>
      <ul>${rows}</ul>
      <p class="settings-hint">${escapeHtml(t('notionLayout.check.answersLater', 'Each participant’s answers table is created with their first upload.'))}</p>
    </div>`;
}

export function renderSessionPicker(sessions, selectedPath) {
  if (!sessions.length) {
    return `<p class="settings-hint">${escapeHtml(t('notionLayout.preview.noSessions', 'No recorded session of this study yet.'))}</p>`;
  }
  const options = sessions.map((session) => {
    const label = `${session.participant_id || '?'} · ${String(session.saved_at || '').slice(0, 16).replace('T', ' ')}`;
    return `<option value="${escapeHtml(session.session_path)}"${session.session_path === selectedPath ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
  return `
    <label class="field">
      <span>${escapeHtml(t('notionLayout.preview.session', 'Preview with a recorded session'))}</span>
      <select data-preview-session><option value="">${escapeHtml(t('notionLayout.preview.choose', 'Choose a session…'))}</option>${options}</select>
    </label>`;
}

export function renderSessionPreview(preview) {
  if (!preview) return '';
  if (!preview.ok) return `<p class="notion-layout-check is-error">${escapeHtml(preview.error || '')}</p>`;
  const rows = preview.rows.map((row) => {
    const cells = row.comparison || row.answers || {};
    const values = Object.entries(cells)
      .filter(([name]) => !['Participant ID', 'Card', 'StudyRunner Key', 'Session ID', 'Session start', 'Status'].includes(name))
      .map(([name, value]) => `${name}: ${Array.isArray(value) ? value.join(', ') : value ?? '—'}`)
      .join(' · ');
    return `<tr><th scope="row">${escapeHtml(row.label)}</th><td>${escapeHtml(row.status)}</td><td>${escapeHtml(values || '—')}</td></tr>`;
  }).join('');
  return `
    <table class="notion-layout-preview">
      <thead><tr>
        <th scope="col">${escapeHtml(t('notionLayout.preview.card', 'Card'))}</th>
        <th scope="col">${escapeHtml(t('notionLayout.preview.status', 'Status'))}</th>
        <th scope="col">${escapeHtml(t('notionLayout.preview.values', 'Values'))}</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="settings-hint">${escapeHtml(t('notionLayout.preview.missingHint', 'Empty values are written as the missing codes.'))}</p>`;
}
