/**
 * The Notion plugin's own study-settings configurator: a page tree to pick
 * or create a target database in, and a column mapping for each target -
 * as a column list, or as a node graph (node-canvas.js) wired to the same
 * underlying columns. See docs/notion-plugin-configurator-plan.md.
 *
 * Loaded on demand by study-settings-panel.js (ui.extensions.study_settings
 * in manifest.json) - this file never runs unless the operator opens it.
 * Everything here talks to the host through the callbacks it is given
 * (`runAction` for the plugin's admin actions, `saveSettings` to persist the
 * mapping), never by importing host modules directly - except the generic,
 * already-public session list (`/api/admin/sessions`), which this reads
 * the same way any other admin page does, to let the operator browse
 * sessions by name instead of typing a folder path.
 */
import { createModal } from '/static/scripts/shared/modal.js';
import { escapeHtml } from '/static/scripts/shared/dom-utils.js';
import { getJson } from '/static/scripts/shared/api-client.js';
import { getLanguage } from '/static/scripts/shared/i18n.js';
import { mountNodeView } from './configurator-node-view.js';

// Column names a preset creates, per language. Every name stays editable
// afterwards; this only decides what a fresh database starts with.
const PRESET_NAMES = {
  en: {
    keySession: 'Session ID', keyParticipant: 'Participant ID', keyCard: 'Row Key',
    participant: 'Participant ID', start: 'Start', duration: 'Duration (min)',
    prompt: 'Prompt', answer: 'Answer', cardDuration: 'Duration (s)',
    sessionsTitle: 'Study Runner export', answersTitle: 'Study Runner answers', newTarget: 'New database',
  },
  de: {
    keySession: 'Sitzungs-ID', keyParticipant: 'Teilnehmer-ID', keyCard: 'Zeilenschlüssel',
    participant: 'Teilnehmer-ID', start: 'Beginn', duration: 'Dauer (min)',
    prompt: 'Frage', answer: 'Antwort', cardDuration: 'Dauer (s)',
    sessionsTitle: 'Study Runner Export', answersTitle: 'Study Runner Antworten', newTarget: 'Neue Datenbank',
  },
};

function defaultNameLanguage() {
  try {
    return String(getLanguage() || '').toLowerCase().startsWith('de') ? 'de' : 'en';
  } catch {
    return 'en';
  }
}

function keyColumnDefault(rowLevel, lang) {
  const names = PRESET_NAMES[lang] || PRESET_NAMES.en;
  return { session: names.keySession, participant: names.keyParticipant, card: names.keyCard }[rowLevel] || names.keySession;
}

const ROW_LEVELS = ['session', 'participant', 'card'];
const COLUMN_TYPES = ['rich_text', 'number', 'date', 'select', 'multi_select'];
const REDUCERS = ['mean', 'min', 'max', 'count', 'join', 'first', 'last'];
const OTHER_SOURCE = '__other__';
const PRESETS = [
  { key: 'simple', title: 'Simple', description: 'One database, one row per session.' },
  { key: 'analysis', title: 'Analysis', description: 'One database, one row per card - good for filtering in Notion.' },
  { key: 'as_before', title: 'As before', description: 'Participants + sessions databases, exactly like today.' },
  { key: 'custom', title: 'Custom', description: 'Start from an empty target list.' },
];

const SUMMARY_TEXT = {
  en: {
    quote: (title) => `“${title}”`,
    rowLevels: { session: 'one row per session', participant: 'one row per participant', card: 'one row per card' },
    columns: '{count} columns',
    untitled: 'Untitled',
    notConnected: 'not connected yet',
    legacyConnected: 'Databases connected, columns as before',
    legacyNew: 'As before: participant and session databases are created below the page',
  },
  de: {
    quote: (title) => `„${title}“`,
    rowLevels: { session: 'eine Zeile pro Sitzung', participant: 'eine Zeile pro Person', card: 'eine Zeile pro Karte' },
    columns: '{count} Spalten',
    untitled: 'Ohne Titel',
    notConnected: 'noch nicht verbunden',
    legacyConnected: 'Datenbanken verbunden, Spalten wie bisher',
    legacyNew: 'Wie bisher: Datenbanken für Teilnehmende und Sitzungen entstehen unter der Seite',
  },
};

/**
 * One line for the study settings panel: where this study's results go.
 * The generic destination block shows it next to "Configure…"; it opens
 * nothing and changes nothing.
 */
