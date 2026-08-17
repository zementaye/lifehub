// CSRF protection (Flask-WTF CSRFProtect, added server-side in app.py).
// Rather than hand-editing every one of this app's ~50 <form method=post>
// templates, we read the token base.html renders into a <meta> tag once
// and inject it into every POST form as a hidden field here — new forms
// added later get it for free. The two spots elsewhere in this file that
// POST via fetch()/sendBeacon() (bypassing <form> entirely) send it
// explicitly instead, via window.LIFEHUB_CSRF_TOKEN.
window.LIFEHUB_CSRF_TOKEN = (function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : '';
})();

(function () {
  function injectToken(form) {
    if (form.querySelector('input[name="csrf_token"]')) return;
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = window.LIFEHUB_CSRF_TOKEN;
    form.appendChild(input);
  }

  function isPost(form) {
    return (form.getAttribute('method') || '').toLowerCase() === 'post';
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form').forEach((form) => {
      if (isPost(form)) injectToken(form);
    });
  });

  // Belt-and-suspenders for any form injected into the DOM dynamically
  // after page load (none today, but cheap insurance against a silent
  // CSRF failure if one gets added later).
  document.addEventListener('submit', (e) => {
    if (isPost(e.target)) injectToken(e.target);
  }, true);
})();

// Light/dark mode toggle. Theme is applied pre-paint by an inline script in
// base.html (to avoid a flash); this just wires up the switch and persists
// the choice for next time.
(function () {
  const body = document.body;
  const switchEl = document.getElementById('modeSwitch');
  const rocker = document.getElementById('modeRocker');
  const label = document.getElementById('modeLabel');
  if (!switchEl) return;

  function reflect() {
    const light = body.dataset.theme === 'light';
    rocker.classList.toggle('on', light);
    label.textContent = light ? 'LIGHT' : 'DARK';
  }

  function setTheme(theme) {
    body.dataset.theme = theme;
    localStorage.setItem('lifehub-theme', theme);
    reflect();
  }

  reflect();
  switchEl.addEventListener('click', () => {
    setTheme(body.dataset.theme === 'light' ? 'dark' : 'light');
  });
  switchEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchEl.click(); }
  });
  switchEl.tabIndex = 0;
})();

// Color palette picker (Settings → Appearance). Same pre-paint pattern as
// the theme switch above: base.html applies the saved palette before first
// paint, this just wires up the swatches and persists the choice.
(function () {
  const picker = document.getElementById('palettePicker');
  if (!picker) return;
  const body = document.body;
  const swatches = picker.querySelectorAll('.palette-swatch');

  function reflect() {
    const current = body.dataset.palette || 'amber';
    swatches.forEach((sw) => sw.classList.toggle('selected', sw.dataset.palette === current));
  }

  function setPalette(palette) {
    body.dataset.palette = palette;
    localStorage.setItem('lifehub-palette', palette);
    reflect();
  }

  reflect();
  swatches.forEach((sw) => {
    sw.addEventListener('click', () => setPalette(sw.dataset.palette));
  });
})();

// Auto-dismiss flash messages ("Note added: ...", etc.) after a few seconds
// instead of leaving them sitting on screen until the next page load.
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.flash-msg').forEach((msg, i) => {
      setTimeout(() => {
        msg.classList.add('flash-out');
        msg.addEventListener('transitionend', () => msg.remove(), { once: true });
      }, 3500 + i * 300); // stagger slightly if there are several at once
    });
  });
})();

