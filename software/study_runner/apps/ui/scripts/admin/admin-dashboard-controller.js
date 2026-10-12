import { getJson, postJson, requestJson } from '../shared/api-client.js';

import { t } from '../shared/i18n.js';
import { escapeHtml } from '../shared/dom-utils.js';
import { openPluginConsole } from './plugin-console.js';
import { renderSensorConnectionPanel, studyBarState } from './sensor-connection-panel.js';
import { bindRuntimeSwitch } from './runtime-switch-input.js';
import { renderLiveTrends } from './live-trend.js';
import { placeTile, sensorColumns, updateMarquees } from './sensor-columns.js';
import { bindSensorTileDrag } from './sensor-tile-drag.js';
import { isDefaultLayout, mergeLayout, moveTile, rowMajorOrder } from './sensor-tile-order.js';
import { PARTICIPANT_TILE_KEY, renderParticipantTile } from './participant-devices-panel.js';
import {
  dashboardUiHelpers,
  fieldLabel,
  formatEnabled,
  formatOnOff,
  formatPluginName,
  formatTimestampAge,
  renderRuntimeButtons,
  statusLabel,
} from './dashboard-ui-helpers.js';
import {
  getPluginCatalog,
  getPluginUiExtension,
  isPluginVisible,
  loadPluginCatalog,
  loadPluginUiExtensions,
  PLUGIN_UI_SURFACES,
  pluginByKey,
  pluginUiIcon,
} from '../shared/plugin-catalog.js';

const POLL_INTERVAL_MS = 2000;
const STATUS_REQUEST_TIMEOUT_MS = 8000;

let pollTimer = null;
let callbacks = {};
const pendingPluginActions = new Set();
// Switch position the operator chose while the start/stop request runs.
const pendingRuntime = new Map();
// Plugins restarting right now: their switch knob stays on Restart until done.
const pendingRestart = new Set();
let latestStatus = null;
// The operator's tile arrangement for this computer (server-side), and the
// one in effect on screen (the saved one applied to the plugins there are).
let savedTileLayout = null;
let tileLayout = null;
let tileDrag = null;
let tileLayoutSaving = 0;
// The tile arrangement is dragged only where there are two columns.
const WIDE_DASHBOARD = '(min-width: 861px)';

export function initializeAdminDashboard(options = {}) {
  callbacks = options;
  const { showToast } = options;
  const elements = getDashboardElements();
  if (!elements.dashboardButton || !elements.dashboard) {
    return;
  }

  elements.dashboardButton.addEventListener('click', () => showDashboard(elements));
  // The same start as in the hub: preparing the sensors and starting the
  // participant happen on one screen.
  elements.startButton?.addEventListener('click', () => {
    void callbacks.startStudy?.({ buttonId: 'btn-dashboard-start-study' });
  });
  elements.dashboard.addEventListener('click', (event) => {
    const targetButton = event.target.closest('[data-participant-target]');
    if (targetButton) {
      void selectParticipantTarget(targetButton.dataset.participantTarget, elements, showToast);
      return;
    }
    const consoleButton = event.target.closest('[data-plugin-console]');
    if (consoleButton?.dataset.pluginConsole) {
      void openPluginConsole(consoleButton.dataset.pluginConsole, { showToast });
      return;
    }
    if (event.target.closest('[data-runtime-switch]')) return; // runtime-switch-input.js
    const stepButton = event.target.closest('[data-connection-action]');
    if (stepButton) {
      void runConnectionStep(stepButton, elements, showToast);
      return;
    }
    const button = event.target.closest('[data-dashboard-action]');
    if (button) {
      void runDashboardAction(button, elements, showToast);
    }
  });
  // Choosing a device in the list connects to it right away.
  elements.dashboard.addEventListener('change', (event) => {
    const select = event.target.closest('select[data-connection-select]');
    if (select) void runConnectionSelect(select, elements, showToast);
  });
  bindRuntimeSwitch(elements.dashboard, (pluginKey, target) => {
    void moveRuntimeSwitch(pluginKey, target, elements, showToast);
  });
  if (elements.sensorTiles) {
    tileDrag = bindSensorTileDrag(elements.sensorTiles, {
      getLayout: () => tileLayout,
      enabled: () => window.matchMedia(WIDE_DASHBOARD).matches,
      onMove: (key, column, position) => void moveSensorTile(key, column, position, elements, showToast),
    });
  }

  const refresh = () => {
    void refreshAdminStatus(elements, showToast);
  };

  refresh();
  pollTimer = window.setInterval(refresh, POLL_INTERVAL_MS);
}

async function selectParticipantTarget(clientId, elements, showToast) {
  try {
    const response = await postJson('/api/admin/study-run/target', { client_id: clientId });
    statusGeneration += 1;
    if (latestStatus) {
      latestStatus.study_run_state = response.run_state;
      latestStatus.study_clients.single_tablet = response.tablet_gate;
      renderSensorTiles(elements.sensorTiles, latestStatus);
    }
    await refreshAdminStatus(elements, showToast);
  } catch (error) {
    showToast?.(error.message || t('dashboard.targetFailed', 'Could not select this device'), 'error');
    await refreshAdminStatus(elements, showToast);
  }
}