export function summarize(settings = {}) {
  const text = SUMMARY_TEXT[defaultNameLanguage()] || SUMMARY_TEXT.en;
  const { targets } = normalizeMapping(settings?.export_mapping);
  if (!targets.length) {
    if (String(settings?.database_id || '').trim()) return text.legacyConnected;
    return String(settings?.parent_page_id || '').trim() ? text.legacyNew : '';
  }
  return targets.map((target) => {
    const parts = [
      text.quote(target.title || text.untitled),
      text.rowLevels[target.row_level],
      text.columns.replace('{count}', String(Object.keys(target.columns).length)),
    ];
    if (!target.database_id) parts.push(text.notConnected);
    return parts.join(' · ');
  }).join('; ');
}

export function openConfigurator({ plugin, studyId, settings, configData, runAction, saveSettings, showToast }) {
  const state = {
    mapping: normalizeMapping(settings?.export_mapping),
    parentPageId: String(settings?.parent_page_id || '').trim(),
    activeTargetId: null,
    tree: { nodes: [], loadedFor: '', loading: false, error: '' },
    preview: null,
    dirty: false,
    nameLanguage: defaultNameLanguage(),
    catalog: [], // session/participant fields - study-wide, loaded once
    sessions: [], // recent finished sessions, for the preview/source picker
    sessionSources: {}, // session_path -> sensor sources actually present there
    previewSessionPath: '',
    viewModeByTarget: {}, // target id -> 'list' | 'node'
    manualSourceColumns: new Set(), // "target.id:column name" currently typed by hand
  };
  state.activeTargetId = state.mapping.targets[0]?.id || null;

  let activeNodeView = null;

  const modal = createModal({
    title: `${plugin?.ui?.label || 'Notion'} export mapping`,
    variant: 'workspace',
    closeLabel: 'Close',
    // Each open builds a fresh modal; remove it on close instead of leaving
    // hidden copies behind in the page.
    onClose: () => { activeNodeView?.destroy(); modal.element.remove(); },
  });

  function render() {
    // The node canvas owns a few window-level listeners (dragging must keep
    // tracking outside it); rebuilding the DOM below does not remove those,
    // so they are torn down explicitly before every rebuild, not only on
    // close - render() runs on nearly every edit while mounted.
    activeNodeView?.destroy();
    activeNodeView = null;
    modal.body.innerHTML = `
      <div class="configurator-rail">${renderRail(state)}</div>
      <div class="configurator-main">${renderMain(state)}</div>
    `;
    bind();
    const target = activeTarget();
    if (target && state.viewModeByTarget[target.id] === 'node') mountNode(target);
  }

  /** The sources a column may pick from: study-wide fields, plus whatever
   * the chosen preview session actually has (real stream/channel names). */
  function combinedCatalog() {
    return [...state.catalog, ...(state.sessionSources[state.previewSessionPath] || [])];
  }

  function mountNode(target) {
    const container = modal.body.querySelector('[data-node-container]');
    if (!container) return;
    const view = mountNodeView(container, target, combinedCatalog(), {
      onCommit: (columns) => { target.columns = columns; state.dirty = true; updateDirtyIndicator(); },
    });
    activeNodeView = view;
    modal.body.querySelector('[data-node-add-source]')?.addEventListener('click', () => {
      void pickSource().then((source) => { if (source) view.addSourceNode(source); });
    });
    modal.body.querySelector('[data-node-add-reducer]')?.addEventListener('click', () => {
      const reducer = window.prompt(`Reducer (${REDUCERS.join('/')}):`, 'mean');
      if (reducer === null) return; // cancelled
      if (!REDUCERS.includes(reducer)) { showToast?.(`Not a reducer: ${reducer}. Use one of: ${REDUCERS.join(', ')}.`, 'error'); return; }
      view.addReducerNode(reducer);
    });
    modal.body.querySelector('[data-node-add-round]')?.addEventListener('click', () => {
      const typed = window.prompt('Decimals:', '1');
      if (typed === null) return;
      const decimals = Number.parseInt(typed, 10);
      if (!Number.isInteger(decimals) || decimals < 0) { showToast?.('Decimals must be a whole number, 0 or more.', 'error'); return; }
      view.addRoundNode(decimals);
    });
    modal.body.querySelector('[data-node-add-default]')?.addEventListener('click', () => {
      const value = window.prompt('Default value when empty:');
      if (value !== null) view.addDefaultNode(value);
    });
    modal.body.querySelector('[data-node-add-column]')?.addEventListener('click', () => {
      const name = window.prompt('New column name:');
      if (name === null) return;
      const trimmed = name.trim();
      if (!trimmed) { showToast?.('A column needs a name.', 'error'); return; }
      if (Object.prototype.hasOwnProperty.call(target.columns, trimmed)) {
        showToast?.(`"${trimmed}" is already a column on this target.`, 'error');
        return;
      }
      target.columns[trimmed] = { type: 'rich_text', source: '', reducer: null };
      view.addColumnNode(trimmed, 'rich_text');
      state.dirty = true;
      updateDirtyIndicator();
    });
    modal.body.querySelector('[data-node-zoom-fit]')?.addEventListener('click', view.zoomToFit);
  }

  /**
   * A real picker for the node view's "+ Source" button, instead of asking
   * the operator to type an id by hand - the whole point of the catalog
   * and session-source discovery actions. Resolves the chosen source, or
   * null if cancelled.
   */
  function pickSource() {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        picker.destroy();
        resolve(value);
      };
      const picker = createModal({ title: 'Pick a source', closeLabel: 'Cancel', onClose: () => finish(null) });
      const options = combinedCatalog();
      picker.body.innerHTML = `
        <ul class="configurator-tree">
          ${options.map((entry) => `
            <li><button class="btn-secondary" type="button" data-pick="${escapeHtml(entry.source)}">${escapeHtml(entry.label)}</button></li>
          `).join('') || '<p class="settings-hint">No sensor sources yet - pick a preview session below to see them.</p>'}
        </ul>
        <label class="field">
          <span>Or type one manually (e.g. card[2].answer)</span>
          <input type="text" placeholder="card[2].answer" data-pick-manual>
        </label>
        <div class="dashboard-actions">
          <button class="btn-primary" type="button" data-pick-manual-confirm>Use typed value</button>
        </div>`;
      picker.body.querySelectorAll('[data-pick]').forEach((button) => {
        button.addEventListener('click', () => finish(button.dataset.pick));
      });
      picker.body.querySelector('[data-pick-manual-confirm]')?.addEventListener('click', () => {
        const value = picker.body.querySelector('[data-pick-manual]')?.value?.trim();
        if (value) finish(value);
      });
      picker.open();
    });
  }

  /** Avoids a full re-render (which would rebuild the node canvas mid-edit)
   * for a plain "you have unsaved changes" cue. */
  function updateDirtyIndicator() {
    modal.body.querySelector('[data-save]')?.classList.add('btn-primary--dirty');
  }

  function bind() {
    const rail = modal.body.querySelector('.configurator-rail');
    const main = modal.body.querySelector('.configurator-main');

    rail?.querySelectorAll('[data-preset]').forEach((button) => {
      button.addEventListener('click', () => applyPreset(button.dataset.preset));
    });
    rail?.querySelector('[data-name-language]')?.addEventListener('change', (event) => {
      state.nameLanguage = event.target.value === 'de' ? 'de' : 'en';
    });
    rail?.querySelectorAll('[data-select-target]').forEach((row) => {
      row.addEventListener('click', () => { state.activeTargetId = row.dataset.selectTarget; render(); });
    });
    rail?.querySelector('[data-add-target]')?.addEventListener('click', addTarget);
    rail?.querySelectorAll('[data-remove-target]').forEach((button) => {
      button.addEventListener('click', (event) => { event.stopPropagation(); removeTarget(button.dataset.removeTarget); });
    });
    rail?.querySelectorAll('[data-attach-database]').forEach((button) => {
      button.addEventListener('click', () => attachDatabase(button.dataset.attachDatabase, button.dataset.attachTitle));
    });
    rail?.querySelectorAll('[data-expand-node]').forEach((button) => {
      button.addEventListener('click', () => void expandNode(button.dataset.expandNode));
    });
    rail?.querySelector('[data-create-database]')?.addEventListener('click', () => void createDatabase());

    main?.querySelectorAll('[data-column-field]').forEach((input) => {
      input.addEventListener('change', () => updateColumn(input));
    });
    main?.querySelector('[data-add-column]')?.addEventListener('click', addColumn);
    main?.querySelectorAll('[data-remove-column]').forEach((button) => {
      button.addEventListener('click', () => removeColumn(button.dataset.removeColumn));
    });
    main?.querySelector('[data-target-title]')?.addEventListener('change', (event) => {
      activeTarget().title = event.target.value;
      markDirty();
    });
    main?.querySelector('[data-target-row-level]')?.addEventListener('change', (event) => {
      const target = activeTarget();
      if (!target || !ROW_LEVELS.includes(event.target.value)) return;
      target.row_level = event.target.value;
      // The key column holds a different value per row level; name it for
      // the new level in the chosen language. Reducers only fit some levels,
      // so clear them and let validation ask again where needed.
      target.key_column = keyColumnDefault(target.row_level, state.nameLanguage);
      Object.values(target.columns).forEach((column) => { column.reducer = null; });
      markDirty();
    });
    main?.querySelector('[data-view-mode]')?.addEventListener('change', (event) => {
      const target = activeTarget();
      if (target) state.viewModeByTarget[target.id] = event.target.value;
      render();
    });
    main?.querySelector('[data-preview-session-picker]')?.addEventListener('change', (event) => {
      void selectPreviewSession(event.target.value);
    });
    main?.querySelector('[data-preview-run]')?.addEventListener('click', () => void runPreview());
    main?.querySelector('[data-save]')?.addEventListener('click', () => void save());
    main?.querySelector('[data-discard]')?.addEventListener('click', () => {
      // Sits right next to "Save mapping"; a misclick between the two must
      // not silently throw away unsaved work.
      if (state.dirty && !window.confirm('Discard every unsaved change?')) return;
      modal.close();
    });
  }

  function activeTarget() {
    return state.mapping.targets.find((target) => target.id === state.activeTargetId) || null;
  }

  function markDirty() {
    state.dirty = true;
    render();
  }

  async function loadCatalogAndSessions() {
    try {
      // Only the questions are needed (to find the participant-id card's
      // stored fields) - sending the whole study, stimulus content and all,
      // risks the admin action's own payload size limit on a large study.
      const minimalConfig = { questions: configData?.questions || [] };
      const response = await runAction('describe_output_catalog', { config_data_json: JSON.stringify(minimalConfig) });
      state.catalog = response?.result?.sources || [];
    } catch (error) {
      console.error('[notion-configurator] Could not load the output catalog:', error);
    }
    try {
      const sessions = await getJson('/api/admin/sessions');
      const all = (Array.isArray(sessions) ? sessions : []).filter((session) => session.session_path);
      // Prefer this study's own sessions; with none yet (e.g. a brand-new
      // study), fall back to every session rather than an empty picker -
      // still useful to try a mapping's shape against real sensor data.
      const ownSessions = studyId ? all.filter((session) => session.study_id === studyId) : all;
      state.sessions = (ownSessions.length ? ownSessions : all).slice(0, 100);
    } catch (error) {
      console.error('[notion-configurator] Could not load the session list:', error);
    }
    render();
  }

  async function selectPreviewSession(sessionPath) {
    state.previewSessionPath = sessionPath;
    if (sessionPath && !state.sessionSources[sessionPath]) {
      try {
        const response = await runAction('describe_session_sources', { session_path: sessionPath });
        state.sessionSources[sessionPath] = response?.result?.sources || [];
      } catch (error) {
        console.error('[notion-configurator] Could not read this session:', error);
        state.sessionSources[sessionPath] = [];
      }
    }
    render();
  }

  function applyPreset(presetKey) {
    // Clicking the already-active preset is almost always an accidental
    // double click, not "reset my edits back to the preset defaults" - do
    // nothing rather than discard whatever the operator built on top of it.
    if (presetKey === state.mapping.preset) return;
    // Confirm whenever there is something real to lose - a connected
    // database, mapped columns - regardless of whether an edit happened in
    // *this* opening of the configurator. A first-click-after-open on a
    // preset button must not be able to silently wipe an already-saved,
    // working mapping just because `dirty` has not been set yet.
    if (state.mapping.targets.length && !window.confirm(
      'This replaces every current target - including any connected database - with the preset\'s defaults. Continue?',
    )) return;
    state.mapping = presetMapping(presetKey, state.nameLanguage);
    state.activeTargetId = state.mapping.targets[0]?.id || null;
    markDirty();
  }

  function addTarget() {
    const target = {
      id: `target-${Date.now().toString(36)}`,
      title: (PRESET_NAMES[state.nameLanguage] || PRESET_NAMES.en).newTarget,
      row_level: 'session',
      key_column: keyColumnDefault('session', state.nameLanguage),
      database_id: '',
      columns: {},
    };
    state.mapping.targets.push(target);
    state.mapping.preset = 'custom';
    state.activeTargetId = target.id;
    markDirty();
  }

  function removeTarget(targetId) {
    const target = state.mapping.targets.find((item) => item.id === targetId);
    if (!target) return;
    // The trash icon sits right in the target list the operator clicks to
    // switch between targets; one miss loses a whole target - its database
    // connection and every mapped column - at once.
    if (!window.confirm(`Remove target "${target.title || '(untitled)'}"? This cannot be undone here.`)) return;
    state.mapping.targets = state.mapping.targets.filter((item) => item.id !== targetId);
    if (state.activeTargetId === targetId) state.activeTargetId = state.mapping.targets[0]?.id || null;
    markDirty();
  }

  function attachDatabase(databaseId, title) {
    const target = activeTarget();
    if (!target) return;
    target.database_id = databaseId;
    if (!target.columns || !Object.keys(target.columns).length) {
      // A freshly attached, unmapped database: describe it so the operator
      // sees its real columns instead of an empty list.
      void runAction('describe_database', { database_id: databaseId }).then((response) => {
        const columns = response?.result?.columns || [];
        target.columns = autoMapColumns(columns, target);
        render();
      });
    }
    markDirty();
  }

  async function expandNode(pageId) {
    state.tree = { nodes: [], loadedFor: pageId, loading: true, error: '' };
    render();
    try {
      const response = await runAction('list_children', { page_id: pageId });
      state.tree = { nodes: response?.result?.children || [], loadedFor: pageId, loading: false, error: response?.result?.error || '' };
    } catch (error) {
      state.tree = { nodes: [], loadedFor: pageId, loading: false, error: error.message || String(error) };
    }
    render();
  }

  async function createDatabase() {
    const target = activeTarget();
    if (!target) return;
    const parentPageId = state.tree.loadedFor || state.parentPageId;
    if (!parentPageId) { showToast?.('Open a page in the tree first.', 'error'); return; }
    // Creates a real, visible database in the operator's own Notion
    // workspace - outward-facing and not something this modal can undo.
    if (!window.confirm(`Create a new Notion database named "${target.title || 'Study Runner export'}" under the opened page?`)) return;
    try {
      const response = await runAction('create_database', {
        parent_page_id: parentPageId,
        title: target.title || 'Study Runner export',
        row_level: target.row_level,
        columns_json: JSON.stringify(Object.entries(target.columns).map(([name, column]) => ({ name, type: column.type }))),
        key_column: keyColumnName(target),
      });
      const result = response?.result;
      if (!result?.ok) { showToast?.(result?.error || 'Could not create the database.', 'error'); return; }
      target.database_id = result.database_id;
      showToast?.('Database created.', 'success');
      markDirty();
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
    }
  }

  function addColumn() {
    const target = activeTarget();
    if (!target) return;
    let name = 'New column';
    let suffix = 2;
    while (Object.prototype.hasOwnProperty.call(target.columns, name)) { name = `New column ${suffix}`; suffix += 1; }
    target.columns[name] = { type: 'rich_text', source: '', reducer: null };
    markDirty();
  }

  function removeColumn(name) {
    const target = activeTarget();
    if (!target) return;
    delete target.columns[name];
    markDirty();
  }

  function updateColumn(input) {
    const target = activeTarget();
    if (!target) return;
    const columnName = input.dataset.columnName;
    const field = input.dataset.columnField;
    const column = target.columns[columnName];
    if (!column) return;
    if (field === 'name') {
      if (!input.value.trim() || input.value === columnName) return;
      const newName = input.value.trim();
      target.columns[newName] = column;
      delete target.columns[columnName];
      if (state.manualSourceColumns.delete(`${target.id}:${columnName}`)) {
        state.manualSourceColumns.add(`${target.id}:${newName}`);
      }
    } else if (field === 'reducer') {
      column.reducer = input.value || null;
    } else if (field === 'source') {
      const key = `${target.id}:${columnName}`;
      if (input.value === OTHER_SOURCE) {
        // Switching to "Other" must show the manual field even when the
        // current source already matches a catalog entry; a transient flag
        // decides that, since matching a catalog entry alone no longer can.
        state.manualSourceColumns.add(key);
        markDirty();
        return;
      }
      state.manualSourceColumns.delete(key);
      column.source = input.value;
    } else if (field === 'source-manual') {
      column.source = input.value;
    } else {
      column[field] = input.value;
    }
    markDirty();
  }

  async function runPreview() {
    if (!state.previewSessionPath) { showToast?.('Pick a session to preview with.', 'error'); return; }
    try {
      const response = await runAction('preview_mapping', {
        mapping_json: JSON.stringify(serializeMapping(state.mapping)),
        session_path: state.previewSessionPath,
      });
      const result = response?.result;
      if (!result?.ok) { showToast?.(result?.error || 'Preview failed.', 'error'); return; }
      state.preview = result.targets || [];
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
      state.preview = null;
    }
    render();
  }

  async function save() {
    try {
      await saveSettings({ export_mapping: serializeMapping(state.mapping) });
      state.dirty = false;
      showToast?.('Export mapping saved.', 'success');
      modal.close();
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
    }
  }

  render();
  modal.open();
  void loadCatalogAndSessions();
}

