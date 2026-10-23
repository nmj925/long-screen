/* Long Screen dashboard. Plain JS, no build step. Reads data/screen.json, data/history.json, data/detail.json. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const EMBED = window.__LONGSCREEN__ || null;      // preview builds inline the data
  const IN_FRAME = (() => { try { return window.self !== window.top; } catch { return true; } })();
  const STORE = "longscreen.v1";

  const S = {
    data: null, history: { days: [] }, detail: null, crit: [], defaults: [], excluded: new Set(), sectors: [],
    view: "pass", q: "", sort: { key: "score", dir: -1 }, watch: new Set(), showAll: false, rows: [],
  };

  // ---------- formatting ----------
  const isNum = (v) => typeof v === "number" && isFinite(v);
  function fmt(kind, v) {
    if (!isNum(v)) return "–";
    switch (kind) {
      case "pct": return (v * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0) + "%";
      case "usd": return v >= 1e12 ? "$" + (v / 1e12).toFixed(2) + "T" : v >= 1e9 ? "$" + (v / 1e9).toFixed(1) + "B" : "$" + (v / 1e6).toFixed(0) + "M";
      case "usd2": return "$" + v.toFixed(2);
      case "int": return Math.round(v).toString();
      case "x": return v >= 999 ? "no debt" : Math.abs(v) < 10 ? v.toFixed(2) : v.toFixed(1);
      default: return v.toFixed(2);
    }
  }
  const unit = (c) => (c.fmt === "pct" ? "%" : c.fmt === "usd" ? "$B" : c.fmt === "x" ? "×" : "");
  const toInput = (c, v) => (c.fmt === "pct" ? +(v * 100).toFixed(2) : c.fmt === "usd" ? +(v / 1e9).toFixed(2) : v);
  const fromInput = (c, v) => (c.fmt === "pct" ? v / 100 : c.fmt === "usd" ? v * 1e9 : v);
  const opSym = (c) => (c.op === "min" ? "≥" : "≤");
  function thrText(c) {
    if (c.op === "pe_or_peg") return `P/E ≤ ${c.value[0]} or PEG ≤ ${c.value[1]}`;
    return `${opSym(c)} ${c.fmt === "usd" ? fmt("usd", c.value) : c.fmt === "pct" ? +(c.value * 100).toFixed(2) + "%" : c.value}`;
  }
  function ago(iso) {
    const m = (Date.now() - new Date(iso).getTime()) / 60000;
    if (!isFinite(m)) return "unknown";
    if (m < 90) return Math.max(1, Math.round(m)) + " min ago";
    if (m < 48 * 60) return Math.round(m / 60) + " h ago";
    return Math.round(m / 1440) + " days ago";
  }
  const dateLabel = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

  // ---------- persistence ----------
  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; }
  }
  function savePrefs() {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        crit: Object.fromEntries(S.crit.map((c) => [c.key, { on: c.on, value: c.value }])),
        excluded: [...S.excluded], watch: [...S.watch], view: S.view, sort: S.sort,
      }));
    } catch { /* storage unavailable: settings last for this visit only */ }
  }

  // ---------- screening ----------
  function check(s, c) {
    if (c.op === "pe_or_peg") {
      const pe = s.pe, peg = s.peg;
      if (!isNum(pe) && !isNum(peg)) return null;
      return (isNum(pe) && pe > 0 && pe <= c.value[0]) || (isNum(peg) && peg > 0 && peg <= c.value[1]);
    }
    const v = s[c.key];
    if (!isNum(v)) return null;
    return c.op === "min" ? v >= c.value : v <= c.value;
  }

  function evaluate() {
    const on = S.crit.filter((c) => c.on);
    for (const s of S.data.stocks) {
      s._res = {};
      let fails = 0;
      for (const c of on) {
        const r = check(s, c);
        s._res[c.key] = r;
        if (r !== true) fails++;
      }
      s._sectorOk = !S.excluded.has(s.sector);
      s._fails = fails;
      s._pass = s._sectorOk && fails === 0;
      s._near = s._sectorOk && fails === 1;
    }
  }

  // ---------- data load ----------
  async function getJSON(path, fallback) {
    if (EMBED && EMBED[path]) return EMBED[path];
    try {
      const r = await fetch(path, { cache: "no-cache" });
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch (e) {
      if (fallback !== undefined) return fallback;
      throw e;
    }
  }

  async function init() {
    try {
      S.data = await getJSON("data/screen.json");
    } catch {
      $("main").innerHTML = `<div class="card" style="padding:24px"><h2>No data yet</h2><p class="hint">Run <code>python screener.py full</code> (or the "Full screen" GitHub workflow), then reload. To preview with sample data, run <code>python tools/make_demo.py</code>.</p></div>`;
      return;
    }
    S.history = await getJSON("data/history.json", { days: [] });
    S.defaults = S.data.criteria.map((c) => ({ ...c, value: Array.isArray(c.value) ? [...c.value] : c.value }));
    const prefs = loadPrefs();
    S.crit = S.defaults.map((c) => {
      const p = prefs.crit && prefs.crit[c.key];
      return p ? { ...c, on: !!p.on, value: Array.isArray(c.value) ? (Array.isArray(p.value) ? p.value : c.value) : (isNum(p.value) ? p.value : c.value) } : { ...c, value: Array.isArray(c.value) ? [...c.value] : c.value };
    });
    S.sectors = [...new Set(S.data.stocks.map((s) => s.sector))].sort();
    S.excluded = new Set(prefs.excluded || S.data.excluded_sectors || []);
    S.watch = new Set(prefs.watch || []);
    if (prefs.view) S.view = prefs.view;
    if (prefs.sort) S.sort = prefs.sort;

    $("#demo-banner").hidden = !S.data.demo;
    $("#universe").textContent = `Quality at a reasonable price · ${S.data.universe} · ${S.data.stocks.length.toLocaleString()} stocks`;
    renderFresh();
    if (IN_FRAME) $("#export").hidden = true; // downloads are blocked inside embedded previews
    wire();
    renderAll();
    getJSON("data/detail.json", {}).then((d) => { S.detail = d; renderTable(); });
  }

  function renderFresh() {
    const p = S.data.prices_updated, g = S.data.generated;
    const stale = (Date.now() - new Date(p).getTime()) / 36e5 > 30 && !S.data.demo;
    $("#fresh").innerHTML =
      `<span class="pill${stale ? " stale" : ""}" title="${esc(dateLabel(p))}"><span class="dot"></span>Prices ${esc(S.data.demo ? dateLabel(p) : ago(p))}</span>` +
      `<span class="pill" title="${esc(dateLabel(g))}">Fundamentals ${esc(S.data.demo ? dateLabel(g) : ago(g))}</span>`;
  }

  // ---------- render: everything ----------
  function renderAll() {
    evaluate();
    renderTiles();
    renderFunnel();
    renderGroups();
    renderSectors();
    renderScatter();
    renderSectorBars();
    renderHistory();
    renderTable();
    savePrefs();
  }

  const median = (a) => { const v = a.filter(isNum).sort((x, y) => x - y); return v.length ? v[Math.floor(v.length / 2)] : null; };

  function renderTiles() {
    const pass = S.data.stocks.filter((s) => s._pass);
    const days = S.history.days || [];
    const last = days[days.length - 1], prev = days[days.length - 2];
    const added = last && prev ? last.passes.filter((t) => !prev.passes.includes(t)) : [];
    const dropped = last && prev ? prev.passes.filter((t) => !last.passes.includes(t)) : [];
    const eligible = S.data.stocks.filter((s) => s._sectorOk).length;
    const ticks = (arr, cls) => arr.length
      ? `<div class="tick-list">${arr.slice(0, 10).map((t) => `<button type="button" class="tick ${cls}" data-t="${esc(t)}">${esc(t)}</button>`).join("")}${arr.length > 10 ? `<span class="hint">+${arr.length - 10}</span>` : ""}</div>`
      : `<span class="d">None</span>`;
    const since = prev ? `since ${new Date(prev.date + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : "after the second run";
    $("#tiles").innerHTML = `
      <div class="card tile hero"><span class="label">Passing now</span><span class="v">${pass.length}</span>
        <span class="d">of ${eligible.toLocaleString()} eligible stocks (${eligible ? ((pass.length / eligible) * 100).toFixed(1) : 0}%)</span></div>
      <div class="card tile"><span class="label">New entries</span><span class="v">${added.length}</span><span class="d">${since}, default criteria</span>${ticks(added, "up")}</div>
      <div class="card tile"><span class="label">Dropped out</span><span class="v">${dropped.length}</span><span class="d">${since}, default criteria</span>${ticks(dropped, "down")}</div>
      <div class="card tile"><span class="label">Typical passing name</span>
        <span class="v">${fmt("pct", median(pass.map((s) => s.roic)))} <span class="d">ROIC</span></span>
        <span class="d">Median P/E ${fmt("x", median(pass.map((s) => s.pe)))} · FCF yield ${fmt("pct", median(pass.map((s) => s.fcf_yield)))} · Rev 3Y ${fmt("pct", median(pass.map((s) => s.rev3y)))}</span></div>`;
  }

  function control(c, compact) {
    const step = c.fmt === "pct" ? 1 : c.fmt === "usd" ? 0.5 : c.fmt === "int" ? 1 : 0.1;
    if (c.op === "pe_or_peg") {
      return `<div class="ctl"><span class="unit">P/E ≤</span><input type="number" id="v-${c.key}-0" data-k="${c.key}" data-i="0" step="1" value="${c.value[0]}" aria-label="Max P/E">
        <span class="or">or PEG ≤</span><input type="number" id="v-${c.key}-1" data-k="${c.key}" data-i="1" step="0.1" value="${c.value[1]}" aria-label="Max PEG"></div>`;
    }
    return `<div class="ctl"><span class="op">${opSym(c)}</span><input type="number" id="${compact ? "f" : "g"}-${c.key}" data-k="${c.key}" step="${step}" value="${toInput(c, c.value)}" aria-label="${esc(c.label)} threshold"><span class="unit">${unit(c)}</span></div>`;
  }

  function renderFunnel() {
    const on = S.crit.filter((c) => c.on);
    const elig = S.data.stocks.filter((s) => s._sectorOk);
    $("#funnel").innerHTML = on.map((c) => {
      const fails = elig.filter((s) => s._res[c.key] !== true).length;
      const sole = elig.filter((s) => s._fails === 1 && s._res[c.key] !== true).length;
      return `<div class="fstep" title="${esc(c.help)}">
        <div class="top-row"><span class="name">${esc(c.label)}</span><span class="cut${fails > elig.length * 0.5 ? " big" : ""}" title="Fails this test / only test failed">−${fails}${sole ? ` · ${sole} near` : ""}</span></div>
        ${control(c, true)}</div>`;
    }).join("");
    const nOn = on.length, nAll = S.crit.length;
    $("#crit-summary").textContent = `${nOn} of ${nAll} metrics active. Numbers show how many eligible stocks each test removes, and how many fail only that test.`;
  }

  function renderGroups() {
    const el = $("#groups");
    el.hidden = !S.showAll;
    $("#toggle-all").textContent = S.showAll ? "Hide metric list" : `Show all ${S.crit.length} metrics`;
    $("#toggle-all").setAttribute("aria-expanded", String(S.showAll));
    if (!S.showAll) return;
    const groups = [...new Set(S.crit.map((c) => c.group))];
    el.innerHTML = groups.map((g) => `<div class="group"><h4>${esc(g)}</h4>${S.crit.filter((c) => c.group === g).map((c) => `
      <div class="opt"><input type="checkbox" id="on-${c.key}" data-on="${c.key}"${c.on ? " checked" : ""}>
        <label for="on-${c.key}">${esc(c.label)}${c.help ? `<span class="h">${esc(c.help)}</span>` : ""}</label>
        ${c.op === "pe_or_peg" ? `<span class="hint">set above</span>` : control(c, false)}</div>`).join("")}</div>`).join("");
  }

  function renderSectors() {
    const counts = {};
    S.data.stocks.forEach((s) => (counts[s.sector] = (counts[s.sector] || 0) + 1));
    $("#sectors").innerHTML = S.sectors.map((sec) =>
      `<button type="button" class="chip" data-sector="${esc(sec)}" aria-pressed="${!S.excluded.has(sec)}">${esc(sec)} <span class="hint">${counts[sec]}</span></button>`).join("");
  }

  // ---------- charts ----------
  const tip = $("#tip");
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - r.width - 8, x + 14);
    const top = y + r.height + 20 > window.innerHeight ? y - r.height - 12 : y + 14;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  const hideTip = () => (tip.hidden = true);
  const ticksFor = (lo, hi, n = 5) => {
    const span = hi - lo, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  };

  function renderScatter() {
    const el = $("#scatter");
    const W = Math.max(300, el.clientWidth || 600), H = Math.round(Math.min(420, Math.max(260, W * 0.55)));
    const m = { l: 44, r: 16, t: 10, b: 34 };
    const peC = S.crit.find((c) => c.key === "pe_or_peg"), roicC = S.crit.find((c) => c.key === "roic");
    const pts = S.data.stocks.filter((s) => s._sectorOk && isNum(s.pe) && s.pe > 0 && isNum(s.roic));
    const xMax = Math.max(60, peC ? peC.value[0] * 1.6 : 60);
    const yMin = -0.1, yMax = 0.6;
    const X = (v) => m.l + (Math.min(v, xMax) / xMax) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (Math.max(yMin, Math.min(yMax, v)) - yMin) / (yMax - yMin)) * (H - m.t - m.b);
    const xt = ticksFor(0, xMax, 6), yt = ticksFor(yMin, yMax, 6);
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Scatter of P/E against ROIC">`;
    yt.forEach((v) => (svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>`));
    xt.forEach((v) => (svg += `<text x="${X(v)}" y="${H - m.b + 16}" text-anchor="middle">${v}${v === xMax ? "+" : ""}</text>`));
    svg += `<line class="base" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/>`;
    svg += `<text x="${W - m.r}" y="${H - 4}" text-anchor="end">Trailing P/E</text><text x="${m.l - 38}" y="${m.t + 4}" text-anchor="start" transform="rotate(-90 ${m.l - 34} ${m.t + 30})">ROIC</text>`;
    if (peC && peC.on && roicC && roicC.on) {
      const x1 = X(peC.value[0]), y1 = Y(roicC.value);
      svg += `<rect class="thr-zone" x="${m.l}" y="${m.t}" width="${x1 - m.l}" height="${y1 - m.t}"/>`;
      svg += `<line class="thr" x1="${x1}" x2="${x1}" y1="${m.t}" y2="${H - m.b}"/><line class="thr" x1="${m.l}" x2="${W - m.r}" y1="${y1}" y2="${y1}"/>`;
      svg += `<text class="lbl" x="${x1 + 4}" y="${m.t + 12}">P/E ${peC.value[0]}</text><text class="lbl" x="${W - m.r}" y="${y1 - 5}" text-anchor="end">ROIC ${Math.round(roicC.value * 100)}%</text>`;
    }
    const groups = [
      [pts.filter((s) => !s._pass && !s._near), "var(--other)", 2.5, false],
      [pts.filter((s) => s._near), "var(--s2)", 4, true],
      [pts.filter((s) => s._pass), "var(--s1)", 4.5, true],
    ];
    groups.forEach(([arr, color, r, ring]) => arr.forEach((s) => {
      svg += `<circle cx="${X(s.pe).toFixed(1)}" cy="${Y(s.roic).toFixed(1)}" r="${r}" fill="${color}"${ring ? ` stroke="var(--surface)" stroke-width="2"` : ""}/>`;
    }));
    const top = pts.filter((s) => s._pass).sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 5);
    const placed = [];
    top.forEach((s) => {
      const x = X(s.pe) + 7, y = Y(s.roic) + 4;
      if (placed.some(([px, py]) => Math.abs(px - x) < 44 && Math.abs(py - y) < 13)) return; // skip colliding labels
      placed.push([x, y]);
      svg += `<text class="lbl" x="${x}" y="${y}" style="font:600 11px var(--mono)">${esc(s.t)}</text>`;
    });
    svg += `<rect class="dot-hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}"/></svg>`;
    el.innerHTML = svg;
    const nPass = pts.filter((s) => s._pass).length, nNear = pts.filter((s) => s._near).length;
    $("#scatter-legend").innerHTML = `<span><i style="background:var(--s1)"></i>Passing (${nPass})</span><span><i style="background:var(--s2)"></i>Near miss, fails one test (${nNear})</span><span><i style="background:var(--other)"></i>Everything else</span><span class="hint">${S.data.stocks.filter((s) => s._sectorOk).length - pts.length} with no positive P/E not shown</span>`;
    const hit = $(".dot-hit", el), svgEl = $("svg", el);
    const find = (ev) => {
      const r = svgEl.getBoundingClientRect(), k = W / r.width;
      const px = (ev.clientX - r.left) * k, py = (ev.clientY - r.top) * k;
      let best = null, bd = 18 * 18;
      for (const s of pts) {
        const d = (X(s.pe) - px) ** 2 + (Y(s.roic) - py) ** 2;
        const w = s._pass ? 0.6 : s._near ? 0.8 : 1.4; // prefer highlighted points when overlapping
        if (d * w < bd) { bd = d * w; best = s; }
      }
      return best;
    };
    hit.addEventListener("pointermove", (ev) => {
      const s = find(ev);
      hit.style.cursor = s ? "pointer" : "default";
      if (!s) return hideTip();
      showTip(`<b>${esc(s.t)} <span class="hint">${esc(s.name)}</span></b>
        <div class="row">P/E <strong>${fmt("x", s.pe)}</strong></div><div class="row">ROIC <strong>${fmt("pct", s.roic)}</strong></div>
        <div class="row">Score <strong>${s.score ?? "–"}</strong></div><div class="row">${s._pass ? "Passes all tests" : s._fails + " test" + (s._fails > 1 ? "s" : "") + " failed"}</div>`, ev.clientX, ev.clientY);
    });
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("click", (ev) => { const s = find(ev); if (s) openDrawer(s.t); });
  }

  function renderSectorBars() {
    const el = $("#sectorbars");
    const pass = S.data.stocks.filter((s) => s._pass);
    const counts = {};
    pass.forEach((s) => (counts[s.sector] = (counts[s.sector] || 0) + 1));
    const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!rows.length) { el.innerHTML = `<p class="empty">Nothing passes yet. Loosen a criterion.</p>`; return; }
    const W = Math.max(260, el.clientWidth || 360), rowH = 26, lw = Math.min(170, W * 0.42);
    const H = rows.length * rowH + 4, max = rows[0][1];
    const bw = (v) => Math.max(2, (v / max) * (W - lw - 36));
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Passing stocks by sector">`;
    rows.forEach(([sec, n], i) => {
      const y = i * rowH + 4, w = bw(n);
      svg += `<g class="bar" data-sector="${esc(sec)}"><rect x="0" y="${y - 2}" width="${W}" height="${rowH}" fill="transparent"/>
        <text class="lbl" x="${lw - 8}" y="${y + 13}" text-anchor="end">${esc(sec.length > 24 ? sec.slice(0, 23) + "…" : sec)}</text>
        <path d="M${lw},${y + 3} h${w - 4} a4,4 0 0 1 4,4 v8 a4,4 0 0 1 -4,4 h-${w - 4} z" fill="var(--s1)"/>
        <text x="${lw + w + 6}" y="${y + 13}" style="fill:var(--ink)">${n}</text></g>`;
    });
    el.innerHTML = svg + `<line class="base" x1="${lw}" x2="${lw}" y1="0" y2="${H}"/></svg>`;
    $$(".bar", el).forEach((g) => {
      g.addEventListener("pointermove", (ev) => {
        const sec = g.dataset.sector, list = pass.filter((s) => s.sector === sec).sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
        showTip(`<b>${list.length} <span class="hint">${esc(sec)}</span></b><div class="hint">${list.slice(0, 12).map((s) => esc(s.t)).join(", ")}${list.length > 12 ? "…" : ""}</div>
          <div class="hint">${((list.length / pass.length) * 100).toFixed(0)}% of passing names</div>`, ev.clientX, ev.clientY);
      });
      g.addEventListener("pointerleave", hideTip);
    });
  }

  function lineChart(el, pts, opt) {
    // pts: [{x: Date, y: number, label}] single series, area wash, end dot, crosshair
    const W = Math.max(260, el.clientWidth || 360), H = opt.h || 180, m = { l: 44, r: 62, t: 10, b: 24 };
    const ys = pts.map((p) => p.y);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    if (opt.zero) lo = Math.min(0, lo);
    if (hi === lo) { hi += 1; lo = Math.max(0, lo - 1); }
    const pad = (hi - lo) * 0.08; hi += pad; if (!opt.zero) lo -= pad;
    const x0 = +pts[0].x, x1 = +pts[pts.length - 1].x;
    const X = (d) => m.l + ((+d - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
    const yt = ticksFor(lo, hi, 4);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
    const area = `${line}L${X(pts[pts.length - 1].x)},${H - m.b}L${X(pts[0].x)},${H - m.b}Z`;
    const last = pts[pts.length - 1];
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opt.aria)}">`;
    yt.forEach((v) => (svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${opt.yfmt(v)}</text>`));
    const fmtD = (d) => d.toLocaleDateString(undefined, { month: "short", ...(opt.year ? { year: "2-digit" } : { day: "numeric" }) });
    [pts[0], pts[Math.floor(pts.length / 2)], last].forEach((p, i) => (svg += `<text x="${X(p.x)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}">${fmtD(p.x)}</text>`));
    svg += `<path d="${area}" fill="${opt.color}" opacity="0.10"/><path d="${line}" fill="none" stroke="${opt.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    svg += `<circle cx="${X(last.x)}" cy="${Y(last.y)}" r="4" fill="${opt.color}" stroke="var(--surface)" stroke-width="2"/>`;
    svg += `<text x="${X(last.x) + 8}" y="${Y(last.y) + 4}" style="fill:var(--ink);font-weight:600">${opt.yfmt(last.y, true)}</text>`;
    svg += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--ink-2)" stroke-width="1" opacity="0" />`;
    svg += `<circle class="xd" r="4" fill="${opt.color}" stroke="var(--surface)" stroke-width="2" opacity="0"/>`;
    svg += `<rect class="dot-hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}"/></svg>`;
    el.innerHTML = svg;
    const svgEl = $("svg", el), hit = $(".dot-hit", el), xh = $(".xh", el), xd = $(".xd", el);
    hit.addEventListener("pointermove", (ev) => {
      const r = svgEl.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
      let best = pts[0];
      for (const p of pts) if (Math.abs(X(p.x) - px) < Math.abs(X(best.x) - px)) best = p;
      xh.setAttribute("x1", X(best.x)); xh.setAttribute("x2", X(best.x)); xh.setAttribute("opacity", "0.35");
      xd.setAttribute("cx", X(best.x)); xd.setAttribute("cy", Y(best.y)); xd.setAttribute("opacity", "1");
      showTip(`<div class="row"><span><span class="key" style="background:${opt.color}"></span>${best.x.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</span><strong>${opt.yfmt(best.y, true)}</strong></div>${best.label ? `<div class="hint">${esc(best.label)}</div>` : ""}`, ev.clientX, ev.clientY);
    });
    hit.addEventListener("pointerleave", () => { hideTip(); xh.setAttribute("opacity", "0"); xd.setAttribute("opacity", "0"); });
  }

  function renderHistory() {
    const el = $("#history");
    const days = S.history.days || [];
    if (days.length < 2) { el.innerHTML = `<p class="empty">The trend fills in after a couple of daily runs.</p>`; return; }
    const pts = days.map((d, i) => {
      const prev = days[i - 1];
      const add = prev ? d.passes.filter((t) => !prev.passes.includes(t)) : [];
      const drop = prev ? prev.passes.filter((t) => !d.passes.includes(t)) : [];
      return { x: new Date(d.date + "T12:00:00"), y: d.n, label: [add.length ? "+" + add.join(", +") : "", drop.length ? "−" + drop.join(", −") : ""].filter(Boolean).join("  ") };
    });
    lineChart(el, pts, { aria: "Number of passing stocks per day", color: "var(--s1)", zero: true, yfmt: (v, full) => (full ? v + " names" : Math.round(v)), h: 170 });
  }

  // ---------- table ----------
  function columns() {
    const cols = [
      { key: "score", label: "Score", fmt: "int", score: true },
      { key: "price", label: "Price", fmt: "usd2" },
      { key: "chg1d", label: "1D", fmt: "pct", signed: true },
      { key: "mcap", label: "Mkt cap", fmt: "usd", crit: "mcap" },
    ];
    for (const c of S.crit.filter((c) => c.on && c.key !== "mcap")) {
      if (c.op === "pe_or_peg") {
        cols.push({ key: "pe", label: "P/E", fmt: "x", crit: c.key }, { key: "peg", label: "PEG", fmt: "x", crit: c.key });
      } else cols.push({ key: c.key, label: shortLabel(c), fmt: c.fmt, crit: c.key, signed: ["mom", "perf6m", "dma200", "upside"].includes(c.key) });
    }
    if (!cols.some((c) => c.key === "perf12m")) cols.push({ key: "perf12m", label: "12M", fmt: "pct", signed: true });
    cols.push({ key: "_spark", label: "1 year", spark: true });
    return cols;
  }
  const SHORT = { rev3y: "Rev 3Y", roic: "ROIC", roic_avg: "ROIC avg", fcfm: "FCF mgn", de: "D/E", gm: "Gross mgn", gm_min: "GM worst", mom: "12-1 mom",
    fpe: "Fwd P/E", ev_ebitda: "EV/EBITDA", fcf_yield: "FCF yld", ps: "P/S", rev_up: "Rev up yrs", eps3y: "EPS 3Y", rev_q: "Rev Q YoY",
    roe: "ROE", om: "Op mgn", fcf_years: "FCF+ yrs", nd_ebitda: "ND/EBITDA", int_cov: "Int cov", cur_ratio: "Curr ratio", dilution: "Shares/yr",
    fscore: "F-score", zscore: "Z-score", perf6m: "6M", dma200: "vs 200d", off_high: "vs 52w hi", vol: "Vol", mdd: "Max DD", beta: "Beta",
    upside: "Upside", rec: "Rating", insider: "Insider", short: "Short" };
  const shortLabel = (c) => SHORT[c.key] || c.label;

  function spark(t, w = 84, h = 22) {
    const d = S.detail && S.detail[t];
    if (!d || !d.spark || d.spark.length < 2) return "";
    const v = d.spark, lo = Math.min(...v), hi = Math.max(...v);
    const X = (i) => (i / (v.length - 1)) * (w - 4) + 1, Y = (x) => h - 3 - ((x - lo) / (hi - lo || 1)) * (h - 6);
    const path = v.map((x, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(x).toFixed(1)}`).join("");
    const up = v[v.length - 1] >= v[0];
    const col = up ? "var(--s1)" : "var(--muted)";
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${path}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round"/><circle cx="${X(v.length - 1)}" cy="${Y(v[v.length - 1])}" r="2.5" fill="${col}"/></svg>`;
  }

  function currentRows() {
    let rows = S.data.stocks;
    if (S.view === "pass") rows = rows.filter((s) => s._pass);
    else if (S.view === "near") rows = rows.filter((s) => s._near);
    else if (S.view === "watch") rows = rows.filter((s) => S.watch.has(s.t));
    if (S.q) {
      const q = S.q.toLowerCase();
      rows = rows.filter((s) => s.t.toLowerCase().includes(q) || (s.name || "").toLowerCase().includes(q));
    }
    const { key, dir } = S.sort;
    return [...rows].sort((a, b) => {
      const x = key === "t" ? a.t : a[key], y = key === "t" ? b.t : b[key];
      if (key === "t") return dir * x.localeCompare(y);
      if (!isNum(x) && !isNum(y)) return 0;
      if (!isNum(x)) return 1;
      if (!isNum(y)) return -1;
      return dir * (x - y);
    });
  }

  function renderTable() {
    const cols = columns();
    const rows = currentRows();
    S.rows = rows;
    const counts = {
      pass: S.data.stocks.filter((s) => s._pass).length, near: S.data.stocks.filter((s) => s._near).length,
      watch: S.data.stocks.filter((s) => S.watch.has(s.t)).length, all: S.data.stocks.length,
    };
    $$("#tabs button").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.view === S.view));
      $(".count", b).textContent = counts[b.dataset.view];
    });
    const notes = {
      pass: "Every active test passes. Sorted by composite score unless you pick a column.",
      near: "Fails exactly one test, shown in red. Good places to look when a single number is distorted by a one-off.",
      watch: "Stars you add are saved in this browser.",
      all: "Every stock in the universe, including excluded sectors. Red cells fail a test.",
    };
    $("#res-note").textContent = notes[S.view];
    const sortAttr = (k) => (S.sort.key === k ? ` aria-sort="${S.sort.dir < 0 ? "descending" : "ascending"}"` : "");
    const head = `<thead><tr><th class="l" aria-label="Watchlist"></th><th class="l sticky" data-sort="t"${sortAttr("t")}>Ticker</th>${cols.map((c) =>
      `<th${c.spark ? "" : ` data-sort="${c.key}"`}${sortAttr(c.key)} class="${c.crit ? "crit" : ""}" title="${esc(c.crit ? thrText(S.crit.find((x) => x.key === c.crit)) : "")}">${esc(c.label)}</th>`).join("")}</tr></thead>`;
    const limit = 600;
    const body = rows.slice(0, limit).map((s) => {
      const cells = cols.map((c) => {
        if (c.spark) return `<td>${spark(s.t)}</td>`;
        const v = s[c.key];
        if (c.score) return `<td><span class="score"><span class="bar"><i style="width:${isNum(v) ? v : 0}%"></i></span>${isNum(v) ? v : "–"}</span></td>`;
        let cls = "";
        if (c.crit && s._res && c.crit in s._res) {
          const r = s._res[c.crit];
          if (r === null) cls = "na";
          else if (r === false) cls = "fail";
        }
        if (!cls && c.signed && isNum(v)) cls = v >= 0 ? "pos" : "neg";
        return `<td class="${cls}">${fmt(c.fmt, v)}</td>`;
      }).join("");
      return `<tr data-t="${esc(s.t)}"><td class="l"><button type="button" class="star" data-star="${esc(s.t)}" aria-pressed="${S.watch.has(s.t)}" aria-label="Watch ${esc(s.t)}">★</button></td>
        <td class="l sticky"><span class="tk">${esc(s.t)}</span><span class="nm">${esc(s.name)}</span></td>${cells}</tr>`;
    }).join("");
    const empty = !rows.length ? `<tr><td class="l" colspan="${cols.length + 2}"><p class="empty">${S.view === "watch" ? "Star a row to add it here." : "No matches. Loosen a criterion or clear the search."}</p></td></tr>` : "";
    const more = rows.length > limit ? `<tr><td class="l" colspan="${cols.length + 2}"><p class="hint">Showing the first ${limit} of ${rows.length}. Search or sort to narrow down.</p></td></tr>` : "";
    $("#table").innerHTML = head + `<tbody>${body}${empty}${more}</tbody>`;
  }

  // ---------- drawer ----------
  async function openDrawer(t) {
    const s = S.data.stocks.find((x) => x.t === t);
    if (!s) return;
    if (!S.detail) S.detail = await getJSON("data/detail.json", {});
    const d = S.detail[t] || {};
    const fails = S.crit.filter((c) => c.on && s._res[c.key] !== true);
    const verdict = !s._sectorOk ? `<span class="verdict no">✕ Excluded sector</span>`
      : s._pass ? `<span class="verdict ok">✓ Passes all ${S.crit.filter((c) => c.on).length} tests</span>`
      : `<span class="verdict no">✕ Fails ${fails.length}: ${esc(fails.map(shortLabel).join(", "))}</span>`;
    const sub = s.sub || {};
    const groups = [...new Set(S.crit.map((c) => c.group))];
    const metricRow = (c) => {
      const r = c.op === "pe_or_peg" ? check(s, c) : check(s, c);
      const val = c.op === "pe_or_peg" ? `${fmt("x", s.pe)} / ${fmt("x", s.peg)}` : fmt(c.fmt, s[c.key]);
      const st = !c.on ? `<span class="st off">not used · ${esc(thrText(c))}</span>` : r === null ? `<span class="st na">no data</span>`
        : r ? `<span class="st ok">✓ ${esc(thrText(c))}</span>` : `<span class="st no">✕ ${esc(thrText(c))}</span>`;
      return `<div class="mrow"><span title="${esc(c.help)}">${esc(c.label)}</span><span class="mv">${val}</span>${st}</div>`;
    };
    const extras = [["P/E (trailing)", "x", s.pe], ["PEG", "x", s.peg], ["3-month return", "pct", s.perf3m], ["12-month return", "pct", s.perf12m],
      ["Dividend yield", "pct", s.divy], ["Institutional ownership", "pct", s.inst], ["Analysts covering", "int", s.analysts],
      ["Data coverage (core metrics)", "pct", s.coverage]];
    const links = S.data.demo ? `<p class="hint">Links to Yahoo Finance, TradingView and SEC filings appear here with real data.</p>` : `<div class="links">
      <a href="https://finance.yahoo.com/quote/${encodeURIComponent(t)}" target="_blank" rel="noopener">Yahoo Finance</a>
      <a href="https://www.tradingview.com/chart/?symbol=${encodeURIComponent(t.replace("-", "."))}" target="_blank" rel="noopener">TradingView chart</a>
      <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(t.replace("-", "."))}&type=10-K" target="_blank" rel="noopener">10-K filings</a></div>`;
    $("#d-body").innerHTML = `
      <div class="d-head"><span class="tkr">${esc(t)} · ${esc(s.idx || "")}</span><h2 id="d-title">${esc(s.name)}</h2>
        <span class="hint">${esc(s.sector)}${s.industry && s.industry !== s.sector ? " · " + esc(s.industry) : ""}${s.fy_end ? " · last annual report " + esc(s.fy_end) : ""}</span></div>
      <div class="d-price"><span class="p">${fmt("usd2", s.price)}</span><span class="${(s.chg1d ?? 0) >= 0 ? "pos" : "neg"}">${fmt("pct", s.chg1d)} today</span>
        <span class="hint">${fmt("usd", s.mcap)} market cap</span>${verdict}
        <button type="button" class="star" data-star="${esc(t)}" aria-pressed="${S.watch.has(t)}" aria-label="Watch ${esc(t)}" style="font-size:18px">★</button></div>
      <div class="subscores">${["quality", "growth", "value", "momentum"].map((k) => `<div><span class="label">${k}</span><div class="v">${sub[k] ?? "–"}</div></div>`).join("")}</div>
      <p class="hint" style="margin:0">Composite score ${s.score ?? "–"}/100. Each part is a percentile rank against eligible stocks.</p>
      <div class="d-sec"><h3>Price, 1 year (weekly)</h3><div class="chart" id="d-price"></div></div>
      <div class="d-sec"><h3>Revenue by fiscal year</h3><div class="chart" id="d-rev"></div></div>
      ${groups.map((g) => `<div class="d-sec"><h3>${esc(g)}</h3>${S.crit.filter((c) => c.group === g).map(metricRow).join("")}</div>`).join("")}
      <div class="d-sec"><h3>Other figures</h3>${extras.map(([l, f, v]) => `<div class="mrow"><span>${l}</span><span class="mv">${fmt(f, v)}</span><span></span></div>`).join("")}</div>
      <div class="d-sec"><h3>Research</h3>${links}</div>`;
    const drawer = $("#drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    // charts need the drawer laid out to measure width
    requestAnimationFrame(() => {
      const pe = $("#d-price");
      if (d.spark && d.spark.length > 2 && d.dates) {
        const end = new Date(d.dates[1] + "T12:00:00");
        const pts = d.spark.map((y, i) => ({ x: new Date(end.getTime() - (d.spark.length - 1 - i) * 7 * 864e5), y }));
        lineChart(pe, pts, { aria: "Weekly price, one year", color: "var(--s1)", yfmt: (v, full) => "$" + (full ? v.toFixed(2) : v >= 100 ? Math.round(v) : v.toFixed(0)), h: 170 });
      } else pe.innerHTML = `<p class="empty">No price history.</p>`;
      revBars($("#d-rev"), d.revh || []);
      $("#d-close").focus();
    });
  }

  function revBars(el, revh) {
    if (!revh.length) { el.innerHTML = `<p class="empty">No annual statements.</p>`; return; }
    const W = Math.max(260, el.clientWidth || 480), H = 120, m = { t: 18, b: 20 };
    const max = Math.max(...revh.map((r) => r[1]), 1), n = revh.length, slot = W / n, bw = Math.min(24, slot * 0.5);
    const f = (v) => (v >= 1000 ? "$" + (v / 1000).toFixed(1) + "B" : "$" + v + "M");
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Annual revenue">`;
    revh.forEach(([yr, v], i) => {
      const h = Math.max(2, (Math.max(v, 0) / max) * (H - m.t - m.b)), x = i * slot + (slot - bw) / 2, y = H - m.b - h;
      svg += `<path d="M${x},${H - m.b} v-${h - 4} a4,4 0 0 1 4,-4 h${bw - 8} a4,4 0 0 1 4,4 v${h - 4} z" fill="var(--s1)"/>`;
      svg += `<text x="${x + bw / 2}" y="${y - 5}" text-anchor="middle" style="fill:var(--ink)">${f(v)}</text><text x="${x + bw / 2}" y="${H - 5}" text-anchor="middle">FY${String(yr).slice(2)}</text>`;
    });
    el.innerHTML = svg + `<line class="base" x1="0" x2="${W}" y1="${H - m.b}" y2="${H - m.b}"/></svg>`;
  }

  function closeDrawer() {
    const d = $("#drawer");
    d.classList.remove("open");
    d.setAttribute("aria-hidden", "true");
    hideTip();
  }

  // ---------- CSV ----------
  function exportCSV() {
    const cols = columns().filter((c) => !c.spark);
    const head = ["Ticker", "Name", "Sector", ...cols.map((c) => c.label), "Passes", "Failed tests"];
    const lines = S.rows.map((s) => [s.t, s.name, s.sector, ...cols.map((c) => (isNum(s[c.key]) ? s[c.key] : "")), s._pass ? "yes" : "no",
      S.crit.filter((c) => c.on && s._res[c.key] !== true).map((c) => c.label).join("; ")]);
    const csv = [head, ...lines].map((r) => r.map((v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v)).join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `long-screen-${S.view}-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // ---------- events ----------
  let raf = 0;
  const rerender = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      const id = document.activeElement && document.activeElement.id;
      renderAll();
      const el = id && document.getElementById(id);
      if (el) el.focus(); // inputs are re-rendered; keep the user's place
    });
  };

  function wire() {
    document.addEventListener("change", (ev) => {
      const el = ev.target;
      if (el.dataset.on) {
        const c = S.crit.find((x) => x.key === el.dataset.on);
        c.on = el.checked;
        rerender();
      } else if (el.dataset.k && el.type === "number") {
        const c = S.crit.find((x) => x.key === el.dataset.k);
        const v = parseFloat(el.value);
        if (!isFinite(v)) return;
        if (c.op === "pe_or_peg") c.value[+el.dataset.i] = v;
        else c.value = fromInput(c, v);
        rerender();
      }
    });
    document.addEventListener("click", (ev) => {
      const el = ev.target.closest("button, tr[data-t], th[data-sort]");
      if (!el) return;
      if (el.dataset.star) {
        ev.stopPropagation();
        const t = el.dataset.star;
        S.watch.has(t) ? S.watch.delete(t) : S.watch.add(t);
        $$(`[data-star="${CSS.escape(t)}"]`).forEach((b) => b.setAttribute("aria-pressed", String(S.watch.has(t))));
        savePrefs();
        if (S.view === "watch") renderTable();
        else $$("#tabs button").find((b) => b.dataset.view === "watch").querySelector(".count").textContent = S.watch.size;
      } else if (el.dataset.sector && el.classList.contains("chip")) {
        const sec = el.dataset.sector;
        S.excluded.has(sec) ? S.excluded.delete(sec) : S.excluded.add(sec);
        rerender();
      } else if (el.dataset.view) {
        S.view = el.dataset.view;
        renderTable(); savePrefs();
      } else if (el.dataset.sort) {
        const k = el.dataset.sort;
        S.sort = S.sort.key === k ? { key: k, dir: -S.sort.dir } : { key: k, dir: k === "t" ? 1 : -1 };
        renderTable(); savePrefs();
      } else if (el.matches("tr[data-t]") || el.classList.contains("tick")) {
        openDrawer(el.dataset.t);
      }
    });
    $("#toggle-all").addEventListener("click", () => { S.showAll = !S.showAll; renderGroups(); });
    $("#reset").addEventListener("click", () => {
      S.crit = S.defaults.map((c) => ({ ...c, value: Array.isArray(c.value) ? [...c.value] : c.value }));
      S.excluded = new Set(S.data.excluded_sectors || []);
      rerender();
    });
    $("#q").addEventListener("input", (ev) => { S.q = ev.target.value.trim(); renderTable(); });
    $("#export").addEventListener("click", exportCSV);
    $("#d-close").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
    let w = window.innerWidth;
    window.addEventListener("resize", () => {
      if (Math.abs(window.innerWidth - w) < 30) return;
      w = window.innerWidth;
      renderScatter(); renderSectorBars(); renderHistory();
    });
    // refresh data in the background so an open tab stays current with the hourly job
    if (!EMBED) setInterval(async () => {
      try {
        const fresh = await getJSON("data/screen.json");
        if (fresh.prices_updated !== S.data.prices_updated) {
          S.data = fresh;
          S.history = await getJSON("data/history.json", S.history);
          renderFresh(); renderAll();
        }
      } catch { /* keep showing what we have */ }
    }, 10 * 60 * 1000);
  }

  init();
})();
