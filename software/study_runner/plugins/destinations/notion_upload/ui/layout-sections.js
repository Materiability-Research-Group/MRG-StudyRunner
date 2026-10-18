/**
 * The configurator's four steps, in the order the Notion structure nests:
 * the Participants table (and whether each participant gets an answers
 * table), the values every card provides (and where each one goes), the
 * cards, and what to write when a card has no value.
 *
 * Renders HTML only; configurator.js binds the data-* hooks it emits.
 * Every text that comes from the study is escaped.
 */
import { escapeHtml } from '/static/scripts/shared/dom-utils.js';
import { t } from '/static/scripts/shared/i18n.js';
import { cardIncluded, isRemovableValue, valueLabel } from './layout-model.js';

const CARD_TYPE_HINT = { info: 'info', stimulus: 'stimulus' };

export function renderSections(layout, sources) {
  return [
    renderParticipants(layout, sources),
    renderValues(layout, sources),
    renderCards(layout, sources),
    renderMissing(layout),
  ].join('');
}

function section(step, title, hint, body) {
  return `
    <section class="notion-layout-section">
      <header class="notion-layout-section-header">
        <span class="notion-layout-step" aria-hidden="true">${step}</span>
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p class="settings-hint">${escapeHtml(hint)}</p>
        </div>
      </header>
      ${body}
    </section>`;
}

function toggle(attribute, checked, title, hint) {
  return `
    <label class="switch-row notion-layout-toggle">
      <span class="switch-row-text"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(hint)}</small></span>
      <span class="switch"><input type="checkbox" ${attribute}${checked ? ' checked' : ''}><span class="switch-slider"></span></span>
    </label>`;
}

function renderParticipants(layout, sources) {
  const chips = (sources.participant_columns || []).map((column) => {
    const on = layout.participants.columns.includes(column.key);
    const disabled = !column.available;
    const title = disabled ? t('notionLayout.notStored', 'The participant card does not store this field.') : '';
    return `
      <label class="notion-layout-chip${disabled ? ' is-disabled' : ''}"${title ? ` title="${escapeHtml(title)}"` : ''}>
        <input type="checkbox" data-participant-column="${escapeHtml(column.key)}"${on && !disabled ? ' checked' : ''}${disabled ? ' disabled' : ''}>
        <span>${escapeHtml(column.label)}</span>
      </label>`;
  }).join('');
  return section(
    1,
    t('notionLayout.participants.title', 'Participants table'),
    t('notionLayout.participants.hint', 'One row per participant on your Notion page. Choose what the table shows next to the participant ID.'),
    `<div class="notion-layout-chips">${chips}</div>
     ${toggle(
    'data-answers-toggle',
    layout.answers_per_participant,
    t('notionLayout.answers.toggle', 'An answers table for every participant'),
    t('notionLayout.answers.hint', 'Opens from the participant’s row: one row per session and card with the values ticked under “Answers table” below.'),
  )}`,
  );
}