function renderRail(state) {
  return `
    <div class="configurator-section">
      <h3>Preset</h3>
      <label class="field">
        <span>Column names / Spaltennamen</span>
        <select data-name-language>
          <option value="de" ${state.nameLanguage === 'de' ? 'selected' : ''}>Deutsch</option>
          <option value="en" ${state.nameLanguage === 'en' ? 'selected' : ''}>English</option>
        </select>
      </label>
      <div class="configurator-presets">
        ${PRESETS.map((preset) => `
          <button class="btn-secondary${state.mapping.preset === preset.key ? ' is-active' : ''}" type="button" data-preset="${escapeHtml(preset.key)}" title="${escapeHtml(preset.description)}">
            ${escapeHtml(preset.title)}
          </button>`).join('')}
      </div>
    </div>
    <div class="configurator-section">
      <h3>Targets</h3>
      <ul class="configurator-target-list">
        ${state.mapping.targets.map((target) => `
          <li class="configurator-target-row${target.id === state.activeTargetId ? ' is-active' : ''}" data-select-target="${escapeHtml(target.id)}">
            <span>
              <strong>${escapeHtml(target.title || '(untitled)')}</strong>
              <small>${escapeHtml(target.row_level)} · ${target.database_id ? 'connected' : 'not connected'}</small>
            </span>
            <button class="icon-button" type="button" data-remove-target="${escapeHtml(target.id)}" title="Remove target"><i class="iconoir-trash"></i></button>
          </li>`).join('') || '<li class="settings-hint">No targets yet.</li>'}
      </ul>
      <button class="btn-secondary" type="button" data-add-target><i class="iconoir-plus"></i> Add target</button>
    </div>
    <div class="configurator-section">
      <h3>Notion page</h3>
      ${renderTree(state)}
    </div>
  `;
}

