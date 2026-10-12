/** Software update status, confirmation, progress, and restart handling. */
export function createAdminUpdateHandling(context) {
  const { state, getJson, postJson, showToast, t, confirmWithModal, $ } = context;

  async function loadUpdateStatus({ silent = false } = {}) {
    try {
      const status = await getJson('/api/admin/update/status');
      state.updateStatus = status;
      renderUpdateStatus(status);
    } catch (error) {
      console.error('[admin] Could not load update status:', error);
      renderUpdateStatusError(error);
      if (!silent) {
        showToast(t('update.statusFailed', 'Update status failed'), 'error');
      }
    }
  }
  
  async function checkForPythonUpdate() {
    setUpdateBusy(true);
    try {
      const status = await postJson('/api/admin/update/check', {});
      state.updateStatus = status;
      renderUpdateStatus(status);
      const available = Boolean(status.update?.available);
      showToast(available ? t('update.availableToast', 'Update available') : t('update.currentToast', 'Study Runner is current'), available ? 'info' : 'success');
    } catch (error) {
      console.error('[admin] Update check failed:', error);
      showToast(error.message || t('update.checkFailed', 'Update check failed'), 'error');
      await loadUpdateStatus({ silent: true });
    } finally {
      setUpdateBusy(false);
    }
  }
  
  /**
   * One flow for source installs (release archive or git clone): confirm what
   * will be ended, download and verify, then restart into the new version.
   * The server ends the study run and aborts a recording session itself
   * ("Software update"); finalizations and uploads continue after the restart.
   */
  async function runSourceUpdate() {
    const version = state.updateStatus?.update?.version || '';
    let activity = {};
    try {
      activity = (await getJson('/api/admin/update/status'))?.activity || {};
    } catch {
      activity = {};
    }
    const consequences = [
      activity.active_session
        ? t('update.endsSession', '- The running session ({participant}) is aborted with the reason "Software update". Data recorded so far is kept.')
          .replace('{participant}', activity.participant_id || '?')
        : '',
      activity.study_run_status === 'running'
        ? t('update.endsRun', '- The running study run is ended.')
        : '',
      activity.pending_finalizations
        ? t('update.finalizationsContinue', '- {count} finalization(s) or upload(s) continue after the restart.')
          .replace('{count}', String(activity.pending_finalizations))
        : '',
      t('update.restartsWindow', '- Study Runner restarts in a new window; this page reloads by itself.'),
    ].filter(Boolean);
    const proceed = await confirmWithModal({
      title: t('update.updateNowTitle', 'Update Study Runner'),
      message: `${t('update.updateNowQuestion', 'Update Study Runner to {version} now?').replace('{version}', version)}\n\n${consequences.join('\n')}`,
      confirmLabel: t('update.updateSourceAction', 'Update now'),
      cancelLabel: t('common.cancel', 'Cancel'),
    });
    if (!proceed) return;
    if (activity.active_session) {
      const reallyAbort = await confirmWithModal({
        title: t('update.abortSessionTitle', 'Abort the running session?'),
        message: t('update.abortSessionMessage', 'Participant {participant} is in a session right now. The update aborts it immediately.')
          .replace('{participant}', activity.participant_id || '?'),
        confirmLabel: t('update.abortSessionConfirm', 'Abort session and update'),
        cancelLabel: t('common.cancel', 'Cancel'),
        variant: 'danger',
      });
      if (!reallyAbort) return;
    }
  
    setUpdateBusy(true);
    startUpdatePolling();
    try {
      let status = await postJson('/api/admin/update/download', {});
      state.updateStatus = status;
      renderUpdateStatus(status);
      status = await postJson('/api/admin/update/install', {});
      state.updateStatus = status;
      renderUpdateStatus(status);
      showToast(t('update.restartingToast', 'Restarting into update ...'), 'info');
      stopUpdatePolling();
      await waitForRestartAndReload();
    } catch (error) {
      console.error('[admin] Update failed:', error);
      showToast(error.message || t('update.updateSourceFailed', 'Update failed'), 'error');
      await loadUpdateStatus({ silent: true });
      stopUpdatePolling();
      setUpdateBusy(false);
    }
  }
  
  /** Wait for the old server to go away and the new one to answer, then reload. */
  async function waitForRestartAndReload() {
    const started = Date.now();
    let serverWentAway = false;
    while (Date.now() - started < 15 * 60 * 1000) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        await getJson('/api/admin/update/status', { timeoutMs: 1500 });
        if (serverWentAway || Date.now() - started > 60 * 1000) {
          window.location.reload();
          return;
        }
      } catch {
        serverWentAway = true;
      }
    }
    showToast(t('update.restartTimeout', 'Study Runner did not come back. Start it again with tools/start-macos.sh or tools\\start-windows.cmd.'), 'error');
  }
  
  async function downloadPythonUpdate() {
    const version = state.updateStatus?.update?.version || '';
    const sourceMode = Boolean(state.updateStatus?.source_mode);
    if (sourceMode) {
      await runSourceUpdate();
      return;
    }
    const message = sourceMode
      ? t('update.updateSourceConfirm', 'Update Study Runner to {version} now? It downloads and checks the new version, then restarts.').replace('{version}', version)
      : t('update.downloadConfirm', 'Download and verify update {version}?').replace('{version}', version);
    const proceed = await confirmWithModal({
      title: sourceMode ? t('update.updateSourceTitle', 'Update checkout') : t('update.downloadTitle', 'Download update'),
      message,
      confirmLabel: sourceMode ? t('update.updateSourceAction', 'Update now') : t('update.download', 'Download'),
      cancelLabel: t('common.cancel', 'Cancel'),
    });
    if (!proceed) {
      return;
    }
  
    setUpdateBusy(true);
    startUpdatePolling();
    try {
      const status = await postJson('/api/admin/update/download', {});
      state.updateStatus = status;
      renderUpdateStatus(status);
      showToast(
        sourceMode ? t('update.updatedSourceToast', 'Checkout updated') : t('update.downloadedToast', 'Update downloaded and verified'),
        'success',
      );
    } catch (error) {
      console.error('[admin] Update download failed:', error);
      showToast(error.message || (sourceMode ? t('update.updateSourceFailed', 'Update failed') : t('update.downloadFailed', 'Update download failed')), 'error');
      await loadUpdateStatus({ silent: true });
    } finally {
      stopUpdatePolling();
      setUpdateBusy(false);
    }
  }
  
  async function installPythonUpdate() {
    const message = t('update.restartConfirm', 'Restart Study Runner into the staged update now?');
    const proceed = await confirmWithModal({
      title: t('update.installTitle', 'Restart and install'),
      message,
      confirmLabel: t('update.install', 'Restart now'),
      cancelLabel: t('common.cancel', 'Cancel'),
    });
    if (!proceed) {
      return;
    }
  
    setUpdateBusy(true);
    try {
      const status = await postJson('/api/admin/update/install', {});
      state.updateStatus = status;
      renderUpdateStatus(status);
      showToast(t('update.restartingToast', 'Restarting into update ...'), 'info');
    } catch (error) {
      console.error('[admin] Update install failed:', error);
      showToast(error.message || t('update.installFailed', 'Update restart failed'), 'error');
      await loadUpdateStatus({ silent: true });
      setUpdateBusy(false);
    }
  }
  
  function startUpdatePolling() {
    stopUpdatePolling();
    state.updatePollTimer = window.setInterval(() => {
      void loadUpdateStatus({ silent: true });
    }, 900);
  }
  
  function stopUpdatePolling() {
    if (state.updatePollTimer) {
      window.clearInterval(state.updatePollTimer);
      state.updatePollTimer = null;
    }
  }
  
  function setUpdateBusy(isBusy) {
    ['btn-update-check', 'btn-update-download', 'btn-update-install'].forEach((id) => {
      const button = $(id);
      if (button) {
        button.disabled = Boolean(isBusy);
      }
    });
  }
  
  function renderUpdateStatusError(error) {
    const pill = $('update-status-pill');
    const versionLine = $('update-version-line');
    const detail = $('update-detail');
    if (pill) {
      pill.className = 'status-pill status-pill--error';
      pill.textContent = t('update.error', 'Error');
    }
    if (versionLine) {
      versionLine.textContent = t('update.unavailable', 'Unavailable');
    }
    if (detail) {
      detail.textContent = error?.message || t('update.statusFailed', 'Update status failed');
    }
    setUpdateActions({});
    setUpdateProgress(null);
    setUpdateLog({});
  }
  
  function renderUpdateStatus(status) {
    const pill = $('update-status-pill');
    const versionLine = $('update-version-line');
    const detail = $('update-detail');
    if (!pill || !versionLine || !detail) {
      return;
    }
  
    const stateName = status.state || 'idle';
    const available = Boolean(status.update?.available);
    const version = status.update?.version || status.current_version || '';
    const staged = Boolean(status.staged?.version);
  
    let pillState = 'waiting';
    let pillText = t('update.idle', 'Idle');
    let line = t('update.versionLine', 'Installed {version}').replace('{version}', status.current_version || '-');
    let message = t('update.idleDetail', 'Check GitHub Releases for Python-only updates.');
  
    if (!status.configured) {
      // Source mode is always "configured" (git needs no signing key), so
      // this branch is packaged-mode-only: no trusted updater key is set up.
      pillState = 'disabled';
      pillText = t('update.disabled', 'Disabled');
      message = status.configuration_error || t('update.notConfiguredDetail', 'No Python updater public key is configured for this build.');
    } else if (stateName === 'error' || stateName === 'install_failed') {
      pillState = 'error';
      pillText = t('update.error', 'Error');
      message = status.error || t('update.statusFailed', 'Update status failed');
    } else if (stateName === 'downloading') {
      pillState = 'starting';
      pillText = t('update.downloading', 'Downloading');
      message = status.source_mode
        ? t('update.updatingSourceDetail', 'Downloading and checking the new version -- this can take a few minutes.')
        : formatUpdateDownload(status.download);
    } else if (stateName === 'verifying') {
      pillState = 'starting';
      pillText = t('update.verifying', 'Verifying');
      message = t('update.verifyingDetail', 'Checking hash and signature.');
    } else if (stateName === 'staged' || staged) {
      pillState = 'ready';
      pillText = t('update.ready', 'Ready');
      line = t('update.readyLine', 'Version {version} staged').replace('{version}', status.staged?.version || version);
      if (!status.install_supported) {
        message = t('update.manualRestartDetail', 'The update is staged. Automatic restart is only available in Python packaged builds.');
      } else if (status.source_mode) {
        message = t('update.readySourceDetail', 'The checkout was updated and dependencies refreshed; ready to restart.');
      } else {
        message = t('update.readyDetail', 'The update is verified and ready for restart.');
      }
    } else if (stateName === 'installing') {
      pillState = 'starting';
      pillText = t('update.restarting', 'Restarting');
      message = t('update.restartingDetail', 'Study Runner is handing off to the staged update.');
    } else if (available) {
      pillState = 'ready';
      pillText = t('update.available', 'Available');
      line = t('update.availableLine', 'Version {version} available').replace('{version}', version);
      message = status.source_mode
        ? t('update.availableSourceDetail', 'Updating runs git pull and the install script only after confirmation.')
        : t('update.availableDetail', 'Download starts only after confirmation.');
    } else if (stateName === 'current') {
      pillState = 'running';
      pillText = t('update.current', 'Current');
      message = t('update.currentDetail', 'The installed Python app version is current.');
    }
  
    pill.className = `status-pill status-pill--${pillState}`;
    pill.textContent = pillText;
    versionLine.textContent = line;
    detail.textContent = message;
    setUpdateActions(status);
    setUpdateProgress(status.download);
    setUpdateLog(status);
  }

  /**
   * After a failed install, the helper's own lines for that attempt: the
   * install script's error is in there, and the operator should not have
   * to find the log file in the data folder first.
   */
  function setUpdateLog(status) {
    const wrap = $('update-log');
    const tail = $('update-log-tail');
    if (!wrap || !tail) return;
    const lines = Array.isArray(status?.log_tail) ? status.log_tail : [];
    const show = status?.state === 'install_failed' && lines.length > 0;
    wrap.hidden = !show;
    if (!show) return;
    tail.textContent = lines.join('\n');
    const path = $('update-log-path');
    if (path) {
      path.textContent = status.log_file
        ? t('update.logFile', 'Full log: {path}').replace('{path}', status.log_file)
        : '';
    }
    wrap.open = true;
    const copy = $('btn-update-copy-log');
    if (copy && !copy.dataset.bound) {
      copy.dataset.bound = 'true';
      copy.addEventListener('click', () => void copyUpdateLog(tail));
    }
  }

  async function copyUpdateLog(tail) {
    try {
      await navigator.clipboard.writeText(tail.textContent || '');
      showToast(t('update.logCopied', 'Log copied'), 'success');
    } catch {
      const range = document.createRange();
      range.selectNodeContents(tail);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      showToast(t('update.logCopyFailed', 'Select the text and copy it by hand.'), 'info');
    }
  }
  
  function setUpdateActions(status) {
    const checkButton = $('btn-update-check');
    const downloadButton = $('btn-update-download');
    const installButton = $('btn-update-install');
    const notesLink = $('update-release-notes');
    const stateName = status.state || 'idle';
    const busy = ['downloading', 'verifying', 'installing'].includes(stateName);
    const hasUpdate = Boolean(status.update?.available);
    const hasStaged = Boolean(status.staged?.version);
  
    if (checkButton) {
      checkButton.disabled = busy || status.configured === false;
    }
    if (downloadButton) {
      downloadButton.hidden = !status.configured || !hasUpdate || hasStaged || busy;
      downloadButton.disabled = busy;
      const label = downloadButton.querySelector('span');
      if (label) {
        label.textContent = status.source_mode ? t('update.updateSourceAction', 'Update now') : t('update.download', 'Download');
      }
    }
    if (installButton) {
      installButton.hidden = !hasStaged;
      installButton.disabled = busy || !status.install_supported;
    }
    if (notesLink) {
      const notesUrl = status.update?.notes_url || '';
      notesLink.hidden = !notesUrl;
      if (notesUrl) {
        notesLink.href = notesUrl;
      }
    }
  }
  
  function setUpdateProgress(download) {
    const wrap = $('update-progress');
    const fill = $('update-progress-fill');
    if (!wrap || !fill) {
      return;
    }
    const stateName = download?.state || '';
    const total = Number(download?.total_bytes || 0);
    const done = Number(download?.bytes_downloaded || 0);
    const visible = ['downloading', 'verifying', 'staged'].includes(stateName) || (total > 0 && done > 0);
    wrap.hidden = !visible;
    const percent = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
    fill.style.width = `${percent}%`;
  }
  
  function formatUpdateDownload(download) {
    const total = Number(download?.total_bytes || 0);
    const done = Number(download?.bytes_downloaded || 0);
    if (!total) {
      return t('update.downloadingDetailUnknown', 'Downloading update ...');
    }
    return t('update.downloadingDetail', 'Downloading {done} of {total}.')
      .replace('{done}', formatBytes(done))
      .replace('{total}', formatBytes(total));
  }
  
  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  }
  

  return {
    checkForPythonUpdate,
    downloadPythonUpdate,
    installPythonUpdate,
    loadUpdateStatus,
    runSourceUpdate,
    waitForRestartAndReload,
  };
}

