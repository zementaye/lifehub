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
      div.innerHTML = `<div class="sr-name">${escapeHtml(r.name)}</div>
        <div class="sr-macro">${Math.round(r.calories)} cal · ${round1(r.protein_g)}g protein · ${round1(r.carbs_g)}g carbs · ${round1(r.fat_g)}g fat (per 100g / serving)</div>`;
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
    preview.textContent = `Selected: ${r.name} (${Math.round(r.calories)} cal per serving as listed)`;
    logForm.style.display = 'flex';
  }

  function round1(n) { return Math.round((n || 0) * 10) / 10; }
  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }
})();