function renderTree(state) {
  const rootId = state.tree.loadedFor || state.parentPageId;
  if (!rootId) {
    return '<p class="settings-hint">Set a parent page id in the Notion settings first.</p>';
  }
  if (!state.tree.loadedFor) {
    return `<button class="btn-secondary" type="button" data-expand-node="${escapeHtml(rootId)}">Open parent page</button>`;
  }
  if (state.tree.loading) return '<p class="settings-hint">Loading…</p>';
  if (state.tree.error) return `<p class="settings-hint">${escapeHtml(state.tree.error)}</p>`;
  const items = state.tree.nodes.map((node) => node.type === 'database'
    ? `<li><button class="btn-secondary" type="button" data-attach-database="${escapeHtml(node.id)}" data-attach-title="${escapeHtml(node.title)}">
         <i class="iconoir-table-rows"></i> ${escapeHtml(node.title || node.id)}
       </button></li>`
    : `<li><button class="btn-secondary" type="button" data-expand-node="${escapeHtml(node.id)}">
         <i class="iconoir-page"></i> ${escapeHtml(node.title || node.id)}
       </button></li>`).join('');
  return `
    <ul class="configurator-tree">${items || '<li class="settings-hint">Nothing here yet.</li>'}</ul>
    <button class="btn-secondary" type="button" data-create-database><i class="iconoir-plus"></i> New database here</button>
  `;
}