async function runDashboardAction(actionSource, elements, showToast) {
  const button = typeof actionSource === 'string' ? null : actionSource;
  const action = typeof actionSource === 'string' ? actionSource : button?.dataset.dashboardAction || '';
  if (button?.dataset.pluginAdminAction && button.dataset.pluginKey) {
    await runPluginAdminAction(button, elements, showToast);
    return;
  }
  if (action === 'reset_sensor_overrides') {
    await resetSensorOverrides(elements, showToast);
    return;
  }
  if (action === 'reset_tile_order') {
    await saveTileLayout(null, elements, showToast);
    return;
  }
  if (action === 'open_settings') {
    callbacks.openSettingsHub?.();
    return;
  }

  const runtimeMatch = action.match(/^runtime_(.+)_(start|stop|restart)$/);
  if (!runtimeMatch) {
    return;
  }

  const [, pluginKey, runtimeAction] = runtimeMatch;
  if (runtimeAction !== 'restart') {
    await switchPluginRuntime(pluginKey, runtimeAction === 'start', elements, showToast);
    return;
  }
  await restartPluginRuntime(pluginKey, elements, showToast);
}

/** Off | On | Restart on the runtime switch. */
async function moveRuntimeSwitch(pluginKey, target, elements, showToast) {
  if (!pluginKey || !target) return;
  if (target === 'restart') {
    await restartPluginRuntime(pluginKey, elements, showToast);
    return;
  }
  await switchPluginRuntime(pluginKey, target === 'on', elements, showToast);
}

/** The knob stays on Restart while the restart runs, then follows the real state. */
async function restartPluginRuntime(pluginKey, elements, showToast) {
  if (!pluginKey || pendingRestart.has(pluginKey) || pendingRuntime.has(pluginKey)) return;
  pendingRestart.add(pluginKey);
  statusGeneration += 1;
  if (latestStatus) renderSensorTiles(elements.sensorTiles, latestStatus);
  try {
    await postJson(`/api/admin/plugins/${encodeURIComponent(pluginKey)}/restart`, {});
    showToast?.(t('sensorConnection.restarted', '{name} restarted').replace('{name}', pluginDisplayName(pluginKey)), 'success');
  } catch (error) {
    console.error('[admin] Plugin restart failed:', error);
    showToast?.(`${t('sensorConnection.restartFailed', 'Could not restart {name}').replace('{name}', pluginDisplayName(pluginKey))}: ${error?.message || error}`, 'error');
  } finally {
    pendingRestart.delete(pluginKey);
    statusGeneration += 1;
    await refreshAdminStatus(elements, showToast);
  }
}

/**
 * The switch shows the plugin's real running state. While the request runs
 * it keeps the chosen position (disabled), and status polls that started
 * before the request finished are discarded, so it never jumps back.
 */
async function switchPluginRuntime(pluginKey, wantRunning, elements, showToast) {
  if (!pluginKey || pendingRuntime.has(pluginKey) || pendingRestart.has(pluginKey)) return;
  pendingRuntime.set(pluginKey, Boolean(wantRunning));
  statusGeneration += 1;
  if (latestStatus) renderSensorTiles(elements.sensorTiles, latestStatus);
  const name = pluginDisplayName(pluginKey);
  try {
    await postJson(`/api/admin/plugins/${encodeURIComponent(pluginKey)}/${wantRunning ? 'start' : 'stop'}`, {});
    showToast?.(
      (wantRunning
        ? t('sensorConnection.switchedOn', '{name} switched on')
        : t('sensorConnection.switchedOff', '{name} switched off')).replace('{name}', name),
      'success',
    );
  } catch (error) {
    console.error('[admin] Plugin switch failed:', error);
    showToast?.(`${t('sensorConnection.switchFailed', 'Could not switch {name}').replace('{name}', name)}: ${error?.message || error}`, 'error');
  } finally {
    pendingRuntime.delete(pluginKey);
    statusGeneration += 1;
    await refreshAdminStatus(elements, showToast);
  }
}

/** A dropped (or arrow-key moved) tile: show it there at once, then remember it. */
async function moveSensorTile(key, column, position, elements, showToast) {
  if (!tileLayout) return;
  const handleFocused = document.activeElement?.closest?.('[data-tile-handle]');
  await saveTileLayout(moveTile(tileLayout, key, column, position), elements, showToast);
  if (handleFocused) {
    [...elements.sensorTiles.querySelectorAll('[data-plugin-tile]')]
      .find((tile) => tile.dataset.pluginTile === key)
      ?.querySelector('[data-tile-handle]')
      ?.focus({ preventScroll: true });
  }
}

/** Store the arrangement for this computer; ``null`` goes back to the default order. */
async function saveTileLayout(layout, elements, showToast) {
  const previous = savedTileLayout;
  savedTileLayout = layout;
  tileLayoutSaving += 1;
  statusGeneration += 1;  // a poll from before this change must not undo it
  if (latestStatus) renderSensorTiles(elements.sensorTiles, latestStatus);
  try {
    const response = layout
      ? await requestJson('/api/admin/dashboard-layout', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(layout),
      })
      : await requestJson('/api/admin/dashboard-layout', { method: 'DELETE' });
    savedTileLayout = response?.layout?.columns ? response.layout : null;
    if (!layout) showToast?.(t('dashboard.tiles.resetDone', 'Tile order reset'), 'success');
  } catch (error) {
    console.error('[admin] Tile order could not be saved:', error);
    savedTileLayout = previous;
    showToast?.(`${t('dashboard.tiles.saveFailed', 'Could not save the tile order')}: ${error?.message || error}`, 'error');
  } finally {
    tileLayoutSaving -= 1;
    statusGeneration += 1;
    if (latestStatus) {
      latestStatus.dashboard_layout = savedTileLayout || { columns: null };
      renderSensorTiles(elements.sensorTiles, latestStatus);
      renderPluginControls(elements.controls, latestStatus.plugins || {}, latestStatus);
    }
  }
}

