"use strict";
// The limiter's, compressor's, noise gate's and noise suppression's live panel, docked at the bottom like the EQ's (and in its
// look): the last seconds of sound, what goes in (faint; amber where it is louder than what comes
// out: what was turned down) and what comes out (the effect's colour), the ceiling or threshold
// as a line to drag, and how much it is turning down now. LSP's own meters cannot be read from
// outside, so the server measures in and out itself: peaks per 10 ms block ({type: "dyn-peaks"}),
// only while a panel shows. The strip below has every setting of the kind (KINDS).
// board.js gives it: DYN.open(key, {dev, title, send, changed, done}).
// Messages out: {op: "dyn-set", key, <setting>?, bypass?, smooth?} · {op: "eq-watch", key, on}

const DYN = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const TOP = 12, BOT = -60, KEEP = 600, NOW = 30;  // dB; blocks (6 s, 0.3 s)
  const W = 1000, HT = 240, STEP = W / KEEP, SPARE = 120, SPAN = KEEP + SPARE;  // a block's width; blocks kept past the left edge
  const REBUILD_MS = 100;  // the shapes are rebuilt this often at most; between, their layer only slides
  // hist: the newest blocks, total: how many ever came; c: the block shown at the right edge, a
  // clock moving every frame (tick), kept a little behind the newest (burst: how many tend to
  // come at once) so that bursts never show as jumps; ref: the block at x 0 of the drawn shapes.
  const P = { key: null, el: null, ctx: null, k: null, line: 0, bypass: false, smooth: false, hist: [], drag: false, sent: 0, timer: null,
              total: 0, c: 0, ref: 0, burst: 2, raf: 0, tLast: 0, drawn: 0, drawnAt: 0 };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const SMOOTH_TIP = "Soft: a slower leveller before the ceiling, far less distortion when it turns down a lot (it breathes instead)";
  const fmt = (d) => (d > 0 ? `+${d.toFixed(1)}` : d < 0 ? `−${Math.abs(d).toFixed(1)}` : "0.0");
  const plain = (v) => String(Number(v.toFixed(1)));
  // What each kind shows: its line (the setting dragged on the graph) and its strip's settings,
  // with ranges as in pipeboard/dynamics.py.
  const KINDS = {
    limiter: {
      word: "limiter", line: { prop: "ceiling_db", label: "Ceiling", lo: -24, hi: 0, dflt: -0.5 }, smooth: true, makeup: null, quiet: "Not limiting",
      hint: "The loudest the sound may be. Drag the line or scroll to move it · amber is what gets turned down", fields: [],
    },
    compressor: {
      word: "compressor", line: { prop: "threshold_db", label: "Threshold", lo: -60, hi: 0, dflt: -18 }, smooth: false, makeup: "makeup_db", quiet: "Not compressing",
      hint: "Above the line: Ratio dB in, 1 dB out · amber is turned down",
      fields: [
        { prop: "ratio", label: "Ratio", lo: 1, hi: 20, dflt: 4, show: (v) => `${plain(v)}:1`, tip: "1 (off) … 20 · how hard it squashes" },
        { prop: "attack_ms", step: "share", label: "Attack", lo: 0.1, hi: 200, dflt: 10, show: (v) => `${plain(v)} ms`, tip: "0.1 … 200 ms · how fast it turns down" },
        { prop: "release_ms", step: "share", label: "Release", lo: 10, hi: 2000, dflt: 150, show: (v) => `${plain(v)} ms`, tip: "10 … 2000 ms · how fast it lets go" },
        { prop: "makeup_db", label: "Makeup", lo: 0, hi: 24, dflt: 0, show: (v) => `${fmt(v)} dB`, tip: "0 … +24 dB · turns it back up after" },
      ],
    },
    // Its threshold is for the "s" band only (pipeboard/dynamics.py), not the whole sound the
    // graph shows: so it is a box in the strip and on the card, never a line on the graph.
    deesser: {
      word: "de-esser", line: { prop: "threshold_db", label: "Threshold", lo: -60, hi: 0, dflt: -30 }, graphLine: false,
      smooth: false, makeup: null, quiet: "Not de-essing",
      hint: "When the band above Frequency gets louder than Threshold, only that band is turned down · amber is what it removed",
      fields: [
        { prop: "freq_hz", label: "Frequency", lo: 2000, hi: 12000, dflt: 5000, step: "semitone", show: (v) => `${(v / 1000).toFixed(1)} kHz`,
          parse: (t) => { const v = parseNum(t); return /k/i.test(t) || v < 100 ? v * 1000 : v; }, tip: "2 … 12 kHz · where the \"s\" band starts" },
      ],
    },
    // Our own Speex plugin (pipeboard/denoise.py). Strength is how far noise is turned down, not a
    // level on the graph: a box in the strip and on the card, like the de-esser's threshold.
    denoise: {
      word: "noise suppression", line: { prop: "strength_db", label: "Strength", lo: -40, hi: 0, dflt: -12 }, graphLine: false,
      smooth: false, makeup: null, quiet: "Passing through",
      hint: "Steady noise (hiss, fans, hum) is turned down by up to Strength · amber is what it removed", fields: [],
    },
    gate: {
      word: "noise gate", line: { prop: "threshold_db", label: "Threshold", lo: -60, hi: 0, dflt: -40 }, smooth: false, makeup: null, quiet: "Open",
      hint: "Under the line it fades down by Range (closing from 4 dB under it) · amber is turned down",
      fields: [
        { prop: "range_db", label: "Range", lo: -70, hi: 0, dflt: -40, show: (v) => `${fmt(v)} dB`, tip: "−70 … 0 dB · how far it turns down when closed" },
        { prop: "attack_ms", step: "share", label: "Attack", lo: 0.1, hi: 200, dflt: 5, show: (v) => `${plain(v)} ms`, tip: "0.1 … 200 ms · how fast it opens" },
        { prop: "hold_ms", step: "share", label: "Hold", lo: 0, hi: 1000, dflt: 100, show: (v) => `${plain(v)} ms`, tip: "0 … 1000 ms · how long it stays open after" },
        { prop: "release_ms", step: "share", label: "Release", lo: 10, hi: 2000, dflt: 150, show: (v) => `${plain(v)} ms`, tip: "10 … 2000 ms · how fast it closes" },
      ],
    },
  };
  const db = (v) => 20 * Math.log10(Math.max(v, 1e-6));
  const yOf = (d) => ((TOP - clamp(d, BOT, TOP)) / (TOP - BOT)) * HT;
  const dbOf = (y) => TOP - (y / HT) * (TOP - BOT);
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
  const value = (dev, f) => (typeof dev[f.prop] === "number" ? dev[f.prop] : f.dflt);

  function open(key, ctx) {
    close(false);
    const k = KINDS[ctx.dev.type];
    Object.assign(P, { key, ctx, k, line: k.line ? value(ctx.dev, k.line) : 0, bypass: !!ctx.dev.bypass, smooth: !!ctx.dev.smooth, hist: [], drag: false,
                       total: 0, c: 0, ref: 0, burst: 2, tLast: 0, drawn: 0, drawnAt: 0 });
    const [sound, graph] = frame();
    const by = k.bypass === false ? null : h("button", { type: "button", class: "card-btn bypass", text: "Bypass", "aria-pressed": String(P.bypass),
                                                         "data-tip": "Let the sound through untouched" });
    const sm = k.smooth ? h("button", { type: "button", class: "card-btn smooth", text: "Soft", "aria-pressed": String(P.smooth),
                                        "data-tip": SMOOTH_TIP }) : null;
    const shut = h("button", { type: "button", class: "eq-close", "aria-label": `Close the ${k.word}`, text: "✕" });
    const head = h("header", { class: "eq-head" }, h("span", { class: "pin" }), h("span", { class: "eq-title", text: ctx.title }),
                   h("span", { class: "eq-count dyn-now" }), h("span", { class: "eq-space" }), sm, by, shut);
    const axis = h("div", { class: "eq-axis-g" }, ...[12, 0, -12, -24, -36, -48].map((g) => h("span", {
      class: g === 0 ? "zero" : null, style: `top:${(yOf(g) / HT) * 100}%`, text: g > 0 ? `+${g}` : String(g).replace("-", "−") })));
    const wrap = h("div", { class: "eq-plot dyn-plot" }, h("div", { class: "dyn-window" }, sound), graph, axis, h("span", { class: "dyn-tag" }), h("div", { class: "eq-axis-f dyn-axis-t" },
      h("span", { text: "6 s ago" }), h("span", { style: "left:100%", text: "now" })));
    P.el = h("section", { class: "eq-panel dyn-panel", tabindex: "-1", role: "dialog", "aria-label": `${ctx.title} ${k.word}` },
             head, wrap, h("div", { class: "eq-strip" }));
    document.body.append(P.el);
    by?.addEventListener("click", () => setFlag("bypass", !P.bypass));
    sm?.addEventListener("click", () => setFlag("smooth", !P.smooth));
    shut.addEventListener("click", () => close());
    const onGraph = k.line && k.graphLine !== false;
    if (onGraph) graph.addEventListener("pointerdown", (e) => { P.drag = true; try { graph.setPointerCapture(e.pointerId); } catch { /* synthetic (shots) */ } aim(e); });
    graph.addEventListener("pointermove", (e) => { if (P.drag) aim(e); });
    graph.addEventListener("pointerup", () => { P.drag = false; push(true); draw(); });
    if (onGraph) graph.addEventListener("wheel", (e) => { e.preventDefault(); setLine(P.line - Math.sign(e.deltaY) * (e.shiftKey ? 0.1 : 0.5)); }, { passive: false });
    P.el.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    P.el.addEventListener("mousedown", (e) => e.stopPropagation());  // the board under it stays put
    ctx.send({ op: "eq-watch", key, on: true });
    draw();
    strip();
    P.el.focus({ preventScroll: true });
    P.raf = requestAnimationFrame(tick);
  }

  function close(tell = true) {
    if (!P.el) return;
    clearTimeout(P.timer);
    cancelAnimationFrame(P.raf);
    P.ctx.send({ op: "eq-watch", key: P.key, on: false });
    P.el.remove();
    P.el = null;
    const key = P.key;
    P.key = null;
    if (tell) P.ctx.done?.(key);
  }

  // The board sent new settings (a profile load, the card's box): take them unless dragging.
  // Not right after an edit here: the state then still carries what was sent a step ago (as the EQ).
  function sync(key, dev, force = false) {
    if (key !== P.key || P.drag || (!force && (P.timer || performance.now() - (P.touched || 0) < 1000))) return;
    P.ctx.dev = dev;
    P.line = P.k.line ? value(dev, P.k.line) : 0;
    P.bypass = !!dev.bypass;
    P.smooth = !!dev.smooth;
    P.el.querySelector(".eq-head .bypass")?.setAttribute("aria-pressed", String(P.bypass));
    P.el.querySelector(".eq-head .smooth")?.setAttribute("aria-pressed", String(P.smooth));
    draw();
    strip();
  }

  function aim(e) {
    const r = P.el.querySelector(".dyn-graph").getBoundingClientRect();
    setLine(dbOf(((e.clientY - r.top) / r.height) * HT), false);
  }
  function setLine(d, now = true) {
    P.line = Math.round(clamp(d, P.k.line.lo, P.k.line.hi) * 10) / 10;
    push(now);
    draw();
    strip();
  }
  function setFlag(flag, on) {
    P[flag] = on;
    P.el.querySelector(`.eq-head .${flag}`).setAttribute("aria-pressed", String(on));
    P.ctx.dev[flag] = on;
    P.ctx.send({ op: "dyn-set", key: P.key, [flag]: on });
    P.ctx.changed?.(P.key);
    draw();
  }
  function setField(f, v) {
    P.touched = performance.now();
    const want = Math.round(clamp(v, f.lo, f.hi) * 10) / 10;
    P.ctx.dev[f.prop] = want;
    P.ctx.send({ op: "dyn-set", key: P.key, [f.prop]: want });
    P.ctx.changed?.(P.key);
    draw();
    strip();
  }
  // The line's changes go out at most ~30 times a second while dragging; the last one always goes.
  function push(now) {
    P.touched = performance.now();
    const prop = P.k.line.prop;
    if (P.ctx.dev[prop] === P.line && now !== true) return;
    P.ctx.dev[prop] = P.line;
    const go = () => { P.sent = performance.now(); P.timer = null; P.ctx.send({ op: "dyn-set", key: P.key, [prop]: P.line }); P.ctx.changed?.(P.key); };
    clearTimeout(P.timer);
    if (now || performance.now() - P.sent > 33) go(); else P.timer = setTimeout(go, 33);
  }

  // How much it turned down in the last 0.3 s: the loudest in against the loudest out, less the
  // makeup gain added after (a block's in and out are a few ms apart, so a window, not a block).
  function reduction() {
    // A block's in and out are the same sound (the server aligns them: dynamics.Aligned); out's
    // window a block wider all the same, so a peak at the edge is never in one and not the other
    // (owner: a phantom −0.8 dB on fading music, when in and out were a block apart).
    const last = P.hist.slice(-NOW - 1), was = P.hist.slice(-NOW, -1);
    if (!was.length || P.bypass) return 0;
    const inn = Math.max(...was.map((p) => p[0])), out = Math.max(...last.map((p) => p[1]));
    const makeup = P.k.makeup ? value(P.ctx.dev, P.k.fields.find((f) => f.prop === P.k.makeup)) : 0;
    return inn < 1e-4 ? 0 : Math.max(0, db(inn) - db(out) + makeup);
  }

  // The graph, made once, in two layers (owner: it stuttered while live wires ran: redrawn whole
  // every frame). Behind, the sound's shapes: their own SVG, SPAN blocks wide, slid by the GPU
  // (tick), rebuilt at most every REBUILD_MS. In front, still: a fade at the left where the
  // history runs out, the grid, the line; it takes the pointer. Gradients: brighter the louder.
  function frame() {
    const grad = (id, stops, attrs = { x1: 0, y1: 0, x2: 0, y2: HT }) => s("linearGradient", { id, gradientUnits: "userSpaceOnUse", ...attrs },
      ...stops.map(([at, cls]) => s("stop", { offset: at, class: cls })));
    const sound = s("svg", { class: "dyn-sound", viewBox: `0 0 ${SPAN * STEP} ${HT}`, preserveAspectRatio: "none", style: `width:${(SPAN / KEEP) * 100}%` },
      s("defs", {}, grad("dyn-g-out", [[0, "g-out top"], [1, "g-out low"]]), grad("dyn-g-cut", [[0, "g-cut top"], [1, "g-cut low"]])),
      s("path", { class: "dyn-cut" }), s("path", { class: "dyn-mask" }), s("path", { class: "dyn-out" }),
      s("path", { class: "dyn-glow" }), s("path", { class: "dyn-top" }), s("path", { class: "dyn-in" }));
    const grid = [6, -6, -12, -18, -24, -30, -36, -42, -48, -54].map((d) => s("line", { class: "eq-grid", x1: 0, x2: W, y1: yOf(d), y2: yOf(d) }));
    const graph = s("svg", { class: "eq-graph dyn-graph", viewBox: `0 0 ${W} ${HT}`, preserveAspectRatio: "none" },
      s("defs", {}, grad("dyn-g-fade", [[0, "g-fade full"], [1, "g-fade none"]], { x1: 0, y1: 0, x2: 70, y2: 0 })),
      s("rect", { class: "dyn-fade", x: 0, y: 0, width: 70, height: HT }), ...grid,
      s("line", { class: "eq-zero", x1: 0, x2: W, y1: yOf(0), y2: yOf(0) }), s("line", { class: "dyn-line", x1: 0, x2: W }));
    return [sound, graph];
  }

  // A level history as a smooth curve (Catmull-Rom as Béziers), in dB lightly evened out over 3
  // blocks (a block's peak flickers a dB or two on music), x from the block at ref. In and out
  // arrive in step: the server delays in by the effect's own delay (owner: the peaks looked
  // misaligned; dynamics.Aligned).
  function curve(i) {
    const n = P.hist.length, x0 = (P.total - n - P.ref) * STEP;
    const y = P.hist.map((p) => db(p[i]));
    const pt = (k) => { const a = y[Math.max(0, k - 1)], b = y[k], c = y[Math.min(n - 1, k + 1)];
                        return [x0 + k * STEP, clamp(yOf((a + 2 * b + c) / 4), 0, HT)]; };
    const ps = y.map((_, k) => pt(k)), f = (v) => v.toFixed(1);
    let d = `M${f(ps[0][0])},${f(ps[0][1])}`;
    for (let k = 0; k < n - 1; k++) {
      const [p0, p1, p2, p3] = [ps[Math.max(0, k - 1)], ps[k], ps[k + 1], ps[Math.min(n - 1, k + 2)]];
      const c1 = clamp(p1[1] + (p2[1] - p0[1]) / 6, 0, HT), c2 = clamp(p2[1] - (p3[1] - p1[1]) / 6, 0, HT);
      d += `C${f(p1[0] + STEP / 3)},${f(c1)} ${f(p2[0] - STEP / 3)},${f(c2)} ${f(p2[0])},${f(p2[1])}`;
    }
    return { line: d, area: `${d}L${f(ps[n - 1][0])},${HT}L${f(ps[0][0])},${HT}Z` };
  }

  function draw() {
    if (!P.el) return;
    const g = P.el.querySelector(".dyn-plot"), yc = yOf(P.line), part = (cls) => g.querySelector(`.${cls}`);
    // In, amber; out drawn over it on the panel's own background: the amber left showing is
    // where in was louder than out, what was turned down. The grid goes on top, faint.
    if (P.hist.length > 1) {
      P.ref = P.total - P.hist.length;
      const inn = curve(0), out = curve(1);
      part("dyn-cut").setAttribute("d", inn.area);
      part("dyn-mask").setAttribute("d", out.area);
      part("dyn-out").setAttribute("d", out.area);
      for (const cls of ["dyn-glow", "dyn-top"]) part(cls).setAttribute("d", out.line);
      part("dyn-in").setAttribute("d", inn.line);
      P.drawn = P.total;
      P.drawnAt = performance.now();
      place();
    }
    g.classList.toggle("bypassed", P.bypass);
    const tag = P.el.querySelector(".dyn-tag"), line = part("dyn-line");
    tag.hidden = !P.k.line || P.k.graphLine === false;
    line.style.display = tag.hidden ? "none" : "";  // (an SVG element ignores `hidden`: the line showed along the top)
    line.classList.toggle("held", P.drag);
    if (!tag.hidden) {
      line.setAttribute("y1", yc);
      line.setAttribute("y2", yc);
      tag.textContent = `${P.k.line.label} ${fmt(P.line)} dB`;
      tag.style.top = `${(yc / HT) * 100}%`;
    }
    const r = reduction(), now = P.el.querySelector(".dyn-now");
    now.textContent = P.bypass ? "Bypassed" : r >= 0.1 ? `Turning down ${fmt(-r)} dB` : P.hist.length ? P.k.quiet : "";
    now.classList.toggle("active", r >= 0.1 && !P.bypass);
  }

  // Where the shapes sit: block c at the right edge. A transform of their own layer, in % of its
  // width (SPAN blocks): no layout read, no repaint.
  function place() {
    const el = P.el?.querySelector(".dyn-sound");
    if (el) el.style.transform = `translate3d(${(-(P.c - KEEP - P.ref) / SPAN * 100).toFixed(4)}%, 0, 0)`;
  }
  // Every frame: the clock moves on by the time passed (a block is 10 ms), a little faster or
  // slower to stay behind the newest by the blocks that come at once and a
  // rebuild's worth; never past what is drawn (the sound stopped: it waits).
  function tick(t) {
    if (!P.el) return;
    const dt = Math.min(100, t - (P.tLast || t));
    P.tLast = t;
    if (P.total > P.drawn && performance.now() - P.drawnAt >= REBUILD_MS) draw();
    if (P.drawn) {
      const back = P.burst + 1 + REBUILD_MS / 10, behind = P.total - back - P.c;
      if (Math.abs(behind) > 60) P.c = P.total - back;  // far off (the tab slept): catch up at once
      else P.c = Math.min(P.drawn, P.c + (dt / 10) * (1 + clamp(behind / 120, -0.2, 0.2)));
      place();
    }
    P.raf = requestAnimationFrame(tick);
  }

  // The strip's numbers (field.js): scroll or ↑/↓ edit one, Enter applies, Esc or clicking away
  // cancels. While one has the focus the strip is not rebuilt under it; it is once it is left.
  function field(label, o) {
    const input = numField({ parse: parseNum, ...o, cls: "eq-field", aria: label, done: () => strip(), keyed: () => P.el.focus() });
    return h("label", { class: "eq-cell" }, h("span", { text: label }), input);
  }
  const fitTo = (lo, hi) => (v) => Math.round(clamp(v, lo, hi) * 10) / 10;

  function strip() {
    const el = P.el.querySelector(".eq-strip"), L = P.k.line;
    if (!el || el.contains(document.activeElement)) return;  // a number being edited stays
    if (!L) { el.replaceChildren(h("span", { class: "eq-hint", text: P.k.hint })); return; }
    el.replaceChildren(
      field(L.label, { value: () => P.line, show: (v) => `${fmt(v)} dB`, step: STEPS.add, lo: L.lo, hi: L.hi, fit: fitTo(L.lo, L.hi),
                       apply: (v) => setLine(v), tip: `${plain(L.lo).replace("-", "−")} … ${plain(L.hi)} dB` }),
      ...P.k.fields.map((f) => field(f.label, { value: () => value(P.ctx.dev, f), show: f.show, step: STEPS[f.step || "add"], lo: f.lo, hi: f.hi,
                                                fit: fitTo(f.lo, f.hi), apply: (v) => setField(f, v), tip: f.tip, ...(f.parse ? { parse: f.parse } : {}) })),
      h("span", { class: "eq-hint", text: P.k.hint }));
  }

  // Peaks from the server: [in, out] per 10 ms, a few at a time. Stored; tick rebuilds the shapes
  // (at most every REBUILD_MS) and slides them.
  function peaks(key, list) {
    if (key !== P.key || !P.el || !list.length) return;
    if (!P.total) P.c = list.length - 1;
    P.hist.push(...list);
    P.total += list.length;
    P.burst = Math.max(list.length, P.burst - 0.02);  // the biggest lately, forgotten slowly
    if (P.hist.length > SPAN) P.hist.splice(0, P.hist.length - SPAN);
  }

  // For the shots and the README: 6 s of made-up sound that looks like the real thing (seeded, so
  // every render is the same). Music for the limiter and compressor: a 120 bpm beat, a verse, a
  // build and a loud chorus. A voice for the others: words of uneven syllables over room hiss, now
  // and then a sharp "s". Each kind's stand-in follows its own settings: gain changes glide by
  // its attack and release, as the real one's do.
  function made() {
    let seed = 7;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    const type = P.ctx.dev.type, n = KEEP;
    if (type === "limiter" || type === "compressor") {
      return Array.from({ length: n }, (_, k) => {
        const base = k < 220 ? -15 : k < 300 ? -15 + (k - 220) * 0.16 : -2;
        const kick = 7 * Math.exp(-(k % 50) / 5), snare = 5 * Math.exp(-((k + 25) % 50) / 3.5), hat = 1.5 * Math.exp(-(k % 12.5) / 1.5);
        return { v: 10 ** ((base + Math.max(kick, snare, hat) + (rnd() - 0.5) * 2.5) / 20) };
      });
    }
    const out = [];
    let k = 0, words = 0;
    const hiss = () => 10 ** ((-51 + (rnd() - 0.5) * 3) / 20);
    while (k < n) {
      const gap = words % 4 === 0 ? 35 + rnd() * 45 : 3 + rnd() * 5;  // between phrases, between syllables
      for (let g = 0; g < gap && k < n; g++, k++) out.push({ v: hiss(), hiss: true });
      const len = Math.round(9 + rnd() * 14), top = -19 + rnd() * 10, sib = rnd() < 0.35;
      for (let s = 0; s < len && k < n; s++, k++) {
        const shape = Math.sin(Math.PI * (s + 0.5) / len) ** 0.6, d = top + 20 * Math.log10(Math.max(shape, 0.05)) + (rnd() - 0.5) * 2;
        out.push({ v: Math.hypot(10 ** (d / 20), hiss()), word: true });
      }
      for (let s = 0; sib && s < 5 && k < n; s++, k++) out.push({ v: 10 ** ((-7 + rnd() * 3) / 20), s: true });
      words++;
    }
    return out;
  }
  // A gain in dB gliding toward where it is going: attack while it turns down more, release back.
  function glide(targets, attackMs, releaseMs) {
    let g = 0;
    return targets.map((t) => {
      const ms = t < g ? attackMs : releaseMs;
      g += (t - g) * (1 - Math.exp(-10 / Math.max(ms, 1)));
      return g;
    });
  }

  const debug = {
    fill: () => {
      const dev = P.ctx.dev, f = (prop) => value(dev, P.k.fields.find((x) => x.prop === prop)), type = dev.type;
      const src = made(), ins = src.map((b) => b.v), dbs = ins.map(db);
      let outs = ins;
      if (!P.bypass && type === "limiter") outs = ins.map((v) => Math.min(v, 10 ** (P.line / 20)));
      if (!P.bypass && type === "compressor") {
        const g = glide(dbs.map((d) => Math.min(0, (P.line - d) * (1 - 1 / f("ratio")))), f("attack_ms"), f("release_ms"));
        outs = ins.map((v, k) => v * 10 ** ((g[k] + f("makeup_db")) / 20));
      }
      if (!P.bypass && type === "gate") {
        let open = 0;  // blocks since it was last over the line (its hold)
        const want = dbs.map((d) => { open = d >= P.line ? 0 : open + 1; return open * 10 <= f("hold_ms") ? 0 : f("range_db"); });
        const g = glide(want, f("release_ms"), f("attack_ms"));  // closing is its release here
        outs = ins.map((v, k) => v * 10 ** (g[k] / 20));
      }
      if (!P.bypass && type === "denoise") {  // the hiss down by Strength, the words kept
        const cut = 10 ** (value(dev, P.k.line) / 20);
        outs = src.map((b) => (b.hiss ? b.v * cut : b.s ? b.v : Math.max(b.v * 0.97, b.v * cut)));
      }
      if (!P.bypass && type === "deesser") outs = src.map((b) => (b.s ? b.v * 10 ** (-8 / 20) : b.v));
      P.hist = ins.map((v, k) => [v, outs[k]]);
      P.total = P.c = KEEP;
      draw();
    },
    drag: (d) => { P.drag = true; setLine(d, false); },
    clock: () => P.c,  // the block at the right edge (the scroll's check reads it every frame)
    blocks: () => ({ hist: P.hist, lag: 0 }),  // for the live alignment check (the server aligns)
  };

  return { open, close, sync, peaks, isOpen: (key) => P.key === key, debug, SMOOTH_TIP, KINDS };
})();