// Habit/to-do checkboxes: tick instantly instead of waiting on a full page
// reload for every click. The click updates the UI right away, and the
// actual save is queued and flushed shortly after (or immediately if the
// user navigates away/closes the tab before that timer fires), so a run of
// quick clicks only costs one request instead of one full reload each.
(function () {
  const forms = document.querySelectorAll('.checklist.interactive .checkbox-form');
  if (!forms.length) return;

  const SAVE_DELAY = 600; // ms of inactivity on an item before we save it
  const pending = new Map(); // itemId -> { url, timer }

  function flush(itemId, useBeacon) {
    const entry = pending.get(itemId);
    if (!entry) return;
    clearTimeout(entry.timer);
    pending.delete(itemId);
    // CSRFProtect checks the token in the request body (form-encoded) or
    // the X-CSRFToken header. sendBeacon can't set custom headers, so it
    // gets the token in an urlencoded body instead; fetch uses the header.
    if (useBeacon && navigator.sendBeacon) {
      const body = new Blob(
        ['csrf_token=' + encodeURIComponent(window.LIFEHUB_CSRF_TOKEN || '')],
        { type: 'application/x-www-form-urlencoded' }
      );
      navigator.sendBeacon(entry.url, body);
    } else {
      fetch(entry.url, {
        method: 'POST',
        keepalive: true,
        headers: { 'X-CSRFToken': window.LIFEHUB_CSRF_TOKEN || '' },
      }).catch(() => {
        // Best-effort — if this fails the state just reverts on next reload,
        // no dangling UI to clean up since we already updated optimistically.
      });
    }
  }

  function flushAll(useBeacon) {
    Array.from(pending.keys()).forEach((id) => flush(id, useBeacon));
  }

  forms.forEach((form) => {
    const li = form.closest('li');
    const btn = form.querySelector('.checkbox-btn');
    const itemId = li ? li.dataset.itemId : null;
    if (!li || !btn || !itemId) return;

    form.addEventListener('submit', (e) => {
      e.preventDefault();

      const willBeDone = !li.classList.contains('done');
      li.classList.toggle('done', willBeDone);
      btn.classList.toggle('checked', willBeDone);
      btn.setAttribute('aria-label', willBeDone ? 'Mark not done' : 'Mark done');

      const url = willBeDone ? form.dataset.checkUrl : form.dataset.uncheckUrl;

      const existing = pending.get(itemId);
      if (existing) clearTimeout(existing.timer);
      pending.set(itemId, {
        url,
        timer: setTimeout(() => flush(itemId, false), SAVE_DELAY),
      });
    });
  });

  // Make sure anything still pending gets saved if they navigate away or
  // close the tab before its debounce timer fires.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushAll(true);
  });
  window.addEventListener('pagehide', () => flushAll(true));
})();

// Habit reminder selects: no submit button — changing a select queues a
// save (debounced, so flipping through day + hour only costs one request),
// and anything still pending is flushed immediately if the user navigates
// away or closes the tab before the debounce timer fires. Mirrors the
// checkbox autosave pattern above.
(function () {
  const forms = document.querySelectorAll('.habit-reminder-form[data-autosave]');
  if (!forms.length) return;

  const SAVE_DELAY = 700; // ms of inactivity before we save
  const pending = new Map(); // form -> timer

  function flush(form, useBeacon) {
    const entry = pending.get(form);
    if (!entry) return;
    clearTimeout(entry);
    pending.delete(form);

    const params = new URLSearchParams();
    form.querySelectorAll('select[name]').forEach((sel) => params.set(sel.name, sel.value));

    const indicator = form.querySelector('.save-indicator');
    const showSaved = () => {
      if (!indicator) return;
      indicator.textContent = 'Saved';
      indicator.classList.add('show');
      setTimeout(() => indicator.classList.remove('show'), 1400);
    };

    if (useBeacon && navigator.sendBeacon) {
      params.set('csrf_token', window.LIFEHUB_CSRF_TOKEN || '');
      navigator.sendBeacon(form.action, new Blob([params.toString()], { type: 'application/x-www-form-urlencoded' }));
    } else {
      fetch(form.action, {
        method: 'POST',
        keepalive: true,
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRFToken': window.LIFEHUB_CSRF_TOKEN || '',
        },
        body: params.toString(),
      }).then(showSaved).catch(() => {
        // Best-effort — if this fails the select just reverts on next reload.
      });
    }
  }

  function flushAll(useBeacon) {
    Array.from(pending.keys()).forEach((form) => flush(form, useBeacon));
  }

  forms.forEach((form) => {
    form.addEventListener('submit', (e) => e.preventDefault());
    form.querySelectorAll('select[name]').forEach((sel) => {
      sel.addEventListener('change', () => {
        clearTimeout(pending.get(form));
        pending.set(form, setTimeout(() => flush(form, false), SAVE_DELAY));
      });
    });
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushAll(true);
  });
  window.addEventListener('pagehide', () => flushAll(true));
})();

