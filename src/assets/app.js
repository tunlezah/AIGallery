/* AI Gallery front end. One classic script shared by the hosted and the
   standalone (file://) builds — no modules, no runtime requests, no History
   API. All data comes from the inline JSON block; all gallery markup is
   already in the served HTML, this script only enhances it. */
(function () {
  'use strict';

  var root = document.documentElement;
  root.classList.add('js');

  /* ---------- guarded storage (file:// and locked-down safe) ---------- */
  var storage = (function () {
    var memory = {};
    var usable = false;
    try {
      var probe = 'ai-gallery:probe';
      window.localStorage.setItem(probe, '1');
      window.localStorage.removeItem(probe);
      usable = true;
    } catch (err) { usable = false; }
    return {
      get: function (key) {
        if (usable) {
          try { return window.localStorage.getItem(key); } catch (err) { }
        }
        return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null;
      },
      set: function (key, value) {
        if (usable) {
          try { window.localStorage.setItem(key, value); return; } catch (err) { }
        }
        memory[key] = value;
      }
    };
  })();

  /* ---------- data ---------- */
  /* The inline block is a lean index for search/filter/sort only; tiles,
     bodies, images and links are already in the served HTML. */
  var data = { items: [], runtime: {} };
  var dataEl = document.getElementById('gallery-data');
  if (dataEl) {
    try { data = JSON.parse(dataEl.textContent); } catch (err) { }
  }
  var runtime = data.runtime || {};
  var items = data.items || [];
  var itemById = {};
  items.forEach(function (item) { itemById[item.id] = item; });

  var tiles = Array.prototype.slice.call(document.querySelectorAll('.tile'));
  var tileById = {};
  tiles.forEach(function (tile) { tileById[tile.getAttribute('data-id')] = tile; });
  var sectionEls = Array.prototype.slice.call(document.querySelectorAll('.gallery-section'));
  var dividerEls = Array.prototype.slice.call(document.querySelectorAll('.rainbow-divider'));

  var searchInput = document.getElementById('search');
  var countNum = document.getElementById('count-num');
  var resultCount = document.getElementById('result-count');
  var emptyState = document.getElementById('empty-state');
  var totalItems = items.length;

  /* ---------- text normalisation (mirrors the build's search folding) ---- */
  function fold(text) {
    text = String(text || '').toLowerCase();
    if (text.normalize) {
      try {
        text = text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
      } catch (err) { }
    }
    return text.replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
  }

  /* ---------- filter state <-> URL hash ---------- */
  var state = { q: '', tags: [], section: '', source: '' };
  var lastWrittenHash = null;

  function serializeState() {
    var parts = [];
    if (state.q) { parts.push('q=' + encodeURIComponent(state.q)); }
    state.tags.forEach(function (tag) { parts.push('tag=' + encodeURIComponent(tag)); });
    if (state.section) { parts.push('section=' + encodeURIComponent(state.section)); }
    if (state.source) { parts.push('source=' + encodeURIComponent(state.source)); }
    return parts.join('&');
  }

  function writeHash() {
    var next = serializeState();
    var current = window.location.hash.replace(/^#/, '');
    if (next === current) { return; }
    lastWrittenHash = next;
    /* Assigning location.hash is the only URL-state mechanism that works on
       file:// documents; the History API throws there. */
    window.location.hash = next;
  }

  function readHash() {
    var raw = window.location.hash.replace(/^#/, '');
    var next = { q: '', tags: [], section: '', source: '' };
    raw.split('&').forEach(function (pair) {
      if (!pair) { return; }
      var eq = pair.indexOf('=');
      var key = eq === -1 ? pair : pair.slice(0, eq);
      var value = eq === -1 ? '' : decodeURIComponent(pair.slice(eq + 1).replace(/\+/g, ' '));
      if (key === 'q') { next.q = value; }
      else if (key === 'tag' && value) { next.tags.push(value); }
      else if (key === 'section') { next.section = value; }
      else if (key === 'source') { next.source = value; }
    });
    return next;
  }

  /* ---------- filtering ---------- */
  function parseQuery(q) {
    var tokens = fold(q).split(' ');
    var parsed = { text: [], tags: [], sections: [], sources: [], featured: false };
    tokens.forEach(function (token) {
      if (!token) { return; }
      if (token === 'is:featured') { parsed.featured = true; }
      else if (token.indexOf('tag:') === 0 && token.length > 4) { parsed.tags.push(token.slice(4)); }
      else if (token.indexOf('section:') === 0 && token.length > 8) { parsed.sections.push(token.slice(8)); }
      else if (token.indexOf('source:') === 0 && token.length > 7) { parsed.sources.push(token.slice(7)); }
      else { parsed.text.push(token); }
    });
    return parsed;
  }

  function itemMatches(item, parsed) {
    var i;
    for (i = 0; i < parsed.text.length; i++) {
      if ((item.search || '').indexOf(parsed.text[i]) === -1) { return false; }
    }
    var tags = item.tags || [];
    for (i = 0; i < state.tags.length; i++) {
      if (tags.indexOf(state.tags[i]) === -1) { return false; }
    }
    for (i = 0; i < parsed.tags.length; i++) {
      if (tags.indexOf(parsed.tags[i]) === -1) { return false; }
    }
    if (state.section && item.section !== state.section) { return false; }
    for (i = 0; i < parsed.sections.length; i++) {
      if (item.section !== parsed.sections[i]) { return false; }
    }
    if (state.source && item.source !== state.source) { return false; }
    for (i = 0; i < parsed.sources.length; i++) {
      if (item.source !== parsed.sources[i]) { return false; }
    }
    if (parsed.featured && !item.featured) { return false; }
    return true;
  }

  function highlightTitle(tile, item, textTokens) {
    var heading = tile.querySelector('.tile-title');
    if (!heading) { return; }
    var target = heading.querySelector('a') || heading;
    var title = item.title || '';
    while (target.firstChild) { target.removeChild(target.firstChild); }
    if (!textTokens.length) {
      target.appendChild(document.createTextNode(title));
      return;
    }
    var low = title.toLowerCase();
    var marks = [];
    textTokens.forEach(function (token) {
      var from = 0;
      var at;
      while (token && (at = low.indexOf(token, from)) !== -1) {
        marks.push([at, at + token.length]);
        from = at + token.length;
      }
    });
    marks.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [];
    marks.forEach(function (span) {
      var last = merged[merged.length - 1];
      if (last && span[0] <= last[1]) { last[1] = Math.max(last[1], span[1]); }
      else { merged.push([span[0], span[1]]); }
    });
    var cursor = 0;
    merged.forEach(function (span) {
      if (span[0] > cursor) {
        target.appendChild(document.createTextNode(title.slice(cursor, span[0])));
      }
      var mark = document.createElement('mark');
      mark.appendChild(document.createTextNode(title.slice(span[0], span[1])));
      target.appendChild(mark);
      cursor = span[1];
    });
    if (cursor < title.length) {
      target.appendChild(document.createTextNode(title.slice(cursor)));
    }
  }

  function refreshPressedStates() {
    var railSections = document.querySelectorAll('.rail-section');
    Array.prototype.forEach.call(railSections, function (button) {
      var pressed = button.getAttribute('data-section') === state.section;
      button.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    });
    var chips = document.querySelectorAll('.chip[data-tag]');
    Array.prototype.forEach.call(chips, function (chip) {
      var pressed = state.tags.indexOf(chip.getAttribute('data-tag')) !== -1;
      chip.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    });
  }

  function applyFilters() {
    var anyFilter = Boolean(state.q || state.tags.length || state.section || state.source);
    if (anyFilter) { revealAllDeferred(); }
    var parsed = parseQuery(state.q);
    var visibleTotal = 0;
    var visibleBySection = {};
    items.forEach(function (item) {
      var tile = tileById[item.id];
      if (!tile) { return; }
      var show = itemMatches(item, parsed);
      tile.hidden = !show;
      if (show) {
        visibleTotal += 1;
        visibleBySection[item.section] = (visibleBySection[item.section] || 0) + 1;
      }
      highlightTitle(tile, item, show ? parsed.text : []);
    });
    sectionEls.forEach(function (sectionEl) {
      var slug = sectionEl.getAttribute('data-section');
      sectionEl.hidden = !(visibleBySection[slug] > 0);
    });
    /* A divider sits before each section after the first; show it only when
       the section after it AND at least one section before it are visible. */
    var seenVisible = !sectionEls.length || !sectionEls[0].hidden;
    dividerEls.forEach(function (divider, index) {
      var after = sectionEls[index + 1];
      divider.hidden = !(after && !after.hidden && seenVisible);
      if (after && !after.hidden) { seenVisible = true; }
    });
    if (emptyState) { emptyState.hidden = visibleTotal !== 0; }
    if (countNum && resultCount) {
      countNum.textContent = String(visibleTotal);
      resultCount.lastChild.textContent = ' of ' + totalItems + ' items' + (anyFilter ? ' (filtered)' : '');
    }
    refreshPressedStates();
    scheduleClampCheck();
    writeHash();
  }

  function setState(next) {
    state = next;
    if (searchInput && searchInput.value !== state.q) { searchInput.value = state.q; }
    applyFilters();
  }

  function resetFilters() {
    setState({ q: '', tags: [], section: '', source: '' });
  }

  /* ---------- hash routing ---------- */
  window.addEventListener('hashchange', function () {
    var raw = window.location.hash.replace(/^#/, '');
    if (raw === lastWrittenHash) { lastWrittenHash = null; return; }
    setState(readHash());
  });

  /* ---------- search input ---------- */
  var debounceTimer = null;
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(function () {
        state.q = searchInput.value;
        applyFilters();
      }, 120);
    });
    searchInput.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && searchInput.value) {
        event.stopPropagation();
        searchInput.value = '';
        state.q = '';
        applyFilters();
      }
    });
  }
  document.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.altKey || event.ctrlKey || event.metaKey) { return; }
    var active = document.activeElement;
    var typing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable);
    if (typing) { return; }
    if (searchInput) {
      event.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  });

  /* ---------- chips, rail sections, reset ---------- */
  document.addEventListener('click', function (event) {
    var chip = event.target.closest ? event.target.closest('.chip[data-tag]') : null;
    if (chip) {
      var tag = chip.getAttribute('data-tag');
      var at = state.tags.indexOf(tag);
      if (at === -1) { state.tags.push(tag); } else { state.tags.splice(at, 1); }
      applyFilters();
      return;
    }
    var railSection = event.target.closest ? event.target.closest('.rail-section') : null;
    if (railSection) {
      var slug = railSection.getAttribute('data-section');
      state.section = state.section === slug ? '' : slug;
      applyFilters();
    }
  });
  var clearButton = document.getElementById('clear-filters');
  if (clearButton) { clearButton.addEventListener('click', resetFilters); }
  var resetButton = document.getElementById('reset-filters');
  if (resetButton) { resetButton.addEventListener('click', resetFilters); }

  /* ---------- mobile rail drawer ---------- */
  var railToggle = document.getElementById('rail-toggle');
  var rail = document.getElementById('rail');
  if (railToggle && rail) {
    railToggle.addEventListener('click', function () {
      var open = rail.classList.toggle('open');
      railToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  /* ---------- image fallback (second line of defence) ---------- */
  var placeholder = runtime.placeholder || 'assets/placeholder.svg';
  document.addEventListener('error', function (event) {
    var img = event.target;
    if (!img || img.tagName !== 'IMG' || img.getAttribute('data-fallback')) { return; }
    if (img.closest && !img.closest('.tile-media')) { return; }
    img.setAttribute('data-fallback', '1');
    img.src = placeholder;
  }, true);

  /* ---------- clamp + More/Less ---------- */
  var clampQueue = [];
  var clampScheduled = false;

  function ensureMoreButton(body) {
    var button = body.parentNode.querySelector('.more-button');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'more-button';
      button.textContent = 'More';
      button.setAttribute('aria-expanded', 'false');
      button.setAttribute('aria-controls', body.id);
      button.addEventListener('click', function () {
        var expanded = button.getAttribute('aria-expanded') === 'true';
        if (expanded) {
          body.classList.add('clamped');
          button.setAttribute('aria-expanded', 'false');
          button.textContent = 'More';
          checkClamp(body);
        } else {
          body.classList.remove('clamped');
          body.classList.remove('has-overflow');
          button.setAttribute('aria-expanded', 'true');
          button.textContent = 'Less';
        }
      });
      body.parentNode.insertBefore(button, body.nextSibling);
    }
    return button;
  }

  function checkClamp(body) {
    if (!body.classList.contains('clamped')) { return; }
    var overflowing = body.scrollHeight > body.clientHeight + 1;
    body.classList.toggle('has-overflow', overflowing);
    var button = body.parentNode.querySelector('.more-button');
    if (overflowing) {
      ensureMoreButton(body).hidden = false;
    } else if (button && button.getAttribute('aria-expanded') !== 'true') {
      button.hidden = true;
    }
  }

  function drainClampQueue() {
    var budget = 200;
    while (clampQueue.length && budget-- > 0) {
      checkClamp(clampQueue.shift());
    }
    if (clampQueue.length) {
      window.requestAnimationFrame(drainClampQueue);
    } else {
      clampScheduled = false;
    }
  }

  function scheduleClampCheck() {
    clampQueue = bodies.filter(function (body) {
      var tile = body.closest ? body.closest('.tile') : null;
      return body.classList.contains('clamped') && (!tile || !tile.hidden);
    });
    if (!clampScheduled && clampQueue.length) {
      clampScheduled = true;
      window.requestAnimationFrame(drainClampQueue);
    }
  }

  var bodies = Array.prototype.slice.call(document.querySelectorAll('.tile-body'))
    .filter(function (body) { return body.childNodes.length > 0; });
  bodies.forEach(function (body) { body.classList.add('clamped'); });

  var resizeObserver = null;
  if (typeof window.ResizeObserver === 'function') {
    /* One shared observer for every tile body; re-checks overflow on resize. */
    resizeObserver = new window.ResizeObserver(function () { scheduleClampCheck(); });
    var mainEl = document.getElementById('main');
    if (mainEl) { resizeObserver.observe(mainEl); }
  } else {
    window.addEventListener('resize', function () { scheduleClampCheck(); });
  }

  /* ---------- sorting ---------- */
  function comparator(mode) {
    function byTitle(a, b) {
      var fa = fold(a.title), fb = fold(b.title);
      return fa < fb ? -1 : fa > fb ? 1 : 0;
    }
    if (mode === 'title') { return byTitle; }
    if (mode === 'added' || mode === 'updated') {
      return function (a, b) {
        var da = a[mode] || '', db = b[mode] || '';
        if (da && !db) { return -1; }
        if (!da && db) { return 1; }
        if (da !== db) { return da > db ? -1 : 1; } /* newest first */
        return byTitle(a, b);
      };
    }
    return function (a, b) {
      if (a.order !== b.order) { return a.order - b.order; }
      return byTitle(a, b);
    };
  }

  function applySort(mode) {
    var compare = comparator(mode);
    sectionEls.forEach(function (sectionEl) {
      var grid = sectionEl.querySelector('.grid');
      if (!grid) { return; }
      var sectionTiles = Array.prototype.slice.call(grid.querySelectorAll('.tile'));
      sectionTiles.sort(function (ta, tb) {
        var ia = itemById[ta.getAttribute('data-id')];
        var ib = itemById[tb.getAttribute('data-id')];
        if (!ia || !ib) { return 0; }
        return compare(ia, ib);
      });
      sectionTiles.forEach(function (tile) { grid.appendChild(tile); });
    });
  }

  /* ---------- incremental reveal for large galleries ---------- */
  var deferredTiles = [];
  var sentinel = null;

  function revealAllDeferred() {
    deferredTiles.forEach(function (tile) { tile.hidden = false; });
    deferredTiles = [];
    if (sentinel && sentinel.parentNode) { sentinel.parentNode.removeChild(sentinel); }
    sentinel = null;
  }

  (function setupIncremental() {
    var chunk = runtime.cards_per_page > 0 ? runtime.cards_per_page : 200;
    var threshold = runtime.cards_per_page > 0 ? runtime.cards_per_page : 800;
    if (tiles.length <= threshold || typeof window.IntersectionObserver !== 'function') { return; }
    deferredTiles = tiles.slice(chunk);
    deferredTiles.forEach(function (tile) { tile.hidden = true; });
    sentinel = document.createElement('div');
    sentinel.className = 'reveal-sentinel';
    sentinel.setAttribute('aria-hidden', 'true');
    document.getElementById('main').appendChild(sentinel);
    var observer = new window.IntersectionObserver(function (entries) {
      if (!entries.some(function (entry) { return entry.isIntersecting; })) { return; }
      var batch = deferredTiles.splice(0, chunk);
      batch.forEach(function (tile) { tile.hidden = false; });
      scheduleClampCheck();
      if (!deferredTiles.length) {
        observer.disconnect();
        if (sentinel && sentinel.parentNode) { sentinel.parentNode.removeChild(sentinel); }
        sentinel = null;
      }
    });
    observer.observe(sentinel);
  })();

  /* ---------- dialogs (settings + help share the wiring) ---------- */
  function wireDialog(dialogId, triggerId, closeId) {
    var dialog = document.getElementById(dialogId);
    var trigger = document.getElementById(triggerId);
    var closeButton = document.getElementById(closeId);
    if (!dialog || !trigger) { return; }
    var supported = typeof dialog.showModal === 'function';

    function isOpen() { return dialog.hasAttribute('open'); }

    function returnFocus() {
      try { trigger.focus({ preventScroll: true }); } catch (err) { trigger.focus(); }
    }

    function openDialog() {
      if (supported) { dialog.showModal(); }
      else { dialog.setAttribute('open', 'open'); }
      var first = dialog.querySelector('input:checked') || dialog.querySelector('button, input');
      if (first) { try { first.focus({ preventScroll: true }); } catch (err) { first.focus(); } }
    }

    function closeDialog() {
      if (supported) { dialog.close(); }
      else {
        dialog.removeAttribute('open');
        returnFocus();
      }
    }

    trigger.addEventListener('click', function () {
      if (isOpen()) { closeDialog(); } else { openDialog(); }
    });
    if (closeButton) { closeButton.addEventListener('click', closeDialog); }
    /* Backdrop click: the dialog element itself is only the click target
       when the click landed outside .dialog-inner. */
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) { closeDialog(); }
    });
    /* Escape triggers 'cancel' then 'close' natively; focus returns here. */
    dialog.addEventListener('close', returnFocus);
  }

  wireDialog('settings-dialog', 'settings-button', 'settings-close');
  wireDialog('help-dialog', 'help-button', 'help-close');

  /* ---------- settings: theme / sort / density ---------- */
  function bindRadioGroup(name, current, onChange) {
    var radios = document.querySelectorAll('input[name="' + name + '"]');
    Array.prototype.forEach.call(radios, function (radio) {
      radio.checked = radio.value === current;
      radio.addEventListener('change', function () {
        if (radio.checked) { onChange(radio.value); }
      });
    });
  }

  var themeNow = root.getAttribute('data-theme') || 'system';
  bindRadioGroup('theme', themeNow, function (value) {
    root.setAttribute('data-theme', value);
    storage.set('ai-gallery:theme', value);
  });

  var densityNow = root.getAttribute('data-density') || 'default';
  bindRadioGroup('density', densityNow, function (value) {
    root.setAttribute('data-density', value);
    storage.set('ai-gallery:density', value);
    scheduleClampCheck();
  });

  var validSorts = ['order', 'title', 'added', 'updated'];
  var sortNow = storage.get('ai-gallery:sort');
  if (validSorts.indexOf(sortNow) === -1) { sortNow = runtime.default_sort || 'order'; }
  if (validSorts.indexOf(sortNow) === -1) { sortNow = 'order'; }
  bindRadioGroup('sort', sortNow, function (value) {
    storage.set('ai-gallery:sort', value);
    applySort(value);
  });
  if (sortNow !== (runtime.default_sort || 'order')) { applySort(sortNow); }

  /* ---------- boot ---------- */
  var initial = readHash();
  if (serializeState() !== window.location.hash.replace(/^#/, '')) {
    setState(initial);
  } else {
    applyFilters();
  }
})();