function renderMain(state) {
  const target = state.mapping.targets.find((item) => item.id === state.activeTargetId);
  if (!target) {
    return '<p class="settings-hint">Add a target on the left to start mapping columns.</p>';
  }
  const viewMode = state.viewModeByTarget[target.id] === 'node' ? 'node' : 'list';
  return `
    <div class="configurator-target-header">
      <label class="field">
        <span>Title</span>
        <input type="text" value="${escapeHtml(target.title)}" data-target-title>
      </label>
      <label class="field">
        <span>One row per</span>
        <select data-target-row-level ${target.database_id ? 'disabled title="Fixed once a database is attached"' : ''}>
          ${ROW_LEVELS.map((level) => `<option value="${level}" ${level === target.row_level ? 'selected' : ''}>${level}</option>`).join('')}
        </select>
      </label>
      <span class="settings-hint">Key column: <strong>${escapeHtml(keyColumnName(target))}</strong></span>
      <label class="field">
        <span>View</span>
        <select data-view-mode>
          <option value="list" ${viewMode === 'list' ? 'selected' : ''}>Column list</option>
          <option value="node" ${viewMode === 'node' ? 'selected' : ''}>Node graph</option>
        </select>
      </label>
    </div>
    ${viewMode === 'node' ? renderNodeView(target) : renderColumnList(state, target)}
    ${renderPreviewSection(state, target)}
    <div class="dashboard-actions">
      <button class="btn-secondary" type="button" data-discard>Discard</button>
      <button class="btn-primary" type="button" data-save>Save mapping</button>
    </div>
  `;
}