// Debounced food search against /nutrition/search, only runs on the nutrition page.
(function () {
  const input = document.getElementById('food-search');
  if (!input) return;

  const resultsBox = document.getElementById('search-results');
  const logForm = document.getElementById('log-form');
  const preview = document.getElementById('log-preview');

  let timer = null;

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      resultsBox.innerHTML = '';
      return;
    }
    timer = setTimeout(() => runSearch(q), 350);
  });

  async function runSearch(q) {
    resultsBox.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const resp = await fetch(`/nutrition/search?q=${encodeURIComponent(q)}`);
      const data = await resp.json();
      renderResults(data.results || []);
    } catch (e) {
      resultsBox.innerHTML = '<p class="muted">Search failed — try again.</p>';
    }
  }

  function renderResults(results) {
    if (results.length === 0) {
      resultsBox.innerHTML = '<p class="muted">No matches found.</p>';
      return;
    }
    resultsBox.innerHTML = '';
    results.forEach((r) => {
      const div = document.createElement('div');
      div.className = 'search-result';
      const tag = r.generic
        ? '<span class="tag">generic</span>'
        : `<span class="tag">branded${r.brand ? ': ' + escapeHtml(r.brand) : ''}</span>`;
      div.innerHTML = `<div class="sr-name">${escapeHtml(r.name)} ${tag}</div>
        <div class="sr-macro">${Math.round(r.calories)} cal · ${round1(r.protein_g)}g protein · ${round1(r.carbs_g)}g carbs · ${round1(r.fat_g)}g fat — per 100g</div>`;
      div.addEventListener('click', () => selectFood(r));
      resultsBox.appendChild(div);
    });
  }

  function selectFood(r) {
    document.getElementById('log-name').value = r.name;
    document.getElementById('log-calories').value = r.calories;
    document.getElementById('log-protein').value = r.protein_g;
    document.getElementById('log-carbs').value = r.carbs_g;
    document.getElementById('log-fat').value = r.fat_g;
    document.getElementById('log-fiber').value = r.fiber_g;
    preview.textContent = `Selected: ${r.name} — ${Math.round(r.calories)} cal per 100g. Enter how many grams you actually ate below.`;
    logForm.style.display = 'flex';
  }

  function round1(n) { return Math.round((n || 0) * 10) / 10; }
  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }
})();

// Count-up animation for any [data-countup] stat number, and animated
// fill-in for any .animated-fill progress bar. Runs on every page.
(function () {
  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function animateCountUp(el) {
    const target = parseFloat(el.dataset.countup);
    if (isNaN(target)) return;
    const duration = 800;
    const start = performance.now();
    const isNegative = target < 0;
    const absTarget = Math.abs(target);

    function frame(now) {
      const t = Math.min((now - start) / duration, 1);
      const value = Math.round(absTarget * easeOutCubic(t));
      el.textContent = (isNegative ? '-' : '') + value.toLocaleString();
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function animateFills() {
    document.querySelectorAll('.animated-fill').forEach((el) => {
      const pct = parseFloat(el.dataset.targetPct);
      if (isNaN(pct)) return;
      // Force layout so the browser registers width:0 first, otherwise it
      // may jump straight to the target instead of animating.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          el.style.width = Math.max(0, Math.min(pct, 100)) + '%';
        });
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-countup]').forEach(animateCountUp);
    animateFills();
  });
})();

// Grouped nav: desktop dropdowns (Health/Vault/Tasks in .topnav) and the
// mobile sidebar drawer. Runs on every page.
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    // --- Desktop dropdown groups ---
    const groups = document.querySelectorAll('.nav-group');
    function closeAllGroups(except) {
      groups.forEach((g) => { if (g !== except) g.classList.remove('open'); });
    }
    groups.forEach((group) => {
      const toggle = group.querySelector('.nav-group-toggle');
      if (!toggle) return;
      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        const willOpen = !group.classList.contains('open');
        closeAllGroups(group);
        group.classList.toggle('open', willOpen);
      });
    });
    document.addEventListener('click', () => closeAllGroups(null));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAllGroups(null); });

    // --- Mobile sidebar drawer ---
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebarClose = document.getElementById('sidebarClose');
    const sidebarBackdrop = document.getElementById('sidebarBackdrop');
    if (!sidebar || !sidebarToggle) return;

    function openSidebar() {
      sidebar.classList.add('open');
      sidebar.setAttribute('aria-hidden', 'false');
      if (sidebarBackdrop) sidebarBackdrop.classList.add('open');
      sidebarToggle.setAttribute('aria-expanded', 'true');
    }
    function closeSidebar() {
      sidebar.classList.remove('open');
      sidebar.setAttribute('aria-hidden', 'true');
      if (sidebarBackdrop) sidebarBackdrop.classList.remove('open');
      sidebarToggle.setAttribute('aria-expanded', 'false');
    }

    sidebarToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
    });
    if (sidebarClose) sidebarClose.addEventListener('click', closeSidebar);
    if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', closeSidebar);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSidebar(); });
  });
})();

