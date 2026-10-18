/**
 * The Notion plugin's study-settings configurator: which tables the study
 * creates in Notion and which values go into them (see layout.py).
 *
 * Left: four steps in the order the Notion structure nests. Right: what
 * Notion will look like, a check against the real page, and a preview with
 * a recorded session. Saving stores the layout as the study's
 * `notion_layout` setting; uploads use it from then on, and "Upload again"
 * in a session's detail view applies it to sessions recorded before.
 *
 * Loaded on demand by study-settings-panel.js (ui.extensions.study_settings
 * in manifest.json). Everything here talks to the host through the
 * callbacks it is given (`runAction` for the plugin's admin actions,
 * `saveSettings` to persist), except the already-public session list
 * (`/api/admin/sessions`) for the preview.
 */
import { createModal } from '/static/scripts/shared/modal.js';
import { escapeHtml } from '/static/scripts/shared/dom-utils.js';
import { getJson } from '/static/scripts/shared/api-client.js';
import { t } from '/static/scripts/shared/i18n.js';
import { getPluginCatalog } from '/static/scripts/shared/plugin-catalog.js';
import {
  addValue,
  exportLayout,
  importLayout,
  normalizeLayout,
  removeValue,
  serializeLayout,
  setCardIncluded,
  setValueFlag,
  summaryText,
  toggleColumn,
} from './layout-model.js';
import { renderSections } from './layout-sections.js';
import { renderCheck, renderSessionPicker, renderSessionPreview, renderTreePreview } from './layout-tree-preview.js';

/** One line for the study settings panel: where this study's results go. */
export function summarize(settings = {}) {
  return summaryText(settings, {
    participants: t('notionLayout.summary.participants', 'Participants table'),
    answers: t('notionLayout.summary.answers', 'answers per participant'),
    comparison: t('notionLayout.summary.comparison', 'one comparison table per card'),
  });
}

