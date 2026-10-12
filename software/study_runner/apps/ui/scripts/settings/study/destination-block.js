/**
 * One upload destination in Study settings > Data destinations.
 *
 * The same block for every destination plugin, built from its manifest
 * (destination-rules.js decides what shows where): a status line naming the
 * next missing step, the per-study credential, the editable fields, a
 * "Target" row with the plugin's own configurator and its panel actions, and
 * the read-only values in a closed "Technical details" section.
 *
 * This module only builds and updates markup. The clicks are handled in
 * study-settings-panel.js through the same data attributes as before.
 */
import { t } from '../../shared/i18n.js';
import { escapeHtml } from '../../shared/dom-utils.js';
import { fieldLabel, fieldPlaceholder, renderPluginHelpButton } from '../../shared/plugin-help.js';
import { loadPluginUiExtension } from '../../shared/plugin-catalog.js';
import { askedPayloadFields, nextDestinationStep, panelActions, splitDestinationFields } from './destination-rules.js';

export function renderDestinationBlock(plugin, selection = {}) {
  const key = String(plugin.plugin_key);
  const label = plugin.ui?.label || key;
  const enabled = selection.enabled === true;
  const settings = selection.settings || {};
  const { main, details } = splitDestinationFields(plugin);
  return `
    <div class="stimulus-toggle-row study-plugin-row destination-block${enabled ? '' : ' stimulus-toggle-row--off'}" data-study-destination="${escapeHtml(key)}">
      <div class="study-plugin-main">
        <span class="stimulus-toggle-text"><strong>${escapeHtml(label)}</strong>${plugin.ui?.description ? `<small>${escapeHtml(plugin.ui.description)}</small>` : ''}</span>
        <span class="study-plugin-controls">
          ${renderPluginHelpButton(plugin)}
          <label class="switch" aria-label="${escapeHtml(label)}"><input type="checkbox" data-plugin-enabled ${enabled ? 'checked' : ''}><span class="switch-slider"></span></label>
        </span>
      </div>
      <p class="destination-status" data-destination-status></p>
      <div class="destination-rows">
        ${renderCredentialRow(plugin)}
        ${main.map(([name, field]) => renderFieldRow(name, field, settings[name])).join('')}
        ${renderTargetRow(plugin)}
      </div>
      ${renderDetails(details, settings)}
    </div>`;
}

/** The input for one manifest-declared setting; also used for sensor fields. */
export function renderSettingControl(name, field, value) {
  const current = value ?? field.default ?? '';
  const disabled = field.read_only === true ? ' disabled' : '';
  const required = field.required === true ? ' required' : '';
  const attribute = `data-plugin-setting="${escapeHtml(name)}"`;
  if (field.type === 'boolean') {
    return `<span class="switch"><input type="checkbox" ${attribute} data-setting-type="boolean" ${current ? 'checked' : ''}${disabled}><span class="switch-slider"></span></span>`;
  }
  if (field.type === 'choice') {
    const options = (field.options || []).map((option) => (
      `<option value="${escapeHtml(option)}" ${String(option) === String(current) ? 'selected' : ''}>${escapeHtml(option)}</option>`
    )).join('');
    return `<select ${attribute} data-setting-type="choice"${disabled}${required}>${options}</select>`;
  }
  const inputType = ['number', 'url'].includes(field.type) ? field.type : 'text';
  const limits = field.type === 'number'
    ? `${field.minimum !== undefined ? ` min="${escapeHtml(field.minimum)}"` : ''}${field.maximum !== undefined ? ` max="${escapeHtml(field.maximum)}"` : ''}`
    : '';
  const example = fieldPlaceholder(field);
  const placeholder = example ? ` placeholder="${escapeHtml(example)}"` : '';
  return `<input type="${inputType}" ${attribute} data-setting-type="${escapeHtml(field.type || 'string')}" value="${escapeHtml(current)}"${limits}${placeholder}${disabled}${required}>`;
}

export function settingHint(field) {
  return field.description_key ? t(field.description_key, field.description || '') : field.description || '';
}

/**
 * Name the next missing step and highlight its control. The study start
 * check counts the same things, so a block that says "Ready" does not block
 * the start.
 */