// Shared delete/destructive-action confirmation modal (see the markup in
// base.html). Any form with data-confirm="..." uses this instead of the
// native browser confirm() popup, so every page's confirmation looks and
// behaves the same. Runs in the capture phase so it intercepts the first
// (unconfirmed) submit attempt before the "saving…" handler below ever
// sees it; the real, confirmed submit is a second dispatch that's let
// through and handled normally (loading overlay, button spinner, etc).
(function () {
  const overlay = document.getElementById('confirm-modal-overlay');
  if (!overlay) return;
  const titleEl = document.getElementById('confirm-modal-title');
  const subEl = document.getElementById('confirm-modal-sub');
  const okBtn = document.getElementById('confirm-modal-ok');
  const cancelBtn = document.getElementById('confirm-modal-cancel');
  let pendingForm = null;

  function open(form) {
    pendingForm = form;
    titleEl.textContent = form.dataset.confirm;

    // Most confirmations are permanent deletes (the default look: red
    // "Delete" button, "This can't be undone."). A form can opt into a
    // milder, reversible-action look via data-confirm-ok (button label)
    // and data-confirm-danger="false" — e.g. "Make admin", "Suspend",
    // "Log out everywhere" aren't destructive and shouldn't be styled or
    // worded as if they were.
    const isDanger = form.dataset.confirmDanger !== 'false';
    okBtn.textContent = form.dataset.confirmOk || 'Delete';
    okBtn.classList.toggle('confirm-modal-danger', isDanger);

    const note = form.dataset.confirmNote !== undefined
      ? form.dataset.confirmNote
      : (isDanger ? "This can't be undone." : '');
    subEl.textContent = note;
    subEl.style.display = note ? '' : 'none';

    overlay.style.display = 'flex';
  }
  function close() {
    pendingForm = null;
    overlay.style.display = 'none';
  }

  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
    if (form.dataset.confirmed === '1') {
      delete form.dataset.confirmed; // this is the real, already-confirmed submit — let it through
      return;
    }
    e.preventDefault();
    open(form);
  }, true);

  cancelBtn.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.style.display !== 'none') close();
  });
  okBtn.addEventListener('click', () => {
    const form = pendingForm;
    if (!form) return;
    close();
    form.dataset.confirmed = '1';
    form.requestSubmit ? form.requestSubmit() : form.submit();
  });

  // Safety net if the page is restored from bfcache mid-confirm.
  window.addEventListener('pageshow', close);
})();

// Global "saving…" state for every ordinary form submit (Save/Add/Renew/
// Delete/etc. across the whole app). These are all full-page POSTs, so
// without this nothing on screen shows a request is in flight until the
// reload lands — inviting duplicate clicks/submits on a slow connection.
// Opts out automatically for the habit/to-do checkbox forms above, which
// already have their own instant-feedback handling.
(function () {
  const overlay = document.createElement('div');
  overlay.className = 'page-loading-overlay';
  overlay.setAttribute('aria-hidden', 'true');
  overlay.innerHTML = '<div class="page-loading-spinner"></div><div class="page-loading-text">Saving…</div>';
  const textEl = overlay.querySelector('.page-loading-text');

  function mountOverlay() {
    if (!overlay.isConnected) document.body.appendChild(overlay);
  }
  if (document.body) {
    mountOverlay();
  } else {
    document.addEventListener('DOMContentLoaded', mountOverlay);
  }

  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.classList.contains('checkbox-form')) return; // handled elsewhere, optimistic UI
    if (form.dataset.noLoading !== undefined) return; // explicit opt-out
    if (e.defaultPrevented) return; // some other handler already took over

    const clicked = e.submitter; // the actual button that triggered this submit, if any
    if (clicked && clicked.tagName === 'BUTTON') {
      clicked.classList.add('btn-loading');
    }
    // Disable every submit button on the page (not just this form's) so a
    // second action can't fire while this one is still in flight.
    document.querySelectorAll('form button').forEach((b) => {
      if (b.type !== 'button') b.disabled = true;
    });
    // A form can override the overlay's wording via data-loading-text — the
    // AI forms use this since a Gemini round-trip is a lot longer than a
    // normal save and "Thinking…" sets that expectation better than "Saving…".
    textEl.textContent = form.dataset.loadingText || 'Saving…';
    mountOverlay();
    overlay.classList.add('visible');
  });

  // If the page is served from bfcache (back/forward) with the overlay
  // still showing from a previous submit, clear it so the UI isn't stuck.
  window.addEventListener('pageshow', () => {
    overlay.classList.remove('visible');
    document.querySelectorAll('form button:disabled').forEach((b) => {
      b.disabled = false;
      b.classList.remove('btn-loading');
    });
  });
})();

