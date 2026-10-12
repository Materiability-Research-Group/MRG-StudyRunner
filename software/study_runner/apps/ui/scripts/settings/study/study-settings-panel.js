/**
 * The per-study settings shell, reachable only from the study editor.
 *
 * Everything in here is saved with the study and travels in the exported
 * .study-runner file: which sensors it uses, what the participant sees, and
 * where its results are uploaded. Machine-level settings (API keys, device
 * choice, timeouts) live in the gear hub instead - the two must not mix, or a
 * study copied to another computer would silently carry that computer's setup.
 *
 * Core panels provide only the shell. Plugin settings, credentials and admin
 * actions are generated from the catalog, so removing a bundle removes its UI
 * without leaving dormant buttons behind.
 */
import { t } from '../../shared/i18n.js';
import { byId, escapeHtml, setText } from '../../shared/dom-utils.js';
import { getJson, postJson } from '../../shared/api-client.js';
import { activateShellPanel, bindShellNav, renderShellNav } from '../../shared/settings-shell.js';
import {
  normalizePlannedSessionDurationMinutes,
  normalizeStudySettings,
} from '../../shared/study-settings.js';
import { bindMediaEditor, collectMediaEditor, renderMediaEditor } from '../../shared/media-editor.js';
import { fieldLabel, openPluginHelp } from '../../shared/plugin-help.js';
import {
  PLUGIN_UI_SURFACES,
  getPluginCatalog,
  getPluginCatalogGeneration,
  loadPluginCatalog,
  loadPluginUiExtension,
  visiblePluginsWithCapability,
} from '../../shared/plugin-catalog.js';
import {
  fillDestinationSummary,
  renderDestinationBlock,
  renderSettingControl,
  settingHint,
  updateDestinationStatus,
} from './destination-block.js';
import { buildActionPayload } from './destination-rules.js';

let callbacks = {};
let initialized = false;
let activePanel = 'sensors';
// Which catalog generation built the visible panel - see the same field in
// settings/machine/machine-settings-panel.js for why this is needed.
let renderedCatalogGeneration = 0;

export function initializeStudySettingsPanel(options = {}) {
  callbacks = options;
  if (initialized) return;
  initialized = true;

  byId('study-sensors-enabled')?.addEventListener('change', syncSensorControls);
  byId('study-plugin-sensor-options')?.addEventListener('change', (event) => {
    if (event.target?.matches('[data-plugin-enabled]')) syncSensorControls();
  });
  byId('study-plugin-sensor-options')?.addEventListener('click', (event) => {
    const remove = event.target?.closest?.('[data-remove-unavailable-plugin]');
    if (remove) void removeUnavailablePlugin(remove.dataset.removeUnavailablePlugin || '');
  });
  byId('study-plugin-destination-options')?.addEventListener('click', (event) => {
    const help = event.target?.closest?.('[data-plugin-help]');
    if (help) {
      openPluginHelp(getPluginCatalog().plugins_by_key?.[help.dataset.pluginHelp]);
      return;
    }
    const saveCredential = event.target?.closest?.('[data-save-study-plugin-credential]');
    if (saveCredential) {
      void saveStudyPluginCredential(saveCredential.dataset.saveStudyPluginCredential || '');
      return;
    }
    const clearCredential = event.target?.closest?.('[data-clear-study-plugin-credential]');
    if (clearCredential) {
      void clearStudyPluginCredential(clearCredential.dataset.clearStudyPluginCredential || '');
      return;
    }
    const action = event.target?.closest?.('[data-study-plugin-action]');
    if (action) void runStudyPluginAction(action);
    const openConfigurator = event.target?.closest?.('[data-open-study-plugin-configurator]');
    if (openConfigurator) void openStudyPluginConfigurator(openConfigurator.dataset.openStudyPluginConfigurator || '');
  });
  byId('btn-save-study-settings')?.addEventListener('click', () => void saveFromPanel());
  byId('btn-save-participant-settings')?.addEventListener('click', () => void saveFromPanel());
  byId('study-cover-enabled')?.addEventListener('change', syncCoverEditor);
  byId('btn-save-plugin-destinations')?.addEventListener('click', () => void saveDestinationPlugins());
  byId('btn-study-settings-download')?.addEventListener('click', () => callbacks.downloadCurrentStudy?.());
}