export function updateDestinationStatus(row, plugin, saved = {}, { credentialConfigured = true } = {}) {
  const status = row.querySelector('[data-destination-status]');
  if (!status) return;
  row.querySelectorAll('.is-next-step').forEach((element) => element.classList.remove('is-next-step'));
  if (!row.querySelector('[data-plugin-enabled]')?.checked) {
    setStatus(status, 'off', t('studySettings.destinationOff', 'Off: this study does not upload here.'));
    return;
  }
  const step = nextDestinationStep(plugin, { ...saved, ...typedSettings(row) }, { credentialConfigured });
  if (step.kind === 'credential') {
    setStatus(status, 'todo', t('studySettings.destinationNeedsCredential', 'Save the key for this study first.'));
    markNextStep(row.querySelector('[data-study-plugin-credential-input]'));
    markNextStep(row.querySelector('[data-save-study-plugin-credential]'));
  } else if (step.kind === 'setting') {
    const schema = plugin.study_settings_schema || plugin.settings?.study || {};
    const message = t('studySettings.destinationNeedsSetting', '{field} is still missing.')
      .replace('{field}', fieldLabel(step.name, schema[step.name] || {}));
    setStatus(status, 'todo', message);
    const input = [...row.querySelectorAll('[data-plugin-setting]')].find((element) => element.dataset.pluginSetting === step.name);
    markNextStep(input || row.querySelector('[data-open-study-plugin-configurator]'));
  } else {
    setStatus(status, 'ready', t('studySettings.destinationReady', 'Ready to upload.'));
  }
}

/** Ask the plugin's own configurator module for its one-line summary. */
export async function fillDestinationSummary(row, plugin, saved = {}) {
  const target = row.querySelector('[data-destination-summary]');
  if (!target) return;
  try {
    const module = await loadPluginUiExtension(plugin, 'study_settings');
    const text = typeof module?.summarize === 'function' ? String(module.summarize(saved) || '') : '';
    target.textContent = text || t('studySettings.destinationNoTarget', 'No target chosen yet.');
  } catch (error) {
    console.debug('[study-settings] Destination summary unavailable:', error);
    target.textContent = '';
  }
}

function renderCredentialRow(plugin) {
  const credential = plugin.capability_config?.credentials || {};
  if (!credential.config_field || credential.per_study !== true) return '';
  const key = escapeHtml(plugin.plugin_key);
  const example = fieldPlaceholder(credential);
  const clearLabel = escapeHtml(t('studySettings.clearCredential', "Delete this study's credential"));
  return `
    <div class="destination-row" data-study-plugin-credential="${key}">
      <span class="destination-row-label">${escapeHtml(fieldLabel(credential.config_field, credential))}</span>
      <span class="destination-row-control field">
        <input type="password" autocomplete="new-password" data-study-plugin-credential-input${example ? ` placeholder="${escapeHtml(example)}"` : ''}
               title="${escapeHtml(t('studySettings.credentialPrivateHint', 'Credentials stay in the local secret store and are never written to the study file.'))}">
        <button class="btn-secondary" type="button" data-save-study-plugin-credential="${key}">
          <i class="iconoir-key"></i> ${escapeHtml(t('studySettings.saveCredentialShort', 'Save'))}
        </button>
        <button class="btn-icon-only" type="button" data-clear-study-plugin-credential="${key}" title="${clearLabel}" aria-label="${clearLabel}">
          <i class="iconoir-trash"></i>
        </button>
      </span>
      <small class="destination-row-hint" data-study-plugin-credential-state>${escapeHtml(t('studySettings.credentialLoading', 'Checking saved credential...'))}</small>
    </div>`;
}

function renderFieldRow(name, field, value) {
  const hint = settingHint(field);
  const unit = field.unit ? ` <span class="settings-unit">(${escapeHtml(field.unit)})</span>` : '';
  return `
    <label class="destination-row">
      <span class="destination-row-label">${escapeHtml(fieldLabel(name, field))}${unit}</span>
      <span class="destination-row-control field">${renderSettingControl(name, field, value)}</span>
      ${hint ? `<small class="destination-row-hint">${escapeHtml(hint)}</small>` : ''}
    </label>`;
}

