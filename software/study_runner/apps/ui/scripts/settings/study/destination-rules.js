/**
 * What the study settings panel shows of an upload destination plugin, and
 * what the operator still has to do before it can upload.
 *
 * Pure functions over a plugin's catalog entry, so one set of tests holds
 * every destination to the same rules:
 * - an action marked "surface": "configurator" belongs to the plugin's own
 *   configurator and never appears as a raw form;
 * - an action never asks for a value the study settings, the credential or
 *   the machine settings already answer (timeouts live in the gear hub);
 * - read-only and "advanced" fields leave the main area for "Technical
 *   details".
 */

/** The actions the panel offers as buttons. */
export function panelActions(plugin) {
  const actions = plugin?.capability_config?.admin_actions?.actions || [];
  return actions.filter((action) => (action?.surface || 'panel') === 'panel');
}

/** The payload fields an action still asks for in the panel. */
export function askedPayloadFields(action, plugin) {
  const answered = new Set(['study_id', ...Object.keys(studySchema(plugin)), ...Object.keys(machineSchema(plugin))]);
  const credentialField = String(plugin?.capability_config?.credentials?.config_field || '');
  if (credentialField) answered.add(credentialField);
  return Object.entries(action?.payload_schema || {}).filter(([name]) => !answered.has(name));
}

/** Editable fields for the main area; read-only and advanced ones for the details. */
export function splitDestinationFields(plugin) {
  const main = [];
  const details = [];
  Object.entries(studySchema(plugin)).forEach(([name, field]) => {
    // An "object" field is too shaped for a generic input; the plugin's
    // own configurator edits it.
    if (!field || typeof field !== 'object' || field.type === 'object') return;
    (isDetailField(field) ? details : main).push([name, field]);
  });
  return { main, details };
}

/**
 * The first thing missing before this destination can upload. It mirrors
 * the study start check (runtime_core/studies/study_readiness_service.py):
 * the credential, then at least one of the required settings.
 */
export function nextDestinationStep(plugin, settings = {}, { credentialConfigured = true } = {}) {
  const requirements = plugin?.capability_config?.readiness_requirements || {};
  if (requirements.requires_secret && !credentialConfigured) return { kind: 'credential' };
  const required = Array.isArray(requirements.requires_settings) ? requirements.requires_settings : [];
  if (required.length && !required.some((name) => String(settings?.[name] ?? '').trim())) {
    const schema = studySchema(plugin);
    const editable = required.find((name) => schema[name] && !isDetailField(schema[name]));
    return { kind: 'setting', name: editable || required[0] };
  }
  return { kind: 'ready' };
}

/**
 * The payload for one panel action: what the operator typed first, then
 * what the study already saved (a read-only field has no input). Machine
 * settings are never sent; the plugin applies its own.
 */
export function buildActionPayload(action, plugin, {
  studyId = '', inputs = {}, saved = {}, credentialValue = '',
} = {}) {
  const credentialField = String(plugin?.capability_config?.credentials?.config_field || '');
  const studyFields = studySchema(plugin);
  const payload = {};
  Object.entries(action?.payload_schema || {}).forEach(([name, field]) => {
    if (name === 'study_id') {
      if (studyId) payload.study_id = studyId;
      return;
    }
    let raw;
    if (Object.prototype.hasOwnProperty.call(inputs, name)) raw = inputs[name];
    else if (credentialField && name === credentialField) raw = credentialValue;
    else if (Object.prototype.hasOwnProperty.call(studyFields, name)) raw = saved?.[name];
    if (raw === undefined || raw === null) return;
    const type = field?.type || 'string';
    if (type === 'boolean') {
      payload[name] = Boolean(raw);
      return;
    }
    const text = String(raw);
    if (text === '' && field?.required !== true) return;
    payload[name] = type === 'integer' ? Number.parseInt(text, 10) : type === 'number' ? Number(text) : text;
  });
  return payload;
}

function isDetailField(field) {
  return field.read_only === true || field.advanced === true;
}

function studySchema(plugin) {
  return plugin?.study_settings_schema || plugin?.settings?.study || {};
}

function machineSchema(plugin) {
  return plugin?.settings?.machine || plugin?.runtime_settings || {};
}
