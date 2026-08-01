"""Locator re-resolution in the page (specification 7.7).

The rule that matters is what happens when a candidate matches more than once:
resolution moves on. Choosing the closest, the first, or the most visible match is
how an automation ends up typing into the wrong field with full confidence, so an
ambiguous candidate is worth exactly as much as one that matched nothing.

The script receives candidate descriptions, never a selector from outside the
driver. An agent supplies an ``element_id``; the chain behind it was built by WCC
from its own observation (specification 10.3).
"""

from __future__ import annotations

RESOLVE_CANDIDATE = r"""
(candidate) => {
  const {kind, key, role, form_id: formId} = candidate;

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };

  const scope = () => {
    if (formId) {
      const escaped = (window.CSS && CSS.escape) ? CSS.escape(formId) : formId;
      return document.querySelector(`form#${escaped}`) || document;
    }
    return document;
  };

  const text = (el) => (el.textContent || '').trim().replace(/\s+/g, ' ');

  const accessibleName = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map(text);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const escaped = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      const label = document.querySelector(`label[for="${escaped}"]`);
      if (label) return text(label);
    }
    const wrapping = el.closest('label');
    if (wrapping) return text(wrapping);
    return text(el);
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') return type;
      if (type === 'search') return 'searchbox';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      return 'textbox';
    }
    return null;
  };

  const INTERACTIVE = 'input,select,textarea,button,a[href],[role]';
  let matches = [];

  if (kind === 1) {
    const escaped = (window.CSS && CSS.escape) ? CSS.escape(key) : key;
    matches = Array.from(document.querySelectorAll(`#${escaped}`));
  } else if (kind === 2) {
    const escaped = (window.CSS && CSS.escape) ? CSS.escape(key) : key;
    matches = Array.from(scope().querySelectorAll(`[name="${escaped}"]`));
  } else if (kind === 3) {
    matches = Array.from(scope().querySelectorAll(INTERACTIVE))
      .filter((el) => accessibleName(el) === key)
      .filter((el) => (role ? roleOf(el) === role : true));
  } else if (kind === 4) {
    matches = Array.from(scope().querySelectorAll(INTERACTIVE))
      .filter((el) => accessibleName(el) === key);
  } else if (kind === 5) {
    const parts = key.split('>').filter(Boolean);
    let nodes = [document];
    for (const part of parts) {
      const m = part.match(/^([a-z0-9-]+)(?:\[(\d+)\])?$/i);
      if (!m) { nodes = []; break; }
      const [, tag, index] = m;
      const next = [];
      for (const node of nodes) {
        const children = node === document
          ? Array.from(document.getElementsByTagName(tag))
          : Array.from(node.children).filter((c) => c.tagName.toLowerCase() === tag);
        if (index) {
          const picked = children[Number(index) - 1];
          if (picked) next.push(picked);
        } else {
          next.push(...children);
        }
      }
      nodes = next;
    }
    matches = nodes.filter((n) => n !== document);
  }

  matches = matches.filter(visible);
  if (matches.length !== 1) return {count: matches.length, handle: null};

  // The winner is tagged so the caller can act on it without a selector ever
  // leaving this function.
  const marker = 'wcc-resolved-' + Math.random().toString(36).slice(2);
  matches[0].setAttribute('data-wcc-resolved', marker);
  return {count: 1, handle: marker};
}
"""