function renderColumnList(state, target) {
  const columns = Object.entries(target.columns);
  return `
    <table class="configurator-columns">
      <thead><tr><th>Column</th><th>Type</th><th>Source</th><th>Reducer</th><th></th></tr></thead>
      <tbody>
        ${columns.map(([name, column]) => renderColumnRow(name, column, target.row_level, state, target.id)).join('')}
      </tbody>
    </table>
    <button class="btn-secondary" type="button" data-add-column><i class="iconoir-plus"></i> Add column</button>
  `;
}

function renderNodeView() {
  return `
    <div class="configurator-node-toolbar">
      <button class="btn-secondary" type="button" data-node-add-source><i class="iconoir-plus"></i> Source</button>
      <button class="btn-secondary" type="button" data-node-add-reducer><i class="iconoir-plus"></i> Reducer</button>
      <button class="btn-secondary" type="button" data-node-add-round><i class="iconoir-plus"></i> Round</button>
      <button class="btn-secondary" type="button" data-node-add-default><i class="iconoir-plus"></i> Default</button>
      <button class="btn-secondary" type="button" data-node-add-column><i class="iconoir-plus"></i> Column</button>
      <button class="btn-secondary" type="button" data-node-zoom-fit><i class="iconoir-frame"></i> Zoom to fit</button>
      <span class="settings-hint">Drag from a port to wire it. Select a node or wire and press Delete to remove it.</span>
    </div>
    <div class="configurator-node-container" data-node-container></div>
  `;
}