/** Open the panel, optionally jumping straight to one section. */
export function openStudySettingsPanel(panelKey) {
  if (panelKey) activePanel = panelKey;
  return callbacks.switchView?.('view-study-settings', {
    onCovered: async () => {
      // The sensor and destination lists come from the catalog, which answers
      // empty while its fetch is in flight. Cached after the first load, so
      // this is free on every later open.
      try {
        await loadPluginCatalog();
      } catch (error) {
        console.debug('[study-settings] Plugin catalog is not available yet:', error);
      }
      renderStudySettingsPanel();
    },
  });
}

/** Re-render when the catalog arrived after the panel was already built. */
export function refreshStudySettingsIfStale() {
  if (!byId('view-study-settings')?.classList.contains('active')) return;
  if (renderedCatalogGeneration === getPluginCatalogGeneration()) return;
  renderStudySettingsPanel();
}

export function renderStudySettingsPanel() {
  const nav = byId('study-settings-nav');
  const root = byId('view-study-settings');
  if (!nav || !root) return;

  renderedCatalogGeneration = getPluginCatalogGeneration();
  nav.innerHTML = renderShellNav(studySettingsEntries(), activePanel);
  activePanel = activateShellPanel(root, activePanel);
  // Switching panels only changes visibility; plugin fields are rebuilt from
  // the currently loaded study every time the shell opens.
  bindShellNav(root, (key) => {
    activePanel = key;
  });

  setText('study-settings-heading', callbacks.getCurrentStudyName?.() || t('studySettings.title', 'Study settings'));
  fillFields();
}

/** Refresh generated labels after a language switch without losing an unsaved form. */
export function translateOpenStudySettingsPanel() {
  const root = byId('view-study-settings');
  if (!root?.classList.contains('active')) return;
  const before = [...root.querySelectorAll('input, select, textarea')];
  const draft = before.map((input) => ({
    id: input.id,
    name: input.name,
    type: input.type,
    value: input.value,
    checked: input.checked,
  }));
  const focusIndex = before.indexOf(document.activeElement);
  renderStudySettingsPanel();
  const after = [...root.querySelectorAll('input, select, textarea')];
  const draftById = new Map(draft.filter((item) => item.id).map((item) => [item.id, item]));
  after.forEach((input, index) => {
    const saved = (input.id && draftById.get(input.id)) || draft[index];
    if (!saved || saved.type !== input.type || saved.name !== input.name) return;
    input.value = saved.value;
    if ('checked' in input) input.checked = saved.checked;
  });
  syncSensorControls();
  syncCoverEditor();
  after[focusIndex]?.focus();
}

function studySettingsEntries() {
  return [
    { key: 'sensors', icon: 'iconoir-activity', label: t('studySettings.navSensors', 'Sensors'), group: t('studySettings.groupRecording', 'Recording') },
    { key: 'participant', icon: 'iconoir-smartphone-device', label: t('studySettings.navParticipant', 'Participant experience'), group: t('studySettings.groupRecording', 'Recording') },
    { key: 'destinations', icon: 'iconoir-cloud-upload', label: t('studySettings.navDestinations', 'Data destinations'), group: t('studySettings.groupUploads', 'Uploads') },
    { key: 'export', icon: 'iconoir-download', label: t('studySettings.navExport', 'Export & privacy'), group: t('studySettings.groupFile', 'Study file') },
  ];
}

function fillFields() {
  const settings = normalizeStudySettings(callbacks.getStudyConfig?.().study_settings);
  const set = (id, value) => { const el = byId(id); if (el) el.checked = Boolean(value); };
  set('study-sensors-enabled', settings.sensors_enabled !== false);
  renderSensorPlugins(settings);
  renderDestinationPlugins(settings);
  set('study-progress-bar-enabled', settings.progress_bar_enabled);
  set('study-card-frame-enabled', settings.card_frame_enabled);
  const participantLanguage = byId('study-participant-language');
  if (participantLanguage) participantLanguage.value = settings.participant_language;
  fillCoverPage(settings.cover_page);
  const duration = byId('study-planned-duration');
  if (duration) duration.value = settings.planned_session_duration_minutes ?? '';
  syncSensorControls();
  void refreshStudyPluginCredentialStates();
}

