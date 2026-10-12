import assert from 'node:assert/strict';
import { createModal } from '../../study_runner/apps/ui/scripts/shared/modal.js';

class Element {
  constructor() { this.listeners = {}; this.hidden = false; this.isConnected = false; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  emit(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); }
  remove() { this.isConnected = false; }
  querySelector(selector) { return this.children?.[selector] || null; }
  focus() {}
}

const documentListeners = {};
globalThis.document = {
  body: { appendChild(element) { element.isConnected = true; } },
  createElement() {
    const backdrop = new Element();
    const dialog = new Element();
    dialog.children = { 'button, [href], input, select, textarea': new Element() };
    backdrop.children = {
      '.settings-modal-body': new Element(), '.settings-modal': dialog,
      '.overlay-close': new Element(),
    };
    return backdrop;
  },
  addEventListener(type, callback) { (documentListeners[type] ||= new Set()).add(callback); },
  removeEventListener(type, callback) { documentListeners[type]?.delete(callback); },
};

let allowed = false;
let closed = 0;
const modal = createModal({ beforeClose: () => allowed, onClose: () => { closed += 1; } });
modal.open();
modal.element.children['.overlay-close'].emit('click');
assert.equal(modal.isOpen(), true);
modal.element.emit('click', { target: modal.element });
assert.equal(modal.isOpen(), true);
for (const callback of documentListeners.keydown) callback({ key: 'Escape' });
assert.equal(modal.isOpen(), true);
modal.close();
assert.equal(modal.isOpen(), true);
modal.destroy();
assert.equal(modal.isOpen(), true);
assert.equal(modal.element.isConnected, true);
assert.equal(closed, 0);
allowed = true;
modal.element.emit('click', { target: modal.element });
assert.equal(modal.isOpen(), false);
assert.equal(closed, 1);
