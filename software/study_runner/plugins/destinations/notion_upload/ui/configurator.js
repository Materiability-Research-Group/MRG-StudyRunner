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
import { getJson, postJson } from '/static/scripts/shared/api-client.js';
import { getLanguage, t } from '/static/scripts/shared/i18n.js';
import { getPluginCatalog } from '/static/scripts/shared/plugin-catalog.js';
import { mountNodeView } from './configurator-node-view.js';
import { renderTargetLinks, bindTargetLinks, mountTargetGraph } from './configurator-target-links.js';
import { appendPresetTarget, exportConfiguration, importConfiguration, downloadConfiguration } from './configurator-transfer.js';

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
    studyId,
    mapping: normalizeMapping(settings?.export_mapping),
    parentPageId: String(settings?.parent_page_id || '').trim(),
    activeTargetId: null,
    tree: { nodes: [], loadedFor: '', loading: false, error: '' },
    preview: null,
    dirty: false,
    nameLanguage: defaultNameLanguage(),
    catalog: [], // declared sources from this study and its enabled plugins
    sessions: [], // finished sessions from this study for value previews
    sessionSources: {}, // session_path -> sensor sources actually present there
    previewSessionPath: '',
    structure: null,
    schemaChanges: null,
    importDraft: null,
    backfill: null,
    legacyDatabaseIds: [settings?.database_id, settings?.sessions_database_id].filter(Boolean),
    relationMode: 'list',
    viewModeByTarget: {}, // target id -> 'list' | 'node'
    manualSourceColumns: new Set(), // "target.id:column name" currently typed by hand
  };
  state.activeTargetId = state.mapping.targets[0]?.id || null;

  let activeNodeView = null;
  let activeTargetGraph = null;

  const modal = createModal({
    title: `${plugin?.ui?.label || 'Notion'} ${t('notionConfigurator.title', 'export mapping')}`,
    variant: 'workspace',
    closeLabel: t('notionConfigurator.close', 'Close'),
    beforeClose: () => !state.dirty || window.confirm(t('notionConfigurator.discardConfirm', 'Discard every unsaved change?')),
    // Each open builds a fresh modal; remove it on close instead of leaving
    // hidden copies behind in the page.
    onClose: () => { activeNodeView?.destroy(); activeTargetGraph?.destroy(); modal.element.remove(); },
  });
  modal.body.addEventListener('input', (event) => {
    if (event.target.matches('input:not([type=file]), textarea')) {
      state.dirty = true;
      updateDirtyIndicator();
    }
  });

  function render() {
    // The node canvas owns a few window-level listeners (dragging must keep
    // tracking outside it); rebuilding the DOM below does not remove those,
    // so they are torn down explicitly before every rebuild, not only on
    // close - render() runs on nearly every edit while mounted.
    activeNodeView?.destroy();
    activeTargetGraph?.destroy();
    activeNodeView = null;
    activeTargetGraph = null;
    modal.body.innerHTML = `
      <div class="configurator-rail">${renderRail(state)}</div>
      <div class="configurator-main">${renderMain(state)}</div>
    `;
    bind();
    const target = activeTarget();
    if (target && state.viewModeByTarget[target.id] === 'node') mountNode(target);
    if (state.relationMode === 'node') {
      const container = modal.body.querySelector('[data-target-graph]');
      if (container) activeTargetGraph = mountTargetGraph(container, state.mapping, () => {
        state.dirty = true;
        updateDirtyIndicator();
        queueMicrotask(render);
      });
    }
  }

  /** Declared study sources plus values observed in the selected run. */
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
    main?.querySelector('[data-check-mapping]')?.addEventListener('click', () => void checkMapping());
    main?.querySelector('[data-prepare-schema]')?.addEventListener('click', () => void prepareSchema());
    main?.querySelector('[data-backfill-preview]')?.addEventListener('click', () => void backfill(false));
    main?.querySelector('[data-backfill-confirm]')?.addEventListener('click', () => void backfill(true));
    main?.querySelector('[data-relation-mode]')?.addEventListener('change', (event) => {
      state.relationMode = event.target.value;
      render();
    });
    rail?.querySelector('[data-parent-page-id]')?.addEventListener('change', (event) => {
      state.parentPageId = event.target.value.trim();
      markDirty();
    });
    if (activeTarget()) bindTargetLinks(main, state.mapping, activeTarget(), markDirty);
    rail?.querySelector('[data-export-config]')?.addEventListener('click', () => {
      downloadConfiguration(exportConfiguration(studyId, state.parentPageId, serializeMapping(state.mapping)),
        `notion-${studyId || 'study'}-mapping.json`);
    });
    rail?.querySelector('[data-import-config]')?.addEventListener('click', () => rail.querySelector('[data-import-file]')?.click());
    rail?.querySelector('[data-import-file]')?.addEventListener('change', (event) => void readImport(event.target.files?.[0]));
    rail?.querySelector('[data-import-keep]')?.addEventListener('click', () => void applyImport(true));
    rail?.querySelector('[data-import-remove]')?.addEventListener('click', () => void applyImport(false));
    main?.querySelector('[data-save]')?.addEventListener('click', () => void save());
    main?.querySelector('[data-discard]')?.addEventListener('click', () => {
      // Sits right next to "Save mapping"; a misclick between the two must
      // not silently throw away unsaved work.
      if (state.dirty && !window.confirm(t('notionConfigurator.discardConfirm', 'Discard every unsaved change?'))) return;
      state.dirty = false;
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

  function currentConfig() {
    const settings = configData?.study_settings || {};
    return {
      study_id: studyId,
      questions: (configData?.questions || []).map(({ type, prompt, title, subtitle, fields, card_id }) =>
        ({ type, prompt, title, subtitle, fields, card_id })),
      study_settings: {
        sensors_enabled: settings.sensors_enabled,
        plugins: Object.fromEntries(Object.entries(settings.plugins || {}).map(([key, value]) =>
          [key, { enabled: value?.enabled === true }])),
      },
      plugin_manifests: getPluginCatalog().plugins.map(({ plugin_key, capabilities, streams }) =>
        ({ plugin_key, capabilities, streams })),
    };
  }

  async function loadCatalogAndSessions() {
    try {
      // Keep stimulus content out of the admin payload while including the
      // enabled plugin manifest data needed to list sensor channels.
      const minimalConfig = currentConfig();
      const response = await runAction('describe_output_catalog', { config_data_json: JSON.stringify(minimalConfig) });
      state.catalog = response?.result?.sources || [];
    } catch (error) {
      console.error('[notion-configurator] Could not load the output catalog:', error);
    }
    try {
      const sessions = await getJson('/api/admin/sessions');
      const all = (Array.isArray(sessions) ? sessions : []).filter((session) => session.session_path);
      state.sessions = all.filter((session) => session.study_id === studyId).slice(0, 100);
    } catch (error) {
      console.error('[notion-configurator] Could not load the session list:', error);
    }
    render();
  }

  async function selectPreviewSession(sessionPath) {
    if (sessionPath && !state.sessions.some((session) => session.session_path === sessionPath)) {
      showToast?.(t('notionConfigurator.wrongStudy', 'This session does not belong to the current study.'), 'error');
      return;
    }
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
    const fresh = presetMapping(presetKey, state.nameLanguage).targets[0];
    if (!fresh) return;
    state.activeTargetId = appendPresetTarget(state.mapping, fresh);
    markDirty();
  }

  function addTarget() {
    const target = {
      id: `target-${crypto.randomUUID()}`,
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
    for (const item of state.mapping.targets) {
      item.relations = (item.relations || []).filter((relation) => relation.target_id !== targetId);
      item.aggregates = (item.aggregates || []).filter((aggregate) => aggregate.target_id !== targetId);
    }
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
        void autoPrepareWhenReady();
      });
    }
    markDirty();
    void autoPrepareWhenReady();
  }

  async function autoPrepareWhenReady() {
    if (state.mapping.targets.length && state.mapping.targets.every((item) =>
      item.database_id && Object.keys(item.columns || {}).length)) {
      await prepareSchema();
    }
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
      void autoPrepareWhenReady();
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

  async function checkMapping() {
    try {
      const response = await runAction('check_mapping', {
        mapping_json: JSON.stringify(serializeMapping(state.mapping)),
        config_data_json: JSON.stringify(currentConfig()),
      });
      state.structure = response?.result || response;
    } catch (error) {
      state.structure = { ok: false, error: error.message || String(error) };
    }
    render();
    return state.structure?.ok;
  }

  async function prepareSchema() {
    if (!await checkMapping()) return;
    try {
      const payload = { mapping_json: JSON.stringify(serializeMapping(state.mapping)) };
      const response = await runAction('prepare_schema', { ...payload, apply: false });
      const result = response?.result || response;
      if (!result.ok) { showToast?.(result.error, 'error'); return; }
      state.schemaChanges = result.changes || [];
      render();
      if (!state.schemaChanges.length) return;
      const summary = state.schemaChanges.map((change) => `${change.target}: ${change.property} (${change.type})`).join('\n');
      if (!window.confirm(`Add these Notion properties?\n\n${summary}`)) return;
      const applied = await runAction('prepare_schema', { ...payload, apply: true });
      const finalResult = applied?.result || applied;
      if (!finalResult.ok) throw new Error(finalResult.error);
      state.schemaChanges = [];
      showToast?.('Notion properties prepared.', 'success');
      render();
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
    }
  }

  async function readImport(file) {
    if (!file) return;
    try {
      state.importDraft = JSON.parse(await file.text());
      importConfiguration(state.importDraft, studyId);
      render();
    } catch (error) {
      state.importDraft = null;
      const code = error instanceof SyntaxError ? 'invalidFile' : error.code;
      showToast?.(t(`notionConfigurator.importError.${code}`, error.message || String(error)), 'error');
    }
  }

  async function backfill(confirm) {
    if (state.dirty) { showToast?.(t('notionConfigurator.saveBeforeBackfill', 'Save the mapping before backfill.'), 'error'); return; }
    if (confirm && !window.confirm(`Queue ${state.backfill?.sessions?.length || 0} saved sessions for Notion backfill?`)) return;
    try {
      const response = await postJson('/api/admin/notion/backfill', { study_id: studyId, confirm });
      if (!response.ok) throw new Error(response.error || 'Backfill failed.');
      state.backfill = response;
      if (confirm) showToast?.(`${response.jobs?.length || 0} upload jobs queued.`, 'success');
      render();
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
    }
  }

  async function applyImport(keepIds) {
    try {
      const imported = importConfiguration(state.importDraft, studyId, { keepIds });
      const response = await runAction('check_mapping', {
        mapping_json: JSON.stringify(imported.mapping),
        config_data_json: JSON.stringify(currentConfig()),
      });
      const result = response?.result || response;
      if (!result.ok) throw new Error(result.error);
      const targets = imported.mapping.targets.map((item) => `${item.title} (${item.row_level})`).join('\n');
      if (!window.confirm(`Import these targets and parent page?\n\n${targets || '(legacy mode)'}\n\nParent page: ${imported.parentPageId || '(reconnect)'}`)) return;
      state.mapping = normalizeMapping(imported.mapping);
      state.parentPageId = imported.parentPageId;
      state.activeTargetId = state.mapping.targets[0]?.id || null;
      state.importDraft = null;
      markDirty();
    } catch (error) {
      showToast?.(t(`notionConfigurator.importError.${error.code}`, error.message || String(error)), 'error');
    }
  }

  async function save() {
    try {
      if (!await checkMapping()) { showToast?.(state.structure?.error || 'Invalid mapping.', 'error'); return; }
      if (state.legacyDatabaseIds.length && state.mapping.targets.length && !settings?.export_mapping?.targets?.length) {
        if (!window.confirm(`Switch this study from legacy uploads to configurable targets? Legacy databases will stop receiving new rows:\n${state.legacyDatabaseIds.join('\n')}`)) return;
      }
      await saveSettings({ parent_page_id: state.parentPageId, export_mapping: serializeMapping(state.mapping) });
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
      <h3>${t('notionConfigurator.presets', 'Presets')}</h3>
      <label class="field">
        <span>Column names / Spaltennamen</span>
        <select data-name-language>
          <option value="de" ${state.nameLanguage === 'de' ? 'selected' : ''}>Deutsch</option>
          <option value="en" ${state.nameLanguage === 'en' ? 'selected' : ''}>English</option>
        </select>
      </label>
      <div class="configurator-presets">
        ${PRESETS.map((preset) => `
          <button class="btn-secondary" type="button" data-preset="${escapeHtml(preset.key)}" title="${escapeHtml(preset.description)}">
            ${escapeHtml(preset.title)}
          </button>`).join('')}
      </div>
    </div>
    <div class="configurator-section">
      <h3>${t('notionConfigurator.targets', 'Targets')}</h3>
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
      <button class="btn-secondary" type="button" data-add-target><i class="iconoir-plus"></i> ${t('notionConfigurator.addTarget', 'Add target')}</button>
    </div>
    <div class="configurator-section">
      <h3>${t('notionConfigurator.notionPage', 'Notion page')}</h3>
      <label class="field"><span>${t('notionConfigurator.parentPage', 'Parent page ID')}</span><input data-parent-page-id value="${escapeHtml(state.parentPageId)}"></label>
      ${renderTree(state)}
    </div>
    <div class="configurator-section">
      <h3>${t('notionConfigurator.transfer', 'Transfer')}</h3>
      <button class="btn-secondary" type="button" data-export-config>${t('notionConfigurator.export', 'Export JSON')}</button>
      <button class="btn-secondary" type="button" data-import-config>${t('notionConfigurator.import', 'Import JSON')}</button>
      <input type="file" accept="application/json,.json" data-import-file hidden>
      ${state.importDraft ? `<p class="settings-hint">From study ${escapeHtml(state.importDraft.study_id)}: ${state.importDraft.export_mapping.targets.length} targets.</p>
        ${state.importDraft.study_id === state.studyId ? `<button class="btn-secondary" type="button" data-import-keep>${t('notionConfigurator.keepIds', 'Keep Notion IDs')}</button>` : ''}
        <button class="btn-secondary" type="button" data-import-remove>${t('notionConfigurator.removeIds', 'Remove Notion IDs and reconnect')}</button>` : ''}
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
    return `<p class="settings-hint">${t('notionConfigurator.addTargetHint', 'Add a target on the left to start mapping columns. Existing legacy studies continue to use their original databases.')}</p>
      <div class="dashboard-actions"><button class="btn-primary" type="button" data-save>${t('notionConfigurator.save', 'Save mapping')}</button></div>`;
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
    <div class="configurator-section">
      <h3>${t('notionConfigurator.connections', 'Target connections')}</h3>
      <label class="field"><span>View</span><select data-relation-mode>
        <option value="list" ${state.relationMode === 'list' ? 'selected' : ''}>List</option>
        <option value="node" ${state.relationMode === 'node' ? 'selected' : ''}>Node graph</option>
      </select></label>
      ${state.relationMode === 'node' ? '<div class="configurator-node-container" data-target-graph></div>' : ''}
      ${renderTargetLinks(state.mapping, target)}
    </div>
    ${renderPreviewSection(state, target)}
    <div class="configurator-section">
      <h3>${t('notionConfigurator.setup', 'Notion setup')}</h3>
      <button class="btn-secondary" type="button" data-prepare-schema>${t('notionConfigurator.reviewProperties', 'Review required properties')}</button>
      ${state.schemaChanges ? `<p class="settings-hint">${state.schemaChanges.length} properties to add.</p>` : ''}
    </div>
    <div class="configurator-section">
      <h3>${t('notionConfigurator.historicalUpload', 'Historical upload')}</h3>
      <button class="btn-secondary" type="button" data-backfill-preview>${t('notionConfigurator.reviewSessions', 'Review saved sessions')}</button>
      ${state.backfill ? `<p class="settings-hint">${state.backfill.sessions?.length || 0} eligible sessions; ${state.backfill.unresolved_cards?.length || 0} unresolved cards.</p>
        ${(state.backfill.unresolved_cards || []).map((card) => `<p class="settings-hint">${escapeHtml(card.session_path)} / card ${escapeHtml(card.question_index)}: ${escapeHtml(card.prompt)}</p>`).join('')}
        ${state.backfill.sessions?.length ? `<button class="btn-secondary" type="button" data-backfill-confirm>${t('notionConfigurator.queueBackfill', 'Queue backfill')}</button>` : ''}` : ''}
    </div>
    <div class="dashboard-actions">
      <button class="btn-secondary" type="button" data-discard>${t('notionConfigurator.discard', 'Discard')}</button>
      <button class="btn-primary" type="button" data-save>${t('notionConfigurator.save', 'Save mapping')}</button>
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
      <h3>${t('notionConfigurator.preview', 'Preview')}</h3>
      <div class="configurator-preview-controls">
        <select data-preview-session-picker>
          <option value="">Pick a finished session…</option>
          ${state.sessions.map((session) => `
            <option value="${escapeHtml(session.session_path)}" ${session.session_path === state.previewSessionPath ? 'selected' : ''}>
              ${escapeHtml(session.study_id || '')} / ${escapeHtml(session.participant_id || '')} / ${escapeHtml(formatSavedAt(session.saved_at))}
            </option>`).join('')}
        </select>
        <button class="btn-secondary" type="button" data-preview-run>${t('notionConfigurator.previewSession', 'Preview with this session')}</button>
        <button class="btn-secondary" type="button" data-check-mapping>${t('notionConfigurator.checkSources', 'Check sources and types')}</button>
      </div>
      ${state.structure ? `<p class="settings-hint">${state.structure.ok
        ? `Mapping valid. ${state.structure.sources || 0} study sources; unavailable: ${(state.structure.unavailable_sources || []).map(escapeHtml).join(', ') || 'none'}; type mismatches: ${(state.structure.type_mismatches || []).map(escapeHtml).join(', ') || 'none'}.`
        : escapeHtml(state.structure.error || 'Invalid mapping.')}</p>` : ''}
      ${!state.sessions.length ? `<p class="settings-hint">${t('notionConfigurator.emptyStudy', 'No finished session for this study. Source and type checks still work.')}</p>` : ''}
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
      aggregates: Array.isArray(target?.aggregates) ? target.aggregates : [],
      comparison_views: target?.comparison_views === true,
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
      aggregates: target.aggregates || [],
      comparison_views: target.comparison_views === true,
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