function fillCoverPage(cover) {
  const enabled = byId('study-cover-enabled');
  if (enabled) enabled.checked = Boolean(cover.enabled);
  const fields = byId('study-cover-fields');
  if (fields) {
    fields.innerHTML = renderMediaEditor(cover, {
      titleLabel: t('studySettings.coverTitle', 'Headline'),
      textLabel: t('studySettings.coverText', 'Text'),
    });
    bindMediaEditor(fields);
  }
  const button = byId('study-cover-button-label');
  if (button) button.value = cover.button_label || '';
  syncCoverEditor();
}

function syncCoverEditor() {
  const editor = byId('study-cover-editor');
  if (editor) editor.hidden = !byId('study-cover-enabled')?.checked;
}

function collectCoverPage() {
  const fields = byId('study-cover-fields');
  return {
    enabled: Boolean(byId('study-cover-enabled')?.checked),
    ...(fields ? collectMediaEditor(fields) : {}),
    button_label: String(byId('study-cover-button-label')?.value || '').trim(),
  };
}

function renderDestinationPlugins(settings) {
  const container = byId('study-plugin-destination-options');
  if (!container) return;
  const plugins = visiblePluginsWithCapability(
    'upload_destination',
    PLUGIN_UI_SURFACES.DESTINATION_SETTINGS,
  );
  if (!plugins.length) {
    container.innerHTML = `<p class="settings-hint">${escapeHtml(t('studySettings.noDestinationPlugins', 'No upload destination plugins are installed.'))}</p>`;
    return;
  }
  container.innerHTML = plugins
    .map((plugin) => renderDestinationBlock(plugin, settings.plugins?.[String(plugin.plugin_key)] || {}))
    .join('');
  container.querySelectorAll('[data-plugin-enabled]').forEach((input) => {
    input.addEventListener('change', syncDestinationControls);
  });
  container.querySelectorAll('[data-plugin-setting]').forEach((input) => {
    input.addEventListener('input', () => refreshDestinationStatus(input.closest('[data-study-destination]')));
  });
  container.querySelectorAll('[data-study-destination]').forEach((row) => {
    const pluginKey = row.dataset.studyDestination;
    void fillDestinationSummary(row, getPluginCatalog().plugins_by_key[pluginKey], savedDestinationSettings(pluginKey));
  });
  syncDestinationControls();
}

function syncDestinationControls() {
  byId('study-plugin-destination-options')?.querySelectorAll('[data-study-destination]').forEach((row) => {
    const enabled = Boolean(row.querySelector('[data-plugin-enabled]')?.checked);
    row.classList.toggle('stimulus-toggle-row--off', !enabled);
    row.querySelectorAll('[data-plugin-setting]').forEach((input) => { input.disabled = !enabled; });
    refreshDestinationStatus(row);
  });
}

/** Saved settings of one destination, as the last save left them. */
function savedDestinationSettings(pluginKey) {
  return normalizeStudySettings(callbacks.getStudyConfig?.().study_settings).plugins?.[pluginKey]?.settings || {};
}

function refreshDestinationStatus(row, credentialConfigured) {
  if (!row) return;
  const plugin = getPluginCatalog().plugins_by_key[row.dataset.studyDestination];
  if (!plugin) return;
  if (credentialConfigured !== undefined) row.dataset.credentialConfigured = credentialConfigured ? 'yes' : 'no';
  updateDestinationStatus(row, plugin, savedDestinationSettings(row.dataset.studyDestination), {
    credentialConfigured: row.dataset.credentialConfigured !== 'no',
  });
}

