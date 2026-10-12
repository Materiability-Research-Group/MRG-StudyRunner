/**
 * The one modal implementation.
 *
 * Before this, every dialog re-created the same `.modal-backdrop` /
 * `.settings-modal` markup plus its own Escape and backdrop-click handlers.
 * This wraps the existing CSS so new dialogs look and behave identically.
 */
import { escapeHtml } from './dom-utils.js';

export function createModal({ title = '', variant = '', closeLabel = 'Close', beforeClose = null, onClose = null } = {}) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.hidden = true;

  const variantClass = variant ? ` settings-modal--${variant}` : '';
  backdrop.innerHTML = `
    <div class="settings-modal${variantClass}" role="dialog" aria-modal="true">
      <div class="settings-modal-header">
        <h2>${escapeHtml(title)}</h2>
        <button class="overlay-close" type="button" aria-label="${escapeHtml(closeLabel)}" title="${escapeHtml(closeLabel)}">
          <i class="iconoir-xmark"></i>
        </button>
      </div>
      <div class="settings-modal-body"></div>
    </div>
  `;

  const body = backdrop.querySelector('.settings-modal-body');
  const dialog = backdrop.querySelector('.settings-modal');
  let escapeHandler = null;

  function isOpen() {
    return !backdrop.hidden;
  }

  function close() {
    if (!isOpen()) return;
    if (beforeClose?.() === false) return;
    backdrop.hidden = true;
    if (escapeHandler) {
      document.removeEventListener('keydown', escapeHandler);
      escapeHandler = null;
    }
    onClose?.();
  }

  function open() {
    if (!backdrop.isConnected) document.body.appendChild(backdrop);
    backdrop.hidden = false;
    escapeHandler = (event) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('keydown', escapeHandler);
    dialog?.querySelector('button, [href], input, select, textarea')?.focus?.();
  }

  backdrop.querySelector('.overlay-close')?.addEventListener('click', close);
  backdrop.addEventListener('click', (event) => {
    if (event.target === backdrop) close();
  });

  function setTitle(value) {
    const heading = dialog?.querySelector('h2');
    if (heading) heading.textContent = value;
  }

  function destroy() {
    close();
    if (isOpen()) return;
    backdrop.remove();
  }

  return { element: backdrop, body, open, close, isOpen, setTitle, destroy };
}

/**
 * A yes/no dialog on the shared modal, replacing window.confirm().
 *
 * confirm() freezes the whole page, cannot be translated, and looks nothing
 * like the rest of the app - which matters most exactly where it was still
 * used: the moment before a study run starts.
 *
 * Resolves true when confirmed, false on cancel, Escape, or backdrop click.
 */
export function confirmWithModal({
  title = '',
  message = '',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = '',
} = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      modal.destroy();
      resolve(value);
    };

    const modal = createModal({
      title,
      variant,
      closeLabel: cancelLabel,
      onClose: () => finish(false),
    });

    modal.body.innerHTML = `
      <p class="settings-hint confirm-modal-message">${escapeHtml(message)}</p>
      <div class="dashboard-actions confirm-modal-actions">
        <button type="button" class="btn-secondary" data-confirm-cancel>${escapeHtml(cancelLabel)}</button>
        <button type="button" class="btn-primary${variant === 'danger' ? ' btn-primary--danger' : ''}" data-confirm-ok>${escapeHtml(confirmLabel)}</button>
      </div>`;

    modal.body.querySelector('[data-confirm-cancel]')?.addEventListener('click', () => finish(false));
    modal.body.querySelector('[data-confirm-ok]')?.addEventListener('click', () => finish(true));
    modal.open();
    modal.body.querySelector('[data-confirm-ok]')?.focus();
  });
}
