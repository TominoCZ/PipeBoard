"use strict";
// The EQ node (M11): its math (the same RBJ biquads as pipeboard/eq.py, so the curve drawn is
// the sound made), the small curve on its card, and the editor panel docked at the bottom.
// board.js gives it what it needs: EQ.open(key, {dev, title, rate, send, menu, done}).
// Messages out: {op: "eq-set", key, bands, bypass} · {op: "eq-watch", key, on}

const EQ = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const TYPES = [["bell", "Bell"], ["low-shelf", "Low shelf"], ["high-shelf", "High shelf"],
                 ["low-cut", "Low cut"], ["high-cut", "High cut"], ["notch", "Notch"]];
  const NAME = Object.fromEntries(TYPES);
  const SLOPES = { 12: [0.7071], 24: [0.5412, 1.3066], 48: [0.5098, 0.6013, 0.9, 2.5629] };
  // SLOTS: what bands may use; eq.py keeps 3 of its 16 for makeup gain (see BAND_SLOTS there).
  const F0 = 20, F1 = 20000, GAIN = 18, VIEW = 20, Q0 = 0.1, Q1 = 18, MAX = 8, SLOTS = 13;
  const COLORS = ["#e5a04d", "#4fc1b0", "#a98ce8", "#e57a9a", "#8ccf6a", "#5aa9e6", "#e5d34d", "#ef7a72"];
  const GRID_F = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
  const isCut = (b) => b.type === "low-cut" || b.type === "high-cut";
  const hasGain = (b) => !isCut(b) && b.type !== "notch";

  // ---- math ---------------------------------------------------------------------------------
  function biquad(type, freq, gain, q, rate) {
    const w = 2 * Math.PI * Math.min(freq, 0.49 * rate) / rate, c = Math.cos(w);
    const al = Math.sin(w) / (2 * q), a = 10 ** (gain / 40), r = 2 * Math.sqrt(a) * al;
    const k = {
      "bell": [1 + al * a, -2 * c, 1 - al * a, 1 + al / a, -2 * c, 1 - al / a],
      "low-shelf": [a * ((a + 1) - (a - 1) * c + r), 2 * a * ((a - 1) - (a + 1) * c), a * ((a + 1) - (a - 1) * c - r),
                    (a + 1) + (a - 1) * c + r, -2 * ((a - 1) + (a + 1) * c), (a + 1) + (a - 1) * c - r],
      "high-shelf": [a * ((a + 1) + (a - 1) * c + r), -2 * a * ((a - 1) + (a + 1) * c), a * ((a + 1) + (a - 1) * c - r),
                     (a + 1) - (a - 1) * c + r, 2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - r],
      "low-cut": [(1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + al, -2 * c, 1 - al],
      "high-cut": [(1 - c) / 2, 1 - c, (1 - c) / 2, 1 + al, -2 * c, 1 - al],
      "notch": [1, -2 * c, 1, 1 + al, -2 * c, 1 - al],
    }[type];
    return k.map((v) => v / k[3]);
  }
  const sections = (b) => (isCut(b) ? SLOPES[b.slope || 12] : [b.q]);
  const slotsUsed = (bands) => bands.reduce((n, b) => n + sections(b).length, 0);
  const coefs = (bands, rate) => bands.filter((b) => b.on !== false)
    .flatMap((b) => sections(b).map((q) => biquad(b.type, b.freq, b.gain, q, rate)));
  function level(cs, f, rate) {  // dB at f
    const w = 2 * Math.PI * f / rate, c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
    let m = 1;
    for (const [b0, b1, b2, a0, a1, a2] of cs) {
      const nr = b0 + b1 * c1 + b2 * c2, ni = b1 * s1 + b2 * s2, dr = a0 + a1 * c1 + a2 * c2, di = a1 * s1 + a2 * s2;
      m *= Math.sqrt((nr * nr + ni * ni) / (dr * dr + di * di));
    }
    return 20 * Math.log10(Math.max(m, 1e-9));
  }

  // ---- drawing helpers ----------------------------------------------------------------------
  const lf0 = Math.log10(F0), lspan = Math.log10(F1) - lf0;
  const xOf = (f, w) => ((Math.log10(f) - lf0) / lspan) * w;
  const fOf = (x, w) => 10 ** (lf0 + (x / w) * lspan);
  const yOf = (db, h) => h / 2 - (Math.max(-VIEW * 1.6, Math.min(VIEW * 1.6, db)) / VIEW) * (h / 2);
  const dbOf = (y, h) => ((h / 2 - y) / (h / 2)) * VIEW;
  const fmtF = (f) => (f >= 1000 ? `${Number((f / 1000).toFixed(f >= 10000 ? 1 : 2))} kHz` : `${Math.round(f)} Hz`);
  const fmtG = (g) => (g > 0 ? `+${Number(g.toFixed(1))}` : g < 0 ? `−${Number(Math.abs(g).toFixed(1))}` : "0") + " dB";
  const fmtQ = (q) => q.toFixed(q < 10 ? 2 : 1);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  function s(tag, attrs = {}, ...kids) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    el.append(...kids);
    return el;
  }
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") el.textContent = v; else if (v !== false && v != null) el.setAttribute(k, v);
    }
    el.append(...kids.filter((k) => k != null));
    return el;
  }
  // Where a curve is sampled: evenly, plus densely around each band, so a sharp Q's tip is drawn
  // round instead of as a spike between two samples (owner).
  function xsFor(bands, w, n) {
    const xs = Array.from({ length: n + 1 }, (_, i) => (i / n) * w);
    for (const b of bands || []) {
      if (b.on === false) continue;
      // An octave each side whatever its Q: a span that followed Q moved every sample as Q changed,
      // and the drawn line wobbled with it.
      const c = xOf(b.freq, w), hw = xOf(2 * b.freq, w) - c;
      for (let k = -40; k <= 40; k++) xs.push(c + Math.sign(k) * (k / 40) ** 2 * hw);  // closest at its tip
    }
    return xs.filter((x) => x >= 0 && x <= w).sort((a, b) => a - b);
  }
  function curvePath(cs, w, ht, rate, n = 160, bands = []) {
    return xsFor(bands, w, n).map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${yOf(level(cs, fOf(x, w), rate), ht).toFixed(1)}`).join("");
  }
  // ---- dynamic bands (M15): a band that moves with how loud it is (pipeboard/eq.py) ----------
  // The stage after the EQ multiplies by 1 + (g − 1)·F, F the band's region (a band-pass for a
  // bell, a low-/high-pass for a shelf), g its Range as a factor at full movement: so the curve
  // the band can reach is exact, not a guess.
  const DYN_SLOTS = 4, DYN_TYPES = ["bell", "low-shelf", "high-shelf"];
  const DYN = { thr: [-60, 0, -24], range: [-18, 18, -6], attack: [0.1, 200, 10], release: [10, 2000, 150] };
  const canDyn = (b) => DYN_TYPES.includes(b.type);
  const isDyn = (b) => b.dyn === true && b.on !== false && canDyn(b);
  function dynFilter(b, rate) {
    const w = 2 * Math.PI * Math.min(b.freq, 0.49 * rate) / rate, c = Math.cos(w);
    const al = Math.sin(w) / (2 * (b.type === "bell" ? b.q : 0.7071)), a0 = 1 + al;
    const num = { "bell": [al, 0, -al], "low-shelf": [(1 - c) / 2, 1 - c, (1 - c) / 2], "high-shelf": [(1 + c) / 2, -(1 + c), (1 + c) / 2] }[b.type];
    return [...num.map((v) => v / a0), 1, -2 * c / a0, (1 - al) / a0];
  }
  function region(b, f, rate) {  // the band's region filter at f, as a complex number
    const [b0, b1, b2, , a1, a2] = dynFilter(b, rate), w = 2 * Math.PI * f / rate;
    const zr = (k) => Math.cos(k * w), zi = (k) => -Math.sin(k * w);
    const nr = b0 + b1 * zr(1) + b2 * zr(2), ni = b1 * zi(1) + b2 * zi(2);
    const dr = 1 + a1 * zr(1) + a2 * zr(2), di = a1 * zi(1) + a2 * zi(2);
    const den = dr * dr + di * di;
    return [(nr * dr + ni * di) / den, (ni * dr - nr * di) / den];
  }
  // dB the stage gives at f when the band has moved by `by` dB (at full movement: its Range).
  function dynLevel(b, f, rate, by = b.range ?? DYN.range[2]) {
    const [hr, hi] = region(b, f, rate), k = 10 ** (by / 20) - 1;
    return 20 * Math.log10(Math.max(Math.hypot(1 + k * hr, k * hi), 1e-9));
  }

  // Where a band's handle sits: at its gain; cuts and notches on the 0 dB line.
  const handleY = (b, ht) => yOf(hasGain(b) ? b.gain : 0, ht);

  // ---- the card's small curve ---------------------------------------------------------------
  function mini(dev, rate, w = 196, ht = 56) {
    const bands = dev.bands || [], cs = dev.bypass ? [] : coefs(bands, rate);
    const svg = s("svg", { class: `eq-mini${dev.bypass ? " bypassed" : ""}`, viewBox: `0 0 ${w} ${ht}`, width: w, height: ht });
    for (const f of [100, 1000, 10000]) svg.append(s("line", { class: "eq-grid", x1: xOf(f, w), x2: xOf(f, w), y1: 0, y2: ht }));
    svg.append(s("line", { class: "eq-zero", x1: 0, x2: w, y1: ht / 2, y2: ht / 2 }));
    const line = curvePath(cs, w, ht, rate, 90, dev.bypass ? [] : bands);
    svg.append(s("path", { class: "eq-fill", d: `${line}L${w},${ht / 2}L0,${ht / 2}Z` }), s("path", { class: "eq-line", d: line }));
    bands.forEach((b, i) => {
      if (b.on === false) return;
      svg.append(s("circle", { class: "eq-dot", cx: xOf(b.freq, w), cy: handleY(b, ht), r: 2.6, fill: COLORS[i % COLORS.length] }));
    });
    return svg;
  }

  // ---- the panel -----------------------------------------------------------------------------
  const P = { key: null, el: null, bands: [], bypass: false, sel: -1, drag: null, spectrum: null, sent: 0, hover: -1 };
  const W = 1000, HT = 280;  // the graph's own units; it scales to the panel

  function open(key, ctx) {
    close(false);
    Object.assign(P, { key, ctx, bands: structuredClone(ctx.dev.bands || []), bypass: !!ctx.dev.bypass,
                       sel: (ctx.dev.bands || []).length ? 0 : -1, spectrum: null });
    const graph = s("svg", { class: "eq-graph", viewBox: `0 0 ${W} ${HT}`, preserveAspectRatio: "none" });
    const by = h("button", { type: "button", class: "card-btn bypass", text: "Bypass", "aria-pressed": String(P.bypass),
                             "data-tip": "Let the sound through untouched" });
    const shut = h("button", { type: "button", class: "eq-close", "aria-label": "Close the EQ", text: "✕" });
    const head = h("header", { class: "eq-head" }, h("span", { class: "pin" }), h("span", { class: "eq-title", text: ctx.title }),
                   h("span", { class: "eq-count" }), h("span", { class: "eq-space" }), by, shut);
    const axisF = h("div", { class: "eq-axis-f" }, ...GRID_F.map((f) => h("span", { style: `left:${(xOf(f, W) / W) * 100}%`, text: f >= 1000 ? `${f / 1000}k` : String(f) })));
    const axisG = h("div", { class: "eq-axis-g" }, ...[18, 12, 6, 0, -6, -12, -18].map((g) => h("span", { class: g === 0 ? "zero" : null, style: `top:${(yOf(g, HT) / HT) * 100}%`, text: g > 0 ? `+${g}` : String(g).replace("-", "−") })));
    // The spectrum on a layer of its own behind the graph (owner: graphs stuttered): it changes
    // every frame, and the graph (curve, bands, handles) is then not repainted with it.
    P.specEl = s("path", { class: "eq-spectrum", d: P.spectrum ? spectrumD(P.spectrum) : "" });
    const spec = s("svg", { class: "eq-spec-layer", viewBox: `0 0 ${W} ${HT}`, preserveAspectRatio: "none" }, P.specEl);
    const wrap = h("div", { class: "eq-plot" }, spec, graph, axisG, axisF, h("div", { class: "eq-read", hidden: "" }));
    const strip = h("div", { class: "eq-strip" });
    P.el = h("section", { class: "eq-panel", tabindex: "-1", role: "dialog", "aria-label": `${ctx.title} equalizer` }, head, wrap, strip);
    document.body.append(P.el);
    by.addEventListener("click", () => { P.bypass = !P.bypass; by.setAttribute("aria-pressed", String(P.bypass)); push(true); draw(); });
    shut.addEventListener("click", () => close());
    graph.addEventListener("pointerdown", down);
    graph.addEventListener("pointermove", move);
    graph.addEventListener("pointerup", up);
    graph.addEventListener("pointerleave", () => { P.hover = -1; showBand(); });
    graph.addEventListener("dblclick", dbl);
    graph.addEventListener("wheel", wheel, { passive: false });
    graph.addEventListener("contextmenu", context);
    P.el.addEventListener("keydown", keys);
    P.el.addEventListener("mousedown", (e) => e.stopPropagation());  // the board under it stays put
    ctx.send({ op: "eq-watch", key, on: true });
    draw();
    P.el.focus({ preventScroll: true });
  }

  function close(tell = true) {
    if (!P.el) return;
    P.ctx.send({ op: "eq-watch", key: P.key, on: false });
    P.el.remove();
    P.el = null;
    const key = P.key;
    P.key = null;
    if (tell) P.ctx.done?.(key);
  }

  // The board sent new settings (a profile load, another window): take them unless a drag is on.
  // Not right after an edit here: the board's state then still carries what was sent a step ago
  // (changes go out at most ~30 times a second), and taking it pulled a scrolled Q back and forth
  // (owner saw the curve jitter). Another window's or a profile's change lands once edits pause.
  const ECHO_MS = 1000;
  function sync(key, dev, force = false) {
    if (key !== P.key || P.drag || (!force && (P.timer || performance.now() - (P.touched || 0) < ECHO_MS))) return;
    P.bands = structuredClone(dev.bands || []);
    P.bypass = !!dev.bypass;
    P.sel = Math.min(P.sel, P.bands.length - 1);
    P.el.querySelector(".eq-head .bypass").setAttribute("aria-pressed", String(P.bypass));
    draw();
  }

  // Changes go out at most ~30 times a second while dragging; the last one always goes.
  function push(now = false) {
    P.touched = performance.now();
    P.ctx.dev.bands = structuredClone(P.bands);
    P.ctx.dev.bypass = P.bypass;
    const go = () => { P.sent = performance.now(); P.timer = null; P.ctx.send({ op: "eq-set", key: P.key, bands: P.bands, bypass: P.bypass }); P.ctx.changed?.(P.key); };
    clearTimeout(P.timer);
    if (now || performance.now() - P.sent > 33) go(); else P.timer = setTimeout(go, 33);
  }

  function draw() {
    if (!P.el) return;
    const g = P.el.querySelector(".eq-graph"), rate = P.ctx.rate;
    g.textContent = "";
    for (const f of [30, 40, 60, 70, 80, 90, 300, 400, 600, 700, 800, 900, 3000, 4000, 6000, 7000, 8000, 9000])
      g.append(s("line", { class: "eq-grid minor", x1: xOf(f, W), x2: xOf(f, W), y1: 0, y2: HT }));
    for (const f of GRID_F) g.append(s("line", { class: "eq-grid", x1: xOf(f, W), x2: xOf(f, W), y1: 0, y2: HT }));
    for (const db of [-18, -12, -6, 6, 12, 18]) g.append(s("line", { class: "eq-grid", x1: 0, x2: W, y1: yOf(db, HT), y2: yOf(db, HT) }));
    g.append(s("line", { class: "eq-zero", x1: 0, x2: W, y1: HT / 2, y2: HT / 2 }));
    // Each band's own shape, in its colour: shown only while its handle (or its dynamic edge) is
    // under the pointer or held (owner: always shown, it looked messy beside the curve).
    if (!P.bypass) P.bands.forEach((b, i) => {
      if (b.on === false) return;
      const d = curvePath(coefs([b], rate), W, HT, rate, 120, [b]);
      g.append(s("path", { class: `eq-band${i === focused() ? " shown" : ""}`, "data-band": i, d: `${d}L${W},${HT / 2}L0,${HT / 2}Z`, fill: COLORS[i % COLORS.length] }));
    });
    const cs = P.bypass ? [] : coefs(P.bands, rate);
    // A dynamic band's reach: between the curve at rest and the curve with that band fully moved;
    // that edge drawn as a line (as FabFilter does) which drags its Range (owner).
    const edges = [];
    if (!P.bypass) P.bands.forEach((db, i) => {
      if (!isDyn(db)) return;
      const rest = [], moved = [];
      let edge = "", was = false;  // the edge only where the band can move (elsewhere it is the curve)
      for (const x of xsFor(P.bands, W, 200)) {
        const f = fOf(x, W), base = level(cs, f, rate), lift = dynLevel(db, f, rate), pt = `${x.toFixed(1)},${yOf(base + lift, HT).toFixed(1)}`;
        rest.push(`${x.toFixed(1)},${yOf(base, HT).toFixed(1)}`);
        moved.push(pt);
        const shows = Math.abs(lift) > 0.05;
        if (shows) edge += `${was ? "L" : "M"}${pt}`;
        was = shows;
      }
      const color = COLORS[i % COLORS.length];
      g.append(s("path", { class: "eq-dyn", fill: color, d: `M${rest.join("L")}L${[...moved].reverse().join("L")}Z` }));
      edges.push(s("path", { class: "eq-dyn-edge", stroke: color, d: edge }), s("path", { class: "eq-dyn-hit", "data-dyn": i, d: edge }));
    });
    const total = curvePath(cs, W, HT, rate, 240, P.bypass ? [] : P.bands);
    g.append(s("path", { class: "eq-fill", d: `${total}L${W},${HT / 2}L0,${HT / 2}Z` }),
             s("path", { class: `eq-total${P.bypass ? " bypassed" : ""}`, d: total }), ...edges,
             P.liveEl = s("path", { class: "eq-dyn-live", d: "", stroke: "url(#eq-now-fade)" }));
    g.append(nowFade(cs, rate));
    liveCurves();
    P.bands.forEach((b, i) => {
      const x = xOf(b.freq, W), y = handleY(b, HT);
      g.append(s("g", { class: `eq-handle${i === P.sel ? " sel" : ""}${b.on === false ? " off" : ""}`, "data-i": i, transform: `translate(${x},${y})` },
                 isDyn(b) ? s("circle", { class: "eq-dyn-ring", r: (i === P.sel ? 11 : 9) + 4, stroke: COLORS[i % COLORS.length] }) : "",
                 s("circle", { r: i === P.sel ? 11 : 9, fill: COLORS[i % COLORS.length] }),
                 s("text", { y: 3.6 }, String(i + 1))));
    });
    P.el.querySelector(".eq-count").textContent = P.bands.length ? `${P.bands.length} band${P.bands.length > 1 ? "s" : ""}` : "";
    strip();
  }

  // ---- the selected band's numbers ----------------------------------------------------------
  // A band's number (field.js): scroll or ↑/↓ edit it, Enter applies, Esc or clicking away
  // cancels. While one has the focus the strip is not rebuilt under it; it is once it is left.
  function field(label, o) {
    const input = numField({ ...o, cls: "eq-field", aria: label, done: () => strip(), keyed: () => P.el.focus() });
    return h("label", { class: "eq-cell" }, h("span", { text: label }), input);
  }
  const num = (t) => Number(String(t).trim().toLowerCase().replace(/−/g, "-").replace(",", ".").replace(/\s*(db|hz)$/, ""));
  const parseF = (t) => { const m = /^([\d.,]+)\s*(k)?/.exec(String(t).trim().toLowerCase()); return m ? num(m[1]) * (m[2] ? 1000 : 1) : NaN; };

  function strip() {
    const el = P.el.querySelector(".eq-strip");
    if (!el || el.contains(document.activeElement)) return;  // a number being edited stays
    el.textContent = "";
    const b = P.bands[P.sel];
    if (!b) {
      el.append(h("span", { class: "eq-hint", text: P.bands.length
        ? "Click a band to edit it · drag to move · scroll for width · double-click to turn it off or on · right-click for type"
        : "Double-click the curve to add a band" }));
      return;
    }
    const color = COLORS[P.sel % COLORS.length];
    const types = h("div", { class: "segmented eq-types", role: "radiogroup", "aria-label": "Band type" },
      ...TYPES.map(([t, label]) => {
        const btn = h("button", { type: "button", role: "radio", "aria-checked": String(b.type === t), text: label });
        btn.addEventListener("click", () => setType(P.sel, t));
        return btn;
      }));
    const one = (v, f) => { f(v); push(true); draw(); };
    const cells = [field("Frequency", { value: () => b.freq, show: fmtF, parse: parseF, step: STEPS.semitone, lo: F0, hi: F1,
                                        fit: (v) => Math.round(clamp(v, F0, F1)), apply: (v) => one(v, (x) => { b.freq = x; }), tip: "Hz, or 1.2k" })];
    if (hasGain(b)) cells.push(field("Gain", { value: () => b.gain, show: fmtG, parse: parseNum, step: STEPS.add, lo: -GAIN, hi: GAIN,
                                                fit: (v) => Math.round(clamp(v, -GAIN, GAIN) * 10) / 10, apply: (v) => one(v, (x) => { b.gain = x; }), tip: "−18 … +18 dB" }));
    if (isCut(b)) {
      cells.push(h("div", { class: "eq-cell" }, h("span", { text: "Slope" }), h("div", { class: "segmented eq-slope", role: "radiogroup", "aria-label": "Slope" },
        ...[12, 24, 48].map((sl) => {
          const btn = h("button", { type: "button", role: "radio", "aria-checked": String((b.slope || 12) === sl), text: `${sl} dB` });
          btn.addEventListener("click", () => setSlope(P.sel, sl));
          return btn;
        }))));
    } else {
      cells.push(field("Q", { value: () => b.q, show: fmtQ, parse: parseNum, step: STEPS.share, lo: Q0, hi: Q1,
                              fit: (v) => Math.round(clamp(v, Q0, Q1) * 100) / 100, apply: (v) => one(v, (x) => { b.q = x; }), tip: "Width: 0.1 (wide) … 18 (narrow)" }));
    }
    const on = h("button", { type: "button", class: "card-btn", "aria-pressed": String(b.on !== false), text: b.on === false ? "Off" : "On", "data-tip": "Turn this band off without losing it" });
    on.addEventListener("click", () => { b.on = b.on === false; push(true); draw(); });
    const rm = h("button", { type: "button", class: "card-btn eq-remove", text: "Remove", "data-tip": "Delete" });
    rm.addEventListener("click", () => remove(P.sel));
    const dyn = canDyn(b) ? h("button", { type: "button", class: "card-btn eq-dyn-btn", "aria-pressed": String(b.dyn === true), text: "Dynamic",
                                          "data-tip": "Move this band with how loud it is: a negative Range cuts it when loud, a positive one boosts it when quiet" }) : null;
    dyn?.addEventListener("click", () => toggleDyn(P.sel));
    el.append(h("span", { class: "eq-badge", style: `--b:${color}`, text: String(P.sel + 1) }), types, ...cells, h("span", { class: "eq-space" }), dyn, on, rm);
    if (b.dyn === true && canDyn(b)) el.append(dynRow(b));
  }

  // The second row of a dynamic band: where it starts moving and how far, how fast.
  function dynRow(b) {
    const one = (k) => (v) => { b[k] = v; push(true); draw(); };
    const num = (k, label, show, step, tip) => {
      const [lo, hi] = DYN[k];
      return field(label, { value: () => b[k] ?? DYN[k][2], show, parse: parseNum, step, lo, hi,
                            fit: (v) => Math.round(clamp(v, lo, hi) * 10) / 10, apply: one(k), tip });
    };
    const ms = (v) => `${Number(v.toFixed(1))} ms`;
    return h("div", { class: "eq-dyn-row" },
      h("span", { class: "eq-dyn-label", text: "Dynamic" }),
      num("thr", "Threshold", fmtG, STEPS.add, "−60 … 0 dB · the band's own level where it starts to move"),
      levelMeter(b),
      num("range", "Range", fmtG, STEPS.add, "−18 … +18 dB · negative: cut when loud · positive: boost when quiet"),
      num("attack", "Attack", ms, STEPS.share, "0.1 … 200 ms"),
      num("release", "Release", ms, STEPS.share, "10 … 2000 ms"));
  }

  // The band's level, live, beside its threshold (owner: to see where to set it): a bar of its
  // level as its processor reads it (−60 … 0 dB), a tick at its recent peak, and the threshold as
  // a line to drag or scroll; amber while the band is over it (working). Updated by meters().
  const M_LO = -60, pct = (db) => `${(clamp((db - M_LO) / -M_LO, 0, 1) * 100).toFixed(1)}%`;
  function levelMeter(b) {
    const el = h("div", { class: "eq-dyn-meter", role: "slider", "aria-label": "Threshold", "aria-valuemin": "-60", "aria-valuemax": "0",
                          "data-tip": "The band's level now, as it reacts to it · the line is Threshold: drag it or scroll" },
      h("div", { class: "eq-meter-fill" }), h("div", { class: "eq-meter-hold" }), h("div", { class: "eq-meter-thr" }));
    const at = (e) => { const r = el.getBoundingClientRect(); return M_LO * (1 - (e.clientX - r.left) / r.width); };
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      try { el.setPointerCapture(e.pointerId); } catch { /* a synthetic pointer (shots) */ }
      P.meterDrag = true; setThr(b, at(e));
    });
    el.addEventListener("pointermove", (e) => { if (P.meterDrag) setThr(b, at(e)); });
    const up = () => { if (P.meterDrag) { P.meterDrag = false; push(true); draw(); } };
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("wheel", (e) => { e.preventDefault(); setThr(b, b.thr - Math.sign(e.deltaY) * (e.shiftKey ? 0.1 : 0.5)); push(true); }, { passive: false });
    return h("div", { class: "eq-meter-wrap" }, el, h("span", { class: "eq-dyn-now" }));
  }
  // Moves the threshold without rebuilding the strip (that would drop the drag); its box follows.
  function setThr(b, v) {
    b.thr = Math.round(clamp(v, DYN.thr[0], DYN.thr[1]) * 10) / 10;
    push();
    const box = P.el.querySelector('.eq-dyn-row input[aria-label="Threshold"]');
    if (box && document.activeElement !== box) box.value = fmtG(b.thr);
    meters();
  }
  function meters() {
    const el = P.el?.querySelector(".eq-dyn-meter"), b = P.bands[P.sel];
    if (!el || !b) return;
    const pk = P.peak?.[P.sel], live = pk != null && pk > -100, now = performance.now();
    const hold = (P.hold ||= {})[P.sel];
    if (live && (!hold || pk >= hold.v || now - hold.t > 1500)) P.hold[P.sel] = { v: pk, t: now };
    const over = live && (b.range < 0 ? pk >= b.thr : pk <= b.thr);
    const fill = el.querySelector(".eq-meter-fill");
    fill.style.width = live ? pct(pk) : "0%";
    fill.classList.toggle("over", over);
    const h1 = el.querySelector(".eq-meter-hold");
    h1.hidden = !live || !P.hold[P.sel];
    if (P.hold[P.sel]) h1.style.left = pct(P.hold[P.sel].v);
    el.querySelector(".eq-meter-thr").style.left = pct(b.thr);
    el.setAttribute("aria-valuenow", String(b.thr));
    const txt = P.el.querySelector(".eq-dyn-now");
    txt.textContent = live ? `${fmtG(Math.round(pk * 10) / 10)}` : "silent";
    txt.classList.toggle("over", over);
  }
  function toggleDyn(i) {
    const b = P.bands[i];
    if (b.dyn !== true && P.bands.filter((x, j) => j !== i && isDyn(x)).length >= DYN_SLOTS) {
      refuse(`At most ${DYN_SLOTS} bands can be dynamic`); return;
    }
    b.dyn = b.dyn !== true;
    for (const [k, [, , dflt]] of Object.entries(DYN)) if (typeof b[k] !== "number") b[k] = dflt;
    push(true); draw();
  }

  // ---- editing --------------------------------------------------------------------------------
  function fits(bands) { return bands.length <= MAX && slotsUsed(bands) <= SLOTS; }
  function refuse(text) {
    const r = P.el.querySelector(".eq-read");
    r.hidden = false; r.textContent = text; r.classList.add("warn");
    clearTimeout(P.readTimer); P.readTimer = setTimeout(() => { r.hidden = true; r.classList.remove("warn"); }, 1400);
  }
  function setType(i, t) {
    const b = { ...P.bands[i], type: t };
    if (isCut(b)) b.slope = b.slope || 12;
    const next = P.bands.map((x, j) => (j === i ? b : x));
    if (!fits(next)) return refuse(`No room for a steeper cut: ${SLOTS} filter slots at most`);
    P.bands = next; push(true); draw();
  }
  function setSlope(i, sl) {
    const next = P.bands.map((x, j) => (j === i ? { ...x, slope: sl } : x));
    if (!fits(next)) return refuse(`No room for a steeper cut: ${SLOTS} filter slots at most`);
    P.bands = next; push(true); draw();
  }
  function remove(i) {
    if (i < 0) return;
    P.bands.splice(i, 1);
    P.sel = Math.min(i, P.bands.length - 1);
    push(true); draw();
  }
  function add(freq, gain) {
    const b = { type: "bell", freq: Math.round(clamp(freq, F0, F1)), gain: Math.round(clamp(gain, -GAIN, GAIN) * 10) / 10, q: 1, on: true };
    if (!fits([...P.bands, b])) return refuse(`${MAX} bands at most`);
    P.bands.push(b);
    P.sel = P.bands.length - 1;
    push(true); draw();
  }

  const at = (e) => {  // the pointer in graph units
    const r = P.el.querySelector(".eq-graph").getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * W, y: ((e.clientY - r.top) / r.height) * HT, sx: W / r.width, sy: HT / r.height };
  };
  const handleOf = (e) => e.target.closest?.(".eq-handle");
  // The band under the pointer by where it is, not by the event's target: after a press the graph
  // holds the pointer capture, and Chrome then delivers click and dblclick to the graph itself
  // (double-clicking a band added a new one instead of turning it off).
  function bandAt(e) {
    const p = at(e), reach = 14 * p.sx;  // 14 screen px, in graph units
    let best = -1, near = Infinity;
    P.bands.forEach((b, i) => {
      const d = Math.hypot(xOf(b.freq, W) - p.x, (handleY(b, HT) - p.y) * (p.sy / p.sx));
      if (d < near) { near = d; best = i; }
    });
    return near <= reach ? best : -1;
  }
  function readout(b, e) {
    const r = P.el.querySelector(".eq-read");
    r.hidden = false; r.classList.remove("warn");
    r.textContent = [fmtF(b.freq), hasGain(b) ? fmtG(b.gain) : null, isCut(b) ? `${b.slope || 12} dB/oct` : `Q ${fmtQ(b.q)}`].filter(Boolean).join(" · ");
    const box = P.el.querySelector(".eq-plot").getBoundingClientRect();
    r.style.left = `${clamp(e.clientX - box.left + 14, 0, box.width - 170)}px`;
    r.style.top = `${clamp(e.clientY - box.top - 30, 0, box.height - 24)}px`;
  }

  // A band is turned off or on by two presses on it, counted here: pressing redraws the graph,
  // which replaces the circle pressed, and Chrome then sends no click and so no dblclick
  // (owner: double-clicking a band did nothing; found with real input through DevTools).
  const DOUBLE_MS = 450;
  function rangeRead(b, e) {
    readout(b, e);
    const r = P.el.querySelector(".eq-read");
    r.textContent = `Range ${fmtG(b.range)} · ${b.range < 0 ? "cuts when loud" : b.range > 0 ? "boosts when quiet" : "does not move"}`;
  }
  function down(e) {
    const hd = handleOf(e), edge = hd ? null : e.target.closest?.(".eq-dyn-hit");
    if (edge && e.button === 0) {  // a dynamic band's edge: its Range follows the pointer up and down
      const i = Number(edge.dataset.dyn), b = P.bands[i];
      P.sel = i;
      P.drag = { range: true, i, y: at(e).y, r: b.range ?? DYN.range[2] };
      try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* a synthetic pointer (shots) */ }
      draw();
      rangeRead(b, e);
      return;
    }
    if (!hd || e.button !== 0) return;
    const i = Number(hd.dataset.i), b = P.bands[i], p = at(e), now = performance.now();
    const again = P.pressed && P.pressed.i === i && now - P.pressed.t < DOUBLE_MS;
    P.pressed = again ? null : { i, t: now };
    if (again) {  // the second press: on or off, keeping its settings
      b.on = b.on === false;
      P.sel = i;
      push(true); draw();
      return;
    }
    P.sel = i;
    P.drag = { i, x: p.x, y: p.y, freq: b.freq, gain: b.gain };
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* a synthetic pointer (shots) */ }
    draw();
    readout(b, e);
  }
  // The band whose shape shows: the one held, else the one under the pointer (-1: none).
  const focused = () => (P.drag ? P.drag.i : P.hover);
  function showBand() {
    for (const el of P.el?.querySelectorAll(".eq-band") || []) el.classList.toggle("shown", Number(el.dataset.band) === focused());
  }
  function hoverOn(e) {
    const hd = handleOf(e), edge = hd ? null : e?.target.closest?.(".eq-dyn-hit");
    const i = hd ? Number(hd.dataset.i) : edge ? Number(edge.dataset.dyn) : -1;
    if (i !== P.hover) { P.hover = i; showBand(); }
  }
  function move(e) {
    if (!P.drag) {
      e.currentTarget.style.cursor = handleOf(e) ? "grab" : e.target.closest?.(".eq-dyn-hit") ? "ns-resize" : "crosshair";
      hoverOn(e);
      return;
    }
    const p = at(e), b = P.bands[P.drag.i], fine = e.shiftKey ? 0.15 : 1;
    if (P.drag.range) {
      const [lo, hi] = DYN.range;
      b.range = Math.round(clamp(P.drag.r + (dbOf(p.y, HT) - dbOf(P.drag.y, HT)) * fine, lo, hi) * 10) / 10;
      push(); draw(); rangeRead(b, e);
      return;
    }
    const x = xOf(P.drag.freq, W) + (p.x - P.drag.x) * fine;
    b.freq = Math.round(clamp(fOf(x, W), F0, F1));
    if (hasGain(b)) b.gain = Math.round(clamp(P.drag.gain + dbOf(HT / 2 + (p.y - P.drag.y) * fine, HT), -GAIN, GAIN) * 10) / 10;
    push();
    draw();
    readout(b, e);
  }
  function up(e) {
    if (!P.drag) return;
    P.drag = null;
    push(true);
    showBand();
    P.el.querySelector(".eq-read").hidden = true;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
  }
  function dbl(e) {
    if (bandAt(e) >= 0) return;  // on a band: down() counted the two presses
    const p = at(e);
    add(fOf(p.x, W), dbOf(p.y, HT));
  }
  // The wheel, on a band or anywhere over the graph for the selected one: its width (up is wider,
  // a lower Q: owner, the other way felt inverted); on a cut, its slope (up is steeper).
  function wheel(e) {
    const hd = handleOf(e), i = hd ? Number(hd.dataset.i) : P.sel, b = P.bands[i];
    if (!b) return;
    e.preventDefault();
    P.sel = i;
    if (isCut(b)) {
      const steps = [12, 24, 48], at = steps.indexOf(b.slope || 12);
      const next = steps[clamp(at + (e.deltaY < 0 ? 1 : -1), 0, 2)];
      if (next !== (b.slope || 12)) setSlope(i, next);
      readout(P.bands[i], e);
    } else {
      b.q = Math.round(clamp(b.q * (e.deltaY < 0 ? 1 / 1.12 : 1.12), Q0, Q1) * 100) / 100;  // up: wider (owner)
      push(); draw(); readout(b, e);
    }
    clearTimeout(P.readTimer); P.readTimer = setTimeout(() => { P.el.querySelector(".eq-read").hidden = true; }, 900);
  }
  function context(e) {
    e.preventDefault();
    e.stopPropagation();
    const hd = handleOf(e);
    if (!hd) {
      const p = at(e);
      return P.ctx.menu(e.clientX, e.clientY, [{ label: "Add a band here", icon: "add", action: () => add(fOf(p.x, W), dbOf(p.y, HT)) }]);
    }
    const i = Number(hd.dataset.i), b = P.bands[i];
    P.sel = i; draw();
    P.ctx.menu(e.clientX, e.clientY, [
      { head: `Band ${i + 1}` },
      ...TYPES.map(([t, label]) => ({ label, icon: b.type === t ? "check" : "", action: () => setType(i, t) })),
      ...(isCut(b) ? [{ label: "Slope", sub: [12, 24, 48].map((sl) => ({ label: `${sl} dB per octave`, icon: (b.slope || 12) === sl ? "check" : "", action: () => setSlope(i, sl) })) }] : []),
      { sep: true },
      { label: b.on === false ? "Turn on" : "Turn off", action: () => { b.on = b.on === false; push(true); draw(); } },
      { label: "Remove", icon: "trash", hint: "Delete", danger: true, action: () => remove(i) },
    ]);
  }
  function keys(e) {
    if (e.target.closest?.("input")) return;
    if (e.key === "Escape") close();
    else if ((e.key === "Delete" || e.key === "Backspace") && P.sel >= 0) remove(P.sel);
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") { if (P.bands.length) { P.sel = (P.sel + (e.key === "ArrowRight" ? 1 : P.bands.length - 1)) % P.bands.length; draw(); } }
    else return;
    e.preventDefault();
    e.stopPropagation();
  }

  // ---- the spectrum: the EQ's output, analysed here --------------------------------------------
  // The server sends its audio (mono, 48 kHz, raw floats, base64) while the panel is open. Every
  // other animation frame: a 4096-point FFT (Hann), folded into BINS log-spaced bins from 20 Hz to
  // 20 kHz, tilted +4.5 dB/octave around 1 kHz (so pink noise, and most music, reads flat), peaks
  // falling at FALL dB/s. Only the spectrum's path is redrawn per frame.
  const N = 4096, RATE = 48000, BINS = 240, FALL = 36;
  const hann = Float64Array.from({ length: N }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (N - 1)));
  const norm = 2 / hann.reduce((a, b) => a + b, 0);
  const ring = new Float32Array(N), ringIn = new Float32Array(N);  // what comes out; what goes in
  let ringAt = 0, fresh = false, shown = null, lastT = 0, tick = 0;

  function fft(re, im) {  // in place, radix 2
    for (let i = 1, j = 0; i < N; i++) {
      let bit = N >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
    }
    for (let len = 2; len <= N; len <<= 1) {
      const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang);
      for (let i = 0; i < N; i += len) {
        let cr = 1, ci = 0;
        for (let k = 0; k < len / 2; k++) {
          const a = i + k, b = a + len / 2, tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
          re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
          [cr, ci] = [cr * wr - ci * wi, cr * wi + ci * wr];
        }
      }
    }
  }

  function analyse(withMoves = true) {
    const re = new Float64Array(N), im = new Float64Array(N);
    for (let i = 0; i < N; i++) re[i] = ring[(ringAt + i) % N] * hann[i];
    fft(re, im);
    if (withMoves) measure(re, im);
    const hz = RATE / N, db = (k) => 20 * Math.log10(Math.max(Math.hypot(re[k], im[k]) * norm, 1e-9));
    return Array.from({ length: BINS }, (_, i) => {
      const f = 10 ** (lf0 + (i / (BINS - 1)) * lspan), lo = f * 2 ** (-0.5 * lspan * 3.32 / BINS), hi = f * 2 ** (0.5 * lspan * 3.32 / BINS);
      let level;
      if ((hi - lo) / hz < 2) {  // narrower than an FFT bin (the lows): between the two nearest
        const at = Math.min(N / 2 - 2, Math.max(1, f / hz)), k = Math.floor(at);
        level = db(k) + (db(k + 1) - db(k)) * (at - k);
      } else {  // wider (the highs): its loudest bin, so peaks are not averaged away
        level = -Infinity;
        for (let k = Math.max(1, Math.floor(lo / hz)); k <= Math.min(N / 2 - 1, Math.ceil(hi / hz)); k++) level = Math.max(level, db(k));
      }
      return level + 4.5 * Math.log2(f / 1000);
    });
  }

  // ---- how far each dynamic band has moved, now (M16) ------------------------------------------
  // LSP's own meter cannot be read, so it is measured: in the band's region (each FFT bin weighed
  // by the region filter's power, squared: its centre counts most), what came out against what the
  // static EQ alone makes of what went in. And as the processor itself: a cut can only be working
  // while the band is near or over its threshold, a boost while it is under: otherwise at rest
  // (else a trace of another frequency in the region, moved by something else, counted).
  // ponytail: two dynamic bands moving the same region both show the sum.
  // A band's level as its processor sees it: the input through the static EQ and the band's
  // region filter, its peak over the last half of the window (the first half lets the filters
  // settle). Averages from the FFT read ~9 dB low on music (peaks over average), and the owner's
  // band showed nothing because it never reached its threshold, with no way to see that.
  // ponytail: the input is both channels summed, halved: right for a centred sound, ~3 dB high
  // for unrelated left and right.
  function biquadRun(x, [b0, b1, b2, a0, a1, a2]) {
    const y = new Float64Array(x.length);
    let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
    for (let i = 0; i < x.length; i++) {
      const o = (b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0;
      x2 = x1; x1 = x[i]; y2 = y1; y1 = o; y[i] = o;
    }
    return y;
  }
  function bandPeaks(dyn) {
    let x = Float64Array.from({ length: N }, (_, i) => ringIn[(ringAt + i) % N] / 2);
    for (const c of coefs(P.bands, RATE)) x = biquadRun(x, c);
    const out = {};
    for (const [b, i] of dyn) {
      const y = biquadRun(x, dynFilter(b, RATE));
      let pk = 0;
      for (let k = N / 2; k < N; k++) pk = Math.max(pk, Math.abs(y[k]));
      out[i] = 20 * Math.log10(pk + 1e-12);
    }
    return out;
  }
  function measure(re, im) {
    const dyn = P.bypass ? [] : P.bands.map((b, i) => [b, i]).filter(([b]) => isDyn(b));
    P.live ||= {};
    if (!dyn.length) return;
    P.peak = bandPeaks(dyn);
    const ri = new Float64Array(N), ii = new Float64Array(N);
    for (let i = 0; i < N; i++) ri[i] = ringIn[(ringAt + i) % N] * hann[i];
    fft(ri, ii);
    const rate = P.ctx.rate, cs = coefs(P.bands, rate), hz = RATE / N;
    for (const [b, i] of dyn) {
      let eIn = 0, eOut = 0;
      for (let k = 1; k < N / 2; k++) {
        const f = k * hz, [hr, hi] = region(b, f, rate), w = (hr * hr + hi * hi) ** 2;
        if (w < 1e-6) continue;
        eIn += (ri[k] * ri[k] + ii[k] * ii[k]) * 10 ** (level(cs, f, rate) / 10) * w;
        eOut += (re[k] * re[k] + im[k] * im[k]) * w;
      }
      const loud = P.peak[i], lo = Math.min(0, b.range), hi = Math.max(0, b.range);
      const idle = loud < -100 || (b.range < 0 ? loud < b.thr - 6 : loud > b.thr + 6);
      const now = idle ? 0 : clamp(10 * Math.log10((eOut + 1e-30) / eIn), lo, hi);
      P.live[i] = (P.live[i] ?? 0) + (now - (P.live[i] ?? 0)) * 0.5;
    }
  }
  // The "now" curve (owner): one continuous line, the EQ curve with every dynamic band's live
  // movement applied, white over each band's core and fading into the pink curve away from it
  // (owner: cut-off ends looked broken; a whole white curve hid the EQ's colour). At rest it lies
  // on the curve; working it moves inside the range.
  function nowFade(cs, rate) {
    const grad = s("linearGradient", { id: "eq-now-fade", gradientUnits: "userSpaceOnUse", x1: 0, y1: 0, x2: W, y2: 0 });
    const dyn = P.bands.filter(isDyn);
    for (let k = 0; k <= 120; k++) {
      const x = (k / 120) * W, f = fOf(x, W);
      const w = Math.max(0, ...dyn.map((b) => Math.abs(dynLevel(b, f, rate)) / Math.max(0.5, Math.abs(b.range))));
      const o = Math.min(1, Math.max(0, (w - 0.15) / 0.45));  // 0 on the far skirts, 1 over the core
      grad.append(s("stop", { offset: (k / 120).toFixed(4), "stop-color": "#fff", "stop-opacity": (o * o * (3 - 2 * o)).toFixed(3) }));
    }
    const defs = s("defs");
    defs.append(grad);
    return defs;
  }
  function liveCurves() {
    const el = P.liveEl;
    if (!P.el || !el) return;
    meters();
    const rate = P.ctx.rate, dyn = P.bypass ? [] : P.bands.map((b, i) => [b, P.live?.[i] ?? 0]).filter(([b]) => isDyn(b));
    if (!dyn.length) { if (el.getAttribute("d")) el.setAttribute("d", ""); return; }  // (unchanged: no repaint)
    const cs = coefs(P.bands, rate);
    el.setAttribute("d", xsFor(P.bands, W, 240).map((x, k) => {
      const f = fOf(x, W), lift = dyn.reduce((sum, [b, by]) => sum + (by ? dynLevel(b, f, rate, by) : 0), 0);
      return `${k ? "L" : "M"}${x.toFixed(1)},${yOf(level(cs, f, rate) + lift, HT).toFixed(1)}`;
    }).join(""));
  }

  function pcm(key, b64) {
    if (key !== P.key || !P.el) return;
    const raw = atob(b64), bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const v = new Float32Array(bytes.buffer, 0, bytes.length >> 2);  // pairs: in, out
    for (let i = 0; i + 1 < v.length; i += 2) { ringIn[ringAt] = v[i]; ring[ringAt] = v[i + 1]; ringAt = (ringAt + 1) % N; }
    if (!fresh) { fresh = true; requestAnimationFrame(frame); }
  }

  function frame(t) {
    if (!P.el) { fresh = false; shown = null; return; }
    // The spectrum every frame (every other one stepped visibly); the dynamic bands' movement,
    // heavier (filters over the whole buffer), every other.
    const moves = tick++ % 2 === 0, now = analyse(moves), dt = lastT ? Math.min(0.2, (t - lastT) / 1000) : 0;
    shown = shown ? now.map((v, i) => Math.max(v, shown[i] - FALL * dt)) : now;
    lastT = t;
    spectrum(P.key, shown);
    if (moves) liveCurves();
    requestAnimationFrame(frame);
  }

  // −90 dB at the bottom, 0 dB at the top. Drawn smooth (user: "a bit square-y"): each point
  // lightly blended with its neighbours, and a curve through the midpoints instead of corners.
  function spectrumD(bins) {
    const n = bins.length, y = (v) => HT - (clamp(v, -90, 0) + 90) / 90 * HT;
    const soft = bins.map((v, i) => 0.25 * bins[Math.max(0, i - 1)] + 0.5 * v + 0.25 * bins[Math.min(n - 1, i + 1)]);
    const pts = soft.map((v, i) => [(i / (n - 1)) * W, y(v)]);
    let d = `M0,${HT}L${pts[0][0]},${pts[0][1].toFixed(1)}`;
    for (let i = 1; i < n - 1; i++) {
      const [x, yy] = pts[i], [nx, ny] = pts[i + 1];
      d += `Q${x.toFixed(1)},${yy.toFixed(1)} ${((x + nx) / 2).toFixed(1)},${((yy + ny) / 2).toFixed(1)}`;
    }
    return `${d}L${W},${pts[n - 1][1].toFixed(1)}L${W},${HT}Z`;
  }
  function spectrum(key, bins) {
    if (key !== P.key || !P.el) return;
    P.spectrum = bins;
    P.specEl?.setAttribute("d", spectrumD(bins));
  }

  // For the shots (scripts/shots.py): a believable spectrum (a guitar-ish tilt with harmonics,
  // through the EQ's own curve), a selection, a drag in progress, a band's menu.
  const debug = {
    // The pointer resting on band i's handle (its shape shows); the audit checks just that one does.
    hover: (i) => {
      const hd = P.el.querySelector(`.eq-handle[data-i="${i}"]`), r = hd.getBoundingClientRect();
      hd.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 }));
      const shown = [...P.el.querySelectorAll(".eq-band.shown")].map((el) => Number(el.dataset.band));
      if (shown.length !== 1 || shown[0] !== i) console.error(`hovering band ${i + 1}, shapes shown: ${shown.map((n) => n + 1).join(", ") || "none"}`);
    },
    // Made-up sound through the real analysis (as the live spectrum does it): a low note with
    // its harmonics and some hiss, shaped by the EQ's own curve.
    spectrum: () => {
      const cs = P.bypass ? [] : coefs(P.bands, P.ctx.rate);
      let seed = 1;
      const noise = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5);
      const parts = [82.4, 164.8, 247.2, 329.6, 412, 494.4, 659.2, 988.8, 1318, 1977].map((f, k) => [f, 0.25 / (1 + k * 0.7)]);
      for (let i = 0; i < N; i++) {
        const t = i / RATE;
        ring[i] = parts.reduce((sum, [f, a]) => sum + a * 10 ** (level(cs, f, P.ctx.rate) / 20) * Math.sin(2 * Math.PI * f * t), 0) + 0.01 * noise();
      }
      ringAt = 0;
      spectrum(P.key, analyse());
    },
    select: (i) => { P.sel = i; draw(); },
    drag: (i, dx, dy) => {
      const hd = P.el.querySelector(`.eq-handle[data-i="${i}"]`), r = hd.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2, g = P.el.querySelector(".eq-graph");
      hd.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: x, clientY: y, button: 0, pointerId: 7 }));
      g.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: x + dx, clientY: y + dy, pointerId: 7 }));
    },
    wheel: (i, ticks) => {  // ticks > 0: scrolled up
      const hd = P.el.querySelector(`.eq-handle[data-i="${i}"]`), r = hd.getBoundingClientRect();
      for (let k = 0; k < Math.abs(ticks); k++) {
        P.el.querySelector(`.eq-handle[data-i="${i}"]`).dispatchEvent(new WheelEvent("wheel", {
          bubbles: true, cancelable: true, deltaY: ticks > 0 ? -100 : 100, clientX: r.left + 8, clientY: r.top + 8 }));
      }
    },
    // Two presses on band i, as a person double-clicks: down and up twice on its circle (the
    // second circle is the redrawn one), no dblclick event (Chrome sends none here).
    dblclick: (i) => {
      for (let k = 0; k < 2; k++) {
        const hd = P.el.querySelector(`.eq-handle[data-i="${i}"]`), r = hd.getBoundingClientRect();
        const at = { bubbles: true, clientX: r.left + r.width / 2 + 3, clientY: r.top + r.height / 2 - 2, button: 0, pointerId: 7 };
        hd.dispatchEvent(new PointerEvent("pointerdown", at));
        P.el.querySelector(".eq-graph").dispatchEvent(new PointerEvent("pointerup", at));
      }
    },
    // Drags dynamic band i's edge (its Range) by dy screen px at the band's frequency, held.
    rangeDrag: (i, dy) => {
      const g = P.el.querySelector(".eq-graph"), r = g.getBoundingClientRect(), hit = P.el.querySelector(`.eq-dyn-hit[data-dyn="${i}"]`);
      const x = r.left + (xOf(P.bands[i].freq, W) / W) * r.width, len = hit.getTotalLength();
      let pt = hit.getPointAtLength(0);  // the edge's point at the band's frequency
      for (let k = 0; k <= 400; k++) { const q = hit.getPointAtLength((k / 400) * len); if (Math.abs(q.x - xOf(P.bands[i].freq, W)) < Math.abs(pt.x - xOf(P.bands[i].freq, W))) pt = q; }
      const y = r.top + (pt.y / HT) * r.height, at = { bubbles: true, clientX: x, clientY: y, button: 0, pointerId: 8 };
      hit.dispatchEvent(new PointerEvent("pointerdown", at));
      g.dispatchEvent(new PointerEvent("pointermove", { ...at, clientY: y + dy }));
    },
    sharp: (i, q) => { P.bands[i].q = q; push(true); draw(); },
    live: () => ({ ...(P.live || {}) }),
    // Known sound through the real analysis: a sine at f of amplitude a going in, coming out at
    // the static EQ's level there moved by `moved` dB; returns each dynamic band's measured move.
    peak: () => ({ ...(P.peak || {}) }),
    feed: (f, a, moved) => {
      const g = 10 ** ((level(coefs(P.bands, P.ctx.rate), f, P.ctx.rate) + moved) / 20);
      for (let i = 0; i < N; i++) { const v = a * Math.sin(2 * Math.PI * f * i / RATE); ringIn[i] = v; ring[i] = v * g; }
      ringAt = 0;
      for (let k = 0; k < 12; k++) analyse();  // settles the smoothing
      liveCurves();
      return { ...P.live };
    },
    // Band i made dynamic with this Range (and threshold), as the strip's toggle does.
    dyn: (i, range, thr = -30) => { const b = P.bands[i]; b.dyn = true; b.range = range; b.thr = thr; b.attack = 10; b.release = 150; push(true); draw(); },
    band: (i) => P.bands[i],
    count: () => P.bands.length,
    menu: (i) => {
      const hd = P.el.querySelector(`.eq-handle[data-i="${i}"]`), r = hd.getBoundingClientRect();
      hd.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: r.left + 8, clientY: r.top + 8 }));
    },
  };

  // Run by the page's audit in every shot: the curve math against the values pipeboard/eq.py is
  // checked against (tests/check.py), and the FFT against a known tone.
  function selftest() {
    const faults = [], r = 192000;
    const near = (got, want, tol, what) => { if (!(Math.abs(got - want) <= tol)) faults.push(`EQ ${what}: ${got.toFixed(2)}, not ${want}`); };
    near(level(coefs([{ type: "bell", freq: 1000, gain: 6, q: 1 }], r), 1000, r), 6, 0.05, "bell at its centre");
    near(level(coefs([{ type: "low-cut", freq: 100, slope: 24 }], r), 100, r), -3.01, 0.05, "low cut at its corner");
    near(level(coefs([{ type: "low-cut", freq: 100, slope: 48 }], r), 50, r), -48.2, 0.5, "48 dB cut an octave down");
    near(level(coefs([{ type: "high-shelf", freq: 4000, gain: -6, q: 0.71 }], r), 18000, r), -6, 0.1, "high shelf");
    const kept = ring.slice(), at0 = ringAt;
    for (let i = 0; i < N; i++) ring[i] = 0.5 * Math.sin((2 * Math.PI * 1000 * i) / RATE);
    ringAt = 0;
    const bins = analyse(), top = bins.indexOf(Math.max(...bins));
    ring.set(kept); ringAt = at0;
    near(10 ** (lf0 + (top / (BINS - 1)) * lspan), 1000, 40, "spectrum peak (Hz)");
    near(bins[top], -6.02, 0.6, "spectrum peak (dB)");
    return faults;
  }

  return { open, close, sync, pcm, mini, selftest, isOpen: (key) => P.key === key,
           math: { biquad, coefs, level, slotsUsed }, COLORS, debug };
})();