function renderSensorPlugins(settings) {
  const container = byId('study-plugin-sensor-options');
  if (!container) return;
  const plugins = visiblePluginsWithCapability('study_sensor', PLUGIN_UI_SURFACES.STUDY_SETTINGS);
  const unavailable = unavailablePluginSelections(settings);
  if (!plugins.length && !unavailable.length) {
    container.innerHTML = `<p class="settings-hint">${escapeHtml(t('studySettings.noSensorPlugins', 'No valid sensor plugins are installed.'))}</p>`;
    return;
  }
  container.innerHTML = [
    ...plugins.map((plugin) => renderSensorPlugin(plugin, settings)),
    ...unavailable.map(([pluginKey, selection]) => renderUnavailablePlugin(pluginKey, selection)),
  ].join('');
}

function unavailablePluginSelections(settings) {
  const installed = new Set(getPluginCatalog().plugins.map((plugin) => String(plugin.plugin_key)));
  return Object.entries(settings.plugins || {})
    .filter(([pluginKey, selection]) => (
      !installed.has(pluginKey)
      && selection
      && typeof selection === 'object'
      && !Array.isArray(selection)
    ));
}

function renderUnavailablePlugin(pluginKey, selection) {
  const enabled = selection.enabled === true;
  const required = enabled && selection.required === true;
  const state = required
    ? t('studySettings.unavailableRequired', 'Required and enabled: study start is blocked')
    : enabled
      ? t('studySettings.unavailableOptional', 'Enabled but unavailable: results may be incomplete')
      : t('studySettings.unavailableDisabled', 'Unavailable and disabled');
  return `
    <div class="stimulus-toggle-row study-plugin-row study-plugin-row--unavailable" data-unavailable-plugin="${escapeHtml(pluginKey)}">
      <div class="study-plugin-main">
        <span class="stimulus-toggle-text">
          <strong>${escapeHtml(pluginKey)}</strong>
          <small>${escapeHtml(t('studySettings.pluginUnavailable', 'This study references a plugin that is not installed. Its settings and card actions are preserved unchanged.'))}</small>
          <small>${escapeHtml(state)}</small>
        </span>
        <i class="iconoir-warning-triangle" aria-hidden="true"></i>
      </div>
      <div class="dashboard-actions">
        <button class="btn-secondary" type="button" data-remove-unavailable-plugin="${escapeHtml(pluginKey)}">
          <i class="iconoir-trash"></i> ${escapeHtml(t('studySettings.removeUnavailablePlugin', 'Remove plugin configuration'))}
        </button>
      </div>
    </div>`;
}

function renderSensorPlugin(plugin, settings) {
  const key = String(plugin.plugin_key);
  const capability = plugin.capability_config?.study_sensor || {};
  const configured = settings.plugins?.[key] || {};
  const legacySelected = settings.sensors?.[key];
  const enabled = configured.enabled ?? legacySelected ?? capability.default_enabled ?? false;
  const required = configured.required ?? capability.default_required ?? true;
  const label = plugin.ui?.label || key;
  const description = plugin.ui?.description || '';
  const studySchema = plugin.study_settings_schema || plugin.settings?.study || {};
  return `
    <div class="stimulus-toggle-row study-plugin-row${enabled ? '' : ' stimulus-toggle-row--off'}" data-study-plugin="${escapeHtml(key)}">
      <div class="study-plugin-main">
        <span class="stimulus-toggle-text"><strong>${escapeHtml(label)}</strong>${description ? `<small>${escapeHtml(description)}</small>` : ''}</span>
        <label class="switch" aria-label="${escapeHtml(label)}"><input type="checkbox" data-plugin-enabled ${enabled ? 'checked' : ''}><span class="switch-slider"></span></label>
      </div>
      <div class="study-plugin-required">
        <span>${escapeHtml(t('studySettings.requiredSensor', 'Required at study start'))}</span>
        <label class="switch" aria-label="${escapeHtml(t('studySettings.requiredSensor', 'Required at study start'))}"><input type="checkbox" data-plugin-required ${required ? 'checked' : ''}><span class="switch-slider"></span></label>
      </div>
      ${renderPluginStudyFields(studySchema, configured.settings || {})}
    </div>`;
}

