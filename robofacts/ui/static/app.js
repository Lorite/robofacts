// The trials UI's shared behaviour (#131): the stop bar's pin, sortable tables, copy
// buttons, operator steps run by INDEX, filter forms that apply on change, and the
// table or cards switch with its video dialog (#137).
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

  // ----------------------------------------------------------------- theme --
  // Auto -> Dark -> Light -> Auto. Auto removes the attribute, so the CSS follows
  // the OS; the choice is remembered per browser (a convenience, never required).
  const themeBtn = document.getElementById('themetoggle');
  const themes = ['auto', 'dark', 'light'];
  const label = { auto: '◐ Auto', dark: '☾ Dark', light: '☀ Light' };
  const currentTheme = () => document.documentElement.getAttribute('data-theme') || 'auto';
  const renderTheme = () => {
    if (!themeBtn) return;
    themeBtn.textContent = label[currentTheme()];
    themeBtn.title = 'theme: ' + currentTheme() + ' (click to change; Auto follows the OS)';
  };
  if (themeBtn) themeBtn.addEventListener('click', () => {
    const next = themes[(themes.indexOf(currentTheme()) + 1) % themes.length];
    if (next === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('trialsTheme', next); } catch (e) { /* not remembered */ }
    renderTheme();
  });
  renderTheme();

  // ------------------------------------------------------------ sort tables --
  // Click a header to sort; a cell may carry data-sort for a value other than its text.
  // A sorted or narrowed table tells its card view (#137) with a 'trials:changed' event.
  const changed = (table) => table.dispatchEvent(new CustomEvent('trials:changed'));
  const sortBy = (table, col, asc) => {
    const headers = table.querySelectorAll('thead th');
    const th = headers[col];
    if (!th) return;
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
    changed(table);
  };
  document.querySelectorAll('table.sortable').forEach((table) => {
    table.querySelectorAll('thead th').forEach((th, col) => {
      if (th.hasAttribute('data-nosort')) return;
      th.classList.add('sortable');
      th.title = th.title || 'click to sort';
      th.addEventListener('click', () => sortBy(table, col, th.dataset.dir !== 'asc'));
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
      changed(table);
    };
    input.addEventListener('input', apply);
  });

  // --------------------------------------------------- table or cards (#137) --
  // The cards are the table's own rows drawn differently: the same records, kept in
  // the table's order and narrowed by the same find box, so switching views keeps
  // the filters (in the URL) and the sort (on the table). The choice is a per-viewer
  // preference: ?view=cards or ?view=table wins and is remembered, else the last
  // choice, else the cards (the default since 2026-10-22, Alejandro).
  const VIEW_KEY = 'trialsView';
  const storedView = () => { try { return localStorage.getItem(VIEW_KEY); } catch (e) { return null; } };
  const urlView = new URLSearchParams(location.search).get('view');
  document.querySelectorAll('.viewbar[data-views]').forEach((bar) => {
    const table = document.getElementById(bar.dataset.views);
    const cards = document.getElementById(bar.dataset.views + '-cards');
    if (!table || !cards) return;
    const wrap = table.closest('.tablewrap') || table;
    const buttons = bar.querySelectorAll('[data-view]');
    const sortLabel = bar.querySelector('.cardsort');
    const sortSelect = bar.querySelector('select[data-sort-for]');
    const byName = new Map();
    cards.querySelectorAll('.tcard').forEach((c) => byName.set(c.dataset.trial, c));
    const sync = () => {
      table.querySelectorAll('tbody tr[data-trial]').forEach((tr) => {
        const card = byName.get(tr.dataset.trial);
        if (!card) return;
        card.hidden = tr.style.display === 'none';
        cards.appendChild(card);
      });
    };
    const show = (view, remember) => {
      const asCards = view === 'cards';
      wrap.hidden = asCards;
      cards.hidden = !asCards;
      if (sortLabel) sortLabel.hidden = !asCards;
      buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === (asCards ? 'cards' : 'table'))));
      if (asCards) {
        sync();
        // Only now do the stills load, and the browser still fetches them lazily. A
        // layer behind a tag (#138) loads when its tag is picked.
        cards.querySelectorAll('img[data-src]').forEach((img) => {
          if (img.closest('[data-layer][hidden]')) return;
          img.src = img.dataset.src;
          img.removeAttribute('data-src');
        });
      }
      if (remember) { try { localStorage.setItem(VIEW_KEY, asCards ? 'cards' : 'table'); } catch (e) { /* not remembered */ } }
    };
    if (sortSelect) {
      table.querySelectorAll('thead th').forEach((th, col) => {
        if (th.hasAttribute('data-nosort')) return;
        const name = th.textContent.replace(/ [▲▼]$/, '').trim();
        [['desc', '▼'], ['asc', '▲']].forEach(([dir, mark]) => {
          const opt = document.createElement('option');
          opt.value = `${col}:${dir}`;
          opt.textContent = `${name} ${mark}`;
          sortSelect.appendChild(opt);
        });
      });
      sortSelect.addEventListener('change', () => {
        const [col, dir] = sortSelect.value.split(':');
        if (col !== '') sortBy(table, Number(col), dir === 'asc');
      });
    }
    table.addEventListener('trials:changed', () => { if (!cards.hidden) sync(); });
    buttons.forEach((b) => b.addEventListener('click', () => show(b.dataset.view, true)));
    bar.hidden = false;
    show(urlView || storedView() || 'cards', Boolean(urlView));
  });

  // Stills of the declared camera (#138): moving the pointer across a card's picture
  // scrubs through them, and leaving it shows the first again. The bar shows where.
  const stillsOf = (el) => {
    if (!el._stills) { try { el._stills = JSON.parse(el.dataset.stills); } catch (e) { el._stills = []; } }
    return el._stills;
  };
  const scrubTo = (el, k) => {
    const list = stillsOf(el);
    const img = el.querySelector('img');
    if (!img || !list.length || img.hasAttribute('data-src')) return;
    k = Math.max(0, Math.min(list.length - 1, k));
    if (img.getAttribute('src') !== list[k]) img.src = list[k];
    const bar = el.querySelector('.scrub i');
    if (bar) bar.style.marginLeft = `${(100 * k) / list.length}%`;
  };
  document.addEventListener('pointermove', (e) => {
    const el = e.target.closest && e.target.closest('.poster[data-stills]');
    if (!el) return;
    const box = el.getBoundingClientRect();
    scrubTo(el, Math.floor(((e.clientX - box.left) / box.width) * stillsOf(el).length));
  });
  document.addEventListener('pointerout', (e) => {
    const el = e.target.closest && e.target.closest('.poster[data-stills]');
    if (el && !el.contains(e.relatedTarget)) scrubTo(el, 0);
  });

  // The tags on a card's picture (#138): trial video, camera, each plot. A tag shows
  // its layer and hides the others, and its image loads only now.
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('.ptab');
    if (!tab) return;
    const box = tab.closest('.pbox');
    box.querySelectorAll('.ptab').forEach((t) => t.setAttribute('aria-pressed', String(t === tab)));
    box.querySelectorAll('.poster[data-layer]').forEach((layer) => {
      const shown = layer.dataset.layer === tab.dataset.layer;
      layer.hidden = !shown;
      const img = shown && layer.querySelector('img[data-src]');
      if (img) { img.src = img.dataset.src; img.removeAttribute('data-src'); }
    });
  });

  // A still that could not be made (no ffmpeg, a broken video) shows the placeholder;
  // the card still plays the video.
  document.addEventListener('error', (e) => {
    if (e.target.matches && e.target.matches('.poster img')) e.target.closest('.poster').classList.add('nostill');
  }, true);

  // Click a still: the video plays in a dialog over the list, which stays as it was.
  let dialog = null;
  const videoDialog = () => {
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.className = 'videodlg';
    dialog.innerHTML = '<div class="dlghead"><a class="dlgtitle"></a>'
      + '<button type="button" class="btn-small dlgclose" title="close (Esc)">✕</button></div>'
      + '<video controls autoplay playsinline preload="auto"></video>';
    const video = dialog.querySelector('video');
    dialog.querySelector('.dlgclose').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => { video.pause(); video.removeAttribute('src'); video.load(); });
    document.body.appendChild(dialog);
    return dialog;
  };
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.poster[data-video]');
    if (!btn) return;
    const dlg = videoDialog();
    const title = dlg.querySelector('.dlgtitle');
    title.textContent = btn.dataset.title || 'video';
    title.href = btn.dataset.page || btn.dataset.video;
    title.title = 'open the trial page';
    const video = dlg.querySelector('video');
    video.src = btn.dataset.video;
    const img = btn.querySelector('img');
    if (img && img.src && !btn.classList.contains('nostill')) video.poster = img.src; else video.removeAttribute('poster');
    if (typeof dlg.showModal === 'function') dlg.showModal(); else window.open(btn.dataset.video, '_blank');
    video.play().catch(() => { /* autoplay refused: the controls are there */ });
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
