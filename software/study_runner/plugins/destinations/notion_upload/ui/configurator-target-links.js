/** Relations and participant aggregates over the same target graph model. */
import { escapeHtml } from '/static/scripts/shared/dom-utils.js';
import { t } from '/static/scripts/shared/i18n.js';
import { createNodeCanvas } from './node-canvas.js';

const PAIRS = new Set(['card:participant', 'card:session', 'session:participant']);
const FUNCTIONS = ['count', 'mean', 'min', 'max', 'last'];

function candidates(mapping, target) {
  return mapping.targets.filter((other) => PAIRS.has(`${target.row_level}:${other.row_level}`));
}

export function renderTargetLinks(mapping, target) {
  const choices = candidates(mapping, target);
  const incoming = mapping.targets.filter((other) =>
    (other.relations || []).some((relation) => relation.target_id === target.id));
  return `
    <section class="configurator-section configurator-links">
      <h3>${t('notionConfigurator.relations', 'Relations')}</h3>
      ${(target.relations || []).map((relation, index) => {
        const other = mapping.targets.find((item) => item.id === relation.target_id);
        return `<div class="configurator-link-row">
          <span>${escapeHtml(other?.title || relation.target_id)}</span>
          <label class="field"><span>${t('notionConfigurator.column', 'Notion column')}</span><input data-relation-field="property_name" data-relation-index="${index}" value="${escapeHtml(relation.property_name || other?.title || '')}"></label>
          <label class="field"><span>${t('notionConfigurator.reverseColumn', 'Reverse column')}</span><input data-relation-field="reverse_property_name" data-relation-index="${index}" value="${escapeHtml(relation.reverse_property_name || '')}" placeholder="Auto"></label>
          ${target.row_level === 'card' && other?.row_level === 'participant'
            ? `<label><input type="checkbox" data-relation-field="linked_view" data-relation-index="${index}" ${relation.linked_view ? 'checked' : ''}> ${t('notionConfigurator.participantView', 'Participant page view')}</label>` : ''}
          <button class="btn-secondary" type="button" data-remove-relation="${index}">${t('notionConfigurator.remove', 'Remove')}</button>
        </div>`;
      }).join('') || `<p class="settings-hint">${t('notionConfigurator.noRelations', 'No relations yet.')}</p>`}
      ${choices.length ? `<label class="field"><span>${t('notionConfigurator.connectTo', 'Connect to target')}</span><select data-relation-target>
        ${choices.map((other) => `<option value="${escapeHtml(other.id)}">${escapeHtml(other.title)} (${escapeHtml(other.row_level)})</option>`).join('')}
      </select></label><button class="btn-secondary" type="button" data-add-relation>${t('notionConfigurator.addRelation', 'Add relation')}</button>` : ''}
    </section>
    ${target.row_level === 'participant' ? `<section class="configurator-section configurator-aggregates">
      <h3>${t('notionConfigurator.summaries', 'Participant summaries')}</h3>
      ${(target.aggregates || []).map((aggregate, index) => `<div class="configurator-link-row">
        <input data-aggregate-field="name" data-aggregate-index="${index}" value="${escapeHtml(aggregate.name)}" aria-label="Summary column">
        <select data-aggregate-field="target_id" data-aggregate-index="${index}">
          ${incoming.map((other) => `<option value="${escapeHtml(other.id)}" ${aggregate.target_id === other.id ? 'selected' : ''}>${escapeHtml(other.title)}</option>`).join('')}
        </select>
        <select data-aggregate-field="column" data-aggregate-index="${index}">
          ${Object.keys(mapping.targets.find((item) => item.id === aggregate.target_id)?.columns || {}).map((name) => `<option value="${escapeHtml(name)}" ${aggregate.column === name ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')}
        </select>
        <select data-aggregate-field="function" data-aggregate-index="${index}">
          ${FUNCTIONS.map((name) => `<option value="${name}" ${aggregate.function === name ? 'selected' : ''}>${name}</option>`).join('')}
        </select>
        <button class="btn-secondary" type="button" data-remove-aggregate="${index}">${t('notionConfigurator.remove', 'Remove')}</button>
      </div>`).join('') || `<p class="settings-hint">${t('notionConfigurator.noSummaries', 'No summaries yet.')}</p>`}
      ${incoming.length ? `<button class="btn-secondary" type="button" data-add-aggregate>${t('notionConfigurator.addSummary', 'Add summary')}</button>` : `<p class="settings-hint">${t('notionConfigurator.connectFirst', 'Connect a card or session target first.')}</p>`}
    </section>` : ''}
    ${target.row_level === 'card' ? `<label class="field"><span><input type="checkbox" data-comparison-views ${target.comparison_views ? 'checked' : ''}> ${t('notionConfigurator.comparisonViews', 'Create comparison view for every card')}</span></label>` : ''}`;
}

export function bindTargetLinks(root, mapping, target, onChange) {
  root.querySelector('[data-add-relation]')?.addEventListener('click', () => {
    const other = mapping.targets.find((item) => item.id === root.querySelector('[data-relation-target]')?.value);
    if (!other || (target.relations || []).some((item) => item.target_id === other.id)) return;
    target.relations ||= [];
    target.relations.push({
      target_id: other.id, property_name: other.title,
      linked_view: target.row_level === 'card' && other.row_level === 'participant',
    });
    onChange();
  });
  root.querySelectorAll('[data-relation-field]').forEach((input) => input.addEventListener('change', () => {
    const relation = target.relations[Number(input.dataset.relationIndex)];
    if (!relation) return;
    if (input.dataset.relationField === 'reverse_property_name' && !input.value.trim()) {
      delete relation.reverse_property_name;
    } else {
      relation[input.dataset.relationField] = input.type === 'checkbox' ? input.checked : input.value.trim();
    }
    onChange();
  }));
  root.querySelectorAll('[data-remove-relation]').forEach((button) => button.addEventListener('click', () => {
    target.relations.splice(Number(button.dataset.removeRelation), 1);
    onChange();
  }));
  root.querySelector('[data-add-aggregate]')?.addEventListener('click', () => {
    const incoming = mapping.targets.find((item) => (item.relations || []).some((relation) => relation.target_id === target.id));
    const column = Object.keys(incoming?.columns || {})[0];
    if (!column) return;
    target.aggregates ||= [];
    target.aggregates.push({ name: `Summary ${target.aggregates.length + 1}`, target_id: incoming.id, column, function: 'count' });
    onChange();
  });
  root.querySelectorAll('[data-aggregate-field]').forEach((input) => input.addEventListener('change', () => {
    const aggregate = target.aggregates[Number(input.dataset.aggregateIndex)];
    if (!aggregate) return;
    aggregate[input.dataset.aggregateField] = input.value.trim();
    if (input.dataset.aggregateField === 'target_id') {
      aggregate.column = Object.keys(mapping.targets.find((item) => item.id === aggregate.target_id)?.columns || {})[0] || '';
    }
    onChange();
  }));
  root.querySelectorAll('[data-remove-aggregate]').forEach((button) => button.addEventListener('click', () => {
    target.aggregates.splice(Number(button.dataset.removeAggregate), 1);
    onChange();
  }));
  root.querySelector('[data-comparison-views]')?.addEventListener('change', (event) => {
    target.comparison_views = event.target.checked;
    onChange();
  });
}

function graphFromMapping(mapping) {
  const nodes = mapping.targets.map((target, index) => {
    const peers = mapping.targets.filter((other) => other.id !== target.id);
    return {
      id: target.id, kind: 'target', label: target.title, detail: target.row_level,
      x: { card: 20, session: 340, participant: 660 }[target.row_level], y: 30 + index * 135,
      inputs: peers.filter((other) => PAIRS.has(`${other.row_level}:${target.row_level}`))
        .map((other) => ({ id: `from:${other.id}`, type: 'any', label: other.title })),
      outputs: peers.filter((other) => PAIRS.has(`${target.row_level}:${other.row_level}`))
        .map((other) => ({ id: `to:${other.id}`, type: 'any', label: other.title })),
    };
  });
  const edges = mapping.targets.flatMap((target) => (target.relations || []).map((relation) => ({
    id: `link:${target.id}:${relation.target_id}`,
    from: { node: target.id, port: `to:${relation.target_id}` },
    to: { node: relation.target_id, port: `from:${target.id}` },
  })));
  return { nodes, edges };
}

export function mountTargetGraph(container, mapping, onCommit) {
  const canvas = createNodeCanvas(container, { onChange: (graph) => {
    if (graph.nodes.length !== mapping.targets.length || graph.edges.some((edge) =>
      edge.from.port !== `to:${edge.to.node}` || edge.to.port !== `from:${edge.from.node}`)) {
      canvas.setGraph(graphFromMapping(mapping));
      return;
    }
    const edgeKeys = new Set(graph.edges.map((edge) => `${edge.from.node}:${edge.to.node}`));
    const currentKeys = new Set(mapping.targets.flatMap((target) => (target.relations || []).map((relation) => `${target.id}:${relation.target_id}`)));
    if (edgeKeys.size === currentKeys.size && [...edgeKeys].every((key) => currentKeys.has(key))) return;
    for (const target of mapping.targets) {
      target.relations = (target.relations || []).filter((relation) => edgeKeys.has(`${target.id}:${relation.target_id}`));
      for (const other of mapping.targets) {
        if (!edgeKeys.has(`${target.id}:${other.id}`) || target.relations.some((relation) => relation.target_id === other.id)) continue;
        target.relations.push({ target_id: other.id, property_name: other.title,
          linked_view: target.row_level === 'card' && other.row_level === 'participant' });
      }
    }
    onCommit();
  } });
  canvas.setGraph(graphFromMapping(mapping));
  canvas.zoomToFit();
  return canvas;
}