async function runConnectionStep(button, elements, showToast) {
  const pluginKey = button.dataset.pluginKey || '';
  const actionKey = button.dataset.pluginAdminAction || '';
  if (!pluginKey || !actionKey || button.disabled) return;
  const connection = latestStatus?.plugins?.[pluginKey]?.connection || {};
  if (button.dataset.connectionAction === 'scan' && connection.phase === 'connected') {
    const device = connection.device?.label || pluginDisplayName(pluginKey);
    if (!window.confirm(t('sensorConnection.confirmScan', 'Searching disconnects {device}. Continue?').replace('{device}', device))) return;
  }
  // The auto-reconnect button is a switch: it sends the new position.
  const payload = button.dataset.connectionAction === 'auto_reconnect'
    ? { enabled: button.getAttribute('aria-pressed') !== 'true' }
    : {};
  await postConnectionAction(pluginKey, actionKey, payload, elements, showToast);
}

async function runConnectionSelect(select, elements, showToast) {
  const pluginKey = select.dataset.pluginKey || '';
  const actionKey = select.dataset.pluginAdminAction || '';
  if (!select.value || !pluginKey || !actionKey) return;
  let payload;
  try {
    payload = JSON.parse(select.value);
  } catch {
    return;
  }
  await postConnectionAction(pluginKey, actionKey, payload, elements, showToast);
}

async function postConnectionAction(pluginKey, actionKey, payload, elements, showToast) {
  const pendingKey = `${pluginKey}:${actionKey}`;
  if (pluginActionPending(pluginKey)) return;
  pendingPluginActions.add(pendingKey);
  statusGeneration += 1;
  if (latestStatus) renderSensorTiles(elements.sensorTiles, latestStatus);
  try {
    const response = await postJson(
      `/api/admin/plugins/${encodeURIComponent(pluginKey)}/actions/${encodeURIComponent(actionKey)}`,
      payload,
    );
    const result = response?.result || {};
    showToast?.(result.last_message || result.message || t('dashboard.pluginActionDone', 'Plugin action completed'), 'success');
  } catch (error) {
    console.error('[admin] Connection action failed:', error);
    showToast?.(error.message || t('dashboard.pluginActionFailed', 'Plugin action failed'), 'error');
  } finally {
    pendingPluginActions.delete(pendingKey);
    statusGeneration += 1;
    await refreshAdminStatus(elements, showToast);
  }
}

function pluginDisplayName(pluginKey) {
  return pluginByKey(pluginKey)?.ui?.label || formatPluginName(pluginKey);
}

async function runPluginAdminAction(button, elements, showToast) {
  const pluginKey = button.dataset.pluginKey || '';
  const actionKey = button.dataset.pluginAdminAction || '';
  const declared = (pluginByKey(pluginKey)?.capability_config?.admin_actions?.actions || [])
    .find((candidate) => candidate.key === actionKey);
  if (!declared || button.disabled) return;
  const pendingKey = `${pluginKey}:${actionKey}`;
  const tile = button.closest('[data-plugin-tile]');
  if (tile?.dataset.runtimeLocked === 'true' || pluginActionPending(pluginKey)) return;
  if (declared.confirm && !window.confirm(t(`plugins.${pluginKey}.actions.${actionKey}.confirm`, declared.description || declared.label || actionKey))) return;

  button.disabled = true;
  pendingPluginActions.add(pendingKey);
  updatePluginActionAvailability(tile, pluginKey);
  try {
    let payload = {};
    const selector = button.closest('[data-plugin-action-select]')?.querySelector('select');
    const encodedPayload = selector ? selector.value : button.dataset.pluginAdminPayload;
    if (selector && !encodedPayload) return;
    if (encodedPayload) {
      try {
        payload = JSON.parse(encodedPayload);
      } catch {
        throw new Error(t('dashboard.pluginActionPayloadInvalid', 'Plugin action payload is invalid'));
      }
    }
    const response = await postJson(
      `/api/admin/plugins/${encodeURIComponent(pluginKey)}/actions/${encodeURIComponent(actionKey)}`,
      payload,
    );
    const result = response?.result || {};
    showToast?.(
      result.last_message || result.message || t('dashboard.pluginActionDone', 'Plugin action completed'),
      'success',
    );
    await refreshAdminStatus(elements, showToast);
  } catch (error) {
    console.error('[admin] Plugin action failed:', error);
    showToast?.(error.message || t('dashboard.pluginActionFailed', 'Plugin action failed'), 'error');
  } finally {
    pendingPluginActions.delete(pendingKey);
    updatePluginActionAvailability(tile, pluginKey);
  }
}

async function resetSensorOverrides(elements, showToast) {
  try {
    await postJson('/api/admin/session-overrides/reset', {});
    showToast?.(t('dashboard.overrideResetDone', 'Temporary dashboard overrides reset'), 'success');
    await refreshAdminStatus(elements, showToast);
  } catch (error) {
    console.error('[admin] Sensor override reset failed:', error);
    showToast?.(t('dashboard.overrideResetFailed', 'Could not reset dashboard overrides'), 'error');
  }
}

function getDashboardElements() {
  return {
    editView: document.getElementById('admin-edit-view'),
    dashboard: document.getElementById('admin-dashboard'),
    dashboardButton: document.getElementById('btn-admin-dashboard'),
    studyBar: document.getElementById('dashboard-study-bar'),
    studyName: document.getElementById('dashboard-study-name'),
    studyStatus: document.getElementById('dashboard-study-status'),
    startButton: document.getElementById('btn-dashboard-start-study'),
    sensorTiles: document.getElementById('dashboard-sensor-tiles'),
    controls: document.getElementById('dashboard-plugin-controls'),
    xdf: document.getElementById('dashboard-xdf'),
  };
}

