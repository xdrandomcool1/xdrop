// Shared helpers + chunked, resumable uploader used by the admin page and the friend upload page.
const $ = s => document.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toLocaleString('hu-HU', { maximumFractionDigits: n < 10 ? 1 : 0 }) + ' ' + u[i];
}
function fmtTime(s) {
  if (!isFinite(s) || s < 0) return '–';
  if (s < 60) return Math.ceil(s) + ' mp';
  if (s < 3600) return Math.floor(s / 60) + ' p ' + Math.floor(s % 60) + ' mp';
  return Math.floor(s / 3600) + ' ó ' + Math.floor(s % 3600 / 60) + ' p';
}
function fmtDate(ts) { return new Date(ts * 1000).toLocaleString('hu-HU', { dateStyle: 'medium', timeStyle: 'short' }); }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800); }
async function copy(text, msg = 'Link kimásolva') {
  try { await navigator.clipboard.writeText(text); }
  catch { const t = el('textarea'); t.value = text; document.body.append(t); t.select(); document.execCommand('copy'); t.remove(); }
  toast(msg);
}

const extraHeaders = {};
async function api(method, url, body) {
  const r = await fetch(url, {
    method, credentials: 'same-origin',
    headers: { 'X-XDrop': '1', ...extraHeaders, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined
  });
  let j = {}; try { j = await r.json(); } catch {}
  if (!r.ok) { const e = new Error(j.detail || ('HTTP ' + r.status)); e.status = r.status; throw e; }
  return j;
}

// opts: { drop, picker, queue, getShareId: async () => id, onIdle: () => void }
function setupUploader(opts) {
  const PARALLEL = 2, MIN_CHUNK = 4 << 20, MAX_CHUNK = 64 << 20, TARGET_SECONDS = 15;
  const queue = []; let active = 0;
  const { drop, picker } = opts;

  drop.onclick = () => picker.click();
  drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); picker.click(); } };
  picker.onchange = e => { enqueue([...e.target.files].map(f => ({ file: f, path: f.name }))); e.target.value = ''; };
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
  drop.addEventListener('drop', async e => {
    const entries = [...(e.dataTransfer.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (entries.length) { const out = []; for (const en of entries) await walk(en, '', out); enqueue(out); }
    else enqueue([...e.dataTransfer.files].map(f => ({ file: f, path: f.name })));
  });
  window.addEventListener('beforeunload', e => { if (active || queue.length) { e.preventDefault(); e.returnValue = ''; } });

  async function walk(entry, prefix, out) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader(); let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const child of batch) await walk(child, prefix + entry.name + '/', out);
      } while (batch.length);
    }
  }

  async function enqueue(list) {
    if (!list.length) return;
    let shareId;
    try { shareId = await opts.getShareId(); } catch (e) { toast('Hiba: ' + e.message); return; }
    for (const { file, path } of list) queue.push({ file, path, shareId, ui: makeRow(path, file.size) });
    pump();
  }
  function pump() {
    while (active < PARALLEL && queue.length) {
      const job = queue.shift(); active++;
      upload(job).catch(err => job.ui.fail(err.message))
        .finally(() => { active--; pump(); if (!active && !queue.length && opts.onIdle) opts.onIdle(); });
    }
  }

  function makeRow(name, size) {
    const it = el('div', 'item'), row = el('div', 'row'), info = el('div', 'grow');
    const st = el('div', 'small muted', 'Várakozik · ' + fmtBytes(size));
    info.append(el('div', 'name', name), st);
    const cancel = el('button', 'danger', 'Mégse');
    row.append(info, cancel);
    const bar = el('div', 'bar'), fill = el('i'); bar.append(fill);
    it.append(row, bar);
    opts.queue.prepend(it);
    opts.queue.closest('.queue-wrap')?.classList.remove('hidden');
    const ui = {
      cancelled: false, xhr: null, fid: null,
      progress(done, speed) {
        fill.style.width = (size ? done / size * 100 : 100) + '%';
        st.textContent = `${fmtBytes(done)} / ${fmtBytes(size)} · ${fmtBytes(speed)}/s · hátra: ${fmtTime((size - done) / speed)}`;
      },
      text(t) { st.textContent = t; },
      ok() { it.classList.add('done'); fill.style.width = '100%'; st.className = 'small status-ok'; st.textContent = 'Kész · ' + fmtBytes(size); cancel.remove(); },
      fail(msg) { it.classList.add('failed'); st.className = 'small status-bad'; st.textContent = 'Hiba: ' + msg; cancel.remove(); }
    };
    cancel.onclick = async () => {
      ui.cancelled = true; if (ui.xhr) ui.xhr.abort();
      const i = queue.findIndex(j => j.ui === ui); if (i >= 0) queue.splice(i, 1);
      if (ui.fid) try { await api('DELETE', '/api/files/' + ui.fid); } catch {}
      it.remove();
    };
    return ui;
  }

  function putChunk(ui, fid, offset, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest(); ui.xhr = x;
      x.open('PUT', `/api/files/${fid}?offset=${offset}`);
      x.setRequestHeader('X-XDrop', '1');
      for (const [k, v] of Object.entries(extraHeaders)) x.setRequestHeader(k, v);
      x.setRequestHeader('Content-Type', 'application/octet-stream');
      x.timeout = 10 * 60 * 1000;
      x.upload.onprogress = e => onProgress(e.loaded);
      x.onload = () => {
        let j = {}; try { j = JSON.parse(x.responseText); } catch {}
        if (x.status === 200 || x.status === 409) resolve(j);
        else { const e = new Error(j.detail || j.error || ('HTTP ' + x.status)); e.status = x.status; reject(e); }
      };
      x.onerror = () => reject(Object.assign(new Error('Hálózati hiba'), { status: 0 }));
      x.ontimeout = () => reject(Object.assign(new Error('Időtúllépés'), { status: 0 }));
      x.onabort = () => reject(Object.assign(new Error('Megszakítva'), { status: -1 }));
      x.send(blob);
    });
  }

  async function upload(job) {
    const { file, ui } = job;
    if (ui.cancelled) return;
    const meta = await api('POST', `/api/shares/${job.shareId}/files`, { name: job.path, size: file.size });
    ui.fid = meta.id;
    let offset = 0, chunk = 8 << 20, failures = 0, status = file.size ? 'uploading' : 'done';
    const t0 = performance.now();
    while (status === 'uploading' && offset < file.size) {
      if (ui.cancelled) return;
      const end = Math.min(offset + chunk, file.size), started = performance.now(), base = offset;
      try {
        const r = await putChunk(ui, meta.id, offset, file.slice(offset, end),
          loaded => ui.progress(base + loaded, (base + loaded) / ((performance.now() - t0) / 1000)));
        const sent = r.received - offset, secs = (performance.now() - started) / 1000;
        if (sent > 0 && secs > 0) chunk = Math.max(MIN_CHUNK, Math.min(MAX_CHUNK, Math.round(sent / secs * TARGET_SECONDS / (1 << 20)) << 20));
        offset = r.received; status = r.status || 'uploading'; failures = 0;
      } catch (e) {
        if (e.status === -1 || ui.cancelled) return;
        if ([400, 401, 403, 404, 410, 413, 507].includes(e.status) || ++failures > 15) throw e;
        chunk = Math.max(MIN_CHUNK, chunk >> 1);
        ui.text(`Újrapróbálás (${failures}/15): ${e.message}`);
        await sleep(Math.min(30000, 1000 * 2 ** failures));
        let s = null;
        try { s = await api('GET', '/api/files/' + meta.id); } catch { /* still offline */ }
        if (s && s.status === 'failed') throw new Error(s.error || 'Sikertelen');
        if (s) { offset = s.received; status = s.status; }
      }
    }
    ui.ok();
  }
}
