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

// Grouped nav: desktop dropdowns (Vault/Tasks in .topnav) and mobile
// slide-up sheets (Vault/Tasks in .bottomnav). Runs on every page.
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

    // --- Mobile slide-up sheets ---
    const backdrop = document.querySelector('[data-sheet-backdrop]');
    const sheetToggles = document.querySelectorAll('.bottomnav-toggle[data-sheet]');
    const sheets = document.querySelectorAll('.nav-sheet');

    function closeAllSheets() {
      sheets.forEach((s) => s.classList.remove('open'));
      if (backdrop) backdrop.classList.remove('open');
    }
    function openSheet(id) {
      closeAllSheets();
      const sheet = document.getElementById(id);
      if (!sheet) return;
      sheet.classList.add('open');
      if (backdrop) backdrop.classList.add('open');
    }
    sheetToggles.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = btn.dataset.sheet;
        const sheet = document.getElementById(id);
        if (sheet && sheet.classList.contains('open')) {
          closeAllSheets();
        } else {
          openSheet(id);
        }
      });
    });
    if (backdrop) backdrop.addEventListener('click', closeAllSheets);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAllSheets(); });
  });
})();