// One refresh at a time: a slow server must not stack up overlapping polls.
// A failure names its step and is toasted once until the next success, so a
// problem is visible without a toast every two seconds.
let statusRefreshInFlight = false;
let lastStatusFailure = '';
// Bumped by every operator action: a poll that started before the action
// finished must not overwrite what the action just changed.
let statusGeneration = 0;
let refreshQueued = false;

async function refreshAdminStatus(elements, showToast) {
  if (statusRefreshInFlight) {
    refreshQueued = true;
    return;
  }
  statusRefreshInFlight = true;
  const generation = statusGeneration;
  let step = 'status';
  try {
    const [status, runtimeInfo] = await Promise.all([
      getJson('/api/admin/status', { timeoutMs: STATUS_REQUEST_TIMEOUT_MS }),
      getJson('/api/runtime-info', { timeoutMs: STATUS_REQUEST_TIMEOUT_MS }),
      loadPluginCatalog(),
    ]);
    if (generation !== statusGeneration) {
      refreshQueued = true;
      return;
    }
    step = 'extensions';
    await loadPluginUiExtensions('dashboard');
    step = 'render';
    status.runtime_info = runtimeInfo;
    renderAdminStatus(elements, status);
    lastStatusFailure = '';
  } catch (error) {
    const detail = `${step}: ${error?.message || error}`;
    console.error(`[admin] Dashboard status failed (${detail})`, error);
    if (detail !== lastStatusFailure) {
      lastStatusFailure = detail;
      showToast?.(`${t('dashboard.statusFailed', 'Dashboard status failed')} (${detail})`, 'error');
    }
  } finally {
    statusRefreshInFlight = false;
    if (refreshQueued) {
      refreshQueued = false;
      void refreshAdminStatus(elements, showToast);
    }
  }
}

function renderAdminStatus(elements, status) {
  latestStatus = status;
  if (!tileLayoutSaving) savedTileLayout = status.dashboard_layout?.columns ? status.dashboard_layout : null;
  const clients = status.study_clients || {};
  elements.dashboardButton.hidden = false;

  renderStudyBar(elements, status);
  renderSensorTiles(elements.sensorTiles, status);
  renderPluginControls(elements.controls, status.plugins || {}, status);
  renderXdf(elements.xdf, status);
  applyRunScope(status);
}

function nextStepText(plugin) {
  const connection = plugin?.connection || {};
  const step = connection.next_step
    || (connection.phase === 'off' ? 'switch_on' : 'wait');
  const fallbacks = {
    scan: 'search', select: 'choose the device', measure_signal: 'measure the signal',
    initialize: 'initialize', switch_on: 'switch on', wait: 'wait until connected',
  };
  return t(`sensorConnection.nextStep.${step}`, fallbacks[step] || step);
}

function renderStudyBar(elements, status) {
  if (!elements.studyBar) return;
  const state = studyBarState(status);
  const studyId = state.runState.study_id || '';
  elements.studyBar.hidden = !studyId;
  if (!studyId) return;
  if (elements.studyName) elements.studyName.textContent = studyId;
  const parts = [];
  if (state.running) {
    parts.push(t('dashboard.studyBar.running', 'The study is running.'));
  } else {
    if (state.needed.length) {
      parts.push(t('dashboard.studyBar.sensorsReady', '{ready}/{total} sensors ready')
        .replace('{ready}', String(state.ready.length))
        .replace('{total}', String(state.needed.length)));
      const first = state.missing[0];
      if (first) {
        const label = state.plugins[first]?.label || pluginDisplayName(first);
        parts.push(`${label}: ${nextStepText(state.plugins[first])}`);
      }
    } else {
      parts.push(t('dashboard.studyBar.noSensors', 'This study needs no sensors.'));
    }
    parts.push(state.tabletOk
      ? t('dashboard.studyBar.tabletReady', 'Tablet connected')
      : t('dashboard.studyBar.tabletMissing', 'Connect the tablet'));
  }
  if (elements.studyStatus) elements.studyStatus.textContent = parts.join(' · ');
  if (elements.startButton) {
    const allReady = !state.missing.length && state.tabletOk;
    elements.startButton.disabled = state.running;
    elements.startButton.classList.toggle('btn-primary', allReady && !state.running);
    elements.startButton.classList.toggle('btn-secondary', !(allReady && !state.running));
  }
}