// HUD ambient starfield. Purely decorative dots scattered behind the
// content (see #hud-stars in base.html / CSS keyframes in style.css).
// Skipped entirely under prefers-reduced-motion since it's animation-only.
(function () {
  const field = document.getElementById('hud-stars');
  if (!field) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const COUNT = 70;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < COUNT; i++) {
    const s = document.createElement('span');
    s.style.left = Math.random() * 100 + '%';
    s.style.top = Math.random() * 100 + '%';
    s.style.animationDelay = (Math.random() * 4) + 's';
    const big = Math.random() < 0.15;
    s.style.width = s.style.height = big ? '3px' : '1.5px';
    frag.appendChild(s);
  }
  field.appendChild(frag);
})();

// HUD panel tilt. Every .card gets a subtle mouse-tracked 3D tilt toward
// the cursor, matching the Command Deck mockup. Disabled under
// prefers-reduced-motion, and skipped on touch-only devices where there's
// no hover to track.
(function () {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia('(hover: none)').matches) return;

  document.querySelectorAll('.card').forEach((card) => {
    card.addEventListener('mousemove', (e) => {
      const r = card.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5;
      const y = (e.clientY - r.top) / r.height - 0.5;
      card.style.transform =
        `perspective(700px) rotateX(${(-y * 4).toFixed(2)}deg) rotateY(${(x * 4).toFixed(2)}deg) translateY(-2px)`;
    });
    card.addEventListener('mouseleave', () => {
      card.style.transform = '';
    });
  });
})();

// ── Reorderable page sections ───────────────────────────────────────────
// Shared by every page that lets a user reorder its top-level blocks
// (Dashboard, Budget, Habits, Health, Nutrition, Settings, To Do, ...).
// Each such page renders its sections inside a wrapper element, each
// direct child tagged class="reorder-section" data-section="<key>", and
// calls LifehubReorder.init(wrapperId, pageKey, initialOrder) once from a
// small nonce'd inline <script> (the actual reorder/save logic lives here
// so it's loaded once, not duplicated per page). initialOrder is the
// server-computed order (see _section_order in app.py) — applied here by
// physically re-appending elements in that order, which keeps the Jinja
// template itself straightforward (always renders sections in their
// natural/default order) while still respecting what the user saved.
window.LifehubReorder = {
  init: function (wrapperId, pageKey, initialOrder) {
    const wrap = document.getElementById(wrapperId);
    if (!wrap) return;

    (initialOrder || []).forEach(function (key) {
      const el = wrap.querySelector('.reorder-section[data-section="' + key + '"]');
      if (el) wrap.appendChild(el);
    });

    function currentOrder() {
      return Array.prototype.map.call(
        wrap.querySelectorAll('.reorder-section'),
        function (el) { return el.getAttribute('data-section'); }
      );
    }

    function saveOrder() {
      fetch('/section-order/' + encodeURIComponent(pageKey), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': window.LIFEHUB_CSRF_TOKEN || '',
        },
        body: JSON.stringify({ order: currentOrder() }),
      }).catch(function () { /* best-effort — worst case it re-saves next click */ });
    }

    wrap.addEventListener('click', function (e) {
      const btn = e.target.closest('.reorder-up, .reorder-down');
      if (!btn) return;
      const section = btn.closest('.reorder-section');
      if (!section) return;
      if (btn.classList.contains('reorder-up')) {
        const prev = section.previousElementSibling;
        if (prev) wrap.insertBefore(section, prev);
      } else {
        const next = section.nextElementSibling;
        if (next) wrap.insertBefore(next, section);
      }
      saveOrder();
    });
  },
};
