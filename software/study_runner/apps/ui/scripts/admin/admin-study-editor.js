/**
 * Study editor, card list, persistence, import/export, and recent studies.
 *
 * The preview column shows every card live, mounted exactly as on the
 * participant page (cards/card-mount.js) in 'preview' mode: animations run,
 * the card being edited can be tried out, nothing is recorded.
 */
import { mountCard } from '../cards/card-mount.js';
import { resetAllCardState } from '../cards/session-state.js';
import { cardTypeLabel } from '../cards/index.js';
import { withLanguage } from '../shared/i18n.js';

export function createAdminStudyEditor(context) {
  const {
    state,
    $,
    CARDS,
    CARD_TYPES,
    defaultFor,
    renderPromptField,
    renderInstructionField,
    renderNoteField,
    renderEditorToggles,
    collectInfo,
    escapeHtml,
    t,
    confirmWithModal,
    normalizeStudySettings,
    postJson,
    getJson,
    loadStudyReadiness,
    loadStudyRunState,
    applyLoadedConfig,
    renderStudyRunState,
    switchView,
    getLanguage,
  } = context;

  function newCard(type) {
    return { ...defaultFor(type), card_id: crypto.randomUUID() };
  }

  function handleListClick(event) {
    if (state.suppressListClick) {
      return;
    }
  
    const removeButton = event.target.closest('[data-role="remove-question"]');
    const item = event.target.closest('.admin-q-item');
  
    if (removeButton) {
      const index = Number(removeButton.dataset.index);
      const qType = state.config.questions[index]?.type;
      if (qType === 'participant-id' || qType === 'finish') {
        showToast(t('toast.bookendsLocked'), 'error');
        return;
      }
      void removeQuestion(index);
      return;
    }
    if (item && !event.target.closest('.admin-q-actions')) {
      selectQuestion(Number(item.dataset.index));
    }
  }
  
  function handleListDragStart(event) {
    const handle = event.target.closest('[data-role="drag-question"]');
    if (!handle || handle.disabled) {
      event.preventDefault();
      return;
    }
  
    const item = handle.closest('.admin-q-item');
    if (!item) {
      event.preventDefault();
      return;
    }
  
    state.draggedElement = item;
    $('admin-q-list').classList.add('admin-q-list--dragging');
  
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.dropEffect = 'move';
      event.dataTransfer.setData('text/plain', item.dataset.index || '');
    }
  
    window.requestAnimationFrame(() => {
      item.classList.add('admin-q-item--dragging');
    });
  }
  
  function handleListDragOver(event) {
    if (!state.draggedElement) {
      return;
    }
  
    event.preventDefault();
  
    const list = $('admin-q-list');
    const placement = getDragPlacement(list, event.clientY);
    clearDragIndicators();
  
    // Boundary checks to keep items between the first and last card
    const questions = state.config.questions || [];
    const firstItem = list.querySelector('.admin-q-item[data-index="0"]');
    const lastItem = list.querySelector(`.admin-q-item[data-index="${questions.length - 1}"]`);
  
    // Block dropping before the first item
    if (placement.targetItem === firstItem && !placement.insertAfter) {
      return;
    }
    // Block dropping after the last item
    if ((placement.targetItem === lastItem && placement.insertAfter) || !placement.targetItem) {
      return;
    }
  
    if (placement.targetItem !== state.draggedElement) {
      placement.targetItem.classList.add(
        placement.insertAfter ? 'admin-q-item--drop-after' : 'admin-q-item--drop-before',
      );
    }
    const referenceNode = placement.insertAfter
      ? placement.targetItem.nextElementSibling
      : placement.targetItem;
  
    if (referenceNode !== state.draggedElement) {
      list.insertBefore(state.draggedElement, referenceNode);
    }
  }
  
  function handleListDrop(event) {
    if (!state.draggedElement) {
      return;
    }
    event.preventDefault();
  }
  
  function handleListDragEnd() {
    finishListDrag();
  }
  
  function getDragPlacement(list, clientY) {
    const items = [...list.querySelectorAll('.admin-q-item:not(.admin-q-item--dragging)')];
  
    for (const item of items) {
      const rect = item.getBoundingClientRect();
      const midpoint = rect.top + (rect.height / 2);
  
      if (clientY < midpoint) {
        return { targetItem: item, insertAfter: false };
      }
      if (clientY < rect.bottom) {
        return { targetItem: item, insertAfter: true };
      }
    }
  
    return { targetItem: null, insertAfter: false };
  }
  
  function finishListDrag() {
    const list = $('admin-q-list');
    const draggedElement = state.draggedElement;
    if (!draggedElement) {
      return;
    }
  
    const previousSelection = state.selectedIndex;
    const shouldKeepOverlayOpen = $('admin-sidebar').classList.contains('has-overlay');
    const previousQuestions = [...(state.config.questions || [])];
    const orderedIndexes = [...list.querySelectorAll('.admin-q-item')].map((item) => Number(item.dataset.index));
    const orderChanged = orderedIndexes.some((originalIndex, newIndex) => originalIndex !== newIndex);
  
    clearDragIndicators();
    list.classList.remove('admin-q-list--dragging');
    draggedElement.classList.remove('admin-q-item--dragging');
    state.draggedElement = null;
    suppressListClickOnce();
  
    if (!orderChanged) {
      return;
    }
  
    state.config.questions = orderedIndexes.map((index) => previousQuestions[index]);
    state.selectedIndex = previousSelection === null ? null : orderedIndexes.indexOf(previousSelection);
  
    rebuildAll();
  
    if (shouldKeepOverlayOpen && state.selectedIndex !== null) {
      openOverlay(state.selectedIndex);
      $(`pc-${state.selectedIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  
    markUnsaved();
  }
  
  function clearDragIndicators() {
    document.querySelectorAll('.admin-q-item--drop-before, .admin-q-item--drop-after').forEach((element) => {
      element.classList.remove('admin-q-item--drop-before', 'admin-q-item--drop-after');
    });
  }
  
  function suppressListClickOnce() {
    state.suppressListClick = true;
    window.setTimeout(() => {
      state.suppressListClick = false;
    }, 0);
  }
  
  function rebuildAll() {
    rebuildList();
    rebuildPreview();
    syncEmptyState();
  }
  
  function rebuildList() {
    const list = $('admin-q-list');
    list.replaceChildren();
    const questions = state.config.questions || [];
  
    questions.forEach((question, questionIndex) => {
      const meta = getMeta(question.type);
      const item = document.createElement('div');
      item.className = `admin-q-item${questionIndex === state.selectedIndex ? ' selected' : ''}`;
      item.dataset.index = questionIndex;
      item.innerHTML = renderListItemMarkup(question, questionIndex, meta);
      list.appendChild(item);
    });
  
    $('q-count').textContent = questions.length ? `(${questions.length})` : '';
  }
  
  function renderListItemMarkup(question, questionIndex, meta) {
    const isFixed = question.type === 'participant-id' || question.type === 'finish';
    return `
      <span class="admin-q-num">${questionIndex + 1}</span>
      <i class="iconoir-${meta.icon} admin-q-type-icon"></i>
      <span class="admin-q-label">${renderCardLabel(question)}</span>
      <div class="admin-q-actions">
        <button type="button" class="admin-q-drag" data-role="drag-question" draggable="${!isFixed}" ${isFixed ? 'disabled' : ''} title="${escapeHtml(t('question.dragToReorder', 'Drag to reorder'))}" aria-label="${escapeHtml(t('question.dragToReorder', 'Drag to reorder'))}">
          <i class="iconoir-menu-scale"></i>
        </button>
        <button type="button" class="del" data-role="remove-question" data-index="${questionIndex}" title="${escapeHtml(t('question.remove', 'Remove'))}" ${isFixed ? 'disabled' : ''}>
          <i class="iconoir-trash"></i>
        </button>
      </div>`;
  }
  
  function rebuildPreview() {
    const preview = $('study-preview');
    // The editor always keeps the card border here, regardless of
    // study_settings.card_frame_enabled: it's what separates the stacked
    // cards in this list, not a frame on a single rendered card.
    // A fresh preview: stop the old cards' animations and forget what was
    // tried out in them.
    resetAllCardState();
    preview.replaceChildren();
    const questions = state.config.questions || [];
  
    questions.forEach((question, questionIndex) => {
      const cardModule = CARDS[question.type];
      if (!cardModule) {
        return;
      }
  
      const wrap = document.createElement('div');
      wrap.className = `preview-card-wrap${questionIndex === state.selectedIndex ? ' selected' : ''}`;
      wrap.id = `pc-${questionIndex}`;
      wrap.innerHTML = `
        <div class="q-card-study"></div>
        <div class="preview-card-overlay">
          <button type="button" data-role="select-card" data-index="${questionIndex}">
            <i class="iconoir-edit-pencil"></i> ${escapeHtml(t('question.edit', 'Edit'))}
          </button>
        </div>`;
      preview.appendChild(wrap);
      withLanguage(state.config.study_settings?.participant_language || 'en', () =>
        mountCard(wrap.querySelector('.q-card-study'), question, questionIndex, { mode: 'preview' }));
    });
  }
  
  function syncEmptyState() {
    $('preview-empty').hidden = (state.config.questions || []).length > 0;
  }
  
  function selectQuestion(index) {
    state.selectedIndex = index;
  
    document.querySelectorAll('.admin-q-item').forEach((element, elementIndex) => {
      element.classList.toggle('selected', elementIndex === index);
    });
    document.querySelectorAll('.preview-card-wrap').forEach((element, elementIndex) => {
      element.classList.toggle('selected', elementIndex === index);
    });
  
    openOverlay(index);
    $(`pc-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  
  function openOverlay(index) {
    const question = state.config.questions[index];
    const cardModule = CARDS[question.type];
    if (!cardModule) {
      return;
    }
  
    const meta = getMeta(question.type);
    $('overlay-type-tag').innerHTML =
      `<i class="iconoir-${escapeHtml(meta.icon)}"></i> ${escapeHtml(cardTypeLabel(question.type, meta.label))} <span class="editor-index">#${index + 1}</span>`;
  
    const editorEl = $('editor-fields');
    // The order an author actually writes a question in: what is being asked,
    // how to answer it, the card's own settings, an optional note, then the
    // switches. Cards used to supply their own prompt field and the shared
    // block was appended after, which put "Required" above the question text.
    editorEl.innerHTML = [
      renderPromptField(question, cardModule.promptPlaceholder),
      renderInstructionField(question),
      cardModule.renderEditor(question, index),
      renderNoteField(question),
      renderEditorToggles(question, cardModule.renderEditorToggles?.(question) || ''),
    ].join('');
    if (typeof cardModule.bindEditorEvents === 'function') {
      cardModule.bindEditorEvents(editorEl);
    }
    $('admin-sidebar').classList.add('has-overlay');
  }
  
  function closeOverlay() {
    $('admin-sidebar').classList.remove('has-overlay');
  }
  
  function liveUpdate(index) {
    const question = state.config.questions[index];
    const cardModule = CARDS[question.type];
    if (!cardModule) {
      return;
    }
  
    const updated = cardModule.collectConfig($('editor-fields'));
    if (!updated) {
      return;
    }
  
    Object.assign(updated, collectInfo($('editor-fields')));
  
    state.config.questions[index] = updated;
  
    const previewWrap = $(`pc-${index}`);
    if (previewWrap) {
      withLanguage(state.config.study_settings?.participant_language || 'en', () =>
        mountCard(previewWrap.querySelector('.q-card-study'), updated, index, { mode: 'preview' }));
    }
  
    const label = $('admin-q-list').querySelector(`.admin-q-item[data-index="${index}"] .admin-q-label`);
    if (label) {
      label.innerHTML = renderCardLabel(updated);
    }
  }
  
  function addQuestion(type) {
    state.config.questions = state.config.questions || [];
    const questions = state.config.questions;
    const finishCardIndex = questions.findIndex(q => q.type === 'finish');
    const insertIndex = finishCardIndex !== -1 ? finishCardIndex : questions.length;
  
    questions.splice(insertIndex, 0, newCard(type));
    rebuildAll();
    selectQuestion(insertIndex);
    requestAnimationFrame(() => $(`pc-${insertIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
    markUnsaved();
  }
  
  async function removeQuestion(index) {
    const message = t('question.removeConfirm', 'Remove question {number}?').replace('{number}', String(index + 1));
    const proceed = await confirmWithModal({
      title: getCardLabel(state.config.questions[index]) || String(index + 1),
      message,
      confirmLabel: t('question.remove', 'Remove'),
      cancelLabel: t('common.cancel', 'Cancel'),
    });
    if (!proceed) {
      return;
    }
  
    state.config.questions.splice(index, 1);
  
    if (state.selectedIndex === index) {
      state.selectedIndex = null;
      closeOverlay();
    } else if (state.selectedIndex > index) {
      state.selectedIndex -= 1;
    }
  
    rebuildAll();
    if (state.selectedIndex !== null) {
      selectQuestion(state.selectedIndex);
    }
    markUnsaved();
  }
  
  function handleTriggerTypePill(pillElement) {
    const triggerType = pillElement.dataset.triggerType;
    const editorFields = $('editor-fields');
  
    editorFields.querySelectorAll('.trigger-pill').forEach((pill) => {
      pill.classList.toggle('active', pill.dataset.triggerType === triggerType);
    });
  
    const hiddenInput = editorFields.querySelector('.se-trigger-type');
    if (hiddenInput) {
      hiddenInput.value = triggerType;
    }
  
    const contentField = editorFields.querySelector('.se-trigger-content-field');
    if (contentField) {
      contentField.hidden = triggerType === 'timer';
  
      const isCode = triggerType === 'html' || triggerType === 'js';
      const currentInput = contentField.querySelector('.se-trigger-content');
      const currentIsCode = currentInput?.tagName === 'TEXTAREA';
  
      if (currentInput && isCode !== currentIsCode) {
        const savedValue = currentInput.value;
        const label = contentField.querySelector('label');
        if (label) {
          label.textContent = isCode ? t('stimulus.codeLabel', 'Code') : t('stimulus.urlLabel', 'URL');
        }
  
        let replacement;
        if (isCode) {
          replacement = document.createElement('textarea');
          replacement.className = 'se-trigger-content se-trigger-content--code';
          replacement.rows = 6;
          replacement.placeholder = t('stimulus.codePlaceholder', 'Paste {type} code here...').replace('{type}', triggerType);
          replacement.value = savedValue;
        } else {
          replacement = document.createElement('input');
          replacement.type = 'url';
          replacement.className = 'se-trigger-content';
          replacement.placeholder = t('stimulus.urlPlaceholder', 'https://...');
          replacement.value = savedValue;
        }
        currentInput.replaceWith(replacement);
      }
    }
  
    editorFields.dispatchEvent(new Event('input', { bubbles: true }));
  }
  
  function ensureBookends(questions) {
    if (!Array.isArray(questions)) return;
    const pidIndex = questions.findIndex(q => q.type === 'participant-id');
    const pidCard = pidIndex !== -1 ? questions.splice(pidIndex, 1)[0] : newCard('participant-id');
    const finIndex = questions.findIndex(q => q.type === 'finish');
    const finCard = finIndex !== -1 ? questions.splice(finIndex, 1)[0] : newCard('finish');
    questions.unshift(pidCard);
    questions.push(finCard);
  }
  
  async function saveConfig(options = {}) {
    const { successMessage = t('toast.studySaved', 'Study saved'), skipToast = false } = options;
    let questions = state.config.questions || [];
    ensureBookends(questions);
  
    const fullConfig = {
      study_id: $('cfg-id').value.trim(),
      questions: questions,
      study_settings: normalizeStudySettings(state.config.study_settings),
    };
  
    try {
      const response = await postJson('/api/config', fullConfig);
      state.config = response?.config || fullConfig;
  
      $('btn-save-config').classList.remove('btn-primary--dirty');
      await loadRecentStudies();
      // Settings just changed - re-check what would block a run.
      void loadStudyReadiness();
      await loadStudyRunState();
      rebuildAll();
      if (!skipToast) {
        showToast(successMessage, 'success');
      }
      return true;
    } catch (error) {
      console.error('[admin] Could not save configuration:', error);
      showToast(t('toast.saveFailed'), 'error');
      return false;
    }
  }
  
  function markUnsaved() {
    if (!state.loaded) return;
    $('btn-save-config').classList.add('btn-primary--dirty');
  }
  
  let _toastTimer = null;
  function showToast(message, type = 'info') {
    const icons = { success: 'iconoir-check', error: 'iconoir-xmark-circle', info: 'iconoir-info-circle', warning: 'iconoir-warning-triangle' };
    $('toast-icon').className = icons[type] || icons.info;
    $('toast-msg').textContent = message;
    const toast = $('toast');
    toast.className = `toast toast--${type} show`;
    toast.onclick = () => toast.classList.remove('show');
    clearTimeout(_toastTimer);
    // Problems need time to be read; a click closes any message early.
    const visibleMs = type === 'error' || type === 'warning' ? 8000 : 3000;
    _toastTimer = setTimeout(() => toast.classList.remove('show'), visibleMs);
  }
  
  function loadFromFile() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.study-runner,.json,application/json,application/zip';
    input.onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        // The server unpacks a study package (study + images) or a plain JSON
        // study and stores the images; saving the study itself stays below.
        const body = new FormData();
        body.append('file', file);
        const response = await fetch('/api/admin/studies/import', { method: 'POST', body });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.config) {
          showToast(payload.error || t('toast.invalidJson'), 'error');
          return;
        }
        const config = payload.config;
        state.studyRunState = { status: 'loaded', study_id: config.study_id || '' };
        applyLoadedConfig(config);
        // Import has to persist. Loading the file into the editor and marking it
        // unsaved left the study invisible in the hub, because the operator who
        // imported from the hub never sees the editor's Save button.
        await saveConfig({
          successMessage: t('toast.importedFile', 'Imported: {name}').replace('{name}', file.name),
        });
        switchView('view-workspace');
      } catch {
        showToast(t('toast.invalidJson'), 'error');
      }
    };
    input.click();
  }
  
  async function _activateStudyFromHub(id, options = {}) {
    try {
      const response = await postJson('/api/admin/study-run/load', { id });
      applyLoadedConfig(response.config || {});
      state.studyRunState = response.run_state || { status: 'loaded', study_id: state.config.study_id || id };
      state.tabletGate = response.tablet_gate || null;
      renderStudyRunState();
      showToast(t('toast.studyLoadedWaiting', 'Study loaded - tablet is waiting'), 'success');
      if (options.openEditor) {
        switchView('view-workspace');
      }
    } catch (e) {
      showToast(t('toast.loadFailed'), 'error');
    }
  }
  
  async function downloadStudy(id) {
    try {
      // A package bundles the study with every image it shows.
      const response = await fetch(`/api/admin/studies/${encodeURIComponent(id)}/package`);
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${id}.study-runner`;
      a.click();
      URL.revokeObjectURL(url);
    } catch(e) {
      showToast(t('toast.downloadFailed'), 'error');
    }
  }
  
  async function deleteStudy(id) {
    const message = t('hub.recent.deleteConfirm', 'Delete study "{id}" permanently?').replace('{id}', id);
    const proceed = await confirmWithModal({
      title: id,
      message,
      confirmLabel: t('hub.recent.delete', 'Delete'),
      cancelLabel: t('common.cancel', 'Cancel'),
    });
    if (!proceed) return;
    try {
      const response = await fetch(`/api/admin/studies/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || t('toast.deleteFailed', 'Delete failed'));
      }
      showToast(t('toast.studyDeleted'), 'success');
      await loadRecentStudies();
    } catch(e) {
      showToast(t('toast.deleteFailed'), 'error');
    }
  }
  
  async function loadRecentStudies() {
    try {
      const studies = await getJson('/api/admin/studies');
      const listEl = $('hub-recent-list');
      if (!studies || studies.length === 0) {
        listEl.innerHTML = `
          <div class="hub-recent-item empty">
            <i class="iconoir-clock"></i>
            <div>${escapeHtml(t('hub.recent.empty', 'No saved studies yet.'))}</div>
          </div>`;
        return;
      }
  
      listEl.innerHTML = studies.map(s => `
        <div class="hub-recent-item" data-study-id="${escapeHtml(s.id)}">
          <div class="hub-recent-item-main">
            <i class="iconoir-journal-page"></i>
            <div>
              <div class="hub-recent-item-title">${escapeHtml(s.id)}</div>
              <div class="hub-recent-item-meta">${escapeHtml(t('hub.recent.modified', 'Last edited'))}: ${new Date(s.modified * 1000).toLocaleString(getLanguage())}</div>
            </div>
          </div>
          <div class="hub-recent-actions">
            <button class="btn-icon-only" data-action="load" title="${escapeHtml(t('hub.recent.load', 'Load'))}"><i class="iconoir-import"></i></button>
            <button class="btn-icon-only" data-action="edit" title="${escapeHtml(t('hub.recent.edit', 'Edit'))}"><i class="iconoir-edit-pencil"></i></button>
            <button class="btn-icon-only" data-action="download" title="${escapeHtml(t('hub.recent.download', 'Download'))}"><i class="iconoir-download"></i></button>
            <button class="btn-icon-only is-danger" data-action="delete" title="${escapeHtml(t('hub.recent.delete', 'Delete'))}"><i class="iconoir-trash"></i></button>
          </div>
        </div>
      `).join('');
  
      listEl.querySelectorAll('.hub-recent-item:not(.empty)').forEach(item => {
        const id = item.dataset.studyId;
        item.querySelector('.hub-recent-item-main').addEventListener('click', () => _activateStudyFromHub(id));
        item.querySelector('[data-action="load"]').addEventListener('click', () => _activateStudyFromHub(id));
        item.querySelector('[data-action="edit"]').addEventListener('click', () => _activateStudyFromHub(id, { openEditor: true }));
        item.querySelector('[data-action="download"]').addEventListener('click', () => downloadStudy(id));
        item.querySelector('[data-action="delete"]').addEventListener('click', () => deleteStudy(id));
      });
    } catch (error) {
      console.error('[admin] Could not load recent studies:', error);
    }
  }
  
  function renderCardLabel(question) {
    const label = getCardLabel(question);
    return label ? escapeHtml(label) : `<em>${escapeHtml(t('question.noText', 'no text'))}</em>`;
  }
  
  function getCardLabel(question) {
    if (question.type === 'stimulus') {
      return (question.title || '').trim();
    }
    return (question.prompt || '').trim();
  }
  
  function getMeta(type) {
    const entry = CARD_TYPES.find((cardType) => cardType.type === type);
    return entry
      ? (entry.overrideMeta || entry.module.meta)
      : { icon: 'question-mark', label: type };
  }
  

  return {
    addQuestion,
    closeOverlay,
    downloadStudy,
    ensureBookends,
    getCardLabel,
    getMeta,
    handleListClick,
    handleListDragEnd,
    handleListDragOver,
    handleListDragStart,
    handleListDrop,
    handleTriggerTypePill,
    liveUpdate,
    loadFromFile,
    loadRecentStudies,
    markUnsaved,
    rebuildAll,
    removeQuestion,
    renderCardLabel,
    saveConfig,
    selectQuestion,
    showToast,
  };
}
