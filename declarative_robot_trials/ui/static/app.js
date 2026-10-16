// The trials UI's shared behaviour (#131): the stop bar's pin, sortable tables, copy
// buttons, operator steps run by INDEX, and filter forms that apply on change.
// Every page also works without this file: the filters are plain GET forms.
(function () {
  // ------------------------------------------------------------ stop bar pin --
  // Pinning is a preference, so it is remembered; the default is pinned because
  // the stop controls are what you reach for in a hurry.
  const bar = document.getElementById('stopbar');
  const pin = document.getElementById('pintoggle');
  const pinned = () => { try { return localStorage.getItem('stopbarPinned') !== 'no'; } catch (e) { return true; } };
  const renderPin = () => {
    if (!bar || !pin) return;
    bar.classList.toggle('pinned', pinned());
    pin.textContent = pinned() ? '📌 pinned' : '📍 unpinned';
  };
  if (pin) pin.addEventListener('click', () => {
    try { localStorage.setItem('stopbarPinned', pinned() ? 'no' : 'yes'); } catch (e) { /* private window */ }
    renderPin();
  });
  renderPin();

  // ------------------------------------------------------------ sort tables --
  // Click a header to sort; a cell may carry data-sort for a value other than its text.
  document.querySelectorAll('table.sortable').forEach((table) => {
    const headers = table.querySelectorAll('thead th');
    headers.forEach((th, col) => {
      if (th.hasAttribute('data-nosort')) return;
      th.classList.add('sortable');
      th.title = th.title || 'click to sort';
      th.addEventListener('click', () => {
        const asc = th.dataset.dir !== 'asc';
        headers.forEach((h) => { delete h.dataset.dir; h.textContent = h.textContent.replace(/ [▲▼]$/, ''); });
        th.dataset.dir = asc ? 'asc' : 'desc';
        th.textContent += asc ? ' ▲' : ' ▼';
        const key = (tr) => {
          const td = tr.children[col];
          if (!td) return '';
          const raw = td.dataset.sort !== undefined ? td.dataset.sort : td.textContent.trim();
          const n = parseFloat(raw);
          return (!isNaN(n) && /^-?[\d.]+$/.test(raw)) ? n : raw.toLowerCase();
        };
        const body = table.querySelector('tbody');
        Array.from(body.rows)
          .sort((a, b) => {
            const ka = key(a), kb = key(b);
            const cmp = (typeof ka === 'number' && typeof kb === 'number') ? ka - kb : String(ka).localeCompare(String(kb));
            return asc ? cmp : -cmp;
          })
          .forEach((r) => body.appendChild(r));
      });
    });
  });

  // -------------------------------------------------------- quick text find --
  // Narrows the rows already on the page by name; the server-side filters
  // (campaign, status, cell) are the CLI's and stay in the URL.
  document.querySelectorAll('input[data-find]').forEach((input) => {
    const table = document.getElementById(input.dataset.find);
    if (!table) return;
    const count = document.getElementById(input.dataset.find + '-count');
    const apply = () => {
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      table.querySelectorAll('tbody tr').forEach((tr) => {
        const hit = !q || tr.textContent.toLowerCase().includes(q);
        tr.style.display = hit ? '' : 'none';
        if (hit) shown += 1;
      });
      if (count) count.textContent = q ? `${shown} match “${input.value.trim()}”` : '';
    };
    input.addEventListener('input', apply);
  });

  // ------------------------------------------------- filters apply on change --
  document.querySelectorAll('form.autosubmit').forEach((form) => {
    form.querySelectorAll('select, input[type=checkbox]').forEach((el) => {
      el.addEventListener('change', () => form.submit());
    });
    const btn = form.querySelector('.applybtn');
    if (btn) btn.style.display = 'none';
  });

  // -------------------------------------------------------------- copy text --
  // Copies a command, exactly the string shown; over a plain-http tunnel the
  // clipboard API throws, so fall back to a temporary selection.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const text = btn.dataset.copy;
    const label = btn.textContent;
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = '✓';
    } catch (err) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      btn.textContent = document.execCommand('copy') ? '✓' : '✗';
      ta.remove();
    }
    setTimeout(() => { btn.textContent = label; }, 1200);
  });

  // ------------------------------------------------------- operator steps --
  // Runs a step by INDEX: the browser never sends a command, so the campaign
  // file stays the allowlist for everything a page can execute.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.runstep');
    if (!btn) return;
    const out = btn.closest('li').querySelector('.stepout');
    btn.disabled = true; btn.textContent = '…';
    out.style.display = 'block'; out.textContent = 'running on the trial host…';
    try {
      const body = new URLSearchParams({
        experiment: btn.dataset.experiment, campaign: btn.dataset.campaign,
        kind: btn.dataset.kind, index: btn.dataset.index,
      });
      const r = await fetch(location.origin + (btn.dataset.url || '/operator_step'), { method: 'POST', body });
      out.textContent = await r.text();
    } catch (err) {
      out.textContent = 'request failed: ' + err;
    }
    btn.disabled = false; btn.textContent = '▶ run';
  });
})();