export function renderSensorTiles(target, status) {
  if (!target) return;
  const plugins = status.plugins || {};
  const items = getPluginCatalog().plugins
    .filter((manifest) => isPluginVisible(manifest, PLUGIN_UI_SURFACES.DASHBOARD))
    .map((manifest) => ({
      ...(plugins[manifest.plugin_key] || {}),
      key: manifest.plugin_key,
      label: plugins[manifest.plugin_key]?.label || manifest.ui?.label || manifest.plugin_key,
      manifest,
    }));
  items.push({ key: PARTICIPANT_TILE_KEY, label: t('dashboard.studyClient', 'Study client') });

  const keys = new Set(items.map((item) => item.key));
  target.querySelectorAll('[data-plugin-tile]').forEach((tile) => {
    if (!keys.has(tile.dataset.pluginTile)) tile.remove();
  });
  const columns = sensorColumns(target);
  tileLayout = mergeLayout(savedTileLayout, items.map((item) => item.key));
  const singleColumnOrder = rowMajorOrder(tileLayout);
  items.forEach((item) => {
    if (item.key === PARTICIPANT_TILE_KEY) {
      const tile = renderParticipantTile(target, status);
      if (!tileDrag?.isDragging()) {
        const column = tileLayout.columns.findIndex((keysInColumn) => keysInColumn.includes(item.key));
        placeTile(columns[column], tile, tileLayout.columns[column].indexOf(item.key), singleColumnOrder.indexOf(item.key));
      }
      return;
    }
    const detail = renderPluginDashboardDetail(item, status);
    const icon = pluginUiIcon(item.manifest);
    let tile = [...target.querySelectorAll('[data-plugin-tile]')].find((node) => node.dataset.pluginTile === item.key);
    if (!tile) {
      const template = document.createElement('template');
      template.innerHTML = `
      <article class="dashboard-card sensor-tile" data-plugin-tile="${escapeHtml(item.key)}">
        <div class="dashboard-card-title" data-tile-handle tabindex="0" role="group" title="${escapeHtml(t('dashboard.tiles.handle', 'Drag to move this tile; on the keyboard the arrow keys move it'))}"><i class="iconoir-menu-scale tile-grip" aria-hidden="true"></i><i class="${escapeHtml(icon)}"></i> <span>${escapeHtml(item.label || item.key)}</span></div>
        <div class="dashboard-card-body"><div data-plugin-inline-controls></div><div data-plugin-live></div><div data-plugin-detail></div><div data-plugin-actions></div>${renderPluginConsoleButton(item.manifest)}</div>
      </article>`.trim();
      tile = template.content.firstElementChild;
    }
    // While a tile is being dragged, polling must not move tiles under the pointer.
    if (!tileDrag?.isDragging()) {
      const column = tileLayout.columns.findIndex((keysInColumn) => keysInColumn.includes(item.key));
      placeTile(columns[column], tile, tileLayout.columns[column].indexOf(item.key), singleColumnOrder.indexOf(item.key));
    }
    const locked = Boolean(item.runtime_locked || status.active_study_session);
    tile.dataset.runtimeLocked = String(locked);
    const template = document.createElement('template');
    template.innerHTML = detail;
    const inlineControls = template.content.querySelector('[data-plugin-dashboard-controls]');
    // Every sensor gets the same connection panel; a plugin's own controls
    // are only used for plugins without a connection block.
    const panel = renderSensorConnectionPanel(item, item.manifest, {
      locked,
      pending: pluginActionPending(item.key),
      pendingRunning: pendingRuntime.has(item.key) ? pendingRuntime.get(item.key) : undefined,
      pendingRestart: pendingRestart.has(item.key),
      deviatesFromStudy: Boolean((status.sensor_runtime?.override_active || {})[item.key]),
    });
    renderPluginActionControls(tile.querySelector('[data-plugin-inline-controls]'), panel || inlineControls?.outerHTML || '');
    inlineControls?.remove();
    // The live view of the sensor data contract: the same graphs for every
    // sensor that declares one, always in place (empty while it is off).
    const liveSlot = tile.querySelector('[data-plugin-live]');
    const liveHtml = renderLiveTrends({ manifest: item.manifest, live: item.live }, dashboardUiHelpers());
    if (liveSlot && liveSlot._rendered !== liveHtml) {
      liveSlot.innerHTML = liveHtml;
      liveSlot._rendered = liveHtml;
    }
    const body = tile.querySelector('[data-plugin-detail]');
    const focusable = 'button, input, select, textarea, summary, [tabindex]';
    const focused = body.contains(document.activeElement) ? document.activeElement : null;
    const focusIndex = [...body.querySelectorAll(focusable)].indexOf(focused);
    const focusIdentity = (node) => node && `${node.tagName}:${node.id}:${node.dataset.dashboardAction || node.dataset.target || node.getAttribute('aria-label') || ''}`;
    const oldFocusIdentity = focusIdentity(focused);
    const openDetails = [...body.querySelectorAll('details')].map((node) => node.open);
    body.innerHTML = template.innerHTML;
    body.querySelectorAll('details').forEach((node, index) => { node.open = openDetails[index] || false; });
    const nextFocus = body.querySelectorAll(focusable)[focusIndex];
    if (focused && focusIdentity(nextFocus) === oldFocusIdentity) nextFocus.focus({ preventScroll: true });
    const actions = tile.querySelector('[data-plugin-actions]');
    renderPluginActionControls(actions, renderPluginAdminActions(item.manifest, item));
    updatePluginActionAvailability(tile, item.key);
  });
  updateMarquees(target);
}

function pluginActionPending(pluginKey) {
  return [...pendingPluginActions].some((key) => key.startsWith(`${pluginKey}:`));
}

function updatePluginActionAvailability(tile, pluginKey) {
  if (!tile) return;
  const locked = tile.dataset.runtimeLocked === 'true';
  const pending = pluginActionPending(pluginKey);
  tile.querySelectorAll('[data-plugin-admin-action], select[data-action-key]').forEach((node) => {
    // A connection step the panel disabled for a reason stays disabled; a
    // lock-exempt control (auto-reconnect) stays usable while recording.
    const lockBlocks = locked && !node.hasAttribute('data-lock-exempt');
    node.disabled = lockBlocks || pending || node.hasAttribute('data-blocked');
  });
}

function optionIdentity(option) {
  return option?.dataset.optionKey || option?.value || '';
}