function renderPluginStudyFields(schema, values) {
  // An "object" field (an opaque, plugin-owned JSON document) is too shaped
  // for a generic input; the plugin's own configurator edits it.
  const entries = Object.entries(schema || {}).filter(([, field]) => field.type !== 'object');
  if (!entries.length) return '';
  return `<div class="study-plugin-fields">${entries.map(([name, field]) => {
    const label = escapeHtml(fieldLabel(name, field));
    const hint = settingHint(field);
    const control = renderSettingControl(name, field, values[name]);
    if (field.type === 'boolean') {
      return `<label class="switch-row"><span class="switch-row-text"><span>${label}</span>${hint ? `<small>${escapeHtml(hint)}</small>` : ''}</span>${control}</label>`;
    }
    const unit = field.unit ? ` <span class="settings-unit">(${escapeHtml(field.unit)})</span>` : '';
    return `<label class="field"><span>${label}${unit}</span>${control}${hint ? `<small class="settings-hint">${escapeHtml(hint)}</small>` : ''}</label>`;
  }).join('')}</div>`;
}

/**
 * A plugin declaring ui.extensions.study_settings opens its own
 * configurator module for what a generic field cannot show; the destination
 * block offers it as "Configure…" next to the target summary.
 */
async function openStudyPluginConfigurator(pluginKey) {
  const plugin = getPluginCatalog().plugins_by_key[pluginKey];
  if (!plugin) return;
  const module = await loadPluginUiExtension(plugin, 'study_settings');
  if (typeof module?.openConfigurator !== 'function') {
    callbacks.showToast?.(t('studySettings.configuratorUnavailable', 'The configurator could not be loaded.'), 'error');
    return;
  }
  const studyConfig = callbacks.getStudyConfig?.() || {};
  const current = normalizeStudySettings(studyConfig.study_settings);
  const selection = current.plugins?.[pluginKey] || {};
  module.openConfigurator({
    plugin,
    studyId: String(studyConfig.study_id || '').trim(),
    settings: selection.settings || {},
    // A plugin's own configurator may need the whole study (its questions,
    // not just this plugin's own settings) to build what it offers to map.
    configData: studyConfig,
    runAction: (actionKey, payload) => runConfiguratorAction(pluginKey, actionKey, payload),
    saveSettings: (nextSettings) => saveStudyPluginSettings(pluginKey, nextSettings),
    showToast: callbacks.showToast,
  });
}

/** A configurator's own calls, reusing the existing admin-action route. */
async function runConfiguratorAction(pluginKey, actionKey, payload = {}) {
  const studyId = String(callbacks.getStudyConfig?.().study_id || '').trim();
  return postJson(
    `/api/admin/plugins/${encodeURIComponent(pluginKey)}/actions/${encodeURIComponent(actionKey)}`,
    { ...(studyId ? { study_id: studyId } : {}), ...payload },
  );
}

/**
 * Persist only this plugin's settings (merged, not replaced) and save the
 * study. Independent of whatever is currently typed into this panel's own
 * generic destination fields, which the configurator never touches.
 */
async function saveStudyPluginSettings(pluginKey, nextSettings) {
  const current = normalizeStudySettings(callbacks.getStudyConfig?.().study_settings);
  const plugins = { ...current.plugins };
  const previous = plugins[pluginKey] || { enabled: false, required: false, settings: {} };
  plugins[pluginKey] = { ...previous, settings: { ...(previous.settings || {}), ...nextSettings } };
  callbacks.setStudySettings?.({ ...current, plugins });
  // saveStudyConfig() resolves to false - never rejects - when the server
  // refused the save (e.g. an invalid plugin setting). Without this check a
  // configurator calling this as its own save would see no error at all:
  // it would report success and close over a change that never happened.
  const saved = await callbacks.saveStudyConfig?.({ successMessage: t('studySettings.saved', 'Study settings saved') });
  if (saved === false) {
    throw new Error(t('studySettings.pluginSettingsSaveFailed', 'Could not save: the server rejected these settings.'));
  }
  renderStudySettingsPanel();
}