export function openConfigurator({ plugin, studyId, settings, configData, runAction, saveSettings, showToast }) {
  const state = {
    sources: { cards: [], participant_columns: [], values: [] },
    layout: normalizeLayout(settings?.notion_layout),
    sessions: [],
    previewPath: '',
    preview: null,
    check: null,
    busy: false,
    dirty: false,
  };

  const modal = createModal({
    title: `${plugin?.ui?.label || 'Notion'} · ${t('notionLayout.title', 'tables and values')}`,
    variant: 'workspace',
    closeLabel: t('common.cancel', 'Cancel'),
    beforeClose: () => !state.dirty || window.confirm(t('notionLayout.discardConfirm', 'Discard your changes?')),
    onClose: () => modal.element.remove(),
  });

  function render() {
    modal.body.innerHTML = `
      <div class="notion-layout-main">
        ${renderSections(state.layout, state.sources)}
        <div class="notion-layout-actions">
          <div class="notion-layout-actions-group">
            <button class="btn-secondary" type="button" data-export><i class="iconoir-download"></i> ${escapeHtml(t('notionLayout.export', 'Export'))}</button>
            <label class="btn-secondary notion-layout-import"><i class="iconoir-upload"></i> ${escapeHtml(t('notionLayout.import', 'Import'))}
              <input type="file" accept="application/json,.json" data-import hidden></label>
          </div>
          <div class="notion-layout-actions-group">
            <button class="btn-secondary" type="button" data-cancel>${escapeHtml(t('common.cancel', 'Cancel'))}</button>
            <button class="btn-primary" type="button" data-save${state.busy ? ' disabled' : ''}><i class="iconoir-check"></i> ${escapeHtml(t('notionLayout.save', 'Save'))}</button>
          </div>
        </div>
      </div>
      <aside class="notion-layout-aside">
        ${renderTreePreview(state.layout, state.sources)}
        <div class="notion-layout-aside-actions">
          <button class="btn-secondary" type="button" data-check${state.busy ? ' disabled' : ''}><i class="iconoir-search"></i> ${escapeHtml(t('notionLayout.check.button', 'Check in Notion'))}</button>
          <button class="btn-secondary" type="button" data-create${state.busy ? ' disabled' : ''}><i class="iconoir-plus"></i> ${escapeHtml(t('notionLayout.create', 'Create tables now'))}</button>
        </div>
        ${renderCheck(state.check)}
        <div class="notion-layout-aside-preview">
          ${renderSessionPicker(state.sessions, state.previewPath)}
          ${renderSessionPreview(state.preview)}
        </div>
      </aside>`;
    bind();
  }

  function change(next) {
    state.layout = next;
    state.dirty = true;
    state.check = null;
    render();
  }

  function bind() {
    const body = modal.body;
    body.querySelectorAll('[data-participant-column]').forEach((input) => input.addEventListener('change', () => {
      change(toggleColumn(state.layout, input.dataset.participantColumn, input.checked));
    }));
    body.querySelector('[data-answers-toggle]')?.addEventListener('change', (event) => {
      change({ ...state.layout, answers_per_participant: event.target.checked });
    });
    body.querySelectorAll('[data-value-flag]').forEach((input) => input.addEventListener('change', () => {
      change(setValueFlag(state.layout, input.dataset.source, input.dataset.valueFlag, input.checked));
    }));
    body.querySelectorAll('[data-remove-value]').forEach((button) => button.addEventListener('click', () => {
      change(removeValue(state.layout, button.dataset.removeValue));
    }));
    body.querySelector('[data-add-value]')?.addEventListener('change', (event) => {
      if (event.target.value) change(addValue(state.layout, event.target.value));
    });
    body.querySelectorAll('[data-card]').forEach((input) => input.addEventListener('change', () => {
      const card = state.sources.cards.find((item) => item.card_id === input.dataset.card);
      if (card) change(setCardIncluded(state.layout, card, input.checked));
    }));
    body.querySelectorAll('[data-missing]').forEach((input) => input.addEventListener('change', () => {
      const value = input.dataset.missing === 'number' ? Number(input.value) : input.value;
      change({ ...state.layout, missing: { ...state.layout.missing, [input.dataset.missing]: value } });
    }));
    body.querySelector('[data-preview-session]')?.addEventListener('change', (event) => void preview(event.target.value));
    body.querySelector('[data-check]')?.addEventListener('click', () => void checkStructure(false));
    body.querySelector('[data-create]')?.addEventListener('click', () => void checkStructure(true));
    body.querySelector('[data-save]')?.addEventListener('click', () => void save());
    body.querySelector('[data-cancel]')?.addEventListener('click', () => modal.close());
    body.querySelector('[data-export]')?.addEventListener('click', downloadLayout);
    body.querySelector('[data-import]')?.addEventListener('change', (event) => void readImport(event.target.files?.[0]));
  }

  /** The study as the plugin needs it: cards and enabled sensors, no stimulus content. */
  function studyForPlugin() {
    const studySettings = configData?.study_settings || {};
    return {
      study_id: studyId,
      questions: (configData?.questions || []).map(({ type, prompt, title, subtitle, fields, card_id }) =>
        ({ type, prompt, title, subtitle, fields, card_id })),
      study_settings: {
        sensors_enabled: studySettings.sensors_enabled,
        plugins: Object.fromEntries(Object.entries(studySettings.plugins || {}).map(([key, value]) =>
          [key, { enabled: value?.enabled === true }])),
      },
      plugin_manifests: getPluginCatalog().plugins.map(({ plugin_key, capabilities, streams }) =>
        ({ plugin_key, capabilities, streams })),
    };
  }

  async function loadSources(sessionPath = '') {
    try {
      const response = await runAction('describe_layout_sources', {
        config_data_json: JSON.stringify(studyForPlugin()),
        ...(sessionPath ? { session_path: sessionPath } : {}),
      });
      const result = response?.result || response;
      if (!result?.ok) throw new Error(result?.error || 'unknown error');
      const firstLoad = !state.sources.participant_columns.length;
      state.sources = result;
      // The defaults depend on which intake fields the study stores.
      if (firstLoad && !state.dirty) state.layout = normalizeLayout(settings?.notion_layout, result);
    } catch (error) {
      console.error('[notion-layout] Could not load the study sources:', error);
      showToast?.(t('notionLayout.sourcesFailed', 'Could not read the study’s cards.'), 'error');
    }
  }

  async function loadSessions() {
    try {
      const sessions = await getJson('/api/admin/sessions');
      state.sessions = (Array.isArray(sessions) ? sessions : [])
        .filter((session) => session.session_path && session.study_id === studyId)
        .slice(0, 100);
    } catch (error) {
      console.error('[notion-layout] Could not load the session list:', error);
    }
  }

  async function preview(sessionPath) {
    state.previewPath = sessionPath;
    state.preview = null;
    if (sessionPath) {
      await loadSources(sessionPath); // adds the sensor values this session recorded
      try {
        const response = await runAction('preview_layout', {
          layout_json: JSON.stringify(serializeLayout(state.layout)),
          config_data_json: JSON.stringify(studyForPlugin()),
          session_path: sessionPath,
        });
        state.preview = response?.result || response;
      } catch (error) {
        state.preview = { ok: false, error: error.message || String(error) };
      }
    }
    render();
  }

  async function checkStructure(apply) {
    if (apply && !window.confirm(t('notionLayout.createConfirm', 'Create the missing tables and columns on your Notion page now? Nothing there is deleted.'))) return;
    state.busy = true;
    render();
    try {
      const response = await runAction('check_structure', {
        layout_json: JSON.stringify(serializeLayout(state.layout)),
        config_data_json: JSON.stringify(studyForPlugin()),
        settings_json: JSON.stringify({
          parent_page_id: settings?.parent_page_id || '',
          database_id: settings?.database_id || '',
          data_source_id: settings?.data_source_id || '',
          comparison_page_id: settings?.comparison_page_id || '',
        }),
        apply,
      });
      state.check = response?.result || response;
      const updates = state.check?.settings_updates || {};
      if (apply && Object.keys(updates).length) {
        // Remember the created tables together with the layout they belong to.
        Object.assign(settings, updates);
        await saveSettings({ ...updates, notion_layout: serializeLayout(state.layout) });
        state.dirty = false;
      }
    } catch (error) {
      state.check = { ok: false, error: error.message || String(error) };
    } finally {
      state.busy = false;
      render();
    }
  }

  async function save() {
    state.busy = true;
    render();
    try {
      await saveSettings({ notion_layout: serializeLayout(state.layout) });
      state.dirty = false;
      showToast?.(t('notionLayout.saved', 'Notion layout saved. Use “Upload again” on a session to apply it to earlier sessions.'), 'success');
      modal.close();
    } catch (error) {
      showToast?.(error.message || String(error), 'error');
      state.busy = false;
      render();
    }
  }

  function downloadLayout() {
    const blob = new Blob([JSON.stringify(exportLayout(state.layout, studyId), null, 2)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `notion-layout-${studyId || 'study'}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function readImport(file) {
    if (!file) return;
    try {
      change(normalizeLayout(importLayout(JSON.parse(await file.text()), studyId), state.sources));
    } catch (error) {
      showToast?.(t('notionLayout.importFailed', 'This file is not a Study Runner Notion layout.'), 'error');
    }
  }

  render();
  modal.open();
  void Promise.all([loadSources(), loadSessions()]).then(render);
}
