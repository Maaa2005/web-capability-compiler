"""The DOM extraction script the driver evaluates in the page.

Kept as data rather than logic: this string runs inside the page, where every
value it touches is untrusted (specification 10.3). It reports raw signals and
decides nothing — eligibility, label priority, and confidence are resolved in
Python, where they can be tested without a browser.

The one decision it does make is a refusal. A password, payment, security-code,
authentication-token, or file-input value is never read here at all, so the value
cannot leak through a later mistake in the Python layer. Specification 7.2 makes
that omission absolute, and :class:`~wcc.models.element.NormalizedElement` rejects
such a value independently as the second barrier.
"""

from __future__ import annotations

EXTRACT_ELEMENTS = r"""
() => {
  const SENSITIVE_TYPES = new Set(['password', 'file', 'hidden']);
  const SENSITIVE_AUTOCOMPLETE = new RegExp(
    '(^|\\s)(cc-(number|csc|exp|exp-month|exp-year|name)' +
    '|current-password|new-password|one-time-code)(\\s|$)', 'i');
  const SENSITIVE_NAME = new RegExp(
    '(csrf|xsrf|token|secret|auth|session|api[-_]?key' +
    '|password|passwd|cvv|cvc|card[-_]?number)', 'i');

  const isSensitive = (el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (SENSITIVE_TYPES.has(type)) return true;
    const autocomplete = el.getAttribute('autocomplete') || '';
    if (SENSITIVE_AUTOCOMPLETE.test(autocomplete)) return true;
    const name = (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '');
    return SENSITIVE_NAME.test(name);
  };

  const text = (node) => (node && node.textContent
    ? node.textContent.trim().replace(/\s+/g, ' ')
    : null);

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (style.opacity === '0') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };

  const labelFor = (el) => {
    if (!el.id) return null;
    const escaped = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
    return text(document.querySelector(`label[for="${escaped}"]`));
  };

  const wrappingLabel = (el) => text(el.closest('label'));

  const ariaLabelledBy = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (!ids.length) return null;
    const parts = ids.map((id) => text(document.getElementById(id))).filter(Boolean);
    return parts.length ? parts.join(' ') : null;
  };

  const legendOrHeading = (el) => {
    const legend = el.closest('fieldset') ? el.closest('fieldset').querySelector('legend') : null;
    if (legend) return text(legend);
    let node = el.closest('section, article, form, main') || document.body;
    const heading = node.querySelector('h1, h2, h3, h4, h5, h6');
    return heading ? text(heading) : null;
  };

  const structuralPath = (el) => {
    const parts = [];
    let node = el;
    const stop = new Set(['FORM', 'MAIN', 'NAV', 'HEADER', 'FOOTER', 'ASIDE', 'SECTION']);
    while (node && node.nodeType === 1 && node !== document.body) {
      const parent = node.parentElement;
      if (!parent) break;
      const tag = node.tagName.toLowerCase();
      const sameTag = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
      const index = sameTag.indexOf(node) + 1;
      parts.unshift(sameTag.length > 1 ? `${tag}[${index}]` : tag);
      if (stop.has(parent.tagName) || parent.getAttribute('role') === 'search') {
        parts.unshift(parent.tagName.toLowerCase());
        break;
      }
      node = parent;
    }
    return parts.join('>');
  };

  const SELECTOR = [
    'input', 'select', 'textarea', 'button', 'a[href]',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
    '[role="combobox"]', '[role="searchbox"]', '[role="textbox"]',
  ].join(',');

  const records = [];
  let ordinal = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    ordinal += 1;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const sensitive = isSensitive(el);
    const box = el.getBoundingClientRect();
    const form = el.form || el.closest('form');

    records.push({
      ordinal,
      tag,
      type,
      role: el.getAttribute('role'),
      id: el.id || null,
      name: el.getAttribute('name'),
      form_id: form ? (form.id || null) : null,
      form_present: Boolean(form),
      label_for: labelFor(el),
      wrapping_label: wrappingLabel(el),
      aria_labelledby: ariaLabelledBy(el),
      aria_label: el.getAttribute('aria-label'),
      element_text: (tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button')
        ? text(el)
        : null,
      placeholder: el.getAttribute('placeholder'),
      legend_or_heading: legendOrHeading(el),
      autocomplete: el.getAttribute('autocomplete'),
      // Never read for a sensitive control. The omission happens here, in the
      // page, so no later layer can be the one that leaks it.
      value: sensitive ? null : (typeof el.value === 'string' ? el.value : null),
      sensitive,
      required: Boolean(el.required),
      disabled: Boolean(el.disabled),
      readonly: Boolean(el.readOnly),
      checked: (type === 'checkbox' || type === 'radio') ? Boolean(el.checked) : null,
      visible: visible(el),
      box: {x: box.x, y: box.y, width: box.width, height: box.height},
      structural_path: structuralPath(el),
      search_signal: type === 'search'
        || el.getAttribute('role') === 'searchbox'
        || (form ? form.getAttribute('role') === 'search' : false),
    });
  }
  return records;
}
"""


EXTRACT_LANDMARKS = r"""
() => {
  // Landmark structure, not content: the roles present and their nesting depth.
  // Specification 7.3 counts this toward both fingerprints, so it must not carry
  // any text that changes between two visits to the same page class.
  const IMPLICIT = {
    HEADER: 'banner', NAV: 'navigation', MAIN: 'main',
    FOOTER: 'contentinfo', ASIDE: 'complementary', FORM: 'form', SECTION: 'region',
  };
  const AD_LIKE = /(^|[-_ ])(ad|ads|advert|advertisement|promo|sponsor|banner)([-_ ]|$)/i;

  const isAdLike = (el) => AD_LIKE.test(el.id || '')
    || AD_LIKE.test(el.className || '')
    || Array.from(el.attributes).some((a) => a.name.startsWith('data-ad'));

  const out = [];
  const walk = (node, depth) => {
    for (const child of node.children) {
      const role = child.getAttribute('role') || IMPLICIT[child.tagName];
      if (role && !isAdLike(child)) out.push(depth + ':' + role);
      walk(child, role ? depth + 1 : depth);
    }
  };
  walk(document.body, 0);
  return out;
}
"""