/**
 * Plugins answer an action with ok:false plus an error or a list of named
 * checks. Show every check, and never report a failed test as success.
 */
export function describeActionResult(details = {}) {
  const checks = Array.isArray(details.checks) ? details.checks.filter((check) => check && typeof check === 'object') : [];
  const ok = details.ok !== false && checks.every((check) => check.ok !== false);
  const failed = checks.find((check) => check.ok === false);
  const summary = ok
    ? (details.message || details.last_message || t('studySettings.pluginActionDone', 'Action completed.'))
    : (details.error || failed?.message || details.message || t('studySettings.pluginActionFailed', 'Action failed.'));
  const lines = checks.length
    ? checks.map((check) => `<span class="plugin-action-check plugin-action-check--${check.ok === false ? 'failed' : 'ok'}"><i class="iconoir-${check.ok === false ? 'xmark-circle' : 'check-circle'}"></i> ${escapeHtml(check.name ? `${check.name}: ` : '')}${escapeHtml(check.message || '')}</span>`).join('')
    : `<span class="plugin-action-check plugin-action-check--${ok ? 'ok' : 'failed'}"><i class="iconoir-${ok ? 'check-circle' : 'xmark-circle'}"></i> ${escapeHtml(summary)}</span>`;
  return { ok, summary, html: lines };
}

async function refreshStudyPluginCredentialStates() {
  const studyId = String(callbacks.getStudyConfig?.().study_id || callbacks.getCurrentStudyName?.() || '').trim();
  if (!studyId) return;
  const controls = byId('study-plugin-destination-options')?.querySelectorAll('[data-study-plugin-credential]') || [];
  if (!controls.length) return;
  try {
    const response = await getJson(`/api/admin/studies/${encodeURIComponent(studyId)}/credentials`);
    controls.forEach((control) => {
      const pluginKey = control.dataset.studyPluginCredential;
      const state = response?.credentials?.[pluginKey] || {};
      const target = control.querySelector('[data-study-plugin-credential-state]');
      if (!target) return;
      target.textContent = state.configured
        ? t('studySettings.credentialConfigured', 'Configured locally ({scope})').replace('{scope}', state.scope || state.source || 'local')
        : t('studySettings.credentialMissing', 'No credential configured');
      target.dataset.state = state.configured ? 'ok' : 'missing';
      refreshDestinationStatus(control.closest('[data-study-destination]'), Boolean(state.configured));
    });
  } catch (error) {
    console.error('[settings] Could not load study credential state:', error);
  }
}

async function saveStudyPluginCredential(pluginKey) {
  const studyId = String(callbacks.getStudyConfig?.().study_id || callbacks.getCurrentStudyName?.() || '').trim();
  const control = [...(byId('study-plugin-destination-options')?.querySelectorAll('[data-study-plugin-credential]') || [])]
    .find((item) => item.dataset.studyPluginCredential === pluginKey);
  const input = control?.querySelector('[data-study-plugin-credential-input]');
  const value = String(input?.value || '');
  if (!studyId || !value) {
    callbacks.showToast?.(t('studySettings.credentialValueRequired', 'Enter a credential before saving.'), 'error');
    return;
  }
  try {
    await postJson(`/api/admin/studies/${encodeURIComponent(studyId)}/credentials`, { [pluginKey]: value });
    if (input) input.value = '';
    await refreshStudyPluginCredentialStates();
    callbacks.showToast?.(t('studySettings.credentialSaved', 'Credential saved locally.'), 'success');
  } catch (error) {
    callbacks.showToast?.(error.message || t('studySettings.credentialSaveFailed', 'Could not save credential.'), 'error');
  }
}

