/** Portable Notion study mapping files contain no credentials. */
export const TRANSFER_VERSION = 1;

function transferError(code, message) {
  return Object.assign(new Error(message), { code });
}

export function appendPresetTarget(mapping, presetTarget, id = `target-${crypto.randomUUID()}`) {
  if (!presetTarget || mapping.targets.some((target) => target.id === id)) {
    throw transferError('duplicateTarget', 'Preset target needs a unique ID.');
  }
  mapping.targets.push({ ...structuredClone(presetTarget), id });
  mapping.preset = 'custom';
  return id;
}

export function exportConfiguration(studyId, parentPageId, mapping) {
  return {
    format: 'study-runner-notion-mapping', version: TRANSFER_VERSION,
    study_id: studyId, parent_page_id: parentPageId,
    export_mapping: structuredClone(mapping),
  };
}

export function importConfiguration(input, studyId, { keepIds = false } = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || input.format !== 'study-runner-notion-mapping' || input.version !== TRANSFER_VERSION
      || typeof input.study_id !== 'string' || typeof input.parent_page_id !== 'string'
      || !input.export_mapping || typeof input.export_mapping !== 'object'
      || !Array.isArray(input.export_mapping.targets)
      || input.export_mapping.targets.some((target) => !target || typeof target !== 'object'
        || typeof target.id !== 'string' || typeof target.title !== 'string'
        || typeof target.row_level !== 'string' || !target.columns || typeof target.columns !== 'object'
        || (target.relations !== undefined && !Array.isArray(target.relations))
        || (target.aggregates !== undefined && !Array.isArray(target.aggregates)))) {
    throw transferError('invalidFile', 'Invalid or unsupported Notion configuration file.');
  }
  if (Object.keys(input).some((key) => !['format', 'version', 'study_id', 'parent_page_id', 'export_mapping'].includes(key))) {
    throw transferError('unsupportedFields', 'The configuration file contains unsupported fields.');
  }
  if (keepIds && input.study_id !== studyId) {
    throw transferError('wrongStudyIds', 'Notion database IDs can only be kept for the same study.');
  }
  const mapping = structuredClone(input.export_mapping);
  if (!keepIds) {
    for (const target of mapping.targets) target.database_id = '';
  }
  return { mapping, parentPageId: keepIds ? input.parent_page_id : '' };
}

export function downloadConfiguration(payload, filename) {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