function renderPluginActionControls(container, html) {
  const choices = new Map([...container.querySelectorAll('select')]
    .map((node) => [node.id, optionIdentity(node.selectedOptions[0])]));
  const focused = container.contains(document.activeElement) ? document.activeElement : null;
  if (focused?.tagName === 'SELECT') {
    // Keep the native popup alive, but invalidate a target that disappeared.
    const template = document.createElement('template');
    template.innerHTML = html;
    container.querySelectorAll('select').forEach((node) => {
      const incoming = [...template.content.querySelectorAll('select')].find((next) => next.id === node.id);
      if (!incoming || ![...incoming.options].some((option) => optionIdentity(option) === choices.get(node.id))) node.value = '';
    });
    return;
  }
  if (container._rendered === html) return;
  const focusKey = focused ? controlIdentity(focused) : '';
  container.innerHTML = html;
  container._rendered = html;
  if (focusKey) {
    const again = [...container.querySelectorAll('button, input')].find((node) => controlIdentity(node) === focusKey);
    again?.focus({ preventScroll: true });
  }
  // Restore only a real earlier choice; otherwise keep the renderer's own
  // `selected` default (for example the band that is connected right now).
  container.querySelectorAll('select').forEach((node) => {
    const previous = choices.get(node.id);
    if (!previous) return;
    const selected = [...node.options].find((option) => optionIdentity(option) === previous);
    if (selected) node.value = selected.value;
  });
}

function controlIdentity(node) {
  return [
    node.dataset?.connectionAction,
    node.closest?.('[data-runtime-switch]')?.dataset.runtimeSwitch,
    node.dataset?.target,
    node.dataset?.dashboardAction,
    node.dataset?.pluginAdminAction,
  ].filter(Boolean).join(':');
}

function renderPluginDashboardDetail(item, status) {
  const extension = getPluginUiExtension(item.manifest, 'dashboard');
  if (!extension) return genericSensorDetail(item);
  try {
    const rendered = extension.renderDashboard(
      { plugin: item, status, manifest: item.manifest },
      dashboardUiHelpers(),
    );
    return typeof rendered === 'string' ? rendered : genericSensorDetail(item);
  } catch (error) {
    console.warn(`[admin] ${item.key} dashboard extension failed:`, error);
    return genericSensorDetail(item);
  }
}

function renderPluginAdminActions(manifest, pluginStatus) {
  if (manifest?.capability_config?.admin_actions?.rendered_by_dashboard) return '';
  // Actions with a connection role are drawn by the connection panel; a
  // plugin's own configurator runs its "configurator" actions itself.
  const actions = (manifest?.capability_config?.admin_actions?.actions || [])
    .filter((action) => !action.role && (action.surface || 'panel') === 'panel');
  if (!actions.length) return '';
  const buttons = actions.flatMap((action) => renderManifestActionInstances(action, manifest, pluginStatus));
  if (!buttons.length) return '';
  return `
    <div class="dashboard-actions plugin-admin-actions">
      ${buttons.join('')}
    </div>`;
}

function renderManifestActionInstances(action, manifest, pluginStatus) {
  const instanceConfig = action?.instances;
  if (!instanceConfig) {
    return [renderManifestActionButton(action, manifest, {}, '')];
  }
  const instances = (instanceConfig.status_paths || [])
    .map((path) => readObjectPath(pluginStatus, path))
    .find((value) => Array.isArray(value)) || [];
  if (instanceConfig.presentation === 'select') {
    const id = `plugin-action-${manifest.plugin_key}-${action.key}`;
    const options = instances.map((instance) => {
      const payload = {};
      Object.entries(instanceConfig.payload_map || {}).forEach(([target, source]) => {
        const value = readObjectPath(instance, source);
        if (value !== undefined && value !== null && value !== '') payload[target] = value;
      });
      const label = [...new Set((instanceConfig.label_fields || []).map((path) => readObjectPath(instance, path)).filter(Boolean))].join(' - ');
      return `<option value="${escapeHtml(JSON.stringify(payload))}">${escapeHtml(label)}</option>`;
    });
    return [`<div data-plugin-action-select><label for="${escapeHtml(id)}">${escapeHtml(t('dashboard.selectDevice', 'Select device'))}</label>
      <select class="dashboard-select" id="${escapeHtml(id)}" data-action-key="${escapeHtml(action.key)}"><option value="">${escapeHtml(t('dashboard.chooseDevice', 'Choose a device'))}</option>${options.join('')}</select>
      ${renderManifestActionButton(action, manifest, {}, '')}</div>`];
  }
  return instances
    .filter((instance) => instance && typeof instance === 'object' && !Array.isArray(instance))
    .map((instance) => {
      const payload = {};
      Object.entries(instanceConfig.payload_map || {}).forEach(([target, source]) => {
        const value = readObjectPath(instance, source);
        if (value !== undefined && value !== null && value !== '') payload[target] = value;
      });
      const detail = (instanceConfig.label_fields || [])
        .map((path) => readObjectPath(instance, path))
        .filter((value, index, values) => value !== undefined && value !== null && value !== '' && values.indexOf(value) === index)
        .join(' - ');
      return renderManifestActionButton(action, manifest, payload, detail);
    });
}

function renderManifestActionButton(action, manifest, payload, detail) {
  const actionLabel = t(`plugins.${manifest.plugin_key}.actions.${action.key}`, action.label || formatPluginName(action.key));
  const label = detail ? `${actionLabel}: ${detail}` : actionLabel;
  return `
    <button type="button" class="btn-secondary${action.danger ? ' plugin-admin-action--danger' : ''}"
            data-dashboard-action="plugin_admin_action"
            data-plugin-key="${escapeHtml(manifest.plugin_key)}"
            data-plugin-admin-action="${escapeHtml(action.key)}"
            data-plugin-admin-payload="${escapeHtml(JSON.stringify(payload))}"
            title="${escapeHtml(action.description || actionLabel)}">
      ${escapeHtml(label)}
    </button>`;
}