function renderColumnRow(name, column, rowLevel, state, targetId) {
  const needsReducer = rowLevel !== 'card' && /^card\.(?!\[)/.test(column.source || '');
  const options = [...state.catalog, ...(state.sessionSources[state.previewSessionPath] || [])];
  const known = options.some((entry) => entry.source === column.source);
  const isOther = state.manualSourceColumns.has(`${targetId}:${name}`) || (column.source && !known);
  return `
    <tr>
      <td><input type="text" value="${escapeHtml(name)}" data-column-field="name" data-column-name="${escapeHtml(name)}"></td>
      <td>
        <select data-column-field="type" data-column-name="${escapeHtml(name)}">
          ${COLUMN_TYPES.map((type) => `<option value="${type}" ${type === column.type ? 'selected' : ''}>${type}</option>`).join('')}
        </select>
      </td>
      <td>
        <select data-column-field="source" data-column-name="${escapeHtml(name)}">
          <option value="">(none)</option>
          ${options.map((entry) => `<option value="${escapeHtml(entry.source)}" ${entry.source === column.source ? 'selected' : ''}>${escapeHtml(entry.label)}</option>`).join('')}
          <option value="${OTHER_SOURCE}" ${isOther ? 'selected' : ''}>Other (type manually)…</option>
        </select>
        ${isOther || !column.source ? `<input type="text" value="${escapeHtml(column.source || '')}" placeholder="session.participant_id" data-column-field="source-manual" data-column-name="${escapeHtml(name)}">` : ''}
      </td>
      <td>
        <select data-column-field="reducer" data-column-name="${escapeHtml(name)}" ${needsReducer ? '' : 'disabled'}>
          <option value="">${needsReducer ? '(choose one)' : '—'}</option>
          ${REDUCERS.map((reducer) => `<option value="${reducer}" ${reducer === column.reducer ? 'selected' : ''}>${reducer}</option>`).join('')}
        </select>
      </td>
      <td><button class="icon-button" type="button" data-remove-column="${escapeHtml(name)}"><i class="iconoir-trash"></i></button></td>
    </tr>`;
}

function renderPreviewSection(state) {
  return `
    <div class="configurator-section">
      <h3>Preview</h3>
      <div class="configurator-preview-controls">
        <select data-preview-session-picker>
          <option value="">Pick a finished session…</option>
          ${state.sessions.map((session) => `
            <option value="${escapeHtml(session.session_path)}" ${session.session_path === state.previewSessionPath ? 'selected' : ''}>
              ${escapeHtml(session.study_id || '')} / ${escapeHtml(session.participant_id || '')} / ${escapeHtml(formatSavedAt(session.saved_at))}
            </option>`).join('')}
        </select>
        <button class="btn-secondary" type="button" data-preview-run>Preview with this session</button>
      </div>
      ${renderPreview(state.preview)}
    </div>
  `;
}

function formatSavedAt(value) {
  if (!value) return '';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return String(value);
  }
}