async function clearStudyPluginCredential(pluginKey) {
  const studyId = String(callbacks.getStudyConfig?.().study_id || callbacks.getCurrentStudyName?.() || '').trim();
  if (!studyId) return;
  if (!window.confirm(t('studySettings.credentialClearConfirm', "Delete this study's local credential?"))) return;
  try {
    await postJson(`/api/admin/studies/${encodeURIComponent(studyId)}/credentials`, { [`clear_${pluginKey}`]: true });
    await refreshStudyPluginCredentialStates();
    callbacks.showToast?.(t('studySettings.credentialCleared', 'Credential deleted.'), 'success');
  } catch (error) {
    callbacks.showToast?.(error.message || t('studySettings.credentialClearFailed', 'Could not delete credential.'), 'error');
  }
}

async function runStudyPluginAction(button) {
  const pluginKey = String(button.dataset.studyPluginKey || '');
  const actionKey = String(button.dataset.studyPluginAction || '');
  const plugin = getPluginCatalog().plugins_by_key[pluginKey];
  const action = (plugin?.capability_config?.admin_actions?.actions || [])
    .find((candidate) => candidate.key === actionKey);
  if (!plugin || !action) return;
  if (action.confirm && !window.confirm(action.description || action.label || actionKey)) return;

  const row = button.closest('[data-study-destination]');
  const inputs = {};
  row?.querySelectorAll('[data-plugin-setting]').forEach((input) => {
    inputs[input.dataset.pluginSetting] = input.type === 'checkbox' ? input.checked : input.value;
  });
  row?.querySelectorAll(`[data-plugin-action-field][data-action-key="${CSS.escape(actionKey)}"]`).forEach((input) => {
    inputs[input.dataset.pluginActionField] = input.type === 'checkbox' ? input.checked : input.value;
  });
  // The study id lets a plugin use the credential stored for this study; a
  // key typed but not yet saved lets the operator test it before saving.
  const payload = buildActionPayload(action, plugin, {
    studyId: String(callbacks.getStudyConfig?.().study_id || '').trim(),
    inputs,
    saved: savedDestinationSettings(pluginKey),
    credentialValue: String(row?.querySelector('[data-study-plugin-credential-input]')?.value || ''),
  });

  const result = row?.querySelector(`[data-study-plugin-action-result="${CSS.escape(actionKey)}"]`);
  if (result) result.hidden = false;
  button.disabled = true;
  try {
    const response = await postJson(
      `/api/admin/plugins/${encodeURIComponent(pluginKey)}/actions/${encodeURIComponent(actionKey)}`,
      payload,
    );
    const details = response?.result || {};
    const outcome = describeActionResult(details);
    if (result) {
      result.innerHTML = outcome.html;
      result.classList.toggle('plugin-action-result--failed', !outcome.ok);
    }
    callbacks.showToast?.(outcome.summary, outcome.ok ? 'success' : 'error');
  } catch (error) {
    if (result) result.textContent = error.message || t('studySettings.pluginActionFailed', 'Action failed.');
    callbacks.showToast?.(error.message || t('studySettings.pluginActionFailed', 'Action failed.'), 'error');
  } finally {
    button.disabled = false;
  }
}

async function removeUnavailablePlugin(pluginKey) {
  if (!pluginKey) return;
  const message = t('studySettings.removeUnavailableConfirm', 'Remove the saved configuration for {plugin}? This cannot be restored unless you undo the study file change.')
    .replace('{plugin}', pluginKey);
  if (!window.confirm(message)) return;
  const current = normalizeStudySettings(callbacks.getStudyConfig?.().study_settings);
  const plugins = { ...current.plugins };
  const sensors = { ...current.sensors };
  delete plugins[pluginKey];
  delete sensors[pluginKey];
  callbacks.setStudySettings?.({ ...current, plugins, sensors });
  await callbacks.saveStudyConfig?.({ successMessage: t('studySettings.pluginConfigurationRemoved', 'Plugin configuration removed.') });
  renderStudySettingsPanel();
}

function syncSensorControls() {
  const enabled = Boolean(byId('study-sensors-enabled')?.checked);
  byId('study-plugin-sensor-options')?.querySelectorAll('[data-study-plugin]').forEach((row) => {
    const pluginEnabled = enabled && Boolean(row.querySelector('[data-plugin-enabled]')?.checked);
    row.classList.toggle('stimulus-toggle-row--off', !pluginEnabled);
    row.querySelectorAll('input, select').forEach((input) => {
      input.disabled = !enabled || (input.matches('[data-plugin-required], [data-plugin-setting]') && !pluginEnabled);
    });
  });
  byId('study-sensor-options')?.classList.toggle('is-disabled', !enabled);
}