function renderPluginConsoleButton(manifest, compact = false) {
  if (!manifest?.runtime?.interactive_stdin) return '';
  const className = compact ? 'btn-secondary btn-xs' : 'btn-secondary';
  const button = `<button type="button" class="${className}" data-plugin-console="${escapeHtml(manifest.plugin_key)}">
      <i class="iconoir-terminal"></i> ${escapeHtml(t('pluginConsole.open', 'Diagnostics'))}
    </button>`;
  return compact ? button : `<div class="dashboard-actions plugin-console-action">${button}</div>`;
}

function readObjectPath(source, path) {
  return String(path || '').split('.').reduce(
    (value, key) => (value && typeof value === 'object' ? value[key] : undefined),
    source,
  );
}

/** Fallback tile for any plugin without a bespoke view. */
function genericSensorDetail(item) {
  if (item.connection) {
    return `
    <dl class="status-list">
      <dt>${fieldLabel('device', 'Device')}</dt><dd>${escapeHtml(item.device_label || '-')}</dd>
      <dt>${fieldLabel('lastActive', 'Last active')}</dt><dd>${formatTimestampAge(item.last_activity_at, item.seconds_since_last_activity)}</dd>
      <dt>${fieldLabel('message', 'Message')}</dt><dd>${escapeHtml(item.last_message || '-')}</dd>
    </dl>`;
  }
  return `
    <div class="status-row">
      <span class="status-pill status-pill--${escapeHtml(item.status || 'unknown')}">${escapeHtml(statusLabel(item.status))}</span>
      <strong>${formatEnabled(item.configured_enabled)}</strong>
    </div>
    <dl class="status-list">
      <dt>${fieldLabel('device', 'Device')}</dt><dd>${escapeHtml(item.device_label || '-')}</dd>
      <dt>${fieldLabel('lastActive', 'Last active')}</dt><dd>${formatTimestampAge(item.last_activity_at, item.seconds_since_last_activity)}</dd>
      <dt>${fieldLabel('message', 'Message')}</dt><dd>${escapeHtml(item.last_message || '-')}</dd>
    </dl>
    ${renderRuntimeButtons(item)}`;
}

/**
 * Grey out what only means something during a run.
 *
 * The sensors above are useful any time - that is the point of reaching the
 * dashboard before pressing Play. The live study readout is not.
 */
function applyRunScope(status) {
  const running = (status.study_run_state || {}).status === 'running';
  document.querySelectorAll('[data-run-scoped]').forEach((element) => {
    element.classList.toggle('is-idle', !running);
  });
}

function renderPluginControls(target, plugins, status = {}) {
  if (!target) return;
  const rows = getPluginCatalog().plugins
    .filter((manifest) => (
      isPluginVisible(manifest, PLUGIN_UI_SURFACES.DASHBOARD)
      || manifest?.runtime?.interactive_stdin
    ))
    .map((manifest) => ({
      ...(plugins[manifest.plugin_key] || {}),
      key: manifest.plugin_key,
      label: plugins[manifest.plugin_key]?.label || manifest.ui?.label || manifest.plugin_key,
      manifest,
    }));
  if (!rows.length) {
    target.innerHTML = `<p>${escapeHtml(t('dashboard.noPlugins', 'No plugins registered.'))}</p>`;
    return;
  }
  const sensorRuntime = status.sensor_runtime || {};
  const hasOverrides = Object.values(sensorRuntime.override_active || {}).some(Boolean)
    || Object.keys(status.session_sensor_overrides || {}).length > 0;
  const warning = hasOverrides
    ? `<div class="plugin-control-warning">${escapeHtml(t('dashboard.overrideWarning', 'Temporary dashboard overrides are active for this server session.'))}</div>`
    : '';
  const resetButton = hasOverrides
    ? `<button type="button" class="btn-secondary btn-xs" data-dashboard-action="reset_sensor_overrides">${escapeHtml(t('dashboard.overrideReset', 'Reset to study settings'))}</button>`
    : '';
  const tileKeys = [
    ...rows.filter((row) => isPluginVisible(row.manifest, PLUGIN_UI_SURFACES.DASHBOARD)).map((row) => row.key),
    PARTICIPANT_TILE_KEY,
  ];
  const tileOrderButton = savedTileLayout && !isDefaultLayout(mergeLayout(savedTileLayout, tileKeys), tileKeys)
    ? `<button type="button" class="btn-secondary btn-xs" data-dashboard-action="reset_tile_order"><i class="iconoir-menu-scale"></i> ${escapeHtml(t('dashboard.tiles.reset', 'Reset tile order'))}</button>`
    : '';
  const settingsButton = `<button type="button" class="btn-secondary btn-xs" data-dashboard-action="open_settings"><i class="iconoir-settings"></i> ${escapeHtml(t('dashboard.openSettings', 'Open settings'))}</button>`;
  target.innerHTML = `<div class="plugin-controls">
    <div class="plugin-control-warning plugin-control-warning--info">${escapeHtml(t('dashboard.settingsHint', 'Plugin setup lives in Settings. This dashboard focuses on live status, start, stop, and recovery.'))}</div>
    ${warning}
    <div class="plugin-control-reset">${settingsButton}${resetButton}${tileOrderButton}</div>
    ${rows.map((row) => renderPluginControlRow(row, sensorRuntime)).join('')}
  </div>`;
}