function renderTargetRow(plugin) {
  const key = escapeHtml(plugin.plugin_key);
  const hasConfigurator = Boolean(plugin.ui?.extensions?.study_settings);
  const actions = panelActions(plugin);
  if (!hasConfigurator && !actions.length) return '';
  const configurator = hasConfigurator
    ? `<button class="btn-secondary" type="button" data-open-study-plugin-configurator="${key}"><i class="iconoir-settings"></i> ${escapeHtml(t('studySettings.openConfigurator', 'Configure…'))}</button>`
    : '';
  return `
    <div class="destination-row destination-row--target">
      <span class="destination-row-label">${hasConfigurator ? escapeHtml(t('studySettings.destinationTarget', 'Target')) : ''}</span>
      <div class="destination-row-control destination-target">
        ${hasConfigurator ? '<span class="destination-summary" data-destination-summary></span>' : ''}
        <div class="destination-buttons">${configurator}${actions.map((action) => renderActionButton(plugin, action)).join('')}</div>
        ${actions.map((action) => `<small class="destination-action-result" data-study-plugin-action-result="${escapeHtml(action.key)}" hidden></small>`).join('')}
      </div>
    </div>
    ${actions.map((action) => renderActionFieldRows(plugin, action)).join('')}`;
}

function renderActionButton(plugin, action) {
  const title = action.description ? ` title="${escapeHtml(action.description)}"` : '';
  // Manifests name actions in English; a locale may translate them.
  const label = t(`pluginActions.${plugin.plugin_key}.${action.key}`, action.label || humanize(action.key));
  return `
    <button class="btn-secondary${action.danger ? ' plugin-admin-action--danger' : ''}" type="button"${title}
            data-study-plugin-action="${escapeHtml(action.key)}" data-study-plugin-key="${escapeHtml(plugin.plugin_key)}">
      <i class="iconoir-ev-plug"></i> ${escapeHtml(label)}
    </button>`;
}

/** An action field nothing else answers: rare, so it gets a plain row of its own. */
function renderActionFieldRows(plugin, action) {
  return askedPayloadFields(action, plugin).map(([name, field]) => {
    const common = `data-plugin-action-field="${escapeHtml(name)}" data-action-key="${escapeHtml(action.key)}" data-setting-type="${escapeHtml(field.type || 'string')}"`;
    let control;
    if (field.type === 'boolean') {
      control = `<span class="switch"><input type="checkbox" ${common}><span class="switch-slider"></span></span>`;
    } else if (Array.isArray(field.enum) && field.enum.length) {
      control = `<select ${common}><option value=""></option>${field.enum.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('')}</select>`;
    } else {
      const numeric = ['integer', 'number'].includes(field.type);
      const step = field.type === 'integer' ? ' step="1"' : field.type === 'number' ? ' step="any"' : '';
      control = `<input type="${numeric ? 'number' : 'text'}" ${common}${step}>`;
    }
    return `
      <label class="destination-row">
        <span class="destination-row-label">${escapeHtml(humanize(name))}</span>
        <span class="destination-row-control field">${control}</span>
      </label>`;
  }).join('');
}

function renderDetails(details, settings) {
  if (!details.length) return '';
  const rows = details.map(([name, field]) => {
    const value = field.read_only === true
      ? `<code>${escapeHtml(String(settings[name] ?? '') || '–')}</code>`
      : `<span class="field">${renderSettingControl(name, field, settings[name])}</span>`;
    return `<dt>${escapeHtml(fieldLabel(name, field))}</dt><dd>${value}</dd>`;
  }).join('');
  return `
    <details class="destination-details">
      <summary>${escapeHtml(t('studySettings.technicalDetails', 'Technical details'))}</summary>
      <dl class="destination-detail-list">${rows}</dl>
    </details>`;
}

function typedSettings(row) {
  const values = {};
  row.querySelectorAll('[data-plugin-setting]').forEach((input) => {
    values[input.dataset.pluginSetting] = input.type === 'checkbox' ? input.checked : input.value;
  });
  return values;
}

function setStatus(element, state, text) {
  element.dataset.state = state;
  element.textContent = text;
}

function markNextStep(element) {
  element?.classList.add('is-next-step');
}

function humanize(value) {
  return String(value || '').replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