function renderPreview(preview) {
  if (!preview) return '';
  return preview.map((target) => `
    <div class="configurator-preview-target">
      <strong>${escapeHtml(target.title)}</strong>
      <table class="configurator-columns">
        <tbody>
          ${target.rows.map((row) => `
            <tr><td><code>${escapeHtml(row.key)}</code></td><td>${
              Object.entries(row.properties).map(([name, value]) => `${escapeHtml(name)}: ${escapeHtml(String(value ?? '—'))}`).join(' · ')
            }</td></tr>`).join('')}
        </tbody>
      </table>
    </div>`).join('');
}

/** The key column's name: the target's own, or the English default. */
function keyColumnName(target) {
  return String(target?.key_column || '').trim()
    || ({ session: 'Session ID', participant: 'Participant ID', card: 'Row Key' }[target?.row_level] || 'Key');
}

/**
 * Columns of an existing database, as a first mapping. Its title column is
 * the key column: Notion allows exactly one, and rows are matched on it.
 */
function autoMapColumns(columns, target) {
  const titleColumn = columns.find((column) => column.type === 'title');
  if (titleColumn) target.key_column = titleColumn.name;
  const key = keyColumnName(target);
  const mapped = {};
  const bySimpleName = {
    'participant id': 'session.participant_id',
    'teilnehmer-id': 'session.participant_id',
    'session id': 'session.session_id',
    'sitzungs-id': 'session.session_id',
    study: 'session.study_id',
    studie: 'session.study_id',
    start: 'session.start',
    beginn: 'session.start',
    ende: 'session.end',
    end: 'session.end',
  };
  for (const column of columns) {
    if (column.name === key) continue;
    const guess = bySimpleName[column.name.trim().toLowerCase()];
    mapped[column.name] = { type: mapToColumnType(column.type), source: guess || '', reducer: null };
  }
  return mapped;
}

function mapToColumnType(notionType) {
  if (notionType === 'number') return 'number';
  if (notionType === 'date') return 'date';
  if (notionType === 'select') return 'select';
  if (notionType === 'multi_select') return 'multi_select';
  return 'rich_text';
}

function normalizeMapping(raw) {
  const mapping = raw && typeof raw === 'object' ? raw : {};
  const targets = Array.isArray(mapping.targets) ? mapping.targets : [];
  return {
    preset: typeof mapping.preset === 'string' ? mapping.preset : 'as_before',
    targets: targets.map((target, index) => ({
      id: String(target?.id || `target-${index}`),
      title: String(target?.title || ''),
      row_level: ROW_LEVELS.includes(target?.row_level) ? target.row_level : 'session',
      key_column: String(target?.key_column || ''),
      database_id: String(target?.database_id || ''),
      columns: target?.columns && typeof target.columns === 'object' ? { ...target.columns } : {},
      relations: Array.isArray(target?.relations) ? target.relations : [],
    })),
  };
}

function serializeMapping(mapping) {
  return {
    preset: mapping.preset,
    targets: mapping.targets.map((target) => ({
      id: target.id,
      title: target.title,
      row_level: target.row_level,
      ...(target.key_column ? { key_column: target.key_column } : {}),
      database_id: target.database_id,
      columns: target.columns,
      relations: target.relations || [],
    })),
  };
}

function presetMapping(presetKey, lang = 'en') {
  const names = PRESET_NAMES[lang] || PRESET_NAMES.en;
  if (presetKey === 'simple') {
    return { preset: 'simple', targets: [{
      id: 'sessions', title: names.sessionsTitle, row_level: 'session', key_column: names.keySession, database_id: '',
      columns: {
        [names.participant]: { type: 'rich_text', source: 'session.participant_id', reducer: null },
        [names.start]: { type: 'date', source: 'session.start', reducer: null },
        [names.duration]: { type: 'number', source: 'session.duration_minutes', reducer: null },
      },
    }] };
  }
  if (presetKey === 'analysis') {
    return { preset: 'analysis', targets: [{
      id: 'answers', title: names.answersTitle, row_level: 'card', key_column: names.keyCard, database_id: '',
      columns: {
        [names.participant]: { type: 'rich_text', source: 'session.participant_id', reducer: null },
        [names.prompt]: { type: 'rich_text', source: 'card.prompt', reducer: null },
        [names.answer]: { type: 'rich_text', source: 'card.answer', reducer: null },
        [names.cardDuration]: { type: 'number', source: 'card.duration_seconds', reducer: null },
      },
    }] };
  }
  // "As before" keeps today's fixed names: existing databases must keep matching.
  if (presetKey === 'as_before') {
    return { preset: 'as_before', targets: [] };
  }
  return { preset: 'custom', targets: [] };
}
