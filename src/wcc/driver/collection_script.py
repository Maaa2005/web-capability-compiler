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
    //
    // A run of same-tag siblings counts once. How many tags a quote carries, or
    // how many links a story's subtext holds, is content; counting them split
    // quotes.toscrape.com's ten quotes into three shapes, so the page's result
    // list was found as three records and lost to a ten-link tag cloud.
    const parts = [];
    const walk = (node, depth) => {
      if (depth > 3) return;
      let previous = null;
      for (const child of node.children) {
        if (child.tagName === previous) continue;
        previous = child.tagName;
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

  // The same projection the element extractor uses, so a record's activation
  // link and a normalized element describe their position identically. Two
  // spellings of "where this is" would resolve differently the first time one of
  // them was changed, and the record path is what makes an entity reference
  // executable rather than merely readable.
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

  // The link that stands for a record: its first link that names something, or
  // its first link when none does. A Hacker News story row starts with its upvote
  // arrow, a link whose only content is an empty div titled "upvote", so the
  // story opened by its first link was a vote. The name is read narrowly — the
  // link's text, its aria-label, or an image's alt — because a title on something
  // inside the link is a tooltip, not what the link says it opens.
  const named = (link) => Boolean(
    (link.textContent || '').trim() ||
    (link.getAttribute('aria-label') || '').trim() ||
    Array.from(link.querySelectorAll('img[alt]')).some((img) => img.getAttribute('alt').trim())
  );
  const recordLink = (el) => {
    if (el.matches('a[href]')) return el;
    const links = Array.from(el.querySelectorAll('a[href]'));
    return links.find(named) || links[0] || null;
  };

  // Where activating this record would click. A record with no link of its own is
  // reported without one rather than with its container's: opening "the record"
  // by clicking a block that merely contains a link is how an automation activates
  // whichever child happens to sit under the cursor.
  const activationPathOf = (el) => {
    const link = recordLink(el);
    return link ? structuralPath(link) : null;
  };

  // Whether the record says anything its links do not. A tag, a category or a
  // menu entry is nothing but its link; a quote, a product or a story has text
  // around its link. Both repeat, and the second is what a result list is made of
  // (specification 7.11): on quotes.toscrape.com a ten-tag sidebar is exactly as
  // long as the ten quotes beside it, and on books.toscrape.com the fifty
  // category links outnumber the twenty books.
  const linkOnly = (el) => {
    if (!el.matches('a[href]') && !el.querySelector('a[href]')) return false;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      const parent = node.parentElement;
      if (!parent || parent.closest('script,style,noscript,template')) continue;
      if (!parent.closest('a[href]')) return false;
    }
    return true;
  };

  const fieldsOf = (el) => {
    const fields = {};
    const heading = el.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"]');
    if (heading) fields.title = clean(heading);
    const link = recordLink(el);
    if (link && link !== el) {
      fields.link_text = clean(link);
      fields.href = link.getAttribute('href') || '';
    }
    for (const node of el.querySelectorAll('[data-field]')) {
      fields[node.getAttribute('data-field')] = clean(node);
    }
    if (!Object.keys(fields).length) fields.text = clean(el);
    return fields;
  };

  // The runs of siblings that share one shape and are long enough to be a
  // repetition (specification 8.4), in the order they first appear.
  //
  // Replaces two opposite mistakes that turned out to have one root. Card and
  // list detection required *every* child to match, so one pagination link, one
  // heading, or one advert beside ten products deleted the whole collection —
  // and real pages essentially always have that extra sibling. Table detection
  // checked shapes not at all, so Hacker News's spacer and navigation rows became
  // records alongside the stories.
  const repeatedGroups = (children) => {
    const groups = new Map();
    for (const child of children) {
      const key = shape(child);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(child);
    }
    // Two is a repetition; one is a thing. The fraction stops three lookalikes
    // among twenty unrelated divs from becoming a collection.
    //
    // A quarter, not a majority. A majority was tried first and could never see
    // a listing built from alternating rows, like Hacker News's story, subtext
    // and spacer: the stories are a third of the rows. A quarter keeps them in
    // reach, but it does not decide between them, and the largest run there was
    // not the stories; tables keep every run that passes (see below).
    return Array.from(groups.values()).filter(
      (group) => group.length >= 2 && group.length * 4 >= children.length
    );
  };

  // The largest such run, the first on a tie. Lists and cards report one
  // collection per container.
  const dominantGroup = (children) => {
    let best = null;
    for (const group of repeatedGroups(children)) {
      if (!best || group.length > best.length) best = group;
    }
    return best;
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
        activation_path: activationPathOf(item),
        link_only: linkOnly(item),
      })),
    });
  };

  // A table's own rows, not every row inside it. Hacker News lays its page out
  // in a table and its stories out in another one nested inside, and reading the
  // nested rows as the outer table's made the page's spacers its largest group.
  //
  // Rows that hold nothing are layout: a spacer says nothing and opens nothing,
  // and counted as rows they outvote the records they separate. A row holding a
  // table of its own is layout too, its rows read where they belong.
  //
  // Every repeated run is reported, not only the largest. One listing can be
  // built from interleaved rows — on Hacker News a story row and its subtext
  // row — and the "More" row, built like a subtext, made the subtexts one longer
  // than the stories. Which run is the result list is not a question of length,
  // and choosing here would answer it before the evidence that can.
  const hasContent = (row) => Boolean(
    clean(row) || row.querySelector('a[href],img,input,select,textarea,button')
  );
  for (const table of document.querySelectorAll('table')) {
    const headers = Array.from(table.querySelectorAll('thead th'))
      .filter((th) => th.closest('table') === table)
      .map(clean);
    const allRows = Array.from(table.tBodies)
      .flatMap((body) => Array.from(body.rows))
      .filter((row) => hasContent(row) && !row.querySelector('table'));
    if (allRows.length < 2) continue;
    for (const rows of repeatedGroups(allRows)) {
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
          // The record's link names it, as it does a card's. Without it a story
          // row was labelled by its first cell, the rank: entity "3." rather than
          // the title an agent would ask for. A header that already says
          // link_text or href keeps its column.
          const link = recordLink(row);
          if (link) {
            if (!('link_text' in fields)) fields.link_text = clean(link);
            if (!('href' in fields)) fields.href = link.getAttribute('href') || '';
          }
          return {
            ordinal: index,
            fields,
            shape: shape(row),
            activation_path: activationPathOf(row),
            link_only: linkOnly(row),
          };
        }),
      });
    }
  }

  for (const list of document.querySelectorAll('ul,ol')) {
    const allItems = Array.from(list.children).filter((c) => c.tagName === 'LI');
    if (allItems.length < 2) continue;
    const items = dominantGroup(allItems);
    if (!items) continue;
    const isResults = /result|search/i.test(
      (list.getAttribute('id') || '') + ' ' +
      (list.getAttribute('aria-label') || '') + ' ' +
      (list.getAttribute('role') || '')
    );
    emit(list, isResults ? 'search_results' : 'list', items);
  }

  // A span is a container too. A run of links inside one is how a page writes a
  // row of "Label: link, link, link" (www.python.org's "Use Python for" rows), and
  // a run never reported is a list the footer rule cannot see: each row's first
  // link heads that run, and without the run on record the rows were compiled as
  // the page's result list. A child that is itself a link counts as content, as a
  // child containing one always did.
  for (const container of document.querySelectorAll('div,section,main,span')) {
    const all = Array.from(container.children).filter((c) => c.tagName !== 'SCRIPT');
    if (all.length < 2) continue;
    const children = dominantGroup(all);
    if (!children) continue;
    if (children.every((c) => c.tagName === 'LI')) continue;
    const content = 'a[href],h1,h2,h3,h4,h5,h6,[data-field]';
    if (!children.some((c) => c.matches('a[href]') || c.querySelector(content))) continue;
    emit(container, 'cards', children);
  }

  const structured = [];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    structured.push(script.textContent || '');
  }

  return {collections, structured};
}
"""