/** Collect the fields this module owns and hand the save to the editor. */
async function saveFromPanel() {
  const durationInput = byId('study-planned-duration');
  const durationRaw = String(durationInput?.value ?? '').trim();
  const plannedDuration = normalizePlannedSessionDurationMinutes(durationRaw);
  if (durationRaw && plannedDuration === null) {
    callbacks.showToast?.(
      t('studySettings.plannedDurationInvalid', 'Enter a positive number of minutes.'),
      'error',
    );
    durationInput?.focus();
    return;
  }
  const sensorsEnabled = Boolean(byId('study-sensors-enabled')?.checked);
  const current = normalizeStudySettings(callbacks.getStudyConfig?.().study_settings);
  const plugins = { ...current.plugins };
  // Preserve legacy sensor selections for unavailable plugins. Their canonical
  // plugin entry is also retained below; neither is removable as a side effect
  // of saving a completely different installed sensor.
  const sensors = { ...current.sensors };
  byId('study-plugin-sensor-options')?.querySelectorAll('[data-study-plugin]').forEach((row) => {
    const pluginKey = row.dataset.studyPlugin;
    const enabled = sensorsEnabled && Boolean(row.querySelector('[data-plugin-enabled]')?.checked);
    const previous = plugins[pluginKey] || {};
    const pluginSettings = { ...(previous.settings || {}) };
    row.querySelectorAll('[data-plugin-setting]').forEach((input) => {
      const name = input.dataset.pluginSetting;
      const type = input.dataset.settingType;
      pluginSettings[name] = type === 'boolean'
        ? Boolean(input.checked)
        : type === 'number'
          ? Number(input.value)
          : input.value;
    });
    sensors[pluginKey] = enabled;
    plugins[pluginKey] = {
      enabled,
      required: enabled && Boolean(row.querySelector('[data-plugin-required]')?.checked),
      settings: pluginSettings,
    };
  });
  // Spread the existing settings first: this panel edits sensors and the
  // progress bar, and must never drop the upload settings the other panels own.
  callbacks.setStudySettings?.({
    ...current,
    sensors_enabled: sensorsEnabled,
    sensors,
    plugins,
    progress_bar_enabled: Boolean(byId('study-progress-bar-enabled')?.checked),
    card_frame_enabled: Boolean(byId('study-card-frame-enabled')?.checked),
    participant_language: byId('study-participant-language')?.value || 'en',
    planned_session_duration_minutes: plannedDuration,
    cover_page: collectCoverPage(),
  });
  await callbacks.saveStudyConfig?.({
    successMessage: t('studySettings.saved', 'Study settings saved'),
  });
}

async function saveDestinationPlugins() {
  const current = normalizeStudySettings(callbacks.getStudyConfig?.().study_settings);
  const plugins = { ...current.plugins };
  byId('study-plugin-destination-options')?.querySelectorAll('[data-study-destination]').forEach((row) => {
    const pluginKey = row.dataset.studyDestination;
    const previous = plugins[pluginKey] || {};
    const pluginSettings = { ...(previous.settings || {}) };
    row.querySelectorAll('[data-plugin-setting]').forEach((input) => {
      const type = input.dataset.settingType;
      pluginSettings[input.dataset.pluginSetting] = type === 'boolean'
        ? Boolean(input.checked)
        : type === 'number'
          ? Number(input.value)
          : input.value;
    });
    plugins[pluginKey] = {
      enabled: Boolean(row.querySelector('[data-plugin-enabled]')?.checked),
      required: false,
      settings: pluginSettings,
    };
  });
  callbacks.setStudySettings?.({ ...current, plugins });
  await callbacks.saveStudyConfig?.({ successMessage: t('studySettings.saved', 'Study settings saved') });
}