function renderPluginControlRow(item, sensorRuntime = {}) {
  const sensorEffective = sensorRuntime.effective || {};
  const hasSensorState = Object.prototype.hasOwnProperty.call(sensorEffective, item.key);
  const configured = hasSensorState ? Boolean(sensorEffective[item.key]) : Boolean(item.configured_enabled ?? item.enabled);
  const status = item.status || (configured ? 'enabled' : 'disabled');
  const runtimeDetail = hasSensorState ? renderSensorRuntimeDetail(item.key, sensorRuntime) : '';

  return `
    <div class="plugin-control-row">
      <div class="plugin-control-main">
        <span class="status-pill status-pill--${escapeHtml(status)}">${escapeHtml(statusLabel(status))}</span>
        <div>
          <strong>${escapeHtml(item.label || item.key)}</strong>
          <span>${buildPluginDetail(item)}</span>
          ${runtimeDetail}
        </div>
      </div>
      <div class="plugin-control-actions">
        <span class="plugin-control-note">${escapeHtml(item.can_toggle ? t('dashboard.settingsManagedInHub', 'Settings hub') : t('dashboard.managedByParent', 'Managed by parent plugin'))}</span>
        ${renderPluginConsoleButton(item.manifest, true)}
      </div>
    </div>`;
}

function buildPluginDetail(item) {
  const details = [];
  if (item.category) details.push(item.category);
  if (item.device_label) details.push(item.device_label);
  if (item.lsl_enabled !== undefined) details.push(`${t('dashboard.dataLsl', 'data LSL')} ${formatOnOff(item.lsl_enabled)}`);
  if (item.recording_enabled !== undefined) details.push(`${t('dashboard.recording', 'recording')} ${formatOnOff(item.recording_enabled)}`);
  if (item.scan_timeout_seconds !== undefined && item.scan_timeout_seconds !== null) details.push(`${t('dashboard.scan', 'scan')} ${item.scan_timeout_seconds}s`);
  if (item.url) details.push(item.url);
  if (item.host) details.push(`${item.host}:${item.port || ''}`);
  return escapeHtml(details.filter(Boolean).join(' - ') || item.last_message || '-');
}

function renderSensorRuntimeDetail(sensorKey, sensorRuntime = {}) {
  const study = sensorRuntime.study || {};
  const overrides = sensorRuntime.overrides || {};
  const overrideActive = sensorRuntime.override_active || {};
  const effective = sensorRuntime.effective || {};
  const hasOverride = Boolean(overrideActive[sensorKey]);
  const overrideLabel = hasOverride ? formatOnOff(overrides[sensorKey]) : t('dashboard.none', 'none');
  return `<span class="plugin-runtime-detail">
    ${escapeHtml(t('dashboard.runtimeStudy', 'Study'))}: ${escapeHtml(formatOnOff(study[sensorKey]))}
    · ${escapeHtml(t('dashboard.runtimeOverride', 'Override'))}: ${escapeHtml(overrideLabel)}
    · ${escapeHtml(t('dashboard.runtimeEffective', 'Effective'))}: ${escapeHtml(formatOnOff(effective[sensorKey]))}
    ${hasOverride ? `<em>${escapeHtml(t('dashboard.temporaryOverride', 'temporary dashboard override'))}</em>` : ''}
  </span>`;
}

function renderXdf(target, status) {
  if (!target) return;
  const infrastructure = status.recording_infrastructure || {};
  const worker = status.recording_worker || {};
  const health = worker.health || {};
  const workerStatus = health.status || worker.status || (infrastructure.available ? 'idle' : 'unavailable');
  const healthAge = Number.isFinite(Number(worker.health_at_epoch))
    ? `${Math.max(0, (Date.now() / 1000) - Number(worker.health_at_epoch)).toFixed(1)} s`
    : '-';
  const workerIssues = Array.isArray(health.issues) ? health.issues : [];
  const issueList = workerIssues.length
    ? `<ul class="status-warning recording-worker-issues">${workerIssues.map((issue) => {
      const code = issue?.code || 'recording_worker_issue';
      const message = issue?.message || issue?.error || code;
      return `<li><strong>${escapeHtml(code)}</strong>: ${escapeHtml(message)}</li>`;
    }).join('')}</ul>`
    : '';
  const state = infrastructure.available
    ? t('dashboard.enabled', 'Enabled')
    : t('dashboard.disabled', 'Disabled');
  target.innerHTML = `
    <div class="status-row">
      <span class="status-pill status-pill--${escapeHtml(workerStatus)}">${escapeHtml(statusLabel(workerStatus))}</span>
      <strong>${escapeHtml(worker.session_id || t('dashboard.noActiveSession', 'No active recording session'))}</strong>
    </div>
    <dl class="status-list">
      <dt>${fieldLabel('recordingWorker', 'Recording worker')}</dt><dd>${escapeHtml(state)}</dd>
      <dt>${fieldLabel('workerHealthAge', 'Worker health age')}</dt><dd>${escapeHtml(healthAge)}</dd>
      <dt>${fieldLabel('workerHealthFailures', 'Health poll failures')}</dt><dd>${escapeHtml(worker.worker_health_failures ?? 0)}</dd>
      <dt>${fieldLabel('message', 'Message')}</dt><dd>${escapeHtml(worker.last_error || infrastructure.reason || '-')}</dd>
    </dl>
    ${issueList}
  `;
}

function showDashboard(elements) {
  elements.editView.hidden = true;
  elements.dashboard.hidden = false;
}

window.addEventListener('beforeunload', () => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
  }
});
