"""Collection and structured-data extraction in the page (specification 8.4).

A collection is a repeated structure: a list, a table, repeated cards, or a block
of search results. The script finds candidate containers and reports their records
as field maps; deciding what counts, and computing the per-record identity that
feeds ``collection_hash``, happens in Python.

The signature used to judge repetition deliberately ignores text. Two cards are
the same *kind* of thing because they are built the same way, not because they say
the same words — grouping on text would split one collection in two the moment a
product name changed.
"""

from __future__ import annotations

EXTRACT_COLLECTIONS = r"""
() => {
  const clean = (node) => (node && node.textContent
    ? node.textContent.trim().replace(/\s+/g, ' ')
    : '');

  const shape = (el) => {
    // Structure only: tag names and depth, never text and never class names,
    // which are the parts a redesign or an A/B test changes most freely.
    const parts = [];
    const walk = (node, depth) => {
      if (depth > 3) return;
      for (const child of node.children) {
        parts.push(depth + ':' + child.tagName.toLowerCase());
        walk(child, depth + 1);
      }
    };
    walk(el, 0);
    return el.tagName.toLowerCase() + '|' + parts.join(',');
  };

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };

  const fieldsOf = (el) => {
    const fields = {};
    const heading = el.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
    if (heading) fields.title = clean(heading);
    const link = el.querySelector('a[href]');
    if (link) {
      fields.link_text = clean(link);
      fields.href = link.getAttribute('href') || '';
    }
    for (const node of el.querySelectorAll('[data-field]')) {
      fields[node.getAttribute('data-field')] = clean(node);
    }
    if (!Object.keys(fields).length) fields.text = clean(el);
    return fields;
  };

  const collections = [];

  const emit = (container, kind, items) => {
    const usable = items.filter(visible);
    if (usable.length < 2) return;
    collections.push({
      kind,
      label: (() => {
        const heading = container.previousElementSibling;
        if (heading && /^H[1-6]$/.test(heading.tagName)) return clean(heading);
        const aria = container.getAttribute('aria-label');
        return aria || null;
      })(),
      order_is_meaningful: kind === 'table' || kind === 'search_results' || kind === 'list',
      records: usable.map((item, index) => ({
        ordinal: index,
        fields: fieldsOf(item),
        shape: shape(item),
      })),
    });
  };

  for (const table of document.querySelectorAll('table')) {
    const headers = Array.from(table.querySelectorAll('thead th')).map(clean);
    const rows = Array.from(table.querySelectorAll('tbody tr'));
    if (rows.length < 2) continue;
    collections.push({
      kind: 'table',
      label: clean(table.querySelector('caption')) || null,
      order_is_meaningful: true,
      records: rows.map((row, index) => {
        const cells = Array.from(row.children).map(clean);
        const fields = {};
        cells.forEach((cell, i) => {
          fields[headers[i] || ('column_' + (i + 1))] = cell;
        });
        return {ordinal: index, fields, shape: shape(row)};
      }),
    });
  }

  for (const list of document.querySelectorAll('ul,ol')) {
    const items = Array.from(list.children).filter((c) => c.tagName === 'LI');
    if (items.length < 2) continue;
    const shapes = new Set(items.map(shape));
    if (shapes.size > 1) continue;
    const isResults = /result|search/i.test(
      (list.getAttribute('id') || '') + ' ' +
      (list.getAttribute('aria-label') || '') + ' ' +
      (list.getAttribute('role') || '')
    );
    emit(list, isResults ? 'search_results' : 'list', items);
  }

  for (const container of document.querySelectorAll('div,section,main')) {
    const children = Array.from(container.children).filter((c) => c.tagName !== 'SCRIPT');
    if (children.length < 2) continue;
    const shapes = new Set(children.map(shape));
    if (shapes.size !== 1) continue;
    if (children.every((c) => c.tagName === 'LI')) continue;
    if (!children.some((c) => c.querySelector('a[href],h1,h2,h3,h4,h5,h6,[data-field]'))) continue;
    emit(container, 'cards', children);
  }

  const structured = [];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    structured.push(script.textContent || '');
  }

  return {collections, structured};
}
"""