function renderValues(layout, sources) {
  const answersOff = !layout.answers_per_participant;
  const rows = layout.values.map((item) => {
    const label = item.source === 'answer' ? t('notionLayout.values.answer', 'Answer') : valueLabel(item.source, sources);
    const remove = isRemovableValue(item.source)
      ? `<button class="btn-icon-only" type="button" data-remove-value="${escapeHtml(item.source)}" title="${escapeHtml(t('notionLayout.values.remove', 'Remove'))}" aria-label="${escapeHtml(t('notionLayout.values.remove', 'Remove'))}"><i class="iconoir-xmark"></i></button>`
      : '';
    return `
      <tr>
        <th scope="row">${escapeHtml(label)}</th>
        <td><input type="checkbox" data-value-flag="answers" data-source="${escapeHtml(item.source)}"${item.answers ? ' checked' : ''}${answersOff ? ' disabled' : ''} aria-label="${escapeHtml(t('notionLayout.values.inAnswers', 'Answers table'))}"></td>
        <td><input type="checkbox" data-value-flag="comparison" data-source="${escapeHtml(item.source)}"${item.comparison ? ' checked' : ''} aria-label="${escapeHtml(t('notionLayout.values.inComparison', 'Card comparison'))}"></td>
        <td>${remove}</td>
      </tr>`;
  }).join('');
  const listed = new Set(layout.values.map((item) => item.source));
  const options = (sources.values || [])
    .filter((item) => !listed.has(item.source))
    .map((item) => `<option value="${escapeHtml(item.source)}">${escapeHtml(item.label)}</option>`)
    .join('');
  const add = options
    ? `<label class="field notion-layout-add"><span class="sr-only">${escapeHtml(t('notionLayout.values.add', 'Add a sensor value'))}</span>
        <select data-add-value><option value="">${escapeHtml(t('notionLayout.values.addPlaceholder', '+ Add a sensor value…'))}</option>${options}</select></label>`
    : `<p class="settings-hint">${escapeHtml(t('notionLayout.values.noSensors', 'Sensor values appear here once the study uses a sensor, or after choosing a recorded session for the preview.'))}</p>`;
  return section(
    2,
    t('notionLayout.values.title', 'Values per card'),
    t('notionLayout.values.hint', 'What every card sends. Tick “Card comparison” to get one table per card that lists every participant’s value side by side.'),
    `<table class="notion-layout-values">
       <thead><tr>
         <th scope="col">${escapeHtml(t('notionLayout.values.value', 'Value'))}</th>
         <th scope="col"${answersOff ? ` title="${escapeHtml(t('notionLayout.values.answersOff', 'Switch on the answers table in step 1 first.'))}"` : ''}>${escapeHtml(t('notionLayout.values.inAnswers', 'Answers table'))}</th>
         <th scope="col">${escapeHtml(t('notionLayout.values.inComparison', 'Card comparison'))}</th>
         <th scope="col"><span class="sr-only">${escapeHtml(t('notionLayout.values.remove', 'Remove'))}</span></th>
       </tr></thead>
       <tbody>${rows}</tbody>
     </table>
     ${add}`,
  );
}

function renderCards(layout, sources) {
  const cards = sources.cards || [];
  const list = cards.length
    ? cards.map((card) => `
        <label class="notion-layout-card">
          <input type="checkbox" data-card="${escapeHtml(card.card_id)}"${cardIncluded(layout, card) ? ' checked' : ''}>
          <span>${escapeHtml(card.label)}</span>
          ${CARD_TYPE_HINT[card.type] ? `<small>${escapeHtml(t(`notionLayout.cards.type.${card.type}`, CARD_TYPE_HINT[card.type]))}</small>` : ''}
        </label>`).join('')
    : `<p class="settings-hint">${escapeHtml(t('notionLayout.cards.none', 'This study has no saved cards yet. Save the study, then open this again.'))}</p>`;
  return section(
    3,
    t('notionLayout.cards.title', 'Cards'),
    t('notionLayout.cards.hint', 'Which cards appear in the answers tables and the card comparison. A skipped or not shown card still gets its row, filled with the codes below.'),
    `<div class="notion-layout-cards">${list}</div>`,
  );
}

function renderMissing(layout) {
  return section(
    4,
    t('notionLayout.missing.title', 'Missing values'),
    t('notionLayout.missing.hint', 'Written where a card was skipped, not shown, or has no value. The Status column says which.'),
    `<div class="notion-layout-missing">
       <label class="field"><span>${escapeHtml(t('notionLayout.missing.number', 'Numbers'))}</span>
         <input type="number" step="any" data-missing="number" value="${escapeHtml(String(layout.missing.number))}"></label>
       <label class="field"><span>${escapeHtml(t('notionLayout.missing.text', 'Text and choices'))}</span>
         <input type="text" maxlength="100" data-missing="text" value="${escapeHtml(String(layout.missing.text))}"></label>
     </div>`,
  );
}
