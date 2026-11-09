"use strict";
// The board: draws the model the server sends, and sends back what the user does (PLAN §2-4).
// Messages in:  {type: "state", model, problems, workspace, mode} · {type: "toast", text, level}
// Messages out: {op: "connect", from, to} · {op: "disconnect", wire} · {op: "place", cards}
//               {op: "rename", key, title} · {op: "forget-name", key, name} · {op: "hide", key, hidden}
//               {op: "view", view}

const PB = (() => {
  const $ = (id) => document.getElementById(id);
  const board = $("board"), grid = $("grid"), world = $("world"), cardsEl = $("cards"), layer = $("wire-layer");
  const SVG = "http://www.w3.org/2000/svg";
  const KIND = { hardware: "Hardware", virtual: "Virtual device", app: "App", effect: "Effect",
                 plugin: "Plugin", diag: "Diagnostic" };
  const ROLE = { interface: "audio interface", mic: "microphone", speaker: "speaker",
                 playback: "playing sound", recording: "recording" };
  const ICON = {
    mic: '<rect x="5" y="1.5" width="4" height="7" rx="2"/><path d="M3 7a4 4 0 0 0 8 0M7 11v2"/>',
    speaker: '<path d="M2 5h2.5L8 2v10L4.5 9H2z"/><path d="M10.5 4.5a3.5 3.5 0 0 1 0 5"/>',
    interface: '<rect x="1.5" y="3.5" width="11" height="7" rx="1.5"/><circle cx="5" cy="7" r="1.3"/><circle cx="9" cy="7" r="1.3"/>',
    app: '<rect x="1.5" y="2.5" width="11" height="9" rx="1.5"/><path d="M1.5 5h11"/>',
    effect: '<path d="M3 2v10M7 2v10M11 2v10"/><circle cx="3" cy="8" r="1.4"/><circle cx="7" cy="4.5" r="1.4"/><circle cx="11" cy="9" r="1.4"/>',
    plugin: '<rect x="2" y="4" width="10" height="8" rx="1.5"/><path d="M5 4V2.2h4V4"/>',
    waveform: '<path d="M1.5 7c1.2-4 2.3-4 3.5 0s2.3 4 3.5 0 2.3-4 3.5 0"/>',
    stereo: '<path d="M1.5 11.5a5.5 5.5 0 0 1 11 0"/><path d="M7 11.5V4M7 11.5 3.5 6M7 11.5l3.5-5.5"/>',
    counter: '<rect x="1.5" y="3" width="11" height="8" rx="1.5"/><path d="M4.5 5.5v3M7 5.5v3M9.5 5.5v3"/>',
    meter: '<path d="M2 12.5h10"/><path d="M3.5 12V8M6 12V4M8.5 12V6M11 12V2.5"/>',
    spectrum: '<path d="M1.5 12.5h11"/><path d="M1.5 10.5c1.5-6 2.5-7 3.5-4s1.8 2 3 0.5 2.5-5 4.5-3.5"/>',
  };
  const S = {
    model: { cards: [], wires: [], clock: null }, problems: [], ws: { cards: {} }, mode: "connecting",
    view: { x: 0, y: 0, z: 1 }, pos: {}, offsets: {}, sizes: {}, els: {}, wireEls: {}, shown: new Set(),
    selected: null, drag: null, queued: null, socket: null, errors: [], moved: false, hovered: [null, null],
  };
  const colors = {};

  // ---- small helpers ------------------------------------------------------------------------
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") el.textContent = v; else if (v !== false && v != null) el.setAttribute(k, v);
    }
    el.append(...kids.filter((k) => k != null));
    return el;
  }
  function s(tag, attrs = {}) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }
  const byKey = () => Object.fromEntries(S.model.cards.map((c) => [c.key, c]));
  const saved = (key) => S.ws.cards?.[key] || {};
  const isHidden = (c) => saved(c.key).hidden ?? c.hidden;
  const visible = () => S.model.cards.filter((c) => !isHidden(c));
  const titleOf = (c) => saved(c.key).nick || c.title;
  // Cards sit on the board's grid: every 14 px, so corners land on the dots or halfway between.
  // Positions and card sizes are whole cells (owner: zoom only changes how the board is drawn,
  // and Tidy cannot drift on float error). A cell is GRID px; cell [0, 0] is the window's centre
  // on a new board, and Tidy centres the board on it. Pixels only where something is drawn.
  const GRID = 14, snap = (v) => Math.round(v / GRID) * GRID, px = (c) => c * GRID, cell = (v) => Math.round(v / GRID);
  // The view as drawn: its offset in whole pixels (applyView).
  const toWorld = (sx, sy) => ({ x: (sx - Math.round(S.view.x)) / S.view.z, y: (sy - Math.round(S.view.y)) / S.view.z });
  const toScreen = (p) => ({ x: p.x * S.view.z + Math.round(S.view.x), y: p.y * S.view.z + Math.round(S.view.y) });

  function visibleWires() {
    const shown = new Set(visible().map((c) => c.key));
    return S.model.wires.filter((w) => shown.has(w.from[0]) && shown.has(w.to[0]));
  }

  // ---- talking to the server ----------------------------------------------------------------
  let socket = null, retry = 500;
  function connect() {
    if (!location.protocol.startsWith("http")) return;
    socket = new WebSocket(`ws://${location.host}/ws`);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => { retry = 500; sayVisible(); };
    socket.onmessage = (e) => (!(e.data instanceof ArrayBuffer) ? receive(JSON.parse(e.data))
      : DIAG.isFrame(e.data) ? DIAG.frame(e.data) : liveFrame(e.data));
    socket.onclose = () => {
      if (S.bye) return setStatus("down", "PipeBoard has quit");
      setStatus("down", "reconnecting…");
      setTimeout(connect, retry = Math.min(retry * 2, 5000));
    };
  }
  function send(msg) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
    else if (S.mode !== "preview") toast("Not connected to PipeBoard; try again in a moment.");
  }
  function receive(msg) {
    if (msg.type === "toast") return toast(msg.text, msg.level);
    if (msg.type === "bye") { S.bye = true; window.close(); return; }  // PipeBoard is quitting
    if (msg.type === "plan") return showPlan(msg);
    if (msg.type === "scope") { newSources(msg.sources, msg.fresh); return; }
    if (msg.type === "plugins") { S.pluginList = msg.list; S.picker?.(msg.added); return; }
    if (msg.type === "eq-pcm") { EQ.pcm(msg.key, msg.pcm); return; }
    if (msg.type === "diag-keys") { DIAG.keys(msg.keys); return; }
    if (msg.type === "dyn-peaks") { DYN.peaks(msg.key, msg.peaks); return; }
    if (msg.type === "drops") { S.drops = msg.counts; showDrops(); return; }
    if (msg.type === "headroom") { S.headroom = msg; showHeadroom(); return; }
    if (msg.type !== "state") return;
    if (S.drag || S.editing) { S.queued = msg; return; }
    load(msg);
  }

  // ---- loading and drawing the model --------------------------------------------------------
  function load(msg) {
    S.lastState = msg;
    const first = !S.model.cards.length && !S.ws.view;
    S.problems = msg.problems || []; S.events = msg.events || []; S.ws = msg.workspace || { cards: {} };
    S.showLatency = !!msg.show_latency;
    $("show-latency").checked = S.showLatency;
    S.profile = msg.profile || null;
    S.liveOn = msg.live ? msg.live.on : S.liveOn;
    S.liveInstant = msg.live ? !!msg.live.instant : !!S.liveInstant;
    S.autostart = !!msg.autostart;
    S.takeover = msg.takeover || {};
    S.editors = msg.editors || [];
    if (msg.live?.sources?.length) S.liveSources = msg.live.sources;
    showWires();
    S.model = withMissing(msg.model, S.profile);
    S.ws.cards = S.ws.cards || {};
    S.mode = msg.mode || S.mode;
    setStatus(S.mode === "live" ? "live" : "fixture",
              { live: "live", fixture: "fixture graph", preview: "preview" }[S.mode] || S.mode);
    showClock(S.model.clock);
    // The idle states or kernel options changed (a password prompt answered): the open panel
    // shows it, drawn again only then, so a click elsewhere in it is never lost to a rebuild
    // (owner: "they don't toggle").
    const rootWas = JSON.stringify([S.engine?.cstates, S.engine?.kernel, S.engine?.gpu?.full, S.engine?.gpu?.tries]);
    S.engine = msg.engine || null;
    if (JSON.stringify([S.engine?.cstates, S.engine?.kernel, S.engine?.gpu?.full, S.engine?.gpu?.tries]) !== rootWas) {
      S.idlePending = S.kernelPending = S.gpuPending = null;
      if ($("engine")) drawEngine();
    }
    // An interface's panel: closed when the card is gone; drawn once the server reads that card
    // (op engine-device), never with another card's figures meanwhile.
    if (S.iface && !(S.engine?.interfaces || []).some((x) => x.key === S.iface)) closeEngine();
    else if (S.iface && !$("engine") && S.engine?.device === S.iface && !S.engine.trial) openEngine();
    // What runs changed (block, margin, rate): the open panel starts again from what runs now.
    if ($("engine") && S.engine?.interface?.length && (!S.iface || S.engine.device === S.iface)) {
      if (S.engine.trial) closeEngine();
      else if (S.pick?.device !== S.engine.device || JSON.stringify(engineShown(S.engine)) !== S.engineShown) {
        if (S.pick?.device !== S.engine.device) S.headroom = null;
        openEngine();
      }
    }
    if (S.pathKey) (S.showLatency && S.model.latency?.[S.pathKey]?.path ? openPath(S.pathKey) : closePath());
    showTrial(S.engine?.trial);
    // Saved places, except just after a Tidy: a state the server sent before it saved them would
    // put cards back for a moment (owner: they jittered); the layout applied here stands until the
    // board shows it saved (or 3 s).
    const placing = S.placing && performance.now() - S.placing.at < 3000 ? S.placing.cards : null;
    for (const c of S.model.cards) {
      const p = saved(c.key), mine = placing?.[c.key];
      if (p.x == null || (mine && (mine.x !== p.x || mine.y !== p.y))) continue;
      S.pos[c.key] = { x: p.x, y: p.y };
    }
    if (placing && Object.entries(placing).every(([k, q]) => saved(k).x === q.x && saved(k).y === q.y)) S.placing = null;
    render();
    if (S.ws.view && first) { S.view = { ...S.ws.view }; applyView(); }
    else if (first) {  // a new board: cell [0, 0] in the window's centre (owner), then fit what is there
      S.view = { x: Math.round(innerWidth / 2), y: Math.round(innerHeight / 2), z: 1 };
      applyView(false);
      fit(false);
    }
    const eqKey = Object.keys(S.ws.devices || {}).find((k) => EQ.isOpen(k));
    if (eqKey) EQ.sync(eqKey, S.ws.devices[eqKey]);
    else if (S.eqOpen && !S.ws.devices?.[S.eqOpen]) EQ.close();  // it was deleted
    const dynKey = Object.keys(S.ws.devices || {}).find((k) => DYN.isOpen(k));
    if (dynKey) DYN.sync(dynKey, S.ws.devices[dynKey]);
    else if (S.dynOpen && !S.ws.devices?.[S.dynOpen]) DYN.close();
    openNew();
  }

  // Devices a profile names that are not on the system: drawn from their saved shape, dimmed,
  // with the wires that wait for them drawn as ghosts.
  function withMissing(model, profile) {
    const socks = (list) => list.map((x) => ({ ...x, ports: Array(x.n).fill(0), names: [] }));
    const gone = (profile?.missing || []).map((m) => ({
      key: m.key, title: m.title, kind: m.kind, role: m.role, missing: true, hidden: false,
      names: [], owned: false, rate: null, clock: null, detail: "not on the system",
      inputs: socks(m.inputs), outputs: socks(m.outputs) }));
    const uniq = (a) => [...new Set(a)];
    const ghosts = (profile?.ghosts || []).map((w) => ({
      ...w, id: `ghost:${w.from.join(":")}>${w.to.join(":")}`, ghost: true, links: [],
      fromPorts: uniq(w.pairs.map((p) => p[0])), toPorts: uniq(w.pairs.map((p) => p[1])),
      partial: false, failed: false, ch: "", chEnd: "" }));
    return { ...model, cards: [...model.cards, ...gone], wires: [...model.wires, ...ghosts] };
  }

  function render() {
    cardsEl.textContent = "";
    cardsEl.append(h("div", { id: "landing", class: "landing", hidden: "" }));
    S.els = {};
    const wired = new Set();
    for (const w of S.model.wires) { wired.add(w.from.join("|")); wired.add(w.to.join("|")); }
    const flagged = problemCards();
    for (const c of visible()) cardsEl.append(S.els[c.key] = cardEl(c, wired, flagged[c.key]));
    // Widths round up to the grid too (heights already are, by the CSS's 28/14 px rhythm).
    for (const el of Object.values(S.els)) el.style.width = `${Math.ceil(el.offsetWidth / GRID) * GRID}px`;
    // (a diagnostic card's height too: its display grows to the next whole cell, so the power
    // button keeps its place in the corner, as on every effect card; owner saw a gap under it)
    for (const el of cardsEl.querySelectorAll(".card.k-diag")) {
      // (a card's picture takes it; the Counter has none: its tiles do, the buttons stay in the corner)
      const d = el.querySelector(".diag-display, .count-tiles"), extra = Math.ceil(el.offsetHeight / GRID) * GRID - el.offsetHeight;
      if (d && extra) d.style.height = `${d.offsetHeight + extra}px`;
      else if (extra) el.style.minHeight = `${el.offsetHeight + extra}px`;
    }
    // An EQ's curve fills its box: the card snapped to the grid can be wider than the curve was
    // drawn (owner: a gap on the right).
    for (const wrap of cardsEl.querySelectorAll(".eq-mini-wrap")) eqMini(wrap, wrap.closest(".card").dataset.key);
    measure();
    // A card that comes back keeps the spot it had (an app's key returns with the place it had the
    // last time it played), and other cards may sit there now: it is placed again like a new one
    // (owner: a new Chrome stream landed on top of another card). On the first draw only apps:
    // the owner's own arrangement is never moved.
    const back = visible().filter((c) => !S.shown.has(c.key) && S.pos[c.key] && (S.shown.size || c.kind === "app")
                                         && covered(c.key)).map((c) => c.key);
    // An app back at an old spot: a fresh layout's spot (by its wires, with the other sources).
    // A card the user put somewhere (a device made from a dropped wire, one shown again from a
    // menu): kept there, only moved to the nearest free spot (owner: it landed far away).
    const isApp = (k) => byKey()[k]?.kind === "app";
    for (const k of back.filter(isApp)) delete S.pos[k];
    if (back.some((k) => !isApp(k))) placeNew(back.filter((k) => !isApp(k)), true);
    const missing = visible().filter((c) => !S.pos[c.key]).map((c) => c.key);
    if (missing.length) placeNew(missing);
    S.shown = new Set(visible().map((c) => c.key));
    if (S.pathKey) markPath();
    for (const c of visible()) placeCard(c.key);
    drawWires();
    renderProblems();
    renderProfile();
    drawUnsaved();
    $("empty").hidden = S.model.cards.length > 0;
  }

  // Show latency (owner): under an effect what it adds, under a virtual device the total arriving.
  const ms = (v) => (v ? `${v.toFixed(1)} ms` : "0 ms");
  function latencyTag(lat, dev, key) {
    if ("own" in lat) {  // dimmed while the node is bypassed, and at 0 ms (owner: either way)
      const known = lat.own != null, off = !!dev?.bypass || (known && !lat.own);
      const says = known ? (lat.own ? `Delays its sound ${ms(lat.own)}.` : "No delay.") : "Delay not reported.";
      return h("span", { class: `lat${off ? " off" : ""}`, text: known ? ms(lat.own) : "?",
        "data-tip": off ? `Bypassed. ${says}` : says });
    }
    const tip = ["Delay of the sound arriving here."];
    if (lat.spread) tip.push("Longest of several paths.");
    if (lat.unsure) tip.push("A ? on the way: at least this.");
    tip.push("Click: breakdown.");
    const tag = h("span", { class: `lat total${S.pathKey === key ? " open" : ""}`, role: "button", tabindex: "0", "data-tip": tip.join("\n"),
      text: `${lat.unsure ? "≥ " : lat.spread ? "up to " : ""}${ms(lat.total)} total` });
    tag.addEventListener("pointerdown", (e) => e.stopPropagation());  // a click, never a card drag
    tag.addEventListener("mousedown", (e) => e.stopPropagation());
    tag.addEventListener("click", (e) => { e.stopPropagation(); S.pathKey === key ? closePath() : openPath(key); });
    tag.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPath(key); } });
    return tag;
  }

  // The path behind a total (owner, from the design): where the sound enters to this device, its
  // delays in one bar, what adds nothing listed; the other cards dimmed while it shows.
  function pathSteps(lat) {
    const cards = byKey();
    return lat.path.map((k) => {
      const c = cards[k];
      if (!c) return null;
      const own = S.model.latency?.[k];
      const ms = c.kind === "hardware" ? (c.delay?.capture ?? null) : own && "own" in own ? own.own : 0;
      return { k, name: c.kind === "hardware" ? `${titleOf(c)} input` : titleOf(c), ms, kind: c.kind };
    }).filter(Boolean);
  }
  function openPath(key) {
    const lat = S.model.latency?.[key];
    if (!lat?.path) { closePath(); return; }
    const steps = pathSteps(lat), cards = byKey();
    // Every new state from the server comes here to keep it current: drawn again only if what it
    // shows changed, and never with the opening animation then (owner: it replayed at random).
    const old = $("latpath"), sig = JSON.stringify([key, lat, steps, titleOf(cards[key])]);
    if (old && S.pathKey === key && old.dataset.sig === sig) { markPath(); return; }
    old?.remove();
    const again = !!old && S.pathKey === key;
    S.pathKey = key;
    // The chain (owner's pick L1): every step a stop on one line, in order; a step that delays
    // the sound stretches with its ms under it, one that does not is a dot with its name.
    const color = (x) => `var(--${["hardware", "plugin", "virtual", "app"].includes(x.kind) ? x.kind : "effect"})`;
    const delays = (x) => x.ms == null || x.ms > 0.05;
    const step = (x, i) => focusOn(x.k, delays(x)
      ? h("div", { class: `seg delay${x.ms == null ? " unknown" : ""}`, style: `flex:${x.ms == null ? 2 : x.ms} 1 0;--c:${color(x)}`,
                   "data-tip": x.ms == null ? `${x.name}: does not report its delay` : `${x.name}: ${ms(x.ms)}` },
          h("span", { class: "bar" }), h("span", { class: "say" }, h("b", { text: x.ms == null ? "?" : ms(x.ms) }), x.name))
      : h("div", { class: `seg stop${i === steps.length - 1 || i === 0 ? " end" : ""}`, style: `--c:${color(x)}`, "data-tip": `${x.name}: no delay` },
          h("i"), h("span", { text: x.name })));
    const chain = steps.flatMap((x, i) => [i ? h("span", { class: "gap" }) : null, step(x, i)]).filter(Boolean);
    const big = steps.filter((x) => x.ms != null && x.ms > 0.05).sort((a, b) => b.ms - a.ms)[0];
    const close = h("button", { type: "button", class: "x", "aria-label": "Close", text: "×" });
    close.addEventListener("click", closePath);
    let most = null;
    if (big?.kind === "hardware") {
      const go = h("button", { type: "button", class: "go-engine", text: "Audio engine ›" });
      go.addEventListener("click", () => { closePath(); openEngine(); });
      most = [h("span", { text: "Most of it is the interface's buffer" }), go];
    } else if (big) most = [h("span", { text: `Most of it: ${big.name}, ${ms(big.ms)}` })];
    const panel = h("div", { id: "latpath", class: "latpath", role: "dialog", "aria-label": "Where the delay comes from" },
      h("div", { class: "latpath-head" },
        h("span", { class: "route" }, h("b", { text: steps[0]?.name || "?" }), h("span", { class: "to", text: "→" }), h("b", { text: titleOf(cards[key]) })),
        h("span", { class: "total" }, `${lat.unsure ? "≥ " : ""}${lat.total.toFixed(1)}`, h("small", { text: "ms" })), close),
      h("div", { class: "chain" }, ...chain),
      h("div", { class: "latpath-foot" },
        h("span", { id: "latpath-drops", class: "drops", title: "Clicks or gaps since this opened." }), h("span", { class: "grow" }),
        lat.unsure ? h("span", { class: "unsure", text: "? = not reported: at least this" }) : null, ...(most || [])));
    panel.addEventListener("keydown", (e) => { if (e.key === "Escape") closePath(); });
    panel.dataset.sig = sig;
    if (!S.dropsOn) { S.dropsOn = true; S.drops = null; send({ op: "drops", on: true }); }
    if (again) panel.style.animation = "none";
    document.body.append(panel);
    markPath();
    showDrops();
  }
  // The path's cards' dropouts since the panel opened (owner: a live drop counter on devices),
  // filled in place as the server counts, never redrawing the panel.
  function showDrops() {
    const el = $("latpath-drops"), path = S.model.latency?.[S.pathKey]?.path;
    if (!el || !path) return;
    const cards = byKey(), heard = path.filter((k) => S.drops?.[k] != null), hit = heard.filter((k) => S.drops[k] > 0);
    // Just the number (owner: the panel already says which path); which card had them, in the tip.
    const total = hit.reduce((t, k) => t + S.drops[k], 0);
    el.classList.toggle("bad", hit.length > 0);
    el.title = hit.length > 1 ? hit.map((k) => `${titleOf(cards[k])}: ${S.drops[k]}`).join("\n")
      : "Clicks or gaps since this opened.";
    el.textContent = S.drops == null ? "Dropouts: counting…" : !heard.length ? "Dropouts: none of these report any."
      : hit.length ? `Dropouts since opened: ${total}` : "No dropouts since opened.";
  }
  // Pointing at a step (a segment or a name) lights its card on the board while the pointer stays.
  function focusOn(key, el) {
    el.addEventListener("mouseenter", () => S.els[key]?.classList.add("path-focus"));
    el.addEventListener("mouseleave", () => S.els[key]?.classList.remove("path-focus"));
    return el;
  }
  function markPath() {  // the path's cards and the wires between them lit, the rest dimmed (owner)
    const path = S.model.latency?.[S.pathKey]?.path || [], on = new Set(path);
    const steps = new Set(path.slice(1).map((k, i) => `${path[i]}>${k}`));
    for (const [k, el] of Object.entries(S.els)) el.classList.toggle("off-path", !!S.pathKey && !on.has(k));
    for (const we of Object.values(S.wireEls)) {
      const off = !!S.pathKey && !steps.has(`${we.w.from[0]}>${we.w.to[0]}`);
      we.g.classList.toggle("off-path", off);
      for (const end of we.ends || []) end.classList.toggle("off-path", off);
    }
    for (const el of document.querySelectorAll(".lat.total")) el.classList.toggle("open", el.closest(".card")?.dataset.key === S.pathKey);
  }
  function closePath() {
    if (S.dropsOn) { S.dropsOn = false; S.drops = null; send({ op: "drops", on: false }); }
    S.pathKey = null;
    $("latpath")?.remove();
    for (const el of document.querySelectorAll(".card.path-focus")) el.classList.remove("path-focus");
    markPath();
  }

  // A card's type icon (its svg's inside), "" for none: on the card and on its docked panel.
  function cardIcon(c) {
    const role = c.kind === "diag" ? S.ws.devices?.[c.key]?.type : c.role || (c.kind === "effect" ? "effect" : c.kind === "app" ? "app" : "");
    return ICON[role] || ICON[c.kind] || "";
  }
  const iconSvg = (icon) => `<svg class="icon" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${icon}</svg>`;

  function cardEl(c, wired, flags) {
    const icon = cardIcon(c);
    const title = h("span", { class: "title", text: titleOf(c) });
    // The type icon leads the title, in the card's colour (owner: it said what the old dot did, and
    // the dot cost the room); a card with no icon keeps the dot.
    const head = h("header", {}, icon ? null : h("span", { class: "pin" }), title);
    if (icon) head.insertAdjacentHTML("afterbegin", iconSvg(icon));
    head.dataset.tip = cardTip(c);
    const col = (side, socks) => h("div", { class: `col ${side}` },
      ...socks.map((sk) => sockEl(c, sk, side, wired.has(`${c.key}|${sk.id}`))));
    const body = h("div", { class: "body" }, col("in", c.inputs), col("out", c.outputs));
    const dev = S.ws.devices?.[c.key];
    const iface = c.kind === "hardware" && !c.missing && ifaceOf(c.key);  // (a sound card: its own settings)
    const editing = EQ.isOpen(c.key) || DYN.isOpen(c.key) || DIAG.isOpen(c.key) || (iface && S.iface === iface.key);  // its panel is open: the card glows
    // A sound card's gear: in a row of its own at the bottom, centred (owner: as the effects' gear,
    // inside the card; it has no power to sit beside).
    const ifaceFoot = iface ? h("div", { class: "card-foot centred" },
      gearBtn(editing, "its settings", () => (editing ? closeEngine() : openIface(iface.key)))) : null;
    ifaceFoot?.addEventListener("mousedown", (e) => e.stopPropagation());  // never drags the card
    const el = h("div", { class: `card k-${c.kind}${c.missing ? " missing" : ""}${editing ? " editing" : ""}`, "data-key": c.key }, head, body, ifaceFoot,
                 dev?.type === "gain" && !c.missing ? gainRow(c.key, dev) : null,
                 DYN.KINDS[dev?.type] && !c.missing ? dynRow(c.key, dev) : null,
                 dev?.type === "plugin" && !c.missing ? pluginFoot(c.key, dev) : null,
                 dev?.type === "eq" && !c.missing ? eqRow(c.key, dev) : null,
                 DIAG.TYPES[dev?.type] && !c.missing ? diagRow(c.key, dev) : null,
                 c.kind === "diag" ? diagFrame() : null);
    if (flags && !c.missing) {
      // Above the card, never inside it: a problem must not resize what it points at.
      el.append(h("div", { class: `badge sev-${flags.severity}`, "data-tip": flags.tip },
                  h("span", { class: "sev-dot" }),
                  h("span", { class: "label", text: `${flags.label}${flags.more ? ` +${flags.more}` : ""}` })));
    }
    const lat = S.showLatency && !c.missing && S.model.latency?.[c.key];
    if (lat) el.append(latencyTag(lat, dev, c.key));
    if (c.missing) {
      el.append(h("span", { class: "warn", "data-tip": "Not on the system. Its connections wait for it.\nRight-click to use another device.", text: "!" }));
    }
    title.addEventListener("dblclick", (e) => { e.stopPropagation(); startRename(c.key); });
    return el;
  }

  // A gain card's level (and a limiter's ceiling, a compressor's or gate's threshold): a dB box
  // (field.js: scroll or ↑/↓ edit the number, Enter applies, Esc or clicking away cancels).
  // Levels: −144 (silence in practice) … +24 dB, one decimal at most, clamped on Enter; "-inf" is
  // a way to type −144 (the lowest).
  const GAIN_BOX = { prop: "gain_db", lo: -144, hi: 24, dflt: 0, label: "Level", aria: "Gain in dB",
                     op: (key, db) => ({ op: "set-gain", key, db }),
                     tip: "Gain in dB (−144 … +24) · Scroll or ↑/↓ to change (Shift: finer) · Enter to apply · Esc or click away to cancel" };
  const showDb = (db) => {
    const one = (v) => (Number.isInteger(v) ? String(Math.abs(v)) : Math.abs(v).toFixed(1));
    return db > 0 ? `+${one(db)}` : db < 0 ? `−${one(db)}` : "0";
  };
  function dbRow(key, dev, o) {
    const level = () => (typeof dev[o.prop] === "number" ? dev[o.prop] : dev[o.prop] === null ? o.lo : o.dflt);
    const field = numField({
      value: level, show: showDb, parse: parseNum, step: STEPS.add, lo: o.lo, hi: o.hi,
      fit: (db) => Math.max(o.lo, Math.min(o.hi, Math.round(db * 10) / 10)),
      apply: (db) => { dev[o.prop] = db; send(o.op(key, db)); if (DYN.isOpen(key)) DYN.sync(key, dev, true); },
      cls: "gain-field", aria: o.aria,
      // While the box has the focus, board updates wait: a redraw would replace it mid-edit.
      focus: () => { S.editing = true; },
      done: () => {
        S.editing = false;
        if (S.queued && !S.drag) { const q = S.queued; S.queued = null; load(q); }
      },
    });
    const row = h("div", { class: "gain-row", "data-tip": o.tip },
                  h("span", { class: "gain-label", text: o.label }), field, h("span", { class: "gain-unit", text: "dB" }));
    row.addEventListener("mousedown", (e) => e.stopPropagation());  // working the box never drags the card
    return row;
  }

  // ---- a card's buttons: Bypass and the gear (its panel or window) are icons (owner), with a
  // tooltip and a name for screen readers; a word otherwise (Soft) ---------------------------
  const BTN_ICON = {
    // The power symbol (owner's pick): lit in the card's colour while the effect is on, dim when
    // bypassed, as a DAW's device switch.
    bypass: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">'
      + '<path d="M8 2.2v5.2"/><path d="M4.4 4.4a5.4 5.4 0 1 0 7.2 0"/></svg>',  // ink centred in 16 × 16
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
      + '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  };
  function cardBtn({ icon, text, label, tip, pressed, action, cls = "" }) {
    const b = h("button", { type: "button", class: `card-btn ${cls}${icon ? " glyph" : ""}`, text: icon ? null : text,
                            "aria-label": label || text, "data-tip": tip, "aria-pressed": String(!!pressed) });
    if (icon) b.innerHTML = BTN_ICON[icon];
    b.addEventListener("click", action);
    return b;
  }
  const bypassBtn = (on, action) => cardBtn({ icon: "bypass", cls: "bypass", label: "Bypass", pressed: on, action,
    tip: on ? "Off (bypassed): the sound passes through untouched · click to turn it on" : "On · click to bypass it (the sound passes through untouched)" });
  const gearBtn = (open, what, action) => cardBtn({ icon: "gear", label: open ? `Close ${what}` : `Open ${what}`,
    tip: open ? `Close ${what}` : `Open ${what}`, pressed: open, action });

  // ---- the EQ (M11): a small curve on the card; the gear opens the panel (eq.js) -------------
  const rateNow = () => S.model.clock?.rate || 48000;
  const eqMini = (wrap, key) => wrap?.replaceChildren(EQ.mini(S.ws.devices[key], rateNow(), wrap.clientWidth || undefined, 50));
  function eqRow(key, dev) {
    const curve = h("div", { class: "eq-mini-wrap", "data-tip": "Click to edit the EQ" }, EQ.mini(dev, rateNow(), undefined, 50));
    curve.addEventListener("click", () => openEq(key));
    const open = EQ.isOpen(key);
    const box = h("div", { class: "eq-row" }, curve, h("div", { class: "card-foot" },
      bypassBtn(!!dev.bypass, () => eqBypass(key)),
      gearBtn(open, "the EQ", () => (open ? EQ.close() : openEq(key)))));
    box.addEventListener("mousedown", (e) => e.stopPropagation());  // working it never drags the card
    return box;
  }
  function eqBypass(key) {
    const dev = S.ws.devices[key];
    dev.bypass = !dev.bypass;
    send({ op: "eq-set", key, bands: dev.bands || [], bypass: dev.bypass });
    if (EQ.isOpen(key)) EQ.sync(key, dev, true);  // the card's own change: never an echo
    render();
  }
  // ---- diagnostic cards (PLAN §8d): Waveform, Stereo; drawn by diag.js, on their own -------
  // Their edge: a dashed line in silver (owner), an SVG over the card sized by CSS.
  function diagFrame() {
    const svg = s("svg", { class: "diag-frame", "aria-hidden": "true" });
    svg.append(s("rect", { class: "diag-edge" }));
    return svg;
  }
  function diagRow(key, dev) {
    DIAG.setMono(key, dev.mono);
    DIAG.setSmooth(key, dev.smooth);
    DIAG.setZoom(key, dev.zoom);
    DIAG.setLines(key, dev.lines);
    const on = !dev.bypass;
    const power = cardBtn({ icon: "bypass", cls: "bypass", label: on ? "Turn off" : "Turn on", pressed: !on,
      tip: on ? "On · click to turn it off (it stops measuring, saving the work)" : "Off: not measuring · click to turn it on",
      action: () => { dev.bypass = on; send({ op: "diag-on", key, on: !on }); if (on) DIAG.close(); render(); } });
    if (dev.type === "counter") return counterRow(key, dev, power);
    const v = DIAG.view(key, dev.type, on);
    v.display.addEventListener("click", () => on && openDiag(key));
    const wave = dev.type === "waveform";
    const box = h("div", { class: "diag-row" }, v.display, v.extra, h("div", { class: "card-foot" }, power,
      wave ? DIAG.modeSwitch(key, (m) => setDiag(key, "mono", m)) : null,
      wave ? DIAG.toggle("smooth", key, (m) => setDiag(key, "smooth", m)) : null,
      wave ? DIAG.toggle("zoom", key, (m) => setDiag(key, "zoom", m)) : null,
      dev.type === "stereo" ? DIAG.modeSwitch(key, (m) => setDiag(key, "lines", m), "drawing") : null, v.info));
    box.addEventListener("mousedown", (e) => e.stopPropagation());  // working it never drags the card
    return box;
  }
  // The Counter (owner): dropouts on the way here and clipping arriving here, since a reset. The
  // server counts (Board.tally, Board.clipped); times shown as clock times, so nothing ticks.
  const clockTime = (t) => {
    const d = new Date(t * 1000), today = d.toDateString() === new Date().toDateString();
    return d.toLocaleString([], today ? { hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  function counterRow(key, dev, power) {
    const c = dev.counts || {}, on = !dev.bypass;
    const tile = (n, label, last, tip) => h("div", { class: `count-tile ${n ? "bad" : "ok"}`, "data-tip": tip },
      h("b", { text: String(n || 0) }), h("small", { text: label }), h("em", { text: last ? `last at ${clockTime(last)}` : "none yet" }));
    const reset = cardBtn({ text: "Reset", label: "Reset", tip: "Count from now", action: () => send({ op: "diag-reset", key }) });
    const box = h("div", { class: "diag-row" },
      h("div", { class: `count-tiles${on ? "" : " off"}` },
        tile(c.drops, "Dropouts", c.last_drop, "Clicks or gaps on the way here: the cards wired into it (their apps left out) and the interface that clocks everything (when it drops out, all sound does). Counted all the time."),
        tile(c.clips, "Clipping", c.last_clip, "Sound arriving here at full scale. Counted while a PipeBoard window shows the board.")),
      h("div", { class: "card-foot" }, power, h("span", { class: "diag-info", text: on ? `since ${c.since ? clockTime(c.since) : "now"}` : "off: not counting" }), reset));
    box.addEventListener("mousedown", (e) => e.stopPropagation());
    return box;
  }
  function setDiag(key, field, on) {  // the Waveform's Stereo/Mono or Smooth (card or panel): saved with the card
    const dev = S.ws.devices?.[key];
    if (!dev) return;
    dev[field] = on;
    ({ mono: DIAG.setMono, smooth: DIAG.setSmooth, zoom: DIAG.setZoom, lines: DIAG.setLines })[field](key, on);
    send({ op: "diag-set", key, [field]: on });
    render();
  }
  function openDiag(key) {
    if (S.iface) closeEngine();  // (one docked panel at a time)
    const dev = S.ws.devices?.[key], c = byKey()[key];
    if (!dev) return;
    closeMenu(); hideTip(); EQ.close(false); S.eqOpen = null; DYN.close(false); S.dynOpen = null;
    movable(DIAG.open(key, { type: dev.type, title: titleOf(c || { key, title: dev.name }), done: () => render(),
                             setMono: (m) => setDiag(key, "mono", m), set: (field, on) => setDiag(key, field, on) }), key);
    render();
  }

  function openEq(key) {
    if (S.iface) closeEngine();  // (one docked panel at a time)
    const dev = S.ws.devices?.[key], c = byKey()[key];
    if (!dev) return;
    closeMenu(); hideTip(); DYN.close(false); S.dynOpen = null; DIAG.close(false);
    EQ.open(key, { dev, title: titleOf(c || { key, title: dev.name }), rate: rateNow(), send, menu: openMenu,
                   changed: (k) => eqMini(S.els[k]?.querySelector(".eq-mini-wrap"), k),
                   done: () => { S.eqOpen = null; render(); } });
    S.eqOpen = key;
    movable(document.querySelector(".eq-panel"), key);
    render();
  }

  // A docked panel (EQ, limiter, compressor) moves when its header is dragged (owner); it is a
  // new element each time one opens, so it always opens docked at the bottom again. A click on
  // its title (no drag) shows its card on the board, flashed.
  // ponytail: a panel moved and then the window shrunk can sit partly outside it until moved.
  function movable(panel, key) {
    const head = panel?.querySelector(".eq-head"), title = head?.querySelector(".eq-title");
    // Its card's icon where the dot was, as on the card itself (owner); every docked panel opens
    // through here (EQ, live view, diagnostics, an interface's).
    const c = byKey()[key], icon = c && cardIcon(c), pin = head?.querySelector(".pin");
    if (icon && pin) pin.outerHTML = iconSvg(icon);
    if (title) title.dataset.tip = "Show it on the board";
    head?.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("button, input")) return;
      e.preventDefault();  // no text selection while dragging; that also keeps the focus, so:
      document.activeElement?.blur();  // a box being edited counts this as a click away (cancels)
      const r = panel.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
      try { head.setPointerCapture(e.pointerId); } catch { /* a synthetic pointer (shots) */ }
      panel.classList.add("moving");
      const move = (ev) => Object.assign(panel.style, {
        left: `${Math.max(0, Math.min(innerWidth - r.width, ev.clientX - dx))}px`,
        top: `${Math.max(0, Math.min(innerHeight - r.height, ev.clientY - dy))}px`,
        bottom: "auto", transform: "none", animation: "none" });
      const up = (ev) => {
        panel.classList.remove("moving");
        if (ev.type === "pointerup" && Math.hypot(ev.clientX - e.clientX, ev.clientY - e.clientY) < 4 && e.target.closest(".eq-title")) showCard(key);
        for (const [type, fn] of [["pointermove", move], ["pointerup", up], ["pointercancel", up]]) head.removeEventListener(type, fn);
      };
      head.addEventListener("pointermove", move);
      head.addEventListener("pointerup", up);
      head.addEventListener("pointercancel", up);
    });
  }

  // ---- the limiter and compressor: the line's setting on the card; Live opens the panel ------
  const DYN_TIP = { limiter: "The loudest the sound may be", compressor: "Above this it turns the sound down",
                   gate: "Under this it turns the sound down",
                   deesser: "When the \"s\" band gets louder than this, it is turned down",
                   denoise: "How far steady noise is turned down" };
  function dynBox(type) {
    const L = DYN.KINDS[type].line;
    return { prop: L.prop, lo: L.lo, hi: L.hi, dflt: L.dflt, label: L.label, aria: `${L.label} in dB`,
             op: (key, db) => ({ op: "dyn-set", key, [L.prop]: db }),
             tip: `${DYN_TIP[type]}, in dB (${showDb(L.lo)} … ${showDb(L.hi)}) · Scroll or ↑/↓ to change (Shift: finer) · Enter to apply · Esc or click away to cancel` };
  }
  function dynRow(key, dev) {
    const open = DYN.isOpen(key);
    const k = DYN.KINDS[dev.type];
    const box = h("div", { class: "dyn-row" }, k.line ? dbRow(key, dev, dynBox(dev.type)) : null, h("div", { class: "card-foot" },
      k.bypass === false ? null : bypassBtn(!!dev.bypass, () => dynFlag(key, "bypass")),
      k.smooth ? cardBtn({ text: "Soft", cls: "smooth", tip: DYN.SMOOTH_TIP, pressed: dev.smooth, action: () => dynFlag(key, "smooth") }) : null,
      gearBtn(open, `the ${k.word}'s live view`, () => (open ? DYN.close() : openDyn(key)))));
    box.addEventListener("mousedown", (e) => e.stopPropagation());
    return box;
  }
  // A gain: its level box, and its power button below (bypassed: 0 dB, its level kept).
  function gainRow(key, dev) {
    const box = h("div", { class: "dyn-row" }, dbRow(key, dev, GAIN_BOX),
                  h("div", { class: "card-foot" }, bypassBtn(!!dev.bypass, () => gainBypass(key))));
    box.addEventListener("mousedown", (e) => e.stopPropagation());
    return box;
  }
  function gainBypass(key) {
    const dev = S.ws.devices[key];
    dev.bypass = !dev.bypass;
    send({ op: "gain-bypass", key, on: dev.bypass });
    render();
  }
  function dynFlag(key, flag) {
    const dev = S.ws.devices[key];
    dev[flag] = !dev[flag];
    send({ op: "dyn-set", key, [flag]: dev[flag] });
    if (DYN.isOpen(key)) DYN.sync(key, dev, true);  // the card's own change: never an echo
    render();
  }
  function openDyn(key) {
    if (S.iface) closeEngine();  // (one docked panel at a time)
    const dev = S.ws.devices?.[key], c = byKey()[key];
    if (!dev) return;
    closeMenu(); hideTip(); EQ.close(false); S.eqOpen = null; DIAG.close(false);
    DYN.open(key, { dev, title: titleOf(c || { key, title: dev.name }), send,
                    changed: (k) => {
                      const el = S.els[k], f = el?.querySelector(".gain-field"), d = S.ws.devices[k];
                      if (f && document.activeElement !== f) f.value = showDb(d[DYN.KINDS[d.type].line.prop]);
                      el?.querySelector(".card-btn.smooth")?.setAttribute("aria-pressed", String(!!d.smooth));
                    },
                    done: () => { S.dynOpen = null; render(); } });
    S.dynOpen = key;
    movable(document.querySelector(".eq-panel"), key);
    render();
  }

  // ---- plugins (M8): Bypass and the plugin's own window; its controls live in that window ----
  function pluginFoot(key, dev) {
    const open = (S.editors || []).includes(key);
    const foot = h("div", { class: "card-foot" }, ...(dev.params ? [
      dev.bypassable ? bypassBtn(!!dev.bypass, () => send({ op: "plugin-bypass", key, on: !dev.bypass })) : null,
      dev.editor ? gearBtn(open, "the plugin's own window", () => send({ op: "plugin-editor", key, on: !open })) : null,
    ] : [h("span", { class: "card-note", text: "Starting…" })]));
    foot.addEventListener("mousedown", (e) => e.stopPropagation());  // pressing a button never drags the card
    return foot;
  }

  // A list with a search box, to pick one plugin. items() → [{value, label, sub}], or null while
  // the list is still coming (the "plugins" message redraws an open picker). Resolves with the
  // value, or null. rescan(): asks for the list again; its answer says how many are new.
  function picker({ title, items, placeholder, loading, rescan, ok = "Add" }) {
    return new Promise((done) => {
      closeMenu(); hideTip();
      const search = h("input", { type: "text", placeholder, "aria-label": placeholder, spellcheck: "false" });
      const list = h("div", { class: "pick-list", role: "listbox", "aria-label": title });
      const count = h("span", { class: "pick-count" });
      const no = h("button", { type: "button", text: "Cancel" });
      const yes = h("button", { type: "button", class: "primary", text: ok });
      const again = rescan && h("button", { type: "button", text: "Rescan",
                                           title: "Look for plugins installed since; only new ones take a while" });
      let scanning = false, found = "";  // found: what the last Rescan found
      again?.addEventListener("click", () => { scanning = true; found = ""; draw(); rescan(); });
      const box = h("div", { class: "dialog picker", role: "dialog", "aria-modal": "true", "aria-label": title },
                    h("h2", { text: title }), search, list, h("div", { class: "actions" }, count, again || null, no, yes));
      let shown = [], active = 0;
      const draw = (added) => {
        const all = items(), words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
        if (added !== undefined) { scanning = false; found = added ? ` · ${added} new` : " · none new"; }
        if (again) {  // (not while the first list is coming: everything would count as new)
          again.disabled = !all || scanning; again.textContent = scanning ? "Scanning…" : "Rescan";
        }
        list.textContent = "";
        if (!all) { list.append(h("div", { class: "pick-note", text: loading })); shown = []; return; }
        shown = all.filter((it) => words.every((w) => `${it.label} ${it.sub}`.toLowerCase().includes(w)));
        active = Math.max(0, Math.min(active, shown.length - 1));
        if (!shown.length) list.append(h("div", { class: "pick-note", text: "Nothing matches" }));
        shown.forEach((it, i) => {
          const row = h("div", { class: `pick${i === active ? " active" : ""}`, role: "option", "aria-selected": String(i === active) },
                        h("span", { class: "pick-label", text: it.label }), h("span", { class: "pick-sub", text: it.sub }));
          row.addEventListener("mousedown", (e) => e.preventDefault());  // the search box keeps the focus
          row.addEventListener("click", () => { active = i; draw(); });
          row.addEventListener("dblclick", () => finish(it.value));
          list.append(row);
        });
        count.textContent = `${shown.length} plugin${shown.length === 1 ? "" : "s"}${found}`;
        list.querySelector(".pick.active")?.scrollIntoView({ block: "nearest" });
      };
      const veil = h("div", { class: "veil" }, box);
      const finish = (v) => { veil.remove(); removeEventListener("keydown", keys, true); S.picker = null; done(v); };
      const confirm = () => shown[active] && finish(shown[active].value);
      const keys = (e) => {
        if (e.key === "Escape") finish(null);
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") { active += e.key === "ArrowDown" ? 1 : -1; draw(); }
        else if (e.key === "Enter") (document.activeElement === no ? finish(null) : confirm());
        else return;
        e.preventDefault(); e.stopPropagation();
      };
      addEventListener("keydown", keys, true);
      search.addEventListener("input", () => { active = 0; draw(); });
      no.addEventListener("click", () => finish(null));
      yes.addEventListener("click", confirm);
      veil.addEventListener("mousedown", (e) => { if (e.target === veil) finish(null); });
      S.picker = draw;
      document.body.append(veil);
      draw();
      search.focus();
    });
  }

  const shapeOf = (p) => (p.ins === 2 && p.outs === 2 ? "stereo" : p.ins === 1 && p.outs === 1 ? "mono" : `${p.ins} in, ${p.outs} out`);

  async function pickPlugin(ctx) {
    send({ op: "plugin-list" });  // the answer redraws the picker
    const ref = await picker({
      title: "Add a plugin", placeholder: "Search by name or maker",
      loading: "Looking for your plugins… The first look can take a minute.",
      rescan: () => send({ op: "plugin-list", rescan: true }),
      items: () => S.pluginList && S.pluginList.map((p) => ({
        value: p.ref, label: p.name, sub: `${p.maker} · ${p.format.toUpperCase()} · ${shapeOf(p)}` })) });
    if (ref) makeDevice({ type: "plugin", plugin: ref }, ctx);
  }

  // ctx: {} plain · {connect: {from|to: [key, sock]}} a wire dropped on empty board ·
  // {insert: wireId} put the new device in the middle of that wire.
  function makeDevice(fields, ctx = {}) {
    const at = S.menuAt || toWorld(innerWidth / 2, innerHeight / 2);
    const where = { x: cell(at.x), y: cell(at.y) };  // (cells)
    // An effect with settings: they open once its card is on the board (owner). The server does
    // not answer with the new key, so it is the first device of that type not here now; a stale
    // wish (it was not made) lapses.
    if (fields.type === "eq" || fields.type === "plugin" || DYN.KINDS[fields.type])
      S.opening = { type: fields.type, known: new Set(Object.keys(S.ws.devices || {})), until: performance.now() + 20000 };
    if (ctx.insert) send({ op: "insert-device", wire: ctx.insert, ...fields, ...where });
    else send({ op: "create-device", ...fields, ...where, connect: ctx.connect });
  }

  function openNew() {
    const p = S.opening;
    if (!p) return;
    if (performance.now() > p.until) { S.opening = null; return; }
    const key = Object.keys(S.ws.devices || {}).find((k) => !p.known.has(k) && S.ws.devices[k].type === p.type);
    const dev = S.ws.devices?.[key];
    if (!dev || !S.els[key] || (p.type === "plugin" && !dev.params)) return;  // (a plugin: once started, so its window is known)
    S.opening = null;
    if (p.type === "eq") openEq(key);
    else if (p.type !== "plugin") openDyn(key);
    else if (dev.editor) send({ op: "plugin-editor", key, on: true });
  }

  function addGain(ctx) { makeDevice({ type: "gain", name: "Gain", channels: 2 }, ctx); }

  function sockEl(c, sk, side, isWired) {
    const dot = h("i", { class: `dot${sk.ports.length === 2 ? " stereo" : ""}` });
    const label = h("span", { text: sk.label });
    const el = h("div", { class: `sock${isWired ? " wired" : ""}`, "data-key": c.key,
                          "data-sock": sk.id, "data-side": side },
                 ...(side === "in" ? [dot, label] : [label, dot]));
    const what = sk.ports.length === 2 ? "Stereo" : "Mono";
    el.dataset.tip = JSON.stringify({
      title: `${titleOf(c)} · ${sk.label}`, sub: `${what} ${side === "in" ? "input" : "output"}`, kind: c.kind,
      rows: sk.names.length ? [["Ports", sk.names.join(", "), true]] : [],
      hint: side === "in" ? (isWired ? "Drag to move its wire · Shift-drag for a new one" : "Connect to it by dragging from an output")
                          : "Drag to another socket to connect" });
    return el;
  }

  function cardTip(c) {
    const rows = [];
    if (saved(c.key).nick) rows.push(["Real name", c.title]);
    if (c.rate) rows.push(["Rate", `${c.rate / 1000} kHz`]);
    const dev = S.ws.devices?.[c.key];
    if (dev?.type === "plugin") rows.push(["Plugin", `${dev.plugin || dev.name} · ${(dev.format || "").toUpperCase()}`]);
    else if (dev) rows.push(["Made by", "PipeBoard"]);
    // Hardware: the device's one identity, not its two long node names.
    // A plugin: its own node, not Carla's empty engine node beside it.
    if (!c.missing) rows.push(["PipeWire", c.key.startsWith("device:") ? c.key.slice(7) : dev?.type === "plugin" ? c.names.at(-1) : c.detail, true]);
    return JSON.stringify({
      title: titleOf(c), sub: [KIND[c.kind], ROLE[c.role]].filter(Boolean).join(" · "), kind: c.kind, rows,
      warn: c.missing ? "Not on the system: its connections wait for it" : "",
      hint: "Double-click the name to rename · right-click for more" });
  }

  const q4 = (v) => Math.round(v * 4) / 4;  // a socket's spot to a quarter pixel: the same at any zoom
  function measure() {
    const z = S.view.z;
    for (const [key, el] of Object.entries(S.els)) {
      const box = el.getBoundingClientRect(), offs = {};
      for (const sk of el.querySelectorAll(".sock")) {
        const d = sk.querySelector(".dot").getBoundingClientRect();
        offs[sk.dataset.sock] = { x: q4((d.left + d.width / 2 - box.left) / z), y: q4((d.top + d.height / 2 - box.top) / z) };
      }
      S.offsets[key] = offs;
      // The card's own layout size, whole CSS pixels whatever the zoom. Read off the screen and
      // divided by the zoom it carried float noise that changed with the zoom (154.0000088 here,
      // 153.9999952 there), and Tidy's centring sat exactly half a grid step: it rounded one way
      // and, after its Fit changed the zoom, the other (owner: a card moved back and forth).
      S.sizes[key] = { w: Math.ceil(el.offsetWidth / GRID), h: Math.ceil(el.offsetHeight / GRID) };  // (cells)
    }
  }

  function placeCard(key) {
    const p = S.pos[key], el = S.els[key];
    if (p && el) el.style.transform = `translate(${px(p.x)}px, ${px(p.y)}px)`;
  }

  // ---- layout: columns by signal flow -------------------------------------------------------
  function depths(cards, wires) {
    // Longest path from the sources, after dropping the edges that close a loop: so apps sit
    // left, then speaker mixes, effects, the interface, and the mics it feeds.
    const adj = {}, d = {}, state = {}, keep = [];
    for (const c of cards) { adj[c.key] = new Set(); d[c.key] = 0; }
    for (const w of wires) if (w.from[0] !== w.to[0] && adj[w.from[0]] && adj[w.to[0]]) adj[w.from[0]].add(w.to[0]);
    const fed = new Set(Object.values(adj).flatMap((n) => [...n]));
    const visit = (k) => {
      state[k] = 1;
      for (const n of adj[k]) {
        if (state[n] === 1) continue;  // closes a loop: not a direction to lay out
        keep.push([k, n]);
        if (!state[n]) visit(n);
      }
      state[k] = 2;
    };
    for (const c of [...cards].sort((a, b) => fed.has(a.key) - fed.has(b.key))) if (!state[c.key]) visit(c.key);
    for (let i = 0; i < cards.length; i++) for (const [a, b] of keep) d[b] = Math.max(d[b], d[a] + 1);
    return d;
  }

  // The second pass (owner): a card that is the only one of its category (its colour: app,
  // speaker or mic, effect…) in its column, among others, joins the next or the previous column
  // when cards of its category are there, stacking below them, as long as the flow still reads
  // left to right: everything it feeds stays right of it, everything feeding it left. A speaker
  // mix next to another one, both feeding the same gain, sits under it, not among the apps. A card
  // alone in its column, or with company of its own kind, stays.
  // The right side mirrors the left (owner): as the apps that only send sit in the first column,
  // a card that only receives (a mix apps record from) stacks with its own kind in the last
  // column: the guitar mix beside the voice mixes, not back among the effects. Only it moves;
  // what feeds it stays put.
  function regroup(cards, wires, d) {
    const last = Math.max(0, ...Object.values(d));
    for (const c of cards) {
      const here = cards.filter((o) => o !== c && d[o.key] === d[c.key]);
      if (!here.length || here.some((o) => o.kind === c.kind)) continue;
      const feeds = wires.filter((w) => w.from[0] === c.key && w.to[0] !== c.key).map((w) => d[w.to[0]]);
      const fedBy = wires.filter((w) => w.to[0] === c.key && w.from[0] !== c.key).map((w) => d[w.from[0]]);
      const fits = (col) => feeds.every((x) => x > col) && fedBy.every((x) => x < col);
      if (!feeds.length && fedBy.length && cards.some((o) => d[o.key] === last && o.kind === c.kind)) { d[c.key] = last; continue; }
      const kin = (col) => cards.filter((o) => o !== c && d[o.key] === col && o.kind === c.kind).length;
      const to = [d[c.key] + 1, d[c.key] - 1].filter((col) => col >= 0 && kin(col) && fits(col))
        .sort((a, b2) => kin(b2) - kin(a))[0];
      if (to !== undefined) d[c.key] = to;
    }
    return d;
  }

  // A column far taller than the rest (owner: five diagnostic cards fed from one EQ stacked into
  // one, and Tidy could neither fold the board nor show it any bigger): its cards that only
  // receive (nothing wired out of them) wrap into extra columns right after it, each no taller
  // than the tallest other column. What passes sound on stays in its own column.
  function wrapTall(list, wires, gap) {
    const tall = (col) => col.reduce((t, c) => t + S.sizes[c.key].h + gap, 0);
    const out = [];
    list.forEach((col, i) => {
      const target = Math.max(28, ...list.filter((_, j) => j !== i).map(tall));
      const ends = col.filter((c) => !wires.some((w) => w.from[0] === c.key && w.to[0] !== c.key));
      if (tall(col) <= target * 1.4 || ends.length < 2) { out.push(col); return; }
      let cur = col.filter((c) => !ends.includes(c)), h = tall(cur);
      for (const c of ends) {
        const ch = S.sizes[c.key].h + gap;
        if (cur.length && h + ch > target) { out.push(cur); cur = []; h = 0; }
        cur.push(c); h += ch;
      }
      out.push(cur);
    });
    return out;
  }

  function layout(keys) {
    const cards = visible(), wires = visibleWires(), d = regroup(cards, wires, depths(cards, wires));
    const cols = [];
    for (const c of cards) (cols[d[c.key]] = cols[d[c.key]] || []).push(c);
    const order = { hardware: 0, app: 1, virtual: 2, effect: 3, plugin: 4, diag: 5 };
    // All in whole cells: every card lands exactly, a column's rows evenly spaced (owner).
    const ROW_GAP = 2, COL_GAP = 9;
    const list = wrapTall(cols.filter(Boolean), wires, ROW_GAP), at = {};
    list.forEach((col, i) => col.forEach((c) => { at[c.key] = i; }));
    const wide = list.map((col) => Math.max(...col.map((c) => S.sizes[c.key].w)));
    const high = list.map((col) => col.reduce((t, c) => t + S.sizes[c.key].h + ROW_GAP, 0));
    const bands = fold(wide, high, wires.map((w) => [at[w.from[0]], at[w.to[0]]]).filter(([a, b]) => a !== undefined && b !== undefined));
    const placed = {}, rows = [], rowOf = {}, edge = {};
    let top = 0;
    bands.forEach(([from, to], r) => {
      let x = r ? PORTAL_COL : 0;  // a later row leaves room for its column of entry portals
      const tallest = Math.max(...high.slice(from, to)), left = x;
      for (const col of list.slice(from, to)) {
        const bary = (c) => {
          const ys = wires.filter((w) => w.to[0] === c.key && placed[w.from[0]]).map((w) => placed[w.from[0]].y);
          return ys.length ? ys.reduce((a, b) => a + b) / ys.length : 1e6 + order[c.kind] * 1e3;
        };
        col.sort((a, b) => bary(a) - bary(b) || order[a.kind] - order[b.kind] || a.title.localeCompare(b.title));
        let y = top + Math.floor((tallest - high[list.indexOf(col)]) / 2);
        for (const c of col) {
          placed[c.key] = { x, y };
          rowOf[c.key] = r;
          edge[c.key] = (col === list[from] ? "first" : "") + (col === list[to - 1] ? "last" : "");
          y += S.sizes[c.key].h + ROW_GAP;
        }
        x += Math.max(...col.map((c) => S.sizes[c.key].w)) + COL_GAP;
      }
      rows.push({ left, right: x - COL_GAP });
      top += tallest + ROW_BAND_GAP;
    });
    // Centred on cell [0, 0] (owner): shifted by whole cells, half the board's size each way.
    const all = Object.entries(placed), dx = -Math.floor((Math.max(0, ...all.map(([k, q]) => q.x + S.sizes[k].w))) / 2);
    const dy = -Math.floor((Math.max(0, ...all.map(([k, q]) => q.y + S.sizes[k].h))) / 2);
    for (const [, q] of all) { q.x += dx; q.y += dy; }
    for (const row of rows) { row.left += dx; row.right += dx; }
    S.fold = { rows, rowOf, edge };
    for (const k of keys) S.pos[k] = placed[k];
    return keys;
  }

  // Tidy's rows (owner: the board only grew wider): the columns, in order, cut into rows when one
  // row would be far wider than the window, at the cuts the fewest wires cross; the rows stacked.
  // Each candidate count of rows is scored by how near its shape is to the window's and by how many
  // wires become portals. wide/high: each column's size; links: [column from, column to] per wire.
  const PORTAL_COL = 8, ROW_BAND_GAP = 7;  // (cells; a portal column: room for its badges off the cards)
  function fold(wide, high, links) {
    const n = wide.length, aspect = innerWidth / Math.max(1, innerHeight - 120);
    const span = (a, b) => wide.slice(a, b).reduce((t, w) => t + w + 9, 0);  // (cells, like layout's COL_GAP)
    const crossing = (cuts) => links.filter(([a, b]) => cuts.some((c) => (a < c) !== (b < c))).length;
    const shape = (cuts) => {
      const edges = [0, ...cuts, n], bands = edges.slice(1).map((e, i) => [edges[i], e]);
      const w = Math.max(...bands.map(([a, b], i) => span(a, b) + (i ? PORTAL_COL : 0)));
      const h = bands.reduce((t, [a, b]) => t + Math.max(...high.slice(a, b)) + ROW_BAND_GAP, 0);
      return { bands, score: Math.abs(Math.log(w / h / aspect)) + 0.04 * crossing(cuts) };  // (a wire between rows: a portal, a small cost)
    };
    if (S.forceCuts) { S.foldInfo = { n, ...shape(S.forceCuts), crossings: crossing(S.forceCuts) }; return S.foldInfo.bands; }  // (renders comparing splits)
    if (S.ws.tidy_portals !== true) return [[0, n]];  // Portals off (off unless turned on, owner): one row
    let best = shape([]);
    for (let r = 2; r <= Math.min(6, n); r++) {
      const total = span(0, n), cuts = [];
      for (let k = 1; k < r; k++) {  // near equal widths, then the neighbour cut fewest wires cross
        let c = 1;
        while (c < n - 1 && span(0, c + 1) <= (total * k) / r) c++;
        c = [c - 1, c, c + 1].filter((v) => v > (cuts.at(-1) ?? 0) && v < n)
          .sort((u, v) => crossing([u]) - crossing([v]) || Math.abs(span(0, u) - (total * k) / r) - Math.abs(span(0, v) - (total * k) / r))[0];
        if (c !== undefined) cuts.push(c);
      }
      const s = shape([...new Set(cuts)]);
      if (s.score < best.score) best = s;
    }
    return best.bands;
  }

  // Tidy's portals: every wire between rows. Its exit badge in one column past its row's right
  // edge, its entry badge in one column before the next row's left (owner), each at its socket's
  // height, nudged apart where two would touch; but from a card not at the row's edge, beside its
  // socket instead (null): a column there drew a stub across the whole row.
  function foldPortals() {
    const { rows, rowOf, edge } = S.fold, cols = {}, set = {};
    for (const w of visibleWires()) {
      const rf = rowOf[w.from[0]], rt = rowOf[w.to[0]];
      if (rf === undefined || rt === undefined || rf === rt || w.ghost) continue;
      const a = anchor(...w.from), b = anchor(...w.to);
      if (!a || !b) continue;
      const out = edge[w.from[0]]?.includes("last") ? [rows[rf].right + PORTAL_COL / 2, cell(a.y)] : null;  // (cells: sockets sit on the grid)
      const into = edge[w.to[0]]?.includes("first") ? [rows[rt].left - PORTAL_COL / 2, cell(b.y)] : null;
      set[w.id] = { how: "tidy", out, in: into };
      if (out) (cols[`o${rf}`] ||= []).push([out, b.y]);  // (with the other end's height: from one
      if (into) (cols[`i${rt}`] ||= []).push([into, a.y]);  //  socket, in the order they arrive: no crossing)
    }
    for (const col of Object.values(cols)) {
      col.sort(([p, py], [q, qy]) => p[1] - q[1] || py - qy);
      for (let i = 1; i < col.length; i++) col[i][0][1] = Math.max(col[i][0][1], col[i - 1][0][1] + 2);  // (two cells apart)
    }
    return set;
  }

  // New cards (an app that starts playing, a device plugged in): their spot in a fresh layout if
  // it is free, else the nearest free grid spot, a grid step clear of every card; then saved, so
  // they stay put. A fresh layout alone ignored where cards had been moved to since, and a new
  // app's card landed right on top of one (owner).
  function covered(key) {  // another card on the board overlaps this one's spot
    const a = S.pos[key], sa = S.sizes[key];
    return !!a && !!sa && visible().some((c) => {
      const b = S.pos[c.key], sb = S.sizes[c.key];
      return c.key !== key && b && sb && a.x < b.x + sb.w && b.x < a.x + sa.w && a.y < b.y + sb.h && b.y < a.y + sa.h;
    });
  }

  // keep: each card's own spot is the one wanted (a card dropped where the user chose, an app back
  // at its old place), only moved to the nearest free spot if it lands on another card; without,
  // the wanted spot is a fresh layout's (owner: a node made from a dropped wire landed far away).
  // Places sent to the server, held on the page until it has saved them (or 3 s): a board update
  // it sent just before carried the old spots, and a card dropped after a drag jumped back, then
  // forward again (owner: "teleport back and forth" after letting go). Tidy had the same guard.
  // auto: a spot the board picked by itself, not the user's move (not offered for saving).
  function sendPlaces(cards, auto = false) {
    const still = S.placing && performance.now() - S.placing.at < 3000 ? S.placing.cards : {};
    S.placing = { cards: { ...still, ...cards }, at: performance.now() };
    send({ op: "place", cards, auto });
  }
  function placeNew(keys, keep = false) {
    if (!keep) layout(keys);
    const want = Object.fromEntries(keys.map((k) => [k, S.pos[k]]));
    for (const k of keys) delete S.pos[k];
    const boxes = Object.keys(S.pos).filter((k) => S.sizes[k]).map((k) => ({ ...S.pos[k], ...S.sizes[k] }));
    const hits = (p, s) => boxes.some((b) => p.x < b.x + b.w + 1 && b.x < p.x + s.w + 1
                                          && p.y < b.y + b.h + 1 && b.y < p.y + s.h + 1);  // (cells: a cell clear)
    const step = 2, left = Math.min(Infinity, ...boxes.map((b) => b.x));
    for (const k of keys) {
      const s = S.sizes[k], w = want[k];
      let at = w;
      ring: for (let r = 1; hits(at, s) && r < 80; r++) {  // rings of grid steps, nearest first
        const ring = [];
        for (let i = -r; i <= r; i++) for (let j = -r; j <= r; j++) if (Math.max(Math.abs(i), Math.abs(j)) === r) ring.push([i, j]);
        // Its own column first (a sideways step counts double), never left of the board's cards.
        ring.sort(([ai, aj], [bi, bj]) => Math.hypot(2 * ai, aj) - Math.hypot(2 * bi, bj));
        for (const [i, j] of ring) {
          const p = { x: w.x + i * step, y: w.y + j * step };
          if (p.x >= Math.min(left, w.x) && !hits(p, s)) { at = p; break ring; }
        }
      }
      S.pos[k] = at;
      boxes.push({ ...at, ...s });
    }
    sendPlaces(Object.fromEntries(keys.map((k) => [k, S.pos[k]])), true);
  }

  // Tidy: everything worked out first, then shown in one go and sent as one message, so no
  // half-done step ever shows (owner).
  function tidy() {
    const keys = layout(visible().map((c) => c.key)), cards = Object.fromEntries(keys.map((k) => [k, S.pos[k]]));
    const portals = foldPortals();
    S.placing = { cards, at: performance.now() };  // (Tidy's own: replaced whole)
    keys.forEach(placeCard);
    setPortals(portals, true, false);  // (redraws the wires)
    send({ op: "tidy", cards, set: portals });
    fit(true);
    S.movesUntil = performance.now() + 2500;  // where the cards came from, for a moment (owner)
    showMoves();
    setTimeout(showMoves, 2600);
  }

  // ---- wires --------------------------------------------------------------------------------
  function anchor(key, sock) {
    const p = S.pos[key], o = S.offsets[key]?.[sock];  // (drawn: pixels)
    return p && o ? { x: px(p.x) + o.x, y: px(p.y) + o.y } : null;
  }

  function curve(a, b) {
    const back = b.x < a.x + 40;
    const dx = back ? Math.max(120, Math.abs(b.x - a.x) * 0.35 + 80) : Math.max(50, (b.x - a.x) * 0.5);
    return [a, { x: a.x + dx, y: a.y }, { x: b.x - dx, y: b.y }, b];
  }
  const pathD = ([a, c1, c2, b]) => `M${a.x},${a.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${b.x},${b.y}`;
  function at([a, c1, c2, b], t) {
    const u = 1 - t, k = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
    return { x: k[0] * a.x + k[1] * c1.x + k[2] * c2.x + k[3] * b.x,
             y: k[0] * a.y + k[1] * c1.y + k[2] * c2.y + k[3] * b.y };
  }

  // Portals (owner): a wire drawn as a short stub out of its output into a lettered badge, and the
  // same badge leading into its input; the whole wire shows while a badge, either card or the
  // wire is hovered, or it is selected. Board-only: the sound still goes straight from A to B.
  // Letters by wire id, so a pair keeps its letter while others come and go.
  function portalLetters(wires) {
    const ids = wires.map((w) => w.id).filter((id) => S.ws.portals?.[id]).sort();
    return Object.fromEntries(ids.map((id, i) => [id, String.fromCharCode(65 + (i % 26)) + (i >= 26 ? Math.floor(i / 26) + 1 : "")]));
  }
  // The owner's pick (D of five drawn): a tunnel, a dark hole with rings fading inward and a lit
  // rim, the pair's letter in it, and an arrowhead on its edge for the way the sound goes: on the
  // right pointing away where the wire leaves, on the left pointing in where it arrives (owner).
  function portalEnd(w, letter, color, by, out) {
    const end = s("g", { class: "portal-end", style: `--pc:${color}`, "data-wire": w.id });
    const R = 11, arrow = out ? `M${R - 1},-4 L${R + 4.5},0 L${R - 1},4 Z` : `M${-R - 4.5},-4 L${-R + 1},0 L${-R - 4.5},4 Z`;
    end.append(s("circle", { class: "portal-disc", r: R }), s("circle", { class: "portal-ring", r: 8 }),
               s("circle", { class: "portal-ring deep", r: 5.5 }), s("path", { class: "portal-arrow", d: arrow }),
               Object.assign(s("text", { class: "portal-letter", "text-anchor": "middle", dy: "3.2" }), { textContent: letter }));
    end.addEventListener("mousedown", (e) => { e.stopPropagation(); select(w.id, e); });
    end.addEventListener("mouseenter", () => hover(w.id));
    end.addEventListener("mouseleave", () => hover(null));
    end.dataset.tip = wireTip(w, by);
    return end;
  }

  function drawWires() {
    layer.textContent = "";
    $("portal-layer").textContent = "";
    $("grads").textContent = "";
    S.wireEls = {};
    const by = byKey(), letters = portalLetters(visibleWires());
    const flagged = new Set(S.problems.filter((p) => p.severity !== "info").flatMap((p) => p.wires));
    visibleWires().forEach((w, i) => {
      const grad = s("linearGradient", { id: `g${i}`, gradientUnits: "userSpaceOnUse" });
      grad.append(s("stop", { offset: "0", "stop-color": colors[by[w.from[0]].kind] }),
                  s("stop", { offset: "1", "stop-color": colors[by[w.to[0]].kind] }));
      $("grads").append(grad);
      const g = s("g", { class: "wire-g", "data-wire": w.id });
      const cls = ["wire", w.partial && "partial", w.failed && "failed", w.ghost && "ghost", flagged.has(w.id) && "flagged",
                   S.selected === w.id && "selected"].filter(Boolean).join(" ");
      const parts = { g, grad, shadow: s("path", { class: "wire-shadow" }),
                      line: s("path", { class: cls, stroke: `url(#g${i})` }),
                      hit: s("path", { class: "wire-hit" }) };
      g.append(parts.shadow, parts.line);
      if (w.ch) {
        parts.tag = s("g", { class: "wire-tag" });
        parts.tag.append(s("circle", { r: 7, class: "wire-tag-bg", stroke: colors[by[w.to[0]].kind] }),  // (it sits at the input end)
                         Object.assign(s("text", { "text-anchor": "middle", dy: "3" }), { textContent: w.ch }));
        g.append(parts.tag);
      }
      g.append(parts.hit);
      if (letters[w.id]) {  // a portal: its stubs and two badges in the source's colour
        const color = colors[by[w.from[0]].kind];
        g.classList.add("portal");
        parts.stubs = s("path", { class: "portal-stub", stroke: `url(#g${i})` });
        parts.ends = [portalEnd(w, letters[w.id], color, by, true), portalEnd(w, letters[w.id], color, by, false)];
        g.append(parts.stubs);
        $("portal-layer").append(...parts.ends);  // above every wire: none can cover a badge (owner)
      }
      parts.hit.addEventListener("mousedown", (e) => { e.stopPropagation(); select(w.id, e); });
      parts.hit.addEventListener("mouseenter", () => hover(w.id));
      parts.hit.addEventListener("mouseleave", () => hover(null));
      parts.hit.dataset.tip = wireTip(w, by);
      layer.append(g);
      S.wireEls[w.id] = { ...parts, w };
    });
    hover(...S.hovered);  // the wires were rebuilt: a hovered card's stay lit (owner: a button press dimmed them)
    placeWires();
    if (S.pathKey) markPath();  // (rebuilt wires lose their dimming)
  }

  function wireTip(w, by) {
    const sock = (end, side) => by[end[0]][side].find((x) => x.id === end[1]) || { label: "", names: [] };
    const a = sock(w.from, "outputs"), b = sock(w.to, "inputs");
    const end = (k, sk) => ({ text: `${titleOf(by[k])} ${sk.label}`, kind: by[k].kind });
    const tip = { wire: [end(w.from[0], a), end(w.to[0], b)], rows: [] };
    if (w.ghost) {
      const away = [w.from[0], w.to[0]].map((k) => by[k]).filter((c) => c.missing).map(titleOf);
      return JSON.stringify({ ...tip, warn: `Waiting for ${away.join(" and ")}`, hint: "Delete forgets this connection" });
    }
    const ports = (sk, used) => used.map((i) => sk.names[i]).join(" + ");
    tip.rows.push(["Ports", `${ports(a, w.fromPorts)} → ${ports(b, w.toPorts)}`, true]);
    if (w.ch) tip.rows.push(["Channel", w.ch === "L" ? "left only" : "right only"]);
    if (w.failed) tip.warn = "PipeWire could not start this connection";
    return JSON.stringify({ ...tip, hint: "Click to select · right-click to insert a node · Delete disconnects" });
  }

  const PORTAL_OUT = 52;  // a portal's badge: this far out from its socket (owner: wires covered them closer)
  function placeWires(only) {
    for (const { g, grad, shadow, line, hit, tag, w } of Object.values(S.wireEls)) {
      if (only && !only.has(w.from[0]) && !only.has(w.to[0])) continue;
      const a = anchor(...w.from), b = anchor(...w.to);
      const ends = S.wireEls[w.id].ends || [];
      if (!a || !b) { g.style.display = "none"; ends.forEach((e) => { e.style.display = "none"; }); continue; }
      g.style.display = "";
      ends.forEach((e) => { e.style.display = ""; });
      const lr = w.ch === "L" ? -1 : w.ch === "R" ? 1 : 0;  // L and R meet a stereo socket apart
      if (lr) (w.chEnd === "from" ? a : b).y += 3 * lr;
      const c = curve(a, b), d = pathD(c);
      for (const p of [shadow, line, hit]) p.setAttribute("d", d);
      S.wireEls[w.id].arc = null;
      S.wireEls[w.id].c = c;
      S.wireEls[w.id].plain = d;
      Object.entries({ x1: a.x, y1: a.y, x2: b.x, y2: b.y }).forEach(([k, v]) => grad.setAttribute(k, v));
      if (tag) {
        const p = at(c, w.chEnd === "from" ? 0.12 : 0.88);
        tag.setAttribute("transform", `translate(${p.x},${p.y + 6 * lr})`);
      }
    }
    placePortals();
    placeChip();
  }

  // Every portal's badges together (owner: they overlapped, and one moved when another was
  // toggled): each wants its spot, Tidy's or a short way off its socket at the socket's own height
  // (not a channel's ±3 px, nor a place in a list); then, from the top down in a fixed order, any
  // badge nearer than PORTAL_GAP to one already placed is pushed below it. Same portals, same spots.
  const PORTAL_GAP = 30;
  function placePortals() {
    const spots = [];
    for (const we of Object.values(S.wireEls)) {
      const w = we.w, a = we.ends && anchor(...w.from), b = we.ends && anchor(...w.to);
      if (!a || !b) continue;
      // (a Tidy's badge at its spot; one whose card is being dragged follows it, beside its socket)
      const how = S.ws.portals?.[w.id], tidy = how && typeof how === "object", held = S.drag?.kind === "card" ? S.drag.key : null;
      const out = tidy && w.from[0] !== held ? how.out : null, into = tidy && w.to[0] !== held ? how.in : null;
      spots.push({ we, i: 0, sock: a, x: out ? px(out[0]) : a.x + PORTAL_OUT, y: out ? px(out[1]) : a.y, id: `${w.id}>0` },
                 { we, i: 1, sock: b, x: into ? px(into[0]) : b.x - PORTAL_OUT, y: into ? px(into[1]) : b.y, id: `${w.id}>1` });
    }
    spots.sort((p, q) => p.y - q.y || p.x - q.x || (p.id < q.id ? -1 : 1));
    // (Pushed below one, it may now touch another: again. Nearer than the gap less half a pixel:
    // exactly a gap apart, rounding read 29.999… and pushed it to the same spot forever, freezing
    // Tidy on the owner's board; and never more rounds than badges, whatever happens.)
    spots.forEach((p, i) => {
      for (let moved = true, rounds = 0; moved && rounds <= spots.length; rounds++) {
        moved = false;
        for (const q of spots.slice(0, i)) {
          if (Math.abs(p.x - q.x) < PORTAL_GAP - 0.5 && Math.abs(p.y - q.y) < PORTAL_GAP - 0.5) { p.y = q.y + PORTAL_GAP; moved = true; }
        }
      }
    });
    // A stub's own S: handles half its length (a wire's curve took one this short for a wire going
    // back and looped it; owner saw it).
    const stub = (p, q) => { const h = Math.max(4, (q.x - p.x) / 2); return [p, { x: p.x + h, y: p.y }, { x: q.x - h, y: q.y }, q]; };
    const at2 = {};
    for (const p of spots) (at2[p.we.w.id] ||= [])[p.i] = p;
    for (const [out, into] of Object.values(at2)) {
      const we = out.we;
      we.stubs.setAttribute("d", `${pathD(stub(out.sock, { x: out.x - 10, y: out.y }))} ${pathD(stub({ x: into.x + 10, y: into.y }, into.sock))}`);
      we.ends[0].setAttribute("transform", `translate(${out.x},${out.y})`);
      we.ends[1].setAttribute("transform", `translate(${into.x},${into.y})`);
    }
  }

  const wireCurve = (we) => { const a = we && anchor(...we.w.from), b = we && anchor(...we.w.to); return a && b ? curve(a, b) : null; };

  // e: the click that picked it; its toolbar then sits where that was along the wire (owner: not
  // always at its middle), and stays on that spot of the wire as cards move or the view pans.
  function select(id, e) {
    S.selected = id;
    S.selT = 0.5;
    const c = e && wireCurve(S.wireEls[id]);
    if (c) {
      const p = toWorld(e.clientX, e.clientY);
      let best = Infinity;
      for (let i = 0; i <= 200; i++) {
        const q = at(c, i / 200), d = (q.x - p.x) ** 2 + (q.y - p.y) ** 2;
        if (d < best) { best = d; S.selT = i / 200; }
      }
    }
    for (const { line, w } of Object.values(S.wireEls)) line.classList.toggle("selected", w.id === id);
    placeChip();
    hover(null);
    return !!S.wireEls[id];
  }

  function placeChip() {
    const chip = $("unlink"), we = S.wireEls[S.selected];
    $("chip-insert").hidden = !!we?.w.ghost;
    if (!we) { chip.hidden = true; return; }
    const c = wireCurve(we);
    if (!c) { chip.hidden = true; return; }
    const m = toScreen(at(c, S.selT ?? 0.5));
    chip.hidden = false;
    chip.classList.toggle("below", m.y < 110);  // (above the wire, unless the top bar is there)
    chip.style.left = `${m.x}px`; chip.style.top = `${m.y}px`;
  }

  function unlinkSelected() {
    if (!S.selected) return;
    const ghost = S.model.wires.find((w) => w.id === S.selected && w.ghost);
    send(ghost ? { op: "forget-wire", from: ghost.from, to: ghost.to } : { op: "disconnect", wire: S.selected });
    S.selected = null; placeChip();
  }

  // Hovering a card or wire quietens every other wire; a selected wire stays lit the whole time.
  function hover(id, cardKey) {
    S.hovered = [id, cardKey];
    const sel = S.wireEls[S.selected] ? S.selected : null;
    board.classList.toggle("focus", !!(id || cardKey || sel));
    for (const { g, w, ends } of Object.values(S.wireEls)) {
      const hot = w.id === id || w.id === sel || w.from[0] === cardKey || w.to[0] === cardKey;
      g.classList.toggle("hot", hot);
      ends?.forEach((e) => e.classList.toggle("hot", hot));
    }
  }

  // ---- live wires: each wire carries its sound along, like an oscilloscope in roll mode -------
  // The server sends what is new (1 value per ms, numbered by a running count, 50 frames a second).
  // As it arrives it is folded into 30 ms slots, each keeping its highest and lowest value. Each
  // slot is one point of the wire, alternately its high and its low (by the slot's own number, so
  // the pattern travels with the sound), smoothed into a wave of ~15 px that moves at a steady
  // 260 px/s from the source down the wire: a long wire shows seconds of sound, a short one the
  // latest. Up-swings are the sound's real positive peaks, down-swings its real negative ones.
  // The loop runs only while some wire has sound.
  // px/s, ms, px. 480 px/s (owner: faster, more like the real sound; was 260), the slots kept
  // ~6 px wide (13 ms), so the wave is as detailed along the wire as before.
  const ROLL_SPEED = 480, SLOT_MS = 13, SLOT_PX = ROLL_SPEED * SLOT_MS / 1000;
  // The swing eases in and out over the same EASE px at both plugs (user: the two ends looked
  // different with 18 px at the source and 70 px at the destination).
  const SLOTS = 1024, LIVE_SWING = 6, EASE = 36, WAVE_PX = 1.5;  // (swing 6 px: owner, 14 wobbled too much, 7 a touch much; a point every 1.5 px keeps a 5 px wave round)
  // Drawn this far behind the newest value: more than a frame interval (20 ms) plus its jitter, so
  // the playhead never runs into the end of what has arrived.
  const LIVE_DELAY = 50;
  S.liveSources = []; S.liveOn = true;
  const rings = {};  // "key|sock" → { hi, lo: per slot; at: which slot number each holds }
  const clock = { newest: -1, when: 0, head: 0, last: 0, sound: 0 };
  let liveRunning = false;

  function sayVisible() { send({ op: "live", visible: !document.hidden }); }

  // New source numbers. Only a new capture (fresh) has a new count: then the old history and
  // clock are forgotten; the same capture following the wires keeps them.
  function newSources(list, fresh) {
    S.liveSources = list;
    if (fresh === false) return;
    for (const k of Object.keys(rings)) delete rings[k];
    clock.newest = -1;
    drawLive();
  }

  // The wiggle follows the dominant pitch (owner: a kick drum a wide wave, a hi-hat dense ones).
  // A wire cannot draw the real waveform (at ROLL_SPEED even a 100 Hz cycle is 2.6 px), so the
  // capture sends each stretch's brightness (scope.bright: 40 Hz … 4 kHz on a log scale, 0 not
  // known) and the wave's wavelength follows it, from WAVE_WIDE px for a kick to WAVE_DENSE for a
  // hi-hat; height is still loudness. ph: the phase at each slot's centre, fixed once the slot
  // before it is complete, so the wave never shimmers as it scrolls.
  const WAVE_WIDE = 44, WAVE_DENSE = 5;  // (owner: a wider range, more detail; was 34 … 6.5; at 4 a hi-hat read as a ribbon)
  const step = (r, i) => {
    const t = r.tone[i] ? Math.max(0, Math.min(1, (r.tone[i] - 50) / 125)) : 0.45;  // kick ~52, voice ~127, hi-hat ~172
    return (2 * Math.PI * SLOT_PX) / (WAVE_WIDE * (WAVE_DENSE / WAVE_WIDE) ** t);
  };
  function feed(key, first, values, tone = 0) {
    const r = rings[key] || (rings[key] = { hi: new Int8Array(SLOTS), lo: new Int8Array(SLOTS), at: new Int32Array(SLOTS).fill(-1),
                                            tone: new Uint8Array(SLOTS), ph: new Float64Array(SLOTS) });
    for (let j = 0; j < values.length; j++) {
      const n = Math.floor((first + j) / SLOT_MS), i = n & (SLOTS - 1), v = values[j];
      if (r.at[i] !== n) {
        const p = (n - 1) & (SLOTS - 1);
        r.ph[i] = r.at[p] === n - 1 ? r.ph[p] + step(r, p) : 0;
        r.at[i] = n; r.hi[i] = 0; r.lo[i] = 0; r.tone[i] = 0;
      }
      if (v > r.hi[i]) r.hi[i] = v;
      if (v < r.lo[i]) r.lo[i] = v;
      if (tone) r.tone[i] = r.tone[i] ? (r.tone[i] + tone) >> 1 : tone;
    }
  }

  // Where the playhead should be now: LIVE_DELAY behind the newest value, moving at real time.
  const target = (now) => clock.newest + (now - clock.when) - LIVE_DELAY;

  function liveFrame(buf) {
    try {
      if (buf.byteLength < 4) return;
      const total = new DataView(buf).getUint32(0, true), bytes = new Int8Array(buf);
      let sound = false;
      for (let i = 4; i + 2 <= bytes.length;) {
        const src = S.liveSources[bytes[i] & 0xff], n = bytes[i + 1] & 0xff;
        if (i + 3 + n > bytes.length) break;
        if (src) { feed(src.join("|"), total - n, bytes.subarray(i + 2, i + 2 + n), bytes[i + 2 + n] & 0xff); sound = true; }
        i += 3 + n;  // (index, count, the values, their brightness: scope.encode)
      }
      const now = performance.now();
      clock.newest = total - 1;  // even a silent frame keeps the clock in time
      clock.when = now;
      if (sound) clock.sound = now;
      if (!liveRunning && sound && S.mode !== "preview") {
        clock.head = target(now);  // start exactly in place: no catching up, no lurch
        clock.last = now;
        liveRunning = true;
        requestAnimationFrame(tick);
      }
    } catch (e) { /* a bad frame is dropped; the wires carry on */ }
  }

  function tick(now) {
    const dt = Math.min(100, now - clock.last), fresh = now - clock.when < 300;
    clock.last = now;
    clock.head += dt;  // real time…
    const off = target(now) - clock.head;
    if (Math.abs(off) > 250) clock.head += off;  // lost the beat (a stall): jump back in place
    else if (Math.abs(off) > 15) clock.head += (off - Math.sign(off) * 15) * 0.02;  // …nudged only past ±15 ms
    if (fresh) clock.head = Math.min(clock.head, clock.newest);  // never ahead of what arrived
    const any = drawLive();
    // On while a wave is in sight or sound arrived lately (a new burst reaches the playhead ~50 ms
    // after it arrives); off in silence, so a quiet board costs nothing.
    if (any || now - clock.sound < 500) requestAnimationFrame(tick);
    else liveRunning = false;
  }

  // A wire's length table, for placing points by distance along it: once per wire position.
  function arc(we) {
    const ts = [0], ds = [0];
    let prev = at(we.c, 0);
    for (let i = 1; i <= 48; i++) {
      const p = at(we.c, i / 48);
      ts.push(i / 48); ds.push(ds[i - 1] + Math.hypot(p.x - prev.x, p.y - prev.y)); prev = p;
    }
    return { ts, ds, len: ds[48], in: Math.min(EASE, ds[48] / 2), out: Math.min(EASE, ds[48] / 2) };
  }

  // Point, unit normal and swing allowance at a distance along the wire.
  function along(we, d) {
    const A = we.arc;
    let lo = 0, hi = 48;  // the last table entry at or before d (a binary search: called per pixel)
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (A.ds[m] < d) lo = m; else hi = m - 1; }
    const k = Math.min(47, lo);
    const t = A.ts[k] + (A.ts[k + 1] - A.ts[k]) * ((d - A.ds[k]) / ((A.ds[k + 1] - A.ds[k]) || 1));
    const [a, c1, c2, b] = we.c, u = 1 - t, p = at(we.c, t);
    const dx = 3 * u * u * (c1.x - a.x) + 6 * u * t * (c2.x - c1.x) + 3 * t * t * (b.x - c2.x);
    const dy = 3 * u * u * (c1.y - a.y) + 6 * u * t * (c2.y - c1.y) + 3 * t * t * (b.y - c2.y);
    const l = Math.hypot(dx, dy) || 1, up = (x, w) => Math.sin(Math.min(1, Math.max(0, x) / w) * Math.PI / 2) ** 2;
    return [p.x, p.y, -dy / l, dx / l, up(d, A.in) * up(A.len - d, A.out)];
  }

  function wave(we, r) {
    we.arc = we.arc || arc(we);
    const len = we.arc.len, slot = clock.head / SLOT_MS;
    const pts = [], clipped = [];
    let loud = 0;
    const put = (d, v, clip) => {
      if (d < 0 || d > len) return;
      const [x, y, nx, ny, ease] = along(we, d), a = Math.max(-1, Math.min(1, v / 126)) * ease * LIVE_SWING;
      if (clip) clipped.push(pts.length);
      pts.push([x + nx * a, y + ny * a]);
    };
    // A slot's loudness (its strongest value), phase and clipping; nothing for a slot not heard.
    const at = (n) => {
      const i = n & (SLOTS - 1);
      if (r.at[i] !== n) return null;
      const peak = Math.max(r.hi[i], -r.lo[i]);
      return { env: peak, ph: r.ph[i], next: r.ph[i] + step(r, i), clip: peak >= 127 };
    };
    // Instant (owner): the newest sound along the whole wire, its loudness the height everywhere,
    // its pitch the wavelength, the wiggle still moving down the wire at the roll speed: the wire
    // answers at once instead of the sound taking a second to travel its length.
    if (S.liveInstant) {
      const n = Math.floor(slot - 0.5), a = at(n), b = at(n - 1), c = a || b;
      const env = Math.max(a ? a.env : 0, b ? b.env : 0);  // (two slots: no flicker between them)
      if (!c || env < 2) return null;
      const k = step(r, n & (SLOTS - 1)) / SLOT_PX, ph = (clock.head / SLOT_MS) * SLOT_PX * k;
      put(0, 0);
      for (let d = WAVE_PX / 2; d <= len; d += WAVE_PX) put(d, env * Math.sin(ph - d * k), c.clip);
      put(len, 0);
      loud = env;
    } else {
    // Points every WAVE_PX along the wire, each between two slot centres: loudness and phase
    // eased from one to the next, so the line is smooth whatever the pitch.
    put(0, 0);
    for (let d = WAVE_PX / 2; d <= len; d += WAVE_PX) {
      const s = slot - d / SLOT_PX - 0.5, n = Math.floor(s), t = s - n, a = at(n), b = at(n + 1);
      if (!a && !b) { put(d, 0); continue; }
      const env = (a ? a.env : 0) * (1 - t) + (b ? b.env : 0) * t;
      const ph = a ? a.ph + (b ? b.ph - a.ph : a.next - a.ph) * t : b.ph - (1 - t) * Math.PI;
      loud = Math.max(loud, env);
      put(d, env * Math.sin(ph), (t < 0.5 ? a : b)?.clip);
    }
    put(len, 0);
    }
    if (loud < 2 || pts.length < 3) return null;
    const f = (p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    // Clipping (owner: a smooth red glow, not flecks or steps): the wave drawn again in red
    // through a gradient along the wire, each point's red as strong as it is near a full-scale
    // point (a bell ~24 px either side): the red rises and falls gently, without seams.
    let clip = null;
    if (clipped.length) {
      const a = pts[0], b = pts[pts.length - 1], dx = b[0] - a[0], dy = b[1] - a[1], len2 = dx * dx + dy * dy || 1;
      const near = new Array(pts.length).fill(Infinity);  // points to the nearest clipped one: two passes
      for (const c of clipped) near[c] = 0;
      for (let k = 1; k < near.length; k++) near[k] = Math.min(near[k], near[k - 1] + 1);
      for (let k = near.length - 2; k >= 0; k--) near[k] = Math.min(near[k], near[k + 1] + 1);
      // A stop every 6 px (the bell is ~24 px: as smooth as one per point), and none inside a run
      // of no red but its two ends: one per point, a long wire had ~400 stops set every frame
      // (traced: the glow doubled the page's and the GPU's work).
      const sigma = 24 / WAVE_PX, every = Math.round(6 / WAVE_PX), red = (k) => Math.exp(-((near[k] / sigma) ** 2));
      const ks = [];
      for (let k = 0; k < pts.length; k += every) ks.push(k);
      if (ks.at(-1) !== pts.length - 1) ks.push(pts.length - 1);
      const stops = ks.filter((k, i) => red(k) > 0.004 || red(ks[i - 1] ?? k) > 0.004 || red(ks[i + 1] ?? k) > 0.004)
        .map((k) => [Math.min(1, Math.max(0, ((pts[k][0] - a[0]) * dx + (pts[k][1] - a[1]) * dy) / len2)), red(k)])
        .sort((p, q) => p[0] - q[0]);
      clip = { a, b, stops };
    }
    let d = `M${f(pts[0])}`;  // smooth: quadratic curves through the midpoints
    for (let i = 1; i < pts.length - 1; i++) d += ` Q${f(pts[i])} ${f([(pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2])}`;
    return { d: `${d} L${f(pts[pts.length - 1])}`, clip };
  }

  function drawLive() {
    let any = false;
    try {
      for (const we of Object.values(S.wireEls)) {
        const shown = !we.ends || we.g.classList.contains("hot");  // a portal's wave only while its wire shows
        const r = S.liveOn && !we.w.ghost && we.c && shown ? rings[we.w.from.join("|")] : null;
        const shape = r ? wave(we, r) : null;
        if (!shape) {
          if (we.live) { we.live = false; we.line.setAttribute("d", we.plain); we.shadow.setAttribute("d", we.plain); we.clip?.forEach((el) => el.remove()); we.clip = null; }
          continue;
        }
        // A live wave draws no shadow: on the dark board it hardly shows, and drawing it again
        // every frame doubled the repaint (owner: live wires stuttered; traced 397 → 200 ms/s).
        if (!we.live) we.shadow.setAttribute("d", "");
        any = we.live = true;
        we.line.setAttribute("d", shape.d);
        if (shape.clip && !we.clip) {
          const id = `${we.grad.id}c`, grad = s("linearGradient", { id, gradientUnits: "userSpaceOnUse" });
          $("grads").append(grad);
          we.clip = [s("path", { class: "live-clip", stroke: `url(#${id})` }), grad];
          we.g.insertBefore(we.clip[0], we.hit);
        }
        if (we.clip) {
          const [path, grad] = we.clip, c = shape.clip;
          if (c) {
            path.setAttribute("d", shape.d);
            for (const [k, v] of Object.entries({ x1: c.a[0], y1: c.a[1], x2: c.b[0], y2: c.b[1] })) grad.setAttribute(k, v);
            // The same stops kept and moved, an attribute set only when it changed: a stop changed
            // restyles the whole page (Chrome; traced), so a still glow (Instant) costs nothing.
            while (grad.childElementCount < c.stops.length) grad.append(s("stop", { "stop-color": "#ff5a4e" }));
            while (grad.childElementCount > c.stops.length) grad.lastChild.remove();
            const set = (el, k, v) => { if (el.getAttribute(k) !== v) el.setAttribute(k, v); };
            c.stops.forEach(([t, o], k) => { set(grad.children[k], "offset", t.toFixed(4)); set(grad.children[k], "stop-opacity", o.toFixed(3)); });
          } else { we.clip.forEach((el) => el.remove()); we.clip = null; }
        }
      }
    } catch (e) { S.errors.push(`live: ${e}`); }
    return any;
  }

  function toggleLive() {
    S.liveOn = !S.liveOn;
    showWires();
    send({ op: "live-setting", on: S.liveOn });
    if (!S.liveOn) { for (const k of Object.keys(rings)) delete rings[k]; drawLive(); }
  }
  // The wires: Still, Flowing or Instant, one choice (owner: Instant alone did not say it needs
  // Animate). Still is Animate off; the Instant setting is kept for the next time it animates.
  const WIRES = {
    still: { label: "Still", desc: "Plain wires", icon: "still" },
    flowing: { label: "Flowing", desc: "Sound travels along each wire", icon: "wave" },
    instant: { label: "Instant", desc: "The whole wire shows the sound now", icon: "bolt" },
  };
  const wiresMode = () => (!S.liveOn ? "still" : S.liveInstant ? "instant" : "flowing");
  function showWires() {
    const m = WIRES[wiresMode()];
    $("wires-mode").textContent = m.label;
    $("wires-icon").innerHTML = MI[m.icon];
  }
  function setWires(mode) {
    const on = mode !== "still", instant = mode === "instant";
    if (on && instant !== !!S.liveInstant) { S.liveInstant = instant; send({ op: "live-instant", on: instant }); }
    if (on !== !!S.liveOn) toggleLive(); else { showWires(); drawLive(); }
  }
  function wiresMenu(e) {
    const r = e.currentTarget.getBoundingClientRect(), now = wiresMode();
    openMenu(r.left, r.bottom + 6, Object.entries(WIRES).map(([k, m]) => ({ ...m, on: k === now, action: () => setWires(k) })));
  }
  // Tidy's one option: Portals (the next Tidy follows it; saved with the board).
  // Tidy's own option: toggling it tidies again (owner: turned off, its portals stayed).
  function setTidyPortals(on, retidy = true) {
    S.ws.tidy_portals = on;
    send({ op: "tidy-portals", on });
    if (retidy) tidy();
  }
  function tidyMenu(e) {
    const r = e.currentTarget.closest(".split").getBoundingClientRect(), on = S.ws.tidy_portals === true;
    openMenu(r.left, r.bottom + 6, [{ label: "Portals", desc: "A long board folds into rows; the wires between them become lettered portals",
      icon: on ? "boxon" : "boxoff", on, action: () => setTidyPortals(!on) }]);
  }

  // ---- view: pan, zoom, fit -----------------------------------------------------------------
  let viewTimer = null;
  function applyView(save = true) {
    // Whole pixels: at a fractional offset Chrome snaps borders to pixels but not what is inside
    // them, so an icon sat up to half a pixel off its button's centre (owner noticed). The view
    // itself keeps its exact numbers; only what is drawn is rounded (the grid with it).
    const { z } = S.view, x = Math.round(S.view.x), y = Math.round(S.view.y);
    world.style.transform = `translate(${x}px, ${y}px) scale(${z})`;
    const gs = 28 * z, mod = (v) => ((v + 64) % gs + gs) % gs;  // the grid's dots line up with x, y (its box starts at −64)
    if (grid.dataset.gs !== String(gs)) { grid.style.backgroundSize = `${gs}px ${gs}px`; grid.dataset.gs = gs; }  // (a zoom repaints it; a pan does not)
    grid.style.transform = `translate3d(${mod(x)}px, ${mod(y)}px, 0)`;
    placeChip();
    if (save && S.mode !== "preview") {
      clearTimeout(viewTimer);
      viewTimer = setTimeout(() => send({ op: "view", view: S.view }), 600);
    }
  }

  function zoomAt(sx, sy, z) {
    closeMenu();  // a menu stays where it opened; zoomed, it no longer points at what it was for (owner)
    z = Math.min(1.75, Math.max(0.35, z));
    const p = toWorld(sx, sy);
    S.view = { x: sx - p.x * z, y: sy - p.y * z, z };
    applyView();
  }

  function bounds(keys) {
    const boxes = keys.filter((k) => S.pos[k] && S.sizes[k])
      .map((k) => ({ x: px(S.pos[k].x), y: px(S.pos[k].y), w: px(S.sizes[k].w), h: px(S.sizes[k].h) }));  // (pixels: the view)
    if (!boxes.length) return null;
    const x0 = Math.min(...boxes.map((b) => b.x)), y0 = Math.min(...boxes.map((b) => b.y));
    const x1 = Math.max(...boxes.map((b) => b.x + b.w)), y1 = Math.max(...boxes.map((b) => b.y + b.h));
    return { x0, y0, x1, y1 };
  }

  // below: room kept clear at the bottom (a docked panel's height).
  function fit(animate = true, keys = visible().map((c) => c.key), maxZ = 1.1, below = 0) {
    const b = bounds(keys);
    if (!b) return;
    const top = 70, pad = 50, W = innerWidth, H = innerHeight - below;
    const z = Math.min(maxZ, Math.max(0.35, Math.min((W - 2 * pad) / (b.x1 - b.x0), (H - top - pad) / (b.y1 - b.y0))));
    const to = { z, x: (W - (b.x1 - b.x0) * z) / 2 - b.x0 * z, y: top + (H - top - pad - (b.y1 - b.y0) * z) / 2 - b.y0 * z };
    if (!animate || S.mode === "preview") { S.view = to; applyView(); return; }
    const from = { ...S.view }, t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 260), e = 1 - (1 - k) ** 3;
      S.view = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, z: from.z + (to.z - from.z) * e };
      applyView(k === 1);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---- pointer: drag cards, draw wires, pan -------------------------------------------------
  function onDown(e) {
    if (e.button === 2) return;
    // The middle button pans: Chrome's own use of it (autoscroll, which took the mouse from the
    // page, and the selection paste on release) made a middle drag stop and stutter (owner).
    if (e.button === 1) e.preventDefault();
    closeMenu();
    if (!S.iface) closeEngine();  // popovers, like the menus: any press on the board closes them (owner); an interface's panel is docked, it stays
    toggleProblems(false);
    const sock = e.target.closest(".sock"), head = e.target.closest(".card header");
    if (sock && e.button === 0) {
      // From an input that has a wire: pick that wire up and move it (Shift: a new one instead).
      // An input with none starts nothing (owner): connections are drawn from outputs.
      const into = sock.dataset.side === "in" && wireInto(sock.dataset.key, sock.dataset.sock);
      if (sock.dataset.side === "in" && !into) return;
      return into && !e.shiftKey ? startMove(into, e) : startWire(sock, e);
    }
    if (head && e.button === 0 && !e.target.isContentEditable) {
      const key = head.parentElement.dataset.key, p = toWorld(e.clientX, e.clientY);
      S.drag = { kind: "card", key, dx: p.x - px(S.pos[key].x), dy: p.y - px(S.pos[key].y), moved: false };
      S.els[key].classList.add("dragging");
      return;
    }
    if (e.target.closest(".card")) return;
    select(null);
    S.drag = { kind: "pan", sx: e.clientX, sy: e.clientY, vx: S.view.x, vy: S.view.y };
    board.classList.add("panning");
  }

  function startWire(sock, e) {
    const { key, sock: id, side } = sock.dataset;
    S.drag = { kind: "wire", key, sock: id, side };
    for (const el of document.querySelectorAll(".sock")) {
      el.classList.toggle("dim", el.dataset.side === side);
    }
    S.drag.path = s("path", { class: "wire pending" });
    layer.append(S.drag.path);
    moveWire(e.clientX, e.clientY);
  }

  // A stereo output dropped on a mono input takes one channel, L then R (graph.next_side). While a
  // wire from a stereo output is held, a tag beside the pointer says what it carries: L or R, or
  // "L R" over a stereo input (both go). Held from a mono input, it shows over a stereo output.
  const sockOf = (key, id, side) => byKey()[key]?.[side === "out" ? "outputs" : "inputs"].find((s) => s.id === id);
  function nextSide(src) {
    const alone = S.model.wires.filter((w) => w.from[0] === src[0] && w.from[1] === src[1] && w.fromPorts?.length === 1).map((w) => w.fromPorts[0]);
    return alone.includes(0) && !alone.includes(1) ? 1 : 0;
  }
  function showSide(target, sx, sy) {
    const d = S.drag, el = $("drag-side"), over = target && [target.dataset.key, target.dataset.sock];
    const [out, inn] = d.side === "out" ? [[d.key, d.sock], over] : [over, [d.key, d.sock]];
    const wide = out && sockOf(...out, "out")?.ports.length === 2, ins = inn && sockOf(...inn, "in")?.ports.length;
    // Moving a wire: it keeps its channels (L stays L), so the tag says what it carries.
    const carries = d.moving && (d.moving.fromPorts?.length === 1 ? "LR"[d.moving.fromPorts[0]] : "L R");
    const text = !wide ? "" : carries || (ins === 2 ? "L R" : ins === 1 || (d.side === "out" && !over) ? "LR"[nextSide(out)] : "");
    el.hidden = !text;
    el.textContent = text;
    el.classList.toggle("both", text.length > 1);
    el.style.transform = `translate(${sx + 14}px, ${sy + 12}px)`;  // below-right: clear of the wire
  }

  function moveWire(sx, sy) {
    const d = S.drag, from = anchor(d.key, d.sock);
    const target = document.elementFromPoint(sx, sy)?.closest(".sock");
    for (const el of document.querySelectorAll(".sock.target")) el.classList.remove("target");
    const ok = target && target.dataset.side !== d.side;
    if (ok) target.classList.add("target");
    showSide(ok ? target : null, sx, sy);
    const end = ok ? anchor(target.dataset.key, target.dataset.sock) : toWorld(sx, sy);
    d.at = { x: sx, y: sy };
    const [a, b] = d.side === "out" ? [from, end] : [end, from];
    d.path.setAttribute("d", pathD(curve(a, b)));
    d.target = ok ? target : null;
  }

  function onMove(e) {
    const d = S.drag;
    if (!d) return tip(e);
    if (d.kind === "pan") {
      S.view = { ...S.view, x: d.vx + e.clientX - d.sx, y: d.vy + e.clientY - d.sy };
      applyView();
    } else if (d.kind === "card") {
      // The card follows the mouse; an outline shows the cell it drops into (a whole cell, always).
      const p = toWorld(e.clientX, e.clientY), free = { x: (p.x - d.dx) / GRID, y: (p.y - d.dy) / GRID };
      S.pos[d.key] = free;
      d.to = { x: Math.round(free.x), y: Math.round(free.y) };
      d.moved = true;
      placeCard(d.key);
      placeWires(new Set([d.key]));
      showLanding(d);
    } else if (d.kind === "wire") moveWire(e.clientX, e.clientY);
  }

  function showLanding(d) {
    const el = $("landing"), size = S.sizes[d.key], at = S.pos[d.key];
    el.hidden = at.x === d.to.x && at.y === d.to.y;
    Object.assign(el.style, { transform: `translate(${px(d.to.x)}px, ${px(d.to.y)}px)`,
                              width: `${px(size.w)}px`, height: `${px(size.h)}px` });
    el.className = `landing k-${byKey()[d.key].kind}`;
  }

  function onUp() {
    const d = S.drag;
    if (!d) return;
    S.drag = null;
    board.classList.remove("panning");
    // A click on empty board (pressed, let go, the view not moved) closes an open effect panel
    // (owner). Panning keeps it open; a card or a wire never gets here.
    if (d.kind === "pan" && Math.hypot(S.view.x - d.vx, S.view.y - d.vy) < 3) { EQ.close(); DYN.close(); DIAG.close(); closePath(); closeEngine(); }  // a click on the board (owner)
    if (d.kind === "card") {
      S.els[d.key]?.classList.remove("dragging");
      $("landing").hidden = true;
      if (d.moved) {
        S.pos[d.key] = d.to;
        // A Tidy's badges on the moved card's own wires: beside its sockets from now on (owner: they
        // stayed behind when the card moved); the other end keeps its spot.
        const moved = {};
        for (const [id, how] of Object.entries(S.ws.portals || {})) {
          const w = typeof how === "object" && S.model.wires.find((x) => x.id === id);
          if (w && (w.from[0] === d.key || w.to[0] === d.key))
            moved[id] = { ...how, out: w.from[0] === d.key ? null : how.out, in: w.to[0] === d.key ? null : how.in };
        }
        if (Object.keys(moved).length) setPortals(moved);
        placeCard(d.key);
        placeWires(new Set([d.key]));
        sendPlaces({ [d.key]: S.pos[d.key] });
      }
    }
    if (d.kind === "wire") finishWire(d);
    if (S.queued) { const q = S.queued; S.queued = null; load(q); }
  }

  // The wire to pick up from an input: the selected one if it ends here, else the latest.
  function wireInto(key, sock) {
    const here = visibleWires().filter((w) => w.to[0] === key && w.to[1] === sock && !w.ghost);
    return here.find((w) => w.id === S.selected) || here.at(-1) || null;
  }

  function startMove(w, e) {
    const el = S.wireEls[w.id];
    if (el) el.g.style.visibility = "hidden";  // it travels with the mouse instead
    startWire(document.querySelector(`.sock[data-key="${CSS.escape(w.from[0])}"][data-sock="${CSS.escape(w.from[1])}"]`), e);
    S.drag.moving = w;
    moveWire(e.clientX, e.clientY);  // the tag now says what this wire carries
  }

  function cancelWire() {
    const d = S.drag;
    if (d?.kind !== "wire") return;
    S.drag = null;
    d.target = null;
    d.cancelled = true;
    finishWire(d);
  }

  function finishWire(d) {
    $("drag-side").hidden = true;
    const keep = !d.target && !d.moving && !d.cancelled;  // may stay up while a drop menu is open
    if (!keep) d.path.remove();
    for (const el of document.querySelectorAll(".sock")) el.classList.remove("dim", "target");
    if (d.moving) {
      const el = S.wireEls[d.moving.id];
      if (el) el.g.style.visibility = "";  // back until the server says it moved
      const to = d.target && [d.target.dataset.key, d.target.dataset.sock];
      if (to && to.join("|") !== d.moving.to.join("|")) send({ op: "move-wire", wire: d.moving.id, to });
      return;  // dropped elsewhere: it stays where it was
    }
    if (!d.target) {
      // Let go on empty board (not on a card, the bar, a panel): offer something to connect to.
      const under = d.at && document.elementFromPoint(d.at.x, d.at.y);
      if (!d.cancelled && d.at && !under?.closest(".card, #bar, .panel, .problems-btn, .changes, .chip")) {
        S.menuAt = toWorld(d.at.x, d.at.y);
        dropMenu(d.at.x, d.at.y, d);
        S.menuAt = toWorld(d.at.x, d.at.y);
        S.dropPath = d.path;  // the wire stays drawn to where it was let go until the menu closes
      } else d.path.remove();
      return;
    }
    const here = [d.key, d.sock], there = [d.target.dataset.key, d.target.dataset.sock];
    const [from, to] = d.side === "out" ? [here, there] : [there, here];
    const id = `${from.join(":")}>${to.join(":")}`;
    if (S.model.wires.some((w) => w.id === id)) return toast("Already connected.", "info");
    send({ op: "connect", from, to });
  }

  // ---- menus: cascading, like the desktop's own right-click menu -----------------------------
  // An item: {label, icon, hint, off, action, sub: [items], forget} · {sep: true} · {head: "text"}
  const MI = {
    diag: '<circle cx="6" cy="6" r="4"/><path d="M9 9l3.5 3.5M4 6h1l1-1.5 1 3 1-1.5"/>',
    waveform: '<path d="M1.5 7c1.2-4 2.3-4 3.5 0s2.3 4 3.5 0 2.3-4 3.5 0"/>',
    stereo: '<path d="M1.5 11.5a5.5 5.5 0 0 1 11 0"/><path d="M7 11.5V4M7 11.5 3.5 6M7 11.5l3.5-5.5"/>',
    counter: '<rect x="1.5" y="3" width="11" height="8" rx="1.5"/><path d="M4.5 5.5v3M7 5.5v3M9.5 5.5v3"/>',
    meter: '<path d="M2 12.5h10"/><path d="M3.5 12V8M6 12V4M8.5 12V6M11 12V2.5"/>',
    spectrum: '<path d="M1.5 12.5h11"/><path d="M1.5 10.5c1.5-6 2.5-7 3.5-4s1.8 2 3 0.5 2.5-5 4.5-3.5"/>',
    add: '<path d="M7 2.5v9M2.5 7h9"/>',
    fit: '<path d="M2 5V2h3M9 2h3v3M12 9v3H9M5 12H2V9"/>',
    still: '<path d="M1.5 7h11"/>',
    wave: '<path d="M1.5 7h2l1.5-4 2 8 2-6 1.2 2h2.3"/>',
    bolt: '<path d="M8 1.5 3 8h4l-1 4.5L11 6H7z"/>',
    boxon: '<rect x="2" y="2" width="10" height="10" rx="2.5"/><path d="M4.6 7.2l1.7 1.7 3.2-3.5"/>',
    boxoff: '<rect x="2" y="2" width="10" height="10" rx="2.5"/>',
    tidy: '<rect x="1.5" y="3" width="3" height="8" rx="1"/><rect x="5.5" y="5" width="3" height="4" rx="1"/><rect x="9.5" y="2" width="3" height="10" rx="1"/>',
    gain: '<circle cx="7" cy="7" r="5"/><path d="M7 7l2.6-2.6"/>',
    eq: '<path d="M1.5 9c2 0 2.5-5 4.5-5s2.5 7 4.5 7 1.5-3 2.5-3"/>',
    limiter: '<path d="M1.5 4h11" stroke-dasharray="1.6 1.4"/><path d="M1.5 11l2.5-4 2 2.5 2-6.5h2.5l2-1"/>',
    compressor: '<path d="M1.5 12.5L7 7l5.5-2"/><path d="M7 7l5.5-5.5" stroke-dasharray="1.4 1.4"/>',
    gate: '<path d="M1.5 11h3.5V4h4v7h3.5"/>',
    denoise: '<path d="M1.5 7h1l1-2 1 4 1-3 1 2h5.5"/><path d="M9 4.5l3 5M12 4.5l-3 5"/>',
    deesser: '<path d="M9.5 3.5c-.8-.7-1.6-1-2.6-1-1.6 0-2.7.9-2.7 2.2 0 2.9 5.8 1.7 5.8 5 0 1.4-1.2 2.3-2.9 2.3-1.1 0-2.1-.4-2.8-1.1"/><path d="M1.5 7h11" stroke-dasharray="1.3 1.5"/>',
    plugin: '<rect x="2" y="4" width="10" height="8" rx="1.5"/><path d="M5 4V2.2h4V4"/>',
    virtual: '<rect x="2.5" y="2.5" width="9" height="9" rx="2" stroke-dasharray="2.2 1.6"/>',
    rename: '<path d="M2.5 11.5l.6-2.6 6.4-6.4 2 2-6.4 6.4z"/>',
    hide: '<path d="M1.5 7s2-3.8 5.5-3.8S12.5 7 12.5 7 10.5 10.8 7 10.8 1.5 7 1.5 7z"/><path d="M2.5 12l9-10"/>',
    names: '<circle cx="7" cy="7" r="5"/><path d="M7 4.2V7l2 1.3"/>',
    check: '<path d="M3 7.5l2.6 2.6L11 4.5"/>',
    save: '<path d="M2.5 2.5h7l2 2v7h-9z"/><path d="M4.8 2.5v3h4v-3M4.8 11.5V8.5h4.4v3"/>',
    trash: '<path d="M2.5 4h9M5.5 4V2.5h3V4M3.6 4l.7 7.5h5.4l.7-7.5"/>',
    portal: '<circle cx="4" cy="7" r="2.5"/><circle cx="10" cy="7" r="2.5" stroke-dasharray="1.3 1.2"/><path d="M6.5 7h1"/>',
    unlink: '<path d="M6 4.5L7.5 3a2.5 2.5 0 0 1 3.5 3.5L9.5 8"/><path d="M8 9.5L6.5 11A2.5 2.5 0 0 1 3 7.5L4.5 6"/><path d="M2 2l1.8 1.8M12 12l-1.8-1.8"/>',
    revert: '<path d="M4.5 3.5L2 6l2.5 2.5"/><path d="M2 6h6.5a3 3 0 0 1 0 6H6"/>',
    ...ICON,
  };
  const CHEVRON = '<svg class="chev" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 3.5L9 7l-3.5 3.5"/></svg>';
  const menus = [];
  let subTimer = null;

  // One popover at a time (owner): a menu (the top bar's dropdowns or a right-click's), the Audio
  // engine panel and the problems list each close the others as they open; the last one stays.
  function openMenu(x, y, items) {
    closeMenu();
    if (!S.iface) closeEngine();  // (an interface's panel is docked, not a popover)
    toggleProblems(false);
    hideTip();
    S.menuAt = toWorld(x, y);  // where something added from this menu will appear
    showLevel(items, { x, y }, null).focus({ preventScroll: true });
  }

  function showLevel(items, at, parentRow) {
    const level = menus.length;
    const menu = h("div", { class: "menu", role: "menu", tabindex: "-1" });
    for (const it of items) menu.append(menuRow(it, level));
    menu.addEventListener("keydown", (e) => { menu.classList.add("keys"); menuKeys(e, level); });
    menu.addEventListener("mousemove", () => menu.classList.remove("keys"));
    document.body.append(menu);
    menus.push(menu);
    const r = menu.getBoundingClientRect();
    let { x, y } = at || {};
    if (parentRow) {
      const pr = parentRow.getBoundingClientRect(), mr = menus[level - 1].getBoundingClientRect();
      // Keep going the way the cascade already turned; turn only when there is no room.
      const left = menus[level - 1].dataset.dir === "left";
      const fitsRight = mr.right + 2 + r.width <= innerWidth - 8, fitsLeft = mr.left - r.width - 2 >= 8;
      const goLeft = left ? fitsLeft || !fitsRight : !fitsRight;
      x = goLeft ? mr.left - r.width - 2 : mr.right + 2;
      y = pr.top - 6;
      menu.dataset.dir = goLeft ? "left" : "right";
    }
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - r.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - r.height - 8))}px`;
    return menu;
  }

  function menuRow(it, level) {
    if (it.sep) return h("hr");
    if (it.head) return h("div", { class: "head", text: it.head });
    const sub = it.sub, off = it.off || (sub && !sub.length);
    const row = h("div", { class: `item${off ? " off" : ""}${sub ? " has-sub" : ""}${it.danger ? " danger" : ""}${it.desc ? " tall" : ""}${it.on ? " on" : ""}`,
                           role: it.on == null ? "menuitem" : "menuitemcheckbox", "aria-checked": it.on == null ? null : String(!!it.on),
                           tabindex: off ? null : "-1", "aria-haspopup": sub ? "menu" : null },
                  h("span", { class: "label" }, it.label, it.desc ? h("span", { class: "desc", text: it.desc }) : null),
                  it.hint ? h("small", { text: it.hint }) : null);
    row.insertAdjacentHTML("afterbegin", `<svg class="mi" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">${MI[it.icon] || ""}</svg>`);
    if (sub) row.insertAdjacentHTML("beforeend", CHEVRON);
    if (it.forget) {
      const x = h("button", { class: "forget", type: "button", title: "Forget this name", text: "✕" });
      x.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); it.forget(); });
      row.append(x);
    }
    row.addEventListener("mouseenter", () => {
      clearTimeout(subTimer);
      if (!off) row.focus({ preventScroll: true });
      // A short pause forgives a mouse cutting across rows on its way into an open submenu.
      subTimer = setTimeout(() => openSub(row, off ? null : sub, level), 140);
    });
    row.addEventListener("click", (e) => {
      e.stopPropagation();
      if (off) return;
      if (sub) { clearTimeout(subTimer); openSub(row, sub, level); return; }
      closeMenu();
      it.action();
    });
    return row;
  }

  function openSub(row, sub, level) {
    if (menus[level + 1] && row.classList.contains("open")) return;
    closeFrom(level + 1);
    if (!sub) return;
    row.classList.add("open");
    return showLevel(sub, null, row);
  }

  function closeFrom(level) {
    for (const m of menus.splice(level)) m.remove();
    menus[level - 1]?.querySelectorAll(".item.open").forEach((r) => r.classList.remove("open"));
  }
  const closeMenu = () => {
    clearTimeout(subTimer);
    closeFrom(0);
    S.dropPath?.remove();  // a dropped wire's dashed path goes with its menu
    S.dropPath = null;
  };

  function menuKeys(e, level) {
    const items = [...menus[level].querySelectorAll(".item:not(.off)")];
    const at = items.indexOf(document.activeElement);
    const go = (i) => items[(i + items.length) % items.length]?.focus();
    const row = items[at];
    if (e.key === "ArrowDown") go(at + 1);
    else if (e.key === "ArrowUp") go(at < 0 ? -1 : at - 1);
    else if (e.key === "ArrowRight" && row?.classList.contains("has-sub")) {
      row.click();
      menus[level + 1]?.querySelector(".item:not(.off)")?.focus();
    } else if (e.key === "ArrowLeft" && level > 0) {
      const parent = menus[level - 1].querySelector(".item.open");
      closeFrom(level);
      parent?.focus();
    } else if (e.key === "Enter" || e.key === " ") row?.click();
    else if (e.key === "Escape") closeMenu();
    else return;
    e.preventDefault();
    e.stopPropagation();
  }

  // ---- what the menus offer -----------------------------------------------------------------
  const iconOf = (c) => (ICON[c.role] ? c.role : c.kind === "app" ? "app" : c.kind === "effect" ? "effect" : "virtual");

  function bringBack(cards, empty, ctx = {}) {
    // Devices that exist but are not on the board: adding them shows them where you clicked, and
    // from a dropped wire, connected to it (only the ones with a socket on the right side).
    const conn = ctx.connect, side = conn?.from ? "inputs" : "outputs";
    if (conn) cards = cards.filter((c) => c[side].length);
    if (!cards.length) return [{ label: conn ? "None that could take this wire" : empty, off: true }];
    const back = (c) => {
      showAt(c.key);
      if (!conn) return;
      const sock = c[side][0].id;
      send(conn.from ? { op: "connect", from: conn.from, to: [c.key, sock] } : { op: "connect", from: [c.key, sock], to: conn.to });
    };
    return [{ head: "Not on the board" },
            ...cards.map((c) => ({ label: titleOf(c), icon: iconOf(c), action: () => back(c) }))];
  }

  function addNodeItems(ctx = {}) {
    const hidden = ctx.insert ? [] : S.model.cards.filter(isHidden), of = (k) => hidden.filter((c) => c.kind === k);
    const effects = of("effect");
    const physical = ctx.insert ? [] : [{ label: "Physical", icon: "interface",
      sub: bringBack(of("hardware"), "All physical devices are on the board", ctx) }];
    return [
      { label: "Effects", icon: "effect", sub: [
        { label: "Gain", icon: "gain", hint: "−144 … +24 dB", action: () => addGain(ctx) },
        { label: "EQ", icon: "eq", hint: "parametric", action: () => makeDevice({ type: "eq", name: "EQ", channels: 2 }, ctx) },
        { label: "Compressor", icon: "compressor", hint: "evens out loudness", action: () => makeDevice({ type: "compressor", name: "Compressor", channels: 2 }, ctx) },
        { label: "Limiter", icon: "limiter", hint: "stops clipping", action: () => makeDevice({ type: "limiter", name: "Limiter", channels: 2 }, ctx) },
        { label: "Noise Gate", icon: "gate", hint: "quiets hiss between words", action: () => makeDevice({ type: "gate", name: "Noise Gate", channels: 2 }, ctx) },
        { label: "De-Esser", icon: "deesser", hint: "tames harsh \"s\" sounds", action: () => makeDevice({ type: "deesser", name: "De-Esser", channels: 2 }, ctx) },
        { label: "Noise Suppression", icon: "denoise", hint: "takes out hiss and hum", action: () => makeDevice({ type: "denoise", name: "Noise Suppression", channels: 2 }, ctx) },
        { sep: true },
        { label: "Plugin…", icon: "plugin", hint: "LV2 · VST3", action: () => pickPlugin(ctx) },
        ...(effects.length ? [{ sep: true }, ...bringBack(effects, "", ctx)] : [])] },
      { label: "Devices", icon: "interface", sub: [
        { label: "Virtual", icon: "virtual", sub: [
          { label: "New microphone…", icon: "mic", action: () => createDevice("mic", ctx) },
          { label: "New speaker…", icon: "speaker", action: () => createDevice("speaker", ctx) },
          ...(of("virtual").length ? [{ sep: true }, ...bringBack(of("virtual"), "", ctx)] : [])] },
        ...physical,
      ] },
      ...(ctx.insert ? [] : [{ label: "Apps", icon: "app", sub: bringBack(of("app"), "All playing apps are on the board", ctx) }]),
      ...(ctx.insert || ctx.connect?.to ? [] : [{ label: "Diagnostic", icon: "diag", sub: [
        { label: "Waveform", icon: "waveform", hint: "the sound's shape, live", action: () => makeDevice({ type: "waveform", name: "Waveform", channels: 2 }, ctx) },
        { label: "Stereo", icon: "stereo", hint: "width, balance, phase", action: () => makeDevice({ type: "stereo", name: "Stereo", channels: 2 }, ctx) },
        { label: "Counter", icon: "counter", hint: "dropouts and clipping, since a reset", action: () => makeDevice({ type: "counter", name: "Counter", channels: 2 }, ctx) },
        { label: "Meter", icon: "meter", hint: "levels and loudness (LUFS)", action: () => makeDevice({ type: "meter", name: "Meter", channels: 2 }, ctx) },
        { label: "Spectrum", icon: "spectrum", hint: "frequency content", action: () => makeDevice({ type: "spectrum", name: "Spectrum", channels: 2 }, ctx) },
        ...(of("diag").length ? [{ sep: true }, ...bringBack(of("diag"), "", ctx)] : [])] }]),
    ];
  }

  // A new wire let go on empty board: what to put there, already connected to it.
  function dropMenu(x, y, d) {
    const ctx = { connect: d.side === "out" ? { from: [d.key, d.sock] } : { to: [d.key, d.sock] } };
    openMenu(x, y, [{ head: "Add and connect" }, ...addNodeItems(ctx)]);
  }

  // A wire's menu: put a node in the middle of it, or disconnect it.
  function wireMenu(id, x, y) {
    select(id);
    const ghost = S.model.wires.find((w) => w.id === id)?.ghost;
    openMenu(x, y, [
      ...(ghost ? [] : [{ label: "Insert node", icon: "add", sub: addNodeItems({ insert: id }) }]),
      ...(ghost ? [] : [S.ws.portals?.[id]
        ? { label: "Show as a wire", icon: "portal", hint: "no portal", action: () => setPortals({ [id]: null }) }
        : { label: "Make portal", icon: "portal", hint: "hide the wire, keep the sound", action: () => setPortals({ [id]: "hand" }) }]),
      { label: ghost ? "Forget connection" : "Disconnect", icon: "trash", hint: "Delete", action: unlinkSelected },
    ]);
  }

  // Portals set here and on the server alike (tidy: a Tidy's own are replaced, the user's kept).
  function setPortals(set, tidyOwn = false, tell = true) {
    const p = S.ws.portals ||= {};
    if (tidyOwn) for (const [id, how] of Object.entries(p)) if (typeof how === "object") delete p[id];  // (a Tidy's own: {how: "tidy", …})
    for (const [id, how] of Object.entries(set)) { if (how) p[id] = how; else delete p[id]; }
    if (tell) send({ op: "portals", set, tidy: tidyOwn });
    drawWires();
  }

  function boardMenu(x, y) {
    openMenu(x, y, [
      { label: "Add node", icon: "add", sub: addNodeItems() },
      { sep: true },
      { label: "Fit to window", icon: "fit", hint: "F", action: () => fit() },
      { label: "Tidy layout", icon: "tidy", action: tidy },
      ...Object.entries(S.takeover || {}).map(([unit, names]) => (
        { label: "Take over devices…", icon: "virtual", hint: `${names.length} from ${unit.replace(/\.service$/, "")}`,
          action: () => askTakeOver(unit, names) })),
      { sep: true },
      { label: "Launch at login", icon: S.autostart ? "check" : "", hint: "in the tray",
        action: () => { S.autostart = !S.autostart; send({ op: "autostart", on: S.autostart }); } },
    ]);
  }

  function replacements(c) {
    const live = S.model.cards.filter((o) => !o.missing && o.kind !== "app");
    const item = (o) => ({ label: titleOf(o), icon: iconOf(o), action: () => send({ op: "retarget", key: c.key, to: o.key }) });
    const same = live.filter((o) => o.kind === c.kind), other = live.filter((o) => o.kind !== c.kind);
    const group = { hardware: "Physical devices", virtual: "Virtual devices", effect: "Effects", plugin: "Plugins" };
    return [...(same.length ? [{ head: group[c.kind] || KIND[c.kind] }, ...same.map(item)] : []),
            ...(other.length ? [{ sep: true }, { head: "Others" }, ...other.map(item)] : [])];
  }

  function missingMenu(c, x, y) {
    openMenu(x, y, [
      { label: "Use another device", icon: "interface", sub: replacements(c) },
      ...(S.ws.devices?.[c.key] ? [{ label: { plugin: "Start this plugin", eq: "Start this EQ", limiter: "Start this limiter", compressor: "Start this compressor", gate: "Start this noise gate", denoise: "Start this noise suppression", deesser: "Start this de-esser", gain: "Create this gain" }[S.ws.devices[c.key].type] || "Create this device",
                                     icon: "add", hint: ["plugin", "eq", "gain"].includes(S.ws.devices[c.key].type) || DYN.KINDS[S.ws.devices[c.key].type] ? "" : "on the system",
                                     action: () => send({ op: "create-missing", key: c.key }) }] : []),
      { sep: true },
      { label: "Remove", icon: "trash", hint: "forget it and its wires", action: () => send({ op: "drop-card", key: c.key }) },
    ]);
  }

  // PipeBoard's own device: its name is real (apps see it), and Remove deletes it (after asking).
  function ownMenu(c, x, y) {
    const names = (saved(c.key).names || []).filter((n) => n !== c.title).map((n) => ({
      label: n, action: () => send({ op: "rename-device", key: c.key, name: n }),
      forget: () => send({ op: "forget-name", key: c.key, name: n }) }));
    const dev = S.ws.devices[c.key], plug = dev.type === "plugin" && dev.params;
    const open = (S.editors || []).includes(c.key);
    openMenu(x, y, [
      { label: "Rename", icon: "rename", hint: "double-click", action: () => startRename(c.key) },
      ...(names.length ? [{ label: "Names", icon: "names", sub: names }] : []),
      ...(plug && dev.editor ? [{ label: open ? "Close editor" : "Open editor", icon: "plugin", action: () => send({ op: "plugin-editor", key: c.key, on: !open }) }] : []),
      ...(plug && dev.bypassable ? [{ label: "Bypass", icon: dev.bypass ? "check" : "", action: () => send({ op: "plugin-bypass", key: c.key, on: !dev.bypass }) }] : []),
      ...(dev.type === "eq" ? [{ label: "Edit EQ", icon: "eq", action: () => openEq(c.key) },
                               { label: "Bypass", icon: dev.bypass ? "check" : "", action: () => eqBypass(c.key) }] : []),
      ...(dev.type === "gain" ? [{ label: "Bypass", icon: dev.bypass ? "check" : "", action: () => gainBypass(c.key) }] : []),
      ...(DYN.KINDS[dev.type] ? [{ label: "Live view", icon: dev.type, action: () => openDyn(c.key) },
                                 ...(DYN.KINDS[dev.type].smooth ? [{ label: "Soft", icon: dev.smooth ? "check" : "", action: () => dynFlag(c.key, "smooth") }] : []),
                                 ...(DYN.KINDS[dev.type].bypass === false ? [] : [{ label: "Bypass", icon: dev.bypass ? "check" : "", action: () => dynFlag(c.key, "bypass") }])] : []),
      ...disconnectAll(c.key),
      { sep: true },
      { label: quietRemove(c) ? "Remove" : "Remove…", icon: "trash", hint: inBoardOnly(c) ? "with its connections" : "deletes it from your system", danger: true, action: () => askDeleteDevice(c) },
    ]);
  }

  async function createDevice(type, ctx) {
    const word = type === "mic" ? "microphone" : "speaker";
    const made = await dialog({
      title: `New virtual ${word}`,
      text: type === "mic" ? "Apps can record from it; wire anything into it on the board."
                           : "Apps can play into it; wire it on to any device on the board.",
      input: { placeholder: type === "mic" ? "e.g. Stream Mic" : "e.g. Game Audio", label: "Name",
               check: (v) => (/^[^"\\{}[\]=]{1,40}$/.test(v) ? "" : "Up to 40 characters, without quotes, brackets, braces or =.") },
      choices: { label: "Channels", options: [["2", "Stereo"], ["1", "Mono"]], value: "2" },
      ok: "Create" });
    if (!made) return;
    makeDevice({ type, name: made.text, channels: Number(made.choice) }, ctx);
  }

  // PipeBoard's own effects, plugins and diagnostic cards: apps never see them (a virtual mic or
  // speaker is a device on the system).
  const inBoardOnly = (c) => ["effect", "plugin", "diag"].includes(c.kind);
  const wiresOf = (key) => S.model.wires.filter((w) => w.from[0] === key || w.to[0] === key).length;
  // Gone without asking (owner): a diagnostic card (it only listens), or an effect wired to nothing.
  const quietRemove = (c) => c.kind === "diag" || (inBoardOnly(c) && !wiresOf(c.key));

  async function askDeleteDevice(c) {
    if (inBoardOnly(c)) return askDeleteEffect(c, S.ws.devices[c.key].type);
    const word = c.role === "mic" ? "microphone" : "speaker";
    const wires = wiresOf(c.key);
    const ok = await dialog({
      title: `Remove the virtual ${word} ${c.title}?`,
      text: `It is removed from your system, not just from the board. Apps that ${c.role === "mic" ? "record from" : "play into"} it `
        + `lose it and switch to another device, and do not come back to it by themselves.`
        + (wires ? ` Its ${wires === 1 ? "connection goes" : `${wires} connections go`} too.` : ""),
      ok: `Remove ${c.title}`, danger: true });
    if (ok) send({ op: "delete-device", key: c.key });
  }

  async function askTakeOver(unit, names) {
    const ok = await dialog({
      title: `Take over the devices ${unit} makes?`,
      text: `PipeBoard stops and disables ${unit} and makes these devices itself, under the same names, so apps `
        + "and your default devices keep finding them. They stay when PipeBoard closes, and Launch at login "
        + "makes them again after a restart. Sound through them stops for a second or two while they are swapped.",
      lists: [["Devices", names, ""],
              ["To undo", ["Delete them in PipeBoard", `then run: systemctl --user enable --now ${unit}`], ""]],
      ok: "Take over", danger: true });
    if (ok) send({ op: "take-over", unit });
  }

  async function askDeleteEffect(c, type) {
    const wires = wiresOf(c.key);
    const ok = quietRemove(c) || await dialog({
      title: `Remove ${c.title}?`,
      text: `Sound passing through it stops`
        + (wires ? `: its ${wires === 1 ? "connection goes" : `${wires} connections go`} too.` : ".")
        + ` Apps are not affected; they never see ${type === "eq" ? "an EQ" : `a ${DYN.KINDS[type]?.word || type}`}.`,
      ok: `Remove ${c.title}`, danger: true });
    if (ok) send({ op: "delete-device", key: c.key });
  }

  // A card's Disconnect (owner wanted it back): every wire in or out of it; none, no entry.
  function disconnectAll(key) {
    const n = S.model.wires.filter((w) => !w.ghost && (w.from[0] === key || w.to[0] === key)).length;
    return n ? [{ label: "Disconnect", icon: "unlink", hint: n === 1 ? "its wire" : `all ${n} wires`, action: () => send({ op: "disconnect", card: key }) }] : [];
  }

  function cardMenu(key, x, y) {
    const c = byKey()[key], ws = saved(key), nick = ws.nick;
    if (c.missing) return missingMenu(c, x, y);
    if (c.owned && S.ws.devices?.[key]) return ownMenu(c, x, y);
    const earlier = (ws.names || []).filter((n) => n !== nick);
    const names = [
      ...(nick ? [{ label: nick, hint: "current", icon: "rename", off: true },
                  { label: c.title, hint: "original", action: () => rename(key, "") }] : []),
      ...earlier.map((n) => ({ label: n, action: () => rename(key, n),
                               forget: () => send({ op: "forget-name", key, name: n }) })),
    ];
    const iface = c.kind === "hardware" && ifaceOf(key);
    openMenu(x, y, [
      { label: "Rename", icon: "rename", hint: "double-click", action: () => startRename(key) },
      ...(names.length ? [{ label: "Names", icon: "names", sub: names }] : []),
      ...(iface ? [{ label: "Interface settings…", icon: "interface", hint: "margin, rate", action: () => openIface(iface.key) }] : []),
      ...disconnectAll(key),
      { sep: true },
      { label: "Remove", icon: "hide", hint: "from the board; your wires go with it", action: () => setHidden(key, true) },
    ]);
  }

  function showAt(key) {
    const p = S.menuAt || toWorld(innerWidth / 2, innerHeight / 2);
    S.pos[key] = { x: cell(p.x), y: cell(p.y) };
    sendPlaces({ [key]: S.pos[key] });
    setHidden(key, false);
  }

  function setHidden(key, hidden) {
    S.ws.cards[key] = { ...saved(key), hidden, ...(S.pos[key] || {}) };
    send({ op: "hide", key, hidden });
    render();
  }

  // ---- renaming: a nickname for hardware, a real name for PipeBoard's own devices (M5) -----
  function startRename(key) {
    const el = S.els[key]?.querySelector(".title");
    if (!el) return;
    const c = byKey()[key], before = el.textContent;
    el.contentEditable = "true";
    el.focus();
    getSelection().selectAllChildren(el);
    const seen = ["mic", "speaker"].includes(S.ws.devices?.[key]?.type);  // the only ones apps list
    const hint = h("div", { class: "rename-hint", text: seen
      ? "Apps see this name from PipeBoard's next start · Enter saves · Esc cancels"
      : c.owned ? "Only PipeBoard shows this name · Enter saves · Esc cancels"
        : "Only PipeBoard shows this name · empty it for the original" });
    S.els[key].append(hint);
    S.els[key].classList.add("renaming");
    if (hint.getBoundingClientRect().right > innerWidth - 8) Object.assign(hint.style, { left: "auto", right: "0" });
    const done = (commit) => {
      el.contentEditable = "false";
      hint.remove();
      S.els[key]?.classList.remove("renaming");
      el.removeEventListener("keydown", keys);
      const now = el.textContent.trim();
      if (!commit || now === before) { el.textContent = before; return; }
      if (c.owned && S.ws.devices?.[key]) send({ op: "rename-device", key, name: now });  // a real rename
      else rename(key, now === c.title ? "" : now);
    };
    const keys = (e) => {
      if (e.key === "Enter") { e.preventDefault(); el.blur(); }
      if (e.key === "Escape") { el.textContent = before; el.blur(); }
      e.stopPropagation();
    };
    el.addEventListener("keydown", keys);
    el.addEventListener("blur", () => done(true), { once: true });
  }

  function rename(key, title) {
    const ws = saved(key);
    S.ws.cards[key] = { ...ws, nick: title || undefined };
    send({ op: "rename", key, title });
    render();
  }

  // ---- profiles: the whole board saved; Default saves itself until the user makes one ----------
  const PROFILE_NAME = /^[\w][\w \-().']{0,39}$/u;

  function renderProfile() {
    const P = S.profile, btn = $("profile");
    btn.hidden = !P;
    if (!P) return;
    $("profile-name").textContent = P.active || "No profile";
    $("profile-note").textContent = P.changes.length ? "unsaved" : "";
    btn.classList.toggle("dirty", P.changes.length > 0);
    const card = $("changes"), list = $("change-list");
    card.hidden = !(P.active && P.changes.length);
    if (card.hidden && S.movesHover) { S.movesHover = false; showMoves(); }  // (saved or reverted under the mouse)
    if (card.hidden) return;
    $("changes-title").textContent = `${P.active} · unsaved changes`;
    list.textContent = "";
    for (const line of P.changes.slice(0, 6)) {
      const sign = line[0] === "+" ? "plus" : line[0] === "−" ? "minus" : "";
      list.append(h("li", { class: sign, text: sign ? line.slice(2) : line }));
    }
    if (P.changes.length > 6) list.append(h("li", { class: "more", text: `and ${P.changes.length - 6} more` }));
  }

  // Unsaved changes on the board (owner): an orange dot on every card with one, always. Where the
  // moved ones were saved (a dashed ghost, an arrow to the card now) while the changes card is
  // hovered and for a moment after a Tidy, everything else dimmed.
  function drawUnsaved() {
    const marks = (S.profile?.active && S.profile.unsaved) || {}, layer = $("moves"), svg = s("svg");
    layer.textContent = "";
    layer.append(svg);
    for (const [k, was] of Object.entries(marks)) {
      const el = S.els[k];
      if (!el) continue;
      el.classList.add("unsaved");
      el.append(h("span", { class: "unsaved-dot", "aria-label": "Unsaved change" }));
      const now = S.pos[k], sz = S.sizes[k];
      if (was.x == null || !now || !sz) continue;
      const w = px(sz.w), ht = px(sz.h), a = { x: px(was.x), y: px(was.y) }, b = { x: px(now.x), y: px(now.y) };
      layer.append(h("div", { class: `ghost k-${byKey()[k].kind}`, style: `transform: translate(${a.x}px, ${a.y}px); width: ${w}px; height: ${ht}px` },
                     h("span", { text: el.querySelector(".title")?.textContent || "" })));
      // The arrow: from the ghost's edge to the card's, along the line between their centres.
      const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len;
      const edge = Math.min(w / 2 / Math.abs(ux || 1e-9), ht / 2 / Math.abs(uy || 1e-9));
      if (len < 2 * edge + 16) continue;  // the spots touch: the ghost says enough
      const cx = a.x + w / 2, cy = a.y + ht / 2, p = { x: cx + ux * (edge + 4), y: cy + uy * (edge + 4) };
      const q = { x: cx + dx - ux * (edge + 6), y: cy + dy - uy * (edge + 6) }, A = 8, W = 4.5;
      const col = getComputedStyle(el).getPropertyValue("--c");
      svg.append(s("path", { class: "trail", stroke: col, d: `M${p.x},${p.y} L${q.x},${q.y}` }),
                 s("path", { class: "trail-head", fill: col, d: `M${q.x},${q.y} L${q.x - ux * A - uy * W},${q.y - uy * A + ux * W} L${q.x - ux * A + uy * W},${q.y - uy * A - ux * W}Z` }));
    }
    showMoves();  // (a Tidy's moves arrive with the state after it)
  }
  // Only with something to show (owner: a Tidy that moved nothing dimmed every card): a moved
  // card's ghost after a Tidy, any card with a change under the hovered changes card.
  function showMoves() {
    const what = S.movesHover ? cardsEl.querySelector(".unsaved")
      : performance.now() < (S.movesUntil || 0) && $("moves").querySelector(".ghost");
    board.classList.toggle("show-moves", !!what);
  }

  function profileMenu(x, y) {
    const P = S.profile, mark = (on) => (on ? "check" : "");
    openMenu(x, y, [
      { head: "Profiles" },
      ...P.names.map((n) => ({ label: n, icon: mark(n === P.active), hint: n === P.launch ? "at launch" : "",
                               action: () => n !== P.active && send({ op: "profile-plan", name: n }) })),
      { sep: true },
      { label: "Save", icon: "save", hint: "Ctrl+S", off: !P.changes.length, action: () => send({ op: "profile-save" }) },
      { label: "Save as new profile…", icon: "add", action: saveAs },
      { label: "Load at launch", icon: "names", sub: [
        { label: "Nothing: the system as it is", icon: mark(!P.launch), action: () => send({ op: "profile-launch", name: null }) },
        ...P.names.map((n) => ({ label: n, icon: mark(P.launch === n), action: () => send({ op: "profile-launch", name: n }) }))] },
      { label: "Delete profile", icon: "trash", sub: P.names.map((n) => ({ label: n, action: () => askDelete(n) })) },
    ]);
  }

  async function showPlan(p) {
    const same = !p.make.length && !p.remove.length;
    const ok = await dialog({
      title: `Load ${p.name}?`,
      text: same ? "The connections already match. Names, places and hidden cards change to the profile's."
                 : "These connections change on your system:",
      lists: [["Connect", p.make, "plus"], ["Disconnect", p.remove, "minus"],
              ["Not on this system: kept, shown dimmed", p.missing, "warn"]],
      ok: `Load ${p.name}` });
    if (ok) send({ op: "profile-load", name: p.name });
  }

  async function saveAs() {
    const name = await dialog({
      title: "Save as a new profile",
      text: "Saves the whole board: devices, connections, names and places.",
      input: { placeholder: "e.g. Streaming", label: "Profile name",
               check: (v) => (PROFILE_NAME.test(v) ? "" : "Letters, digits, spaces and - _ ( ), up to 40.") },
      ok: "Save" });
    if (!name) return;
    if (S.profile.names.includes(name) && !(await dialog({
      title: `Replace ${name}?`, text: `A profile called ${name} exists. Saving replaces it with this board.`,
      ok: `Replace ${name}`, danger: true }))) return;
    send({ op: "profile-save", name });
  }

  async function askDelete(name) {
    const ok = await dialog({
      title: `Delete the profile ${name}?`,
      text: "Only the saved profile is deleted. Nothing on your system changes: devices and connections stay as they are.",
      ok: `Delete ${name}`, danger: true });
    if (ok) send({ op: "profile-delete", name });
  }

  // A modal card: resolves to true, the typed text, or null when cancelled. A dangerous one starts
  // on Cancel, so Enter alone never confirms it.
  function dialog({ title, text, lists = [], input, choices, ok, danger = false, cancel = "Cancel" }) {
    return new Promise((done) => {
      closeMenu();
      const box = h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title },
                    h("h2", { text: title }), text ? h("p", { text }) : null);
      for (const [head, items, cls] of lists) {
        if (!items.length) continue;
        const shown = items.slice(0, 8).map((t) => h("li", { text: t }));
        if (items.length > 8) shown.push(h("li", { class: "more", text: `and ${items.length - 8} more` }));
        box.append(h("h3", { text: head }), h("ul", { class: cls }, ...shown));
      }
      const field = input ? h("input", { type: "text", placeholder: input.placeholder, "aria-label": input.label, maxlength: "40" }) : null;
      let choice = choices?.value;
      const seg = choices ? h("div", { class: "segmented", role: "radiogroup", "aria-label": choices.label },
        ...choices.options.map(([value, label]) => {
          const b = h("button", { type: "button", role: "radio", "aria-checked": String(value === choice), text: label });
          b.addEventListener("click", () => {
            choice = value;
            for (const x of b.parentElement.children) x.setAttribute("aria-checked", String(x === b));
          });
          return b;
        })) : null;
      const err = h("p", { class: "err", "aria-live": "polite" });
      if (field) box.append(field, err);
      if (seg) box.append(seg);
      const no = h("button", { type: "button", text: cancel });
      const yes = h("button", { type: "button", class: danger ? "danger" : "primary", text: ok });
      box.append(h("div", { class: "actions" }, no, yes));
      const veil = h("div", { class: "veil" }, box);
      document.body.append(veil);
      const finish = (v) => { veil.remove(); removeEventListener("keydown", keys, true); done(v); };
      const keys = (e) => {
        if (e.key === "Escape") finish(null);
        else if (e.key === "Enter") (document.activeElement?.tagName === "BUTTON" ? document.activeElement : yes).click();
        else return;
        e.preventDefault(); e.stopPropagation();
      };
      addEventListener("keydown", keys, true);
      no.addEventListener("click", () => finish(null));
      yes.addEventListener("click", () => {
        if (!field) return finish(true);
        const v = field.value.trim(), problem = input.check ? input.check(v) : "";
        if (problem || !v) { err.textContent = problem || "Give it a name."; field.focus(); return; }
        finish(choices ? { text: v, choice } : v);
      });
      veil.addEventListener("mousedown", (e) => { if (e.target === veil) finish(null); });
      (field || (danger ? no : yes)).focus();
    });
  }

  // ---- problems -----------------------------------------------------------------------------
  const RANK = { error: 0, warning: 1, info: 2 };
  // One word for the tag above a card; the problem center has the whole story.
  const SHORT = { "Feedback loop": "Feedback", "Connection failed": "Failed",
                  "Only one side of a stereo input": "One side", "Same sound arrives twice": "Doubled",
                  "Clipping": "Clipping", "Device missing": "Missing", "Dropouts": "Dropouts" };
  // An event going on tags its card too, by its kind (its title names the card: too long here).
  const KIND_TAG = { dropouts: "Dropouts", clipping: "Too loud", missing: "Missing", stopped: "Stopped" };

  function problemCards() {
    const flagged = {};
    const live = (S.events || []).filter((e) => e.active && !e.muted).map((e) => ({ ...e, title: KIND_TAG[e.kind] || e.title }));
    for (const p of [...S.problems, ...live].sort((a, b) => RANK[a.severity] - RANK[b.severity])) {  // worst first, so the first problem on a card names its tag
      if (p.severity === "info") continue;
      for (const k of new Set(p.cards)) {
        const f = flagged[k];
        if (f) { f.more += 1; f.tip += `\n${p.title}`; continue; }
        flagged[k] = { severity: p.severity, label: SHORT[p.title] || p.title, tip: p.title, more: 0 };
      }
    }
    return flagged;
  }

  // Notifications (owner: "Problems and Events, split by a separator and a label"): Problems, how
  // the board is now; Events, what happened, a log that remembers (problems.Events). An event is
  // new until Seen, and new again when it happens again.
  const clockAt = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  function ago(t) {
    const s = Date.now() / 1000 - t;
    if (s < 10) return "just now";
    if (s < 60) return `${Math.round(s)} s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    const d = new Date(t * 1000);
    return d.toDateString() === new Date().toDateString() ? `at ${clockAt(t)}`
      : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${clockAt(t)}`;
  }
  function renderProblems() {
    const panel = $("problems"), btn = $("problems-btn"), ps = S.problems, evs = S.events || [];
    // (muted, owner: "I know there's a limiter after it": out of the list and the count)
    const heard = evs.filter((e) => !e.muted), muted = evs.filter((e) => e.muted);
    const fresh = heard.filter((e) => e.new), seen = heard.filter((e) => !e.new);
    const loud = [...ps.filter((p) => p.severity !== "info"), ...fresh];
    const worst = ["error", "warning", "info"].find((sv) => [...ps, ...fresh].some((x) => x.severity === sv)) || "ok";
    btn.className = `problems-btn sev-${worst}`;
    const n = $("problems-count");
    n.hidden = !loud.length;
    n.textContent = String(loud.length);
    btn.title = loud.length ? `${loud.length} to look at` : "All good";
    const all = h("button", { type: "button", class: "quiet", text: "Mark all seen", "data-k": "seen-all" });
    all.addEventListener("click", () => send({ op: "event-seen" }));
    const more = h("button", { type: "button", class: "quiet more", text: S.showSeen ? "Hide seen" : `Show seen (${seen.length})`, "data-k": "show-seen" });
    more.addEventListener("click", () => { S.showSeen = !S.showSeen; renderProblems(); });
    const hush = h("button", { type: "button", class: "quiet more", text: S.showMuted ? "Hide muted" : `Muted (${muted.length})`, "data-k": "show-muted" });
    hush.addEventListener("click", () => { S.showMuted = !S.showMuted; renderProblems(); });
    // Each section its own block, its header a bar with its count (owner: "really hard to
    // discern where the events and problems sections are and which cards are in what section").
    const bar = (title, sub, n, sev, ...acts) => h("header", { class: "n-bar" },
      h("span", { class: `n-title sev-${n ? sev : "ok"}` }, h("span", { class: "sev-dot" }), title),
      h("span", { class: "n-sub", text: sub }), ...acts);
    const pSev = ps.some((p) => p.severity === "error") ? "error" : ps.some((p) => p.severity === "warning") ? "warning" : "info";
    const eSev = fresh.some((e) => e.severity === "error") ? "error" : "warning";
    panel.replaceChildren(
      h("section", { class: "n-block" },
        bar("Problems", ps.length ? `${ps.length} on the board now` : "none", ps.length, pSev),
        ps.length ? h("ol", {}, ...ps.map(problemItem)) : h("p", { class: "n-none", text: "Nothing to fix. Free sockets are fine." })),
      h("section", { class: "n-block" },
        bar("Events", fresh.length ? `${fresh.length} new` : "nothing new", fresh.length, eSev, fresh.length ? all : null),
        fresh.length || S.showSeen || S.showMuted
          ? h("ol", {}, ...fresh.map(eventItem), ...(S.showSeen ? seen.map(eventItem) : []), ...(S.showMuted ? muted.map(eventItem) : []))
          : h("p", { class: "n-none", text: evs.length ? "What happened before is under Show seen." : "Nothing has happened yet." }),
        // (replaceChildren would write a null as "null")
        ...(seen.length || muted.length ? [h("div", { class: "n-more" }, seen.length ? more : null, muted.length ? hush : null)] : [])));
  }
  function problemItem(p) {
    const show = h("button", { type: "button", text: "Show" });
    show.addEventListener("click", () => showProblem(p));
    // Laid out (owner: concise, formatted): where in bold, what muted, the fix on its own line.
    return h("li", { class: `sev-${p.severity}` },
      h("div", { class: "p-head" }, h("span", { class: "sev-dot" }), h("strong", { text: p.title }), p.cards.length ? show : null),
      p.where ? h("p", { class: "p-where", text: p.where }) : null,
      h("p", { class: "p-what", text: p.what ?? p.text }),
      p.fix ? h("p", { class: "p-fix", text: p.fix }) : null);
  }
  // An event: what, where, when (going on now, or when it ended), how often; its own facts by
  // kind; Show and Seen. A dropout found to come from the GPU's power saving says so and offers
  // the fix (owner: identify it, explain it simply, say how to fix it).
  function eventItem(e) {
    const d = e.data || {}, peak = e.peak || {};
    const when = e.active ? `now · since ${clockAt(e.since)}` : ago(e.ended || e.last);
    const show = h("button", { type: "button", text: "Show" });
    show.addEventListener("click", () => showProblem({ cards: e.cards, wires: [] }));
    const seenBtn = h("button", { type: "button", text: "Seen", "data-k": `seen-${e.id}` });
    seenBtn.addEventListener("click", () => send({ op: "event-seen", ids: [e.id] }));
    const muteBtn = h("button", { type: "button", class: "quiet", text: e.muted ? "Unmute" : "Mute", "data-k": `mute-${e.id}`,
                                  title: e.muted ? "Show it again when it happens" : `Never show ${e.title.toLowerCase()} here again, until you unmute it` });
    muteBtn.addEventListener("click", () => send({ op: "event-mute", id: e.id, on: !e.muted }));
    const facts = [];
    if (e.kind === "dropouts") {  // (its numbers as they are now, not as when it was logged)
      facts.push(e.active ? `${d.minute} short gaps or clicks in the last minute: the computer didn't hand over the sound in time.`
        : "Short gaps or clicks: the computer didn't hand over the sound in time.");
      if (peak.minute > (e.active ? d.minute : 0)) facts.push(`The worst minute had ${peak.minute}.`);
    } else facts.push(e.what);
    const gpuCause = e.kind === "dropouts" && d.cause === "gpu";
    let cause = null;
    if (gpuCause) {
      const fix = h("button", { type: "button", class: "go", text: "Keep the graphics card at full speed", "data-k": "gpu-fix", disabled: !!S.gpuPending });
      fix.addEventListener("click", () => { S.gpuPending = true; send({ op: "gpu-full", on: true }); renderProblems(); });
      cause = h("div", { class: "n-cause" },
        h("b", { text: "Likely cause: the graphics card saving power" }),
        h("p", { text: `${d.saving} a second while it saves power, ${d.up} while it doesn't. While a graphics card saves power, some drivers keep the processor waiting on it for milliseconds: long enough to crackle, mostly while a video plays.` }),
        fix, h("p", { class: "n-small", text: "It uses more power. Needs your password. To undo: Audio engine › Advanced › GPU." }));
    }
    return h("li", { class: `sev-${e.severity} ev${e.new ? "" : " old"}${e.active ? " live" : ""}${e.muted ? " muted" : ""}` },
      h("div", { class: "p-head" }, h("span", { class: "sev-dot" }), h("strong", { text: e.title }),
        e.count > 1 ? h("span", { class: "n-count", title: `Happened ${e.count} times`, text: `×${e.count}` }) : null,
        h("span", { class: "n-when", text: when })),
      e.where ? h("p", { class: "p-where", text: e.where }) : null,
      ...facts.map((t) => h("p", { class: "p-what", text: t })),
      cause || (e.fix ? h("p", { class: "p-fix", text: e.fix }) : null),
      h("div", { class: "n-acts" }, e.cards?.length ? show : null, e.new ? seenBtn : null, muteBtn));
  }

  function showProblem(p) {
    if (p.wires[0]) select(p.wires[0]);
    showCard(...p.cards);
  }
  // Frames these cards on the board and flashes them (a problem's Show; an open panel's title).
  function showCard(...keys) {
    for (const k of keys) {
      const el = S.els[k];
      if (!el) continue;
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    }
    fit(true, keys.filter((k) => S.els[k]), 1);
  }

  function toggleProblems(open) {
    const panel = $("problems"), btn = $("problems-btn");
    panel.hidden = !(open ?? panel.hidden);
    if (!panel.hidden) { closeMenu(); if (!S.iface) closeEngine(); }  // (one popover at a time)
    else if (S.showSeen || S.showMuted) { S.showSeen = S.showMuted = false; renderProblems(); }  // (folded again when it closes, owner)
    btn.setAttribute("aria-expanded", String(!panel.hidden));
  }

  // ---- status, tooltip, toast ---------------------------------------------------------------
  function setStatus(cls, text) { const el = $("status"); el.className = `status ${cls}`; el.textContent = text; }
  // ---- the audio engine (PLAN §8c): block size, safety margin, priority, tried for a minute ----
  const BLOCKS = [128, 256, 512, 1024, 2048], MARGINS = [0, 1, 2, 3];  // (the margin in whole blocks: engine.MARGIN_BLOCKS)
  const roundTrip = (e, rate) => (2 * (e.block + e.margin) / rate) * 1000;  // in and out: block + margin each
  // Draws the open panel: the Audio engine's (S.iface null) or an interface's (S.iface its key),
  // once the server reads that interface (until then the figures would be another card's).
  function openEngine() {
    const e = S.engine;
    if (!e?.interface?.length) { toast("No audio interface clocks the graph: there is nothing to set.", "info"); return; }
    if (e.trial) { toast("A setting is being tried: keep or revert it first.", "info"); return; }
    if (S.iface && e.device !== S.iface) return;
    if (!$("engine")) { closeMenu(); toggleProblems(false); }  // (one popover at a time; not on a redraw)
    S.pick = { block: e.block, blocks: e.margin_blocks ?? 1, prio: e.prio, nice: e.nice, tsched: !!e.tsched, rate: cardRate(e), device: e.device, advanced: S.pick?.advanced || false };
    drawEngine();
  }
  // The safety margin in whole blocks (owner: only values that work; under one block the input
  // dropped out, over one did not fit until the buffer is made to fit: engine.texts).
  function p0() { S.pick.margin = S.pick.block * S.pick.blocks; }
  // Whether trying these values makes the interface start again (engine.reopens).
  const reopensNow = (p, e) => p.tsched !== !!e.tsched || p.blocks !== (e.margin_blocks ?? 1) || rateAsked(p, e) !== (e.fixed_rate || null) ||
    ((!p.tsched || !!e.irq_elsewhere) && p.block !== e.block);
  const kHz = (r) => `${+(r / 1000).toFixed(1)} kHz`;
  // What the open panel draws from the engine as it runs: drawn again when any of it changes.
  // Before, only on opening: started after a change of rate, the bars and the round trip kept
  // the figures of the moment it opened (a friend).
  const engineShown = (e) => [e.device, e.block, e.margin, e.rate, e.dev_rate, e.margin_blocks, !!e.tsched, e.fixed_rate,
    e.rates, e.blocks, e.prio, e.nice, e.prio_now, e.nice_now, (e.interfaces || []).map((x) => [x.key, x.rate])];
  // An interface's own settings (margin, scheduling, rate: one WirePlumber rule each) are in its
  // own panel, docked at the bottom like an effect's (owner): its card's gear, or the Audio
  // engine's list of them. The server reads that card while it is open (op engine-device).
  const ifaceOf = (cardKey) => (S.engine?.interfaces || []).find((x) => `device:${x.key}` === cardKey);
  function openIface(key) {
    if (S.engine?.trial) { toast("A setting is being tried: keep or revert it first.", "info"); return; }
    closeEngine(); closeMenu(); hideTip();
    EQ.close(false); S.eqOpen = null; DYN.close(false); S.dynOpen = null; DIAG.close(false);
    S.iface = key;
    S.headroom = null;
    send({ op: "engine-device", device: key });
    openEngine();  // (at once when the server reads it already; else when its state comes)
    render();
  }
  const ifaceLine = (x) => (x.rate ? [kHz(x.rate), x.fixed_rate ? "fixed" : null, `${x.margin_ms} ms margin`, x.tsched ? "timer" : "interrupts"]
    : ["closed now"]).filter(Boolean).join(" · ");
  // The Audio engine's list of interfaces: each one's name and settings; a click opens its panel.
  function ifaceList(e) {
    return h("div", { class: "ifaces", role: "list", "aria-label": "Interfaces" }, ...e.interfaces.map((x) => {
      const b = h("button", { type: "button", role: "listitem", "data-k": `iface-${x.key}`, title: `${x.title}: its margin, scheduling and rate` },
        // (the name its own line: beside its settings it was cut, "Steinberg…")
        h("span", { class: "name", text: x.title || x.key }), h("span", { class: "chev", text: "›" }), h("span", { class: "what", text: ifaceLine(x) }));
      b.addEventListener("click", () => openIface(x.key));
      return b;
    }));
  }
  // The rate the interface opens at, only when the card lists the ones it can (engine.rates): the
  // one it runs at picked and marked like the block's; another fixes it there, and its part of the
  // graph follows (node.force-rate). No "Auto" (owner: no reason for it; on their machine the
  // graph allows one rate, so it was 192 kHz under another name).
  const cardRate = (e) => e.fixed_rate || e.dev_rate || null;
  // What a Try asks: the rate as it is (none fixed stays none: a margin change must not fix the
  // rate it runs at, which would reopen the card), or the one picked.
  const rateAsked = (p, e) => (p.rate === cardRate(e) ? e.fixed_rate || null : p.rate);
  function rateSeg(p, e) {
    return h("div", { class: "segmented", role: "radiogroup", "aria-label": "Sample rate" }, ...e.rates.map((v) => {
      const b = h("button", { type: "button", role: "radio", "aria-checked": String(p.rate === v), class: v === cardRate(e) ? "cur" : "",
                              title: v === cardRate(e) ? "What runs now" : "", "data-k": `rate-${v}`, text: String(+(v / 1000).toFixed(1)) });
      b.addEventListener("click", () => { p.rate = v; drawEngine(); });
      return b;
    }));
  }
  // CPU idle states, one toggle each (owner): lit = allowed. By the names and wake times the CPU
  // reports, any vendor. Applied at once through the system's password prompt; nothing restarts.
  // Apps' audio in real time (apprt.py): each app's PipeWire audio thread raised when it did not
  // get real time itself (owner: Spotify's Snap left it at normal priority and stalled every
  // sound). On by default; off puts back what PipeBoard raised. The thread only, not the app.
  function appsRealtime(e, row) {
    const a = e.app_rt || { on: true, apps: [] };
    const say = { own: "real time by itself", raised: "raised by PipeBoard", normal: "normal priority", refused: "could not be raised", none: "no audio thread found" };
    const b = h("button", { type: "button", class: "chip-state", "aria-pressed": String(!!a.on), "data-k": "app-rt",
                            title: a.apps.length ? a.apps.map((x) => `${x.name}: ${say[x.state] || x.state}`).join("\n") : "No app is playing" },
      h("b", { text: "Real time" }));
    b.addEventListener("click", () => send({ op: "app-rt", on: !a.on }));
    const raised = a.apps.filter((x) => x.state === "raised").map((x) => x.name);
    return [row("Apps' audio", b, ""),  // (no value: the button and the hint say it, owner)
            h("p", { class: "hint", text: a.on
              ? `Lets each app's sound go first, so a busy app can't stall all sound. Costs nothing you'd notice.${raised.length ? ` Helping now: ${raised.join(", ")}.` : ""}`
              : "Off: an app whose sound doesn't go first can stall all sound (Spotify's did)." })];
  }
  function deepSleep(e, row) {
    const states = e.cstates?.states || [];
    if (!states.length) return [row("CPU idle states", h("span", { class: "of", text: "none reported" }), ""), null];
    const off = states.filter((x) => x.off).map((x) => x.name);
    const chips = h("div", { class: "chips" }, ...states.map((x) => {
      const b = h("button", { type: "button", class: `chip-state${S.idlePending === x.name ? " pending" : ""}`, "aria-pressed": String(!x.off), "data-k": `idle-${x.name}`,
                              disabled: !!S.idlePending,
                              title: `${x.name}: ${x.us} µs to wake. ${x.off ? "Off" : "Allowed"}. Click to ${x.off ? "allow" : "turn off"}.` },
        h("b", { text: x.name }), h("small", { text: `${x.us} µs` }));
      b.addEventListener("click", () => {
        S.idlePending = x.name;  // (until the server's answer: no second click on a stale chip)
        send({ op: "cstates", off: x.off ? off.filter((n) => n !== x.name) : [...off, x.name] });
        drawEngine();
      });
      return b;
    }));
    const kept = (e.cstates.rules || []).map((r) => r.split("/").pop()).join(", ");
    return [row("CPU idle states", chips, ""),  // (the chips say which are off)
            h("p", { class: "hint", text: `How deeply the processor may sleep. The deeper ones save power but wake too slowly for sound: turn them off if you hear crackles. Needs your password.${kept ? ` Boot rule: ${kept}.` : ""}` })];
  }
  // The GPU at full speed (gpu.py): saving power, it made the owner's 1 ms blocks drop out while a
  // video played (measured: none at its stock top memory clock). PipeBoard watches in the
  // background and says when that is the cause; the switch sets it, with the password, and holds
  // it at every start. Afterwards it says what changed.
  function gpuSpeed(e, row) {
    const g = e.gpu || {};
    if (!g.gpus?.length) return [];
    const v = g.verdict, a = g.after, min = a ? Math.round(a.s / 60) : 0;
    const b = h("button", { type: "button", class: `chip-state${S.gpuPending ? " pending" : ""}`, "aria-pressed": String(!!g.full), "data-k": "gpu-full",
                            disabled: !!S.gpuPending, title: g.gpus.map((x) => x.name).join(", ") }, h("b", { text: "Full speed" }));
    b.addEventListener("click", () => { S.gpuPending = true; send({ op: "gpu-full", on: !g.full }); drawEngine(); });
    // Why it is there at all (owner: "needs an explanation as to why that setting even matters"):
    // what goes wrong, in a sentence; then what PipeBoard found here, or what the fix did.
    const why = "Why: while the GPU saves power, some drivers keep the processor waiting on it for milliseconds, which is enough for small blocks to crackle, mostly while a video plays.";
    const now = g.full
      ? `${min >= 1 ? `Since then: ${a.drops} dropout${a.drops === 1 ? "" : "s"} in ${min} min. ` : ""}Kept at its normal top clocks (not an overclock); it uses more power.`
      : v ? `It happens here: ${v.saving} dropouts a second while it saves power, ${v.up} while it's up. Full speed stops that, for more power use.`
        : "PipeBoard watches for it and tells you if it happens here. Full speed uses more power; needs your password.";
    return [row("GPU", b, ""), h("p", { class: "hint", text: why }), h("p", { class: `hint${v && !g.full ? " warn" : ""}`, text: now })];
  }
  // Low-latency kernel options, one toggle each: what linux-lowlatency adds to Ubuntu's kernel.
  // Lit = on at the next boot (PipeBoard's GRUB snippet); set in another GRUB file = not ours to
  // take off. Needs the password and a restart.
  const KERNEL_WHAT = { "preempt=full": "full preemption", "rcu_nocbs=all": "RCU offload" };
  function kernelOpts(e, row) {
    const k = e.kernel;
    if (!k?.grub) return [row("Kernel", h("span", { class: "of", text: "no GRUB found" }), ""), null];
    const next = (x) => x.ours || x.elsewhere.length > 0;
    const on = k.opts.filter((x) => x.ours).map((x) => x.name);
    const chips = h("div", { class: "chips" }, ...k.opts.map((x) => {
      const where = x.elsewhere.map((f) => f.split("/").pop()).join(", ");
      const b = h("button", { type: "button", class: `chip-state${S.kernelPending === x.name ? " pending" : ""}`, "aria-pressed": String(next(x)), "data-k": `kernel-${x.name}`,
                              disabled: !!S.kernelPending || (!!where && !x.ours),
                              title: `${x.name} (${KERNEL_WHAT[x.name]}): ${x.now ? "on" : "off"} now, ${next(x) ? "on" : "off"} after a restart.` +
                                     (where ? ` Set in ${where}: change it there.` : ` Click to turn ${x.ours ? "off" : "on"} at the next boot.`) },
        h("b", { text: x.name }));
      b.addEventListener("click", () => {
        S.kernelPending = x.name;  // (until the server's answer, as for the idle states)
        send({ op: "kernel", on: x.ours ? on.filter((n) => n !== x.name) : [...on, x.name] });
        drawEngine();
      });
      return b;
    }));
    const restart = k.opts.some((x) => next(x) !== x.now);
    return [row("Kernel", chips, restart ? "on restart" : ""),  // (only what the chips can't show)
            h("p", { class: "hint", text: "Low-latency options for the system's core. Rarely needed, small effect. Needs your password and a restart." })];
  }
  // None · 1 · 2 · 3 blocks, the one running marked like the block's; each with its time under it (owner:
  // a lone "5.3 ms" beside the buttons read as the buffer headroom's, then as awkward)
  function marginSeg(p, e) {
    return h("div", { class: "segmented words margins", role: "radiogroup", "aria-label": "Safety margin" }, ...MARGINS.map((v) => {
      const ms = `${(v * p.block / e.rate * 1000).toFixed(1)} ms`, name = v === 0 ? "None" : v === 1 ? "1 block" : `${v} blocks`;
      const b = h("button", { type: "button", role: "radio", "aria-checked": String(p.blocks === v), class: v === (e.margin_blocks ?? 1) ? "cur" : "",
                              "data-k": `blocks-${v}`, "aria-label": `${name}, ${ms}` },
        h("span", { text: name }), h("small", { text: ms }));
      b.addEventListener("click", () => { p.blocks = v; drawEngine(); });
      return b;
    }));
  }
  function scheduling(p, e) {  // Interrupts | Timer, the one running marked like the block's
    return h("div", { class: "segmented words", role: "radiogroup", "aria-label": "Scheduling" }, ...[[false, "Interrupts"], [true, "Timer"]].map(([v, text]) => {
      const b = h("button", { type: "button", role: "radio", "aria-checked": String(p.tsched === v), class: v === !!e.tsched ? "cur" : "", "data-k": `tsched-${v}`, text });
      b.addEventListener("click", () => { p.tsched = v; drawEngine(); });
      return b;
    }));
  }
  function closeEngine() {
    $("engine")?.remove();
    $("clock").classList.remove("open");
    if (S.headroomOn) { S.headroomOn = false; send({ op: "headroom", on: false }); }
    if (S.iface) { S.iface = null; send({ op: "engine-device", device: null }); render(); }
  }
  // Buffer headroom (owner's pick G, always open): how close the interface's buffers came to
  // running out, sampled by the server while the panel is open (headroom.py). The line: each
  // side's lowest in the last minute; the graph: that minute, 0 ms (a click) red at the bottom.
  // Red under a quarter block, amber under half (owner: a fixed 0.5 ms filled a third of the graph
  // at block 128). ponytail: a stand-in for this machine's real wake-up lateness, which
  // pw-profiler could measure; the glow fades from 0 ms up to half a block, not a solid band.
  const HEAD_RED_BLOCK = 0.25, HEAD_WARN_BLOCK = 0.5, HEAD_W = 280, HEAD_H = 64;
  function headroomBlock() {
    if (!S.headroomOn) { S.headroomOn = true; S.headroom = null; send({ op: "headroom", on: true }); }
    const el = h("div", { id: "headroom", class: "headroom" });
    fillHeadroom(el);
    return el;
  }
  function showHeadroom() { const el = $("headroom"); if (el) fillHeadroom(el); }
  function fillHeadroom(el) {
    const d = S.headroom, e = S.engine, every = d?.every || 0.5, n = Math.round(60 / every);
    const top = (e.block + e.margin) / e.rate * 1000;  // the graph's scale: one half of the round trip
    const blockMs = e.block / e.rate * 1000, red = blockMs * HEAD_RED_BLOCK, warn = blockMs * HEAD_WARN_BLOCK;
    // Only the sides the interface has (a USB mic may have no output); a side sampled but never
    // running (nothing plays through it: its buffer is closed) says so, not "measuring…" for ever.
    const sides = [["out", "Output"], ["in", "Input"]].filter(([k]) => !d?.sides || d.sides.includes(k)).map(([k, name]) => {
      const xs = (d?.[k] || []).slice(-n);
      let low = null;
      xs.forEach((v, i) => { if (v != null && (low == null || v < xs[low])) low = i; });
      return { k, name, xs, low: low == null ? null : xs[low], ago: low == null ? null : (xs.length - 1 - low) * every, at: low };
    });
    const level = (v) => (v < red ? "bad" : v < warn ? "warn" : "ok");
    const idle = (x) => x.xs.length >= 4 && x.low == null;  // (two seconds sampled, never running)
    const line = h("div", { class: "hr-line" }, ...sides.map((x) => x.low == null
      ? h("span", idle(x) ? { class: "wait", "data-tip": `${x.name}: not running. Nothing ${x.k === "out" ? "plays through" : "records from"} it now, so its buffer is closed.` } : { class: "wait" },
          idle(x) ? `${x.name} not running` : `${x.name} measuring…`)
      : h("span", { "data-tip": x.low < 0
          ? `${x.name}: ran out, ${(-x.low).toFixed(2)} ms short: a click, ${x.ago < 1 ? "just now" : `${Math.round(x.ago)} s ago`}`
          : `${x.name}: ${x.low.toFixed(2)} ms at its lowest, ${x.ago < 1 ? "just now" : `${Math.round(x.ago)} s ago`}` },
        h("i", { class: `hr-dot ${level(x.low)}` }), `${x.name} `, ...(x.low < 0 ? [h("b", { text: "ran out" })] : [h("b", { text: x.low.toFixed(1) }), " ms"]))));
    const X = (i, len) => ((i + n - len) / (n - 1)) * HEAD_W, Y = (v) => HEAD_H - (Math.max(0, Math.min(v, top)) / top) * HEAD_H;  // (under 0, ran dry: on the 0 line, not over the legend)
    const svg = s("svg", { width: HEAD_W, height: HEAD_H, viewBox: `0 0 ${HEAD_W} ${HEAD_H}`, class: "hr-plot", "aria-hidden": "true" });
    const fade = s("linearGradient", { id: "hr-fade", x1: 0, y1: 1, x2: 0, y2: 0 });
    fade.append(s("stop", { offset: 0, "stop-opacity": 0.4 }), s("stop", { offset: HEAD_RED_BLOCK / HEAD_WARN_BLOCK, "stop-opacity": 0.14 }),
                s("stop", { offset: 1, "stop-opacity": 0 }));
    const defs = s("defs");
    defs.append(fade);
    svg.append(defs, s("rect", { class: "hr-red", x: 0, y: Y(warn), width: HEAD_W, height: HEAD_H - Y(warn), fill: "url(#hr-fade)" }),
               s("rect", { class: "hr-zero", x: 0, y: HEAD_H - 1, width: HEAD_W, height: 1 }));
    for (const x of [...sides].reverse()) {  // (the output, the tight one, drawn on top)
      const dpath = x.xs.map((v, i) => (v == null ? "" : `${i && x.xs[i - 1] != null ? "L" : "M"}${X(i, x.xs.length).toFixed(1)},${Y(v).toFixed(1)}`)).join("");
      if (dpath) svg.append(s("path", { class: `hr-${x.k}`, d: dpath }));
    }
    const worst = sides.filter((x) => x.low != null).sort((a, b) => a.low - b.low)[0];
    const graph = h("div", { class: "hr-graph" }, svg,
      h("span", { class: "hr-axis top", text: `${top.toFixed(0)} ms` }), h("span", { class: "hr-axis zero", text: "0 ms" }));
    if (worst) {
      const cx = X(worst.at, worst.xs.length), cy = Y(worst.low);
      svg.append(s("circle", { class: "hr-low", cx, cy, r: 3 }));
      const tag = h("span", { class: "hr-tag", text: `${worst.low < 0 ? "ran out" : `${worst.low.toFixed(1)} ms`} · ${worst.ago < 1 ? "now" : `${Math.round(worst.ago)} s ago`}` });
      tag.style.left = `${Math.max(0, Math.min(HEAD_W - 92, cx - 46))}px`;
      tag.style.top = `${cy > 22 ? cy - 20 : cy + 6}px`;
      graph.append(tag);
    }
    // (its title inside its box, as the round trip's: the two boxes side by side start level, owner)
    el.replaceChildren(
      h("div", { class: "hr-box" }, h("div", { class: "hr-top" }, h("span", { text: "Buffer headroom" }), h("small", { text: "lowest, last minute" })), line, graph,
        h("div", { class: "hr-foot" }, h("span", { text: "1 min ago" }),
          ...sides.map((x) => h("span", { class: `key ${x.k}`, text: x.name })), h("span", { text: "now" }))),
      h("p", { class: "hr-note" }, "How close the card came to running out of sound. ", h("span", { class: "red", text: "At 0 ms you hear a click." })));
  }
  function drawEngine() {
    // Drawn again on every change: no opening animation then, and the focus back where it was
    // (owner: each + or − made the panel close and open).
    const again = !!$("engine"), focused = document.activeElement?.dataset?.k;
    // (the old one removed once the new one is built, not closeEngine: Buffer headroom keeps
    // sampling across a redraw, and a moved interface panel stays where it was put)
    p0();
    // The round trip compares only when the block changes (owner: a priority shown as a new
    // round trip); otherwise it is what runs (on the timer PipeWire pads the margin itself).
    const e = S.engine, p = S.pick, rate = e.rate, resized = p.block !== e.block || p.blocks !== (e.margin_blocks ?? 1), shown = resized ? p : e;
    // The sizes to offer (engine.blocks): an interface with implicit feedback (1 ms USB transfers)
    // whole transfers only; the one running stays shown even when it is not one of them.
    const offered = [...new Set([...(e.blocks?.length ? e.blocks : BLOCKS), e.block])].sort((a, b) => a - b);
    const scale = Math.max(roundTrip(e, rate), roundTrip(shown, rate));
    const w = (samples) => `${(samples / rate * 1000) / scale * 100}%`;  // a part of the round trip, as wide as its share
    const bar = (v, cls) => h("div", { class: `bar${cls}` }, ...[v.block, v.margin, v.block, v.margin].map((x, i) =>
      h("i", { class: i % 2 ? "margin" : "", style: `width:${w(x)}` })));
    const seg = (vals, key, label) => h("div", { class: "segmented", role: "radiogroup", "aria-label": label },
      ...vals.map((v) => {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(p[key] === v), class: v === e[key] ? "cur" : "", "data-k": `${key}-${v}`,
          title: v === e[key] ? "What runs now" : "", text: v === 0 ? "None" : String(v) });
        b.addEventListener("click", () => { p[key] = v; drawEngine(); });
        return b;
      }));
    const stepper = (key, lo, hi) => {
      const box = h("span", { class: "stepper" }), minus = h("button", { type: "button", text: "−", "aria-label": "Lower", "data-k": `${key}-` }),
        plus = h("button", { type: "button", text: "+", "aria-label": "Higher", "data-k": `${key}+` });
      minus.addEventListener("click", () => { p[key] = Math.max(lo, p[key] - 1); drawEngine(); });
      plus.addEventListener("click", () => { p[key] = Math.min(hi, p[key] + 1); drawEngine(); });
      box.append(minus, h("span", { text: String(p[key]) }), plus);
      return box;
    };
    const row = (label, control, val) => h("div", { class: "eng-row" }, h("label", { text: label }), control, h("span", { class: "val", text: val }));
    const msOf = (n) => `${(n / rate * 1000).toFixed(1)} ms`;
    // What PipeWire's threads run (the server reads /proc), and whether the file asks otherwise:
    // its file's priority applied only at a Try until 2026-11-08 (inside context.properties
    // PipeWire ignored it), and the panel showed the file's 95 while 88 ran (owner).
    const prioNow = e.prio_now ?? e.prio, niceNow = e.nice_now ?? e.nice, pending = prioNow !== e.prio || niceNow !== e.nice;
    const changed = ["block", "prio", "nice"].some((k) => p[k] !== e[k]) || (!S.iface && (p.prio !== prioNow || p.nice !== niceNow)) || p.tsched !== !!e.tsched || p.blocks !== (e.margin_blocks ?? 1) ||
      rateAsked(p, e) !== (e.fixed_rate || null);
    S.engineShown = JSON.stringify(engineShown(e));
    const now = roundTrip(e, rate), next = roundTrip(shown, rate);
    // Closed, it says what is set across all it holds (owner: "priority · nice" was two of five):
    // idle states off, kernel options waiting on a restart, the priority that runs, apps' audio.
    const sleepOff = (e.cstates?.states || []).filter((x) => x.off).map((x) => x.name);
    const kernelWaits = (e.kernel?.opts || []).some((x) => (x.ours || x.elsewhere.length > 0) !== x.now);
    const prioPart = h("span", { class: pending ? "warn-text" : "", text: `priority ${prioNow}${pending ? ` (asked ${e.prio})` : ""}` });
    const summary = [sleepOff.length ? `${sleepOff.join(", ")} off` : null, kernelWaits ? "kernel on restart" : null,
      prioPart, (e.app_rt || { on: true }).on ? "apps real time" : "apps normal", e.gpu?.full ? "GPU full speed" : null].filter(Boolean).flatMap((x, i) => (i ? [" · ", x] : [x]));
    // (its name in the labels' column: the triangle at the right end, owner)
    const adv = h("button", { type: "button", class: "adv-head", "aria-expanded": String(p.advanced), "data-k": "advanced" },
      h("span", { class: "name", text: "Advanced" }), p.advanced ? null : h("small", {}, ...summary),
      h("span", { class: "chev", "aria-hidden": "true" }));
    adv.addEventListener("click", () => { p.advanced = !p.advanced; drawEngine(); });
    const cancel = h("button", { type: "button", class: "btn quiet", text: "Cancel" });
    // Only the priority or nice changed: nothing that can break the sound, so it is set, not
    // tried for a minute (owner: "Save" or "Set"); the server decides the same way.
    const tried = p.block !== e.block || p.blocks !== (e.margin_blocks ?? 1) || p.tsched !== !!e.tsched || rateAsked(p, e) !== (e.fixed_rate || null);
    const go = h("button", { type: "button", class: "btn go", text: changed && !tried ? "Set" : "Try it", disabled: !changed });
    cancel.addEventListener("click", closeEngine);
    go.addEventListener("click", () => { send({ op: "engine-try", device: e.device, block: p.block, margin: p.margin, prio: p.prio, nice: p.nice, tsched: p.tsched, rate: rateAsked(p, e) }); closeEngine(); });
    const close = h("button", { type: "button", class: S.iface ? "eq-close" : "x", "aria-label": "Close", text: S.iface ? "✕" : "×" });
    close.addEventListener("click", closeEngine);
    const runs = e.dev_rate ? ` · ${kHz(e.dev_rate)}` : " · closed now";
    const trip = h("div", { class: "rt" },
      h("div", { class: "rt-top" }, h("span", { class: "what", text: S.iface ? "Round trip" : `Round trip through ${e.title || "the interface"}` }),
        resized && Math.abs(next - now) > 0.05 ? h("span", { class: "now struck", text: `${now.toFixed(1)} ms` }) : null,
        resized && Math.abs(next - now) > 0.05 ? h("span", { class: "arrow", text: "→" }) : null,
        h("span", { class: "new", text: next.toFixed(1) }, h("small", { text: "ms" }))),
      h("div", { class: "bars" }, resized ? bar(e, " ghost") : null, bar(shown, "")),
      h("div", { class: "legend" }, h("span", { text: "block, in and out" }), h("span", { class: "m", text: "safety margin" }),
        // (the buffers only: the card's own delay is not known here, owner asked that it be true)
        h("span", { class: "more", "data-tip": "The card's converters and its USB transfers add their own delay, not counted here.", text: "+ the card's own delay" })));
    // Cards on interrupts: their ALSA period is the block, a new block reopens them.
    const irq = (e.interfaces || []).filter((x) => x.rate && !x.tsched).map((x) => x.title);
    const note = changed && !tried ? "Applies at once, and stays."
      : !reopensNow(p, e) ? "Applies at once. A minute to Keep."
      : S.iface || p.block === e.block || !irq.length ? "The interface restarts: silent until it is back. Then a minute to Keep."
        : `${irq.join(", ")} restart${irq.length > 1 ? "" : "s"}: silent until back. Then a minute to Keep.`;
    const foot = h("div", { class: "eng-foot" }, h("span", { class: "note", text: note }), cancel, go);
    // The Audio engine: what is one for the whole graph and system; the interfaces listed.
    const global = () => [
      trip,
      row("Block size", seg(offered, "block", "Block size"), msOf(p.block)),
      h("p", { class: "hint", text: e.usb_step
        ? `How much sound is handled at once, in steps that fit ${e.title || "this card"}'s USB packets. Smaller: less delay, but more work and more risk of crackles.`
        : "How much sound is handled at once. Smaller: less delay, but more work and more risk of crackles." }),
      e.usb_step && !(e.blocks || []).includes(e.block)
        ? h("p", { class: "hint warn", text: `${e.block}, running now, doesn't fit those packets and can crackle.` }) : null,
      (e.interfaces || []).length ? row("Interfaces", ifaceList(e), "") : null,
      (e.interfaces || []).length ? h("p", { class: "hint", text: "Each sound card's own settings: click one, or the gear on its card." }) : null,
      adv,  // (no line above it: the box is its own edge)
      ...(p.advanced ? deepSleep(e, row) : []),
      ...(p.advanced ? kernelOpts(e, row) : []),
      ...(p.advanced ? gpuSpeed(e, row) : []),
      p.advanced ? row("Audio priority", h("div", {}, stepper("prio", 1, 95), h("span", { class: "of", text: "of 95" })), pending ? `runs ${prioNow}` : "") : null,
      p.advanced ? row("Control nice", h("div", {}, stepper("nice", -19, 0), h("span", { class: "of", text: "to −19" })), pending ? `runs ${niceNow}` : "") : null,
      p.advanced ? h("p", { class: `hint${pending ? " warn" : ""}`, text: pending
        ? `Running at ${prioNow} and ${niceNow} now, not the ${e.prio} and ${e.nice} set. Try it applies them now and after every restart.`
        : "How far the sound system goes ahead of other programs. It already goes first; raising this rarely helps." }) : null,
      ...(p.advanced ? appsRealtime(e, row) : []),
      foot];
    // An interface's own: its margin, rate and scheduling, its buffers.
    // Two columns, docked low and wide like an effect's panel: the settings, then its buffers.
    const own = () => [h("div", { class: "iface-cols" }, h("div", {},
      trip,
      row("Safety margin", marginSeg(p, e), ""),  // (each button says its time)
      h("p", { class: "hint", text: "Spare sound kept ready in case the computer is late. More: fewer crackles, more delay." }),
      // (no value beside it: the picked button says it, the head the rate it runs at; a UR44C's six
      // rates and Auto left no room, the value cut off at the panel's edge)
      e.rates?.length ? row("Sample rate", rateSeg(p, e), "") : null,
      // (the card clocking the graph sets everyone's rate, plugins too; owner: "so the sample rate IS global")
      e.rates?.length ? h("p", { class: "hint", text: e.clocks
        ? "Samples per second, from those this card supports. This card clocks the graph: everything runs at this rate. Changing it restarts the card."
        : "Samples per second, from those this card supports, converted to the graph's rate. Changing it restarts the card." }) : null,
      row("Scheduling", scheduling(p, e), ""),  // (the picked button says it)
      h("p", { class: "hint", text: "Interrupts: the card says when it needs sound. Timer: the computer keeps time itself, and block size changes don't restart the card." })),
      Object.keys(e.pcms || {}).length ? headroomBlock() : null),
      foot];
    const old = $("engine");
    const panel = S.iface
      ? h("section", { id: "engine", class: "eng eq-panel iface-panel", role: "dialog", "aria-label": `${e.title} settings`, tabindex: "-1" },
        h("header", { class: "eq-head" }, h("span", { class: "pin" }), h("span", { class: "eq-title", text: e.title || "Interface" }),
          h("span", { class: "eq-count", text: runs.slice(3) }), h("span", { class: "eq-space" }), close),
        h("div", { class: "eng-body" }, ...own()))
      : h("div", { id: "engine", class: "eng", role: "dialog", "aria-label": "Audio engine" },
        h("div", { class: "eng-head" }, h("b", { text: "Audio engine" }), h("span", { class: "sub", text: `clocked by ${e.title || "the interface"}${runs}` }), h("span", { class: "sp" }), close),
        h("div", { class: "eng-body" }, ...global()));  // (scrolls in a short window; the head and its tip stay)
    panel.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); closeEngine(); } });
    // (the position first: cssText replaces the whole style, the animation:none with it)
    if (S.iface && old?.classList.contains("iface-panel")) panel.style.cssText = old.style.cssText;  // (where it was moved to)
    if (again) panel.style.animation = "none";
    old?.remove();
    document.body.append(panel);
    if (focused) panel.querySelector(`[data-k="${focused}"]`)?.focus();
    if (S.iface) { movable(panel, `device:${S.iface}`); return; }
    const r = $("clock").getBoundingClientRect(), left = Math.max(12, Math.min(r.left - 30, innerWidth - panel.offsetWidth - 12));
    panel.style.left = `${left}px`;
    panel.style.setProperty("--tip", `${r.left + r.width / 2 - left - 5}px`);
    $("clock").classList.add("open");
  }
  // The trial: one bar under the top bar, counting down to going back (the server keeps time too).
  let trialTimer = null;
  // Built once per trial; each new state only updates its numbers in place. Rebuilt with every
  // state (the dropout count sends one every 2 s), a press on Keep and its release could land on
  // two different buttons, and the click was lost (owner: "I have to click Keep twice").
  function trialButtons(trial, waiting) {
    const back = h("button", { type: "button", class: "btn danger", text: "Revert", title: `Back to block ${trial.was.block} now` });
    const keep = h("button", { type: "button", class: "btn go", text: "Keep", disabled: waiting, title: waiting ? "Once its sound is back" : null });
    back.addEventListener("click", () => send({ op: "engine-back" }));
    keep.addEventListener("click", () => send({ op: "engine-keep" }));
    return [back, keep];
  }
  function showTrial(trial) {
    if (S.engine?.starting) { showStarting(S.engine.starting, trial); return; }
    if (!trial) { clearInterval(trialTimer); $("trial")?.remove(); return; }
    const v = trial.values, sig = JSON.stringify([v, trial.was]);
    let bar = $("trial");
    if (!bar || bar.dataset.sig !== sig) {
      clearInterval(trialTimer);
      bar?.remove();
      const [back, keep] = trialButtons(trial, false);
      const what = [`block ${v.block}`];
      if (v.margin !== v.block) what.push(`${v.margin / v.block}-block margin`);
      if (v.tsched !== trial.was.tsched) what.push(v.tsched ? "timer" : "interrupts");
      if ((v.rate || null) !== (trial.was.rate || null)) what.push(v.rate ? kHz(v.rate) : "its rate as before");
      // Design A (owner's pick): the seconds left in a ring, what is tried, then its numbers.
      bar = h("div", { id: "trial", class: "trial", role: "status", "data-sig": sig },
        h("span", { class: "ring", title: `Goes back to block ${trial.was.block} when it runs out` }, h("span", { class: "count" })),
        h("span", { class: "lines" },
          h("span", {}, "Trying ", h("b", { text: what.join(" · ") })),
          h("span", { class: "sub" }, `${roundTrip(v, S.engine.rate).toFixed(1)} ms round trip`, h("span", { class: "sep", text: "·" }),
            h("span", { class: "drops", title: "Clicks or gaps since this setting began" }),
            h("span", { class: "took warn-text" }))),
        back, keep);
      document.body.append(bar);
      bar.ends = performance.now() + trial.left * 1000;
      const ring = bar.querySelector(".ring"), count = bar.querySelector(".count");
      const tick = () => {
        const left = Math.max(0, Math.round((bar.ends - performance.now()) / 1000));
        count.textContent = String(left);
        ring.style.setProperty("--p", `${(left / 60) * 100}%`);
      };
      tick();
      trialTimer = setInterval(tick, 500);
    }
    const drops = bar.querySelector(".drops"), took = bar.querySelector(".took"), n = trial.dropouts;
    drops.className = `drops ${n ? "warn-text" : n === 0 ? "ok" : ""}`;
    drops.textContent = n == null ? "counting dropouts…" : `${n} dropout${n === 1 ? "" : "s"}`;
    took.hidden = !!trial.took;
    took.textContent = trial.took ? "" : ` · runs block ${S.engine.block}, margin ${S.engine.margin}: not as asked`;
  }

  // While a reopened interface comes back (server: starting) the same bar says so: its name (as
  // PipeWire calls it), the step, seconds counted up, Keep waiting; then a moment saying how long
  // it took (owner's pick, designs B + E). No time is promised: it differs per device.
  const STARTING = { reopen: "Reopening it", wires: "Reconnecting wires", sound: "Waiting for its sound" };
  function showStarting(st, trial) {
    const name = S.engine.title || "the audio interface", back = st.back != null;
    const sig = JSON.stringify(["starting", back, !!trial]);
    let bar = $("trial");
    if (!bar || bar.dataset.sig !== sig) {
      clearInterval(trialTimer);
      bar?.remove();
      bar = h("div", { id: "trial", class: `trial${back ? " good" : ""}`, role: "status", "data-sig": sig },
        h("span", { class: `ring ${back ? "done" : "spin"}` }, h("span", { class: "count" })),
        h("span", { class: "lines" },
          back ? h("span", {}, h("b", { text: name }), " is back") : h("span", {}, "Starting ", h("b", { text: name }), "…"),
          h("span", { class: "sub" })),
        ...(trial ? trialButtons(trial, !back) : []));
      document.body.append(bar);
      if (!back) trialTimer = setInterval(() => { bar.querySelector(".count").textContent = String(Math.floor((performance.now() - bar.since) / 1000)); }, 250);
    }
    const count = bar.querySelector(".count"), sub = bar.querySelector(".sub");
    if (back) {
      count.textContent = "✓";
      sub.replaceChildren(`Sound returned after ${st.back} s`, ...(trial ? [h("span", { class: "sep", text: "·" }), "now try it for a minute"] : []));
      return;
    }
    bar.since = performance.now() - st.elapsed * 1000;
    count.textContent = String(Math.floor(st.elapsed));
    const at = Object.keys(STARTING).indexOf(st.step);
    sub.replaceChildren(h("span", { class: "dots", title: "Reopened · wires back · sound back" },
      ...Object.keys(STARTING).map((_, i) => h("i", { class: i < at ? "on" : i === at ? "now" : "" }))), STARTING[st.step] ?? "");
  }

  function showClock(clock) {
    $("clock-text").textContent = clock ? `${clock.rate / 1000} kHz · ${clock.quantum} · ${(clock.quantum / clock.rate * 1000).toFixed(1)} ms` : "";
  }
  let tipTimer = null;
  function hideTip() { clearTimeout(tipTimer); $("tip").hidden = true; }

  function tip(e) {
    const el = e.target.closest?.("[data-tip]"), box = $("tip");
    clearTimeout(tipTimer);
    if (!el) { box.hidden = true; hover(null); return; }
    if (menus.length || document.querySelector(".veil")) { box.hidden = true; return; }  // a menu or dialog is up
    const card = e.target.closest(".card");
    if (card && !S.drag) hover(null, card.dataset.key);
    tipTimer = setTimeout(() => {
      fillTip(box, el.dataset.tip);
      box.hidden = false;
      const r = box.getBoundingClientRect();
      box.style.left = `${Math.min(e.clientX + 14, innerWidth - r.width - 8)}px`;
      box.style.top = `${Math.min(e.clientY + 16, innerHeight - r.height - 8)}px`;
    }, 450);
  }
  // A tooltip: plain text, or {title, sub, kind, wire, rows: [[label, value, mono]], warn, hint}.
  function fillTip(box, raw) {
    box.textContent = "";
    let t;
    try { t = raw.startsWith("{") ? JSON.parse(raw) : null; } catch { t = null; }
    box.className = `tip${t ? " rich" : ""}${t?.kind ? ` k-${t.kind}` : ""}`;
    if (!t) { box.textContent = raw; return; }
    if (t.wire) {
      box.classList.add(`k-${t.wire[0].kind}`);
      box.append(h("div", { class: "tip-wire" }, ...t.wire.flatMap((e, i) => [
        i ? h("span", { class: "tip-arrow", text: "→" }) : null,
        h("span", { class: `tip-end k-${e.kind}` }, h("i", { class: "tip-dot" }), e.text)]).filter(Boolean)));
    }
    if (t.title) box.append(h("div", { class: "tip-title", text: t.title }));
    if (t.sub) box.append(h("div", { class: "tip-sub" }, h("i", { class: "tip-dot" }), t.sub));
    if (t.warn) box.append(h("div", { class: "tip-warn", text: t.warn }));
    if (t.rows?.length) {
      box.append(h("dl", { class: "tip-rows" }, ...t.rows.flatMap(([k, v, mono]) => [
        h("dt", { text: k }), h("dd", { class: mono ? "mono" : "", text: v })])));
    }
    if (t.hint) box.append(h("div", { class: "tip-hint", text: t.hint }));
  }

  let toastTimer = null;
  function toast(text, level = "error") {
    const el = $("toast");
    el.textContent = text; el.className = `toast ${level}`; el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
  }

  // ---- wiring up the page -------------------------------------------------------------------
  function bind() {
    const css = getComputedStyle(document.documentElement);
    for (const k of Object.keys(KIND)) colors[k] = css.getPropertyValue(`--${k}`).trim();
    board.addEventListener("mousedown", onDown);
    addEventListener("mousemove", onMove);
    addEventListener("mouseup", onUp);
    board.addEventListener("auxclick", (e) => { if (e.button === 1) e.preventDefault(); });  // (no middle-click paste)
    board.addEventListener("wheel", (e) => { e.preventDefault(); zoomAt(e.clientX, e.clientY, S.view.z * Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
    board.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      hideTip();
      const wire = e.target.closest?.(".wire-g, .portal-end");
      if (wire) return wireMenu(wire.dataset.wire, e.clientX, e.clientY);
      const card = e.target.closest(".card");
      if (card) cardMenu(card.dataset.key, e.clientX, e.clientY); else boardMenu(e.clientX, e.clientY);
    });
    $("add").addEventListener("click", (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      openMenu(r.left, r.bottom + 6, addNodeItems());
      S.menuAt = toWorld(innerWidth / 2, innerHeight / 2);
    });
    $("fit").addEventListener("click", () => fit());
    $("wires-btn").addEventListener("click", wiresMenu);
    $("tidy-more").addEventListener("click", tidyMenu);
    document.addEventListener("visibilitychange", sayVisible);
    $("profile").addEventListener("click", (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      profileMenu(r.left, r.bottom + 6);
    });
    $("save-changes").addEventListener("click", () => send({ op: "profile-save" }));
    $("changes").addEventListener("mouseenter", () => { S.movesHover = true; showMoves(); });
    $("changes").addEventListener("mouseleave", () => { S.movesHover = false; showMoves(); });
    $("revert-changes").addEventListener("click", () => send({ op: "profile-revert" }));
    $("tidy").addEventListener("click", tidy);
    $("clock").addEventListener("click", () => { const was = $("engine") && !S.iface; closeEngine(); if (!was) openEngine(); });
    $("show-latency").addEventListener("change", (e) => {  // (a setting, like Animate)
      S.showLatency = e.target.checked;
      if (!S.showLatency) closePath();
      send({ op: "show-latency", on: e.target.checked });
      render();
    });
    $("chip-disconnect").addEventListener("click", unlinkSelected);
    $("chip-insert").addEventListener("click", (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const id = S.selected;
      S.menuAt = toWorld(r.left, r.bottom + 30);
      openMenu(r.left, r.bottom + 6, [{ head: "Insert into this wire" }, ...addNodeItems({ insert: id })]);
      S.menuAt = toWorld(r.left, r.bottom + 30);
    });
    $("problems-btn").addEventListener("click", () => toggleProblems());
    addEventListener("keydown", (e) => {
      if (e.target.isContentEditable || e.target.tagName === "INPUT") return;
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (S.profile?.active) send({ op: "profile-save" });
        return;
      }
      if (e.key === "Delete" || e.key === "Backspace") unlinkSelected();
      if (e.key === "Escape") { cancelWire(); closeMenu(); select(null); toggleProblems(false); }
      if (e.key === "f" || e.key === "F") fit();
    });
    addEventListener("mousedown", (e) => { hideTip(); if (!e.target.closest(".menu, #add, #profile")) closeMenu(); }, true);
    addEventListener("resize", () => placeChip());
    addEventListener("error", (e) => S.errors.push(String(e.message)));
    const err = console.error.bind(console);
    console.error = (...a) => { S.errors.push(a.join(" ")); err(...a); };
  }

  // ---- self-audit for scripts/shots.py ------------------------------------------------------
  function audit() {
    const faults = [...S.errors];
    const rect = (el) => el && !el.hidden && el.getClientRects().length ? el.getBoundingClientRect() : null;
    const cards = Object.entries(S.els).map(([k, el]) => [k, el.getBoundingClientRect()]);
    for (let i = 0; i < cards.length; i++) {
      for (let j = i + 1; j < cards.length; j++) {
        const [a, b] = [cards[i][1], cards[j][1]];
        if (a.left < b.right - 2 && b.left < a.right - 2 && a.top < b.bottom - 2 && b.top < a.bottom - 2)
          faults.push(`cards overlap: ${cards[i][0]} and ${cards[j][0]}`);
      }
      const r = cards[i][1];
      if (!S.moved && (r.right > innerWidth || r.bottom > innerHeight || r.left < 0 || r.top < 0))
        faults.push(`card off screen: ${cards[i][0]}`);
    }
    // A tag or badge must stack above the card's outline layer (it once sat under it). Hit-testing
    // cannot tell: the outline ignores the mouse. So compare the stacking order itself.
    for (const tag of document.querySelectorAll(".card .badge, .card .warn, .card .lat, .card .unsaved-dot")) {
      const card = tag.closest(".card"), z = (s) => parseInt(s.zIndex, 10) || 0;
      if (z(getComputedStyle(tag)) <= z(getComputedStyle(card, "::before")))
        faults.push(`under the card outline: "${tag.textContent}" on ${card.dataset.key}`);
    }
    if (board.classList.contains("show-moves") && !cardsEl.querySelector(".card.unsaved")) faults.push("every card dimmed: no unsaved change to show");
    if (!$("tip").hidden && (menus.length || document.querySelector(".veil"))) faults.push("a tooltip shows over an open menu");
    const menuBoxes = [...document.querySelectorAll(".menu")].map((m) => m.getBoundingClientRect());
    for (let i = 0; i < menuBoxes.length; i++) {
      for (let j = i + 1; j < menuBoxes.length; j++) {
        const [a, b] = [menuBoxes[i], menuBoxes[j]];
        if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)
          faults.push(`menu ${j} covers menu ${i}`);
      }
    }
    for (const [k, el] of Object.entries(S.els)) {
      if (el.offsetWidth % GRID || el.offsetHeight % GRID) faults.push(`card off the grid: ${k} is ${el.offsetWidth}×${el.offsetHeight}`);
      const sz = S.sizes[k], p = S.pos[k];  // whole cells whatever the zoom: Tidy must not depend on float noise
      if (sz && !(Number.isInteger(sz.w) && Number.isInteger(sz.h))) faults.push(`card size not whole cells: ${k} ${sz.w}×${sz.h}`);
      if (p && S.drag?.key !== k && !(Number.isInteger(p.x) && Number.isInteger(p.y))) faults.push(`card not on a cell: ${k} at ${p.x},${p.y}`);  // (a dragged one follows the mouse)
      for (const [sock, o] of Object.entries(S.offsets[k] || {}))
        if (Math.abs(o.y - snap(o.y)) > 0.6 || Math.abs(o.x - snap(o.x)) > 0.6) faults.push(`socket off the grid: ${k} ${sock} at ${o.x},${o.y}`);
    }
    for (const d of document.querySelectorAll(".card .sock .dot")) {  // centred on the edge (owner: outs were not)
      const c = d.closest(".card").getBoundingClientRect(), r = d.getBoundingClientRect();
      const edge = d.closest(".sock").dataset.side === "in" ? c.left : c.right;
      if (Math.abs((r.left + r.right) / 2 - edge) > 0.6) faults.push(`socket dot off its edge by ${((r.left + r.right) / 2 - edge).toFixed(1)} px: ${d.closest(".card").dataset.key} ${d.closest(".sock").dataset.sock}`);
    }
    for (const sp of document.querySelectorAll(".sock span, .menu .item span"))
      if (sp.scrollWidth > sp.clientWidth + 1) faults.push(`text clipped: "${sp.textContent}"`);
    for (const t of document.querySelectorAll(".card .title"))
      if (t.scrollWidth > t.clientWidth + 1 && t.parentElement.parentElement.offsetWidth < 255) faults.push(`title clipped though the card could grow: "${t.textContent}"`);
    for (const el of [...["bar", "problems", "problems-btn", "unlink", "toast", "changes"].map($),
                      ...document.querySelectorAll(".rename-hint, .menu, .dialog, .badge, .eq-panel, .eng, .trial, .latpath")]) {
      const r = rect(el), id = el.id || el.className;
      if (r && (r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5 || r.left < -0.5 || r.top < -0.5)) faults.push(`#${id} leaves the window`);
    }
    faults.push(...EQ.selftest());
    faults.push(...DIAG.selftest());
    for (const wrap of document.querySelectorAll(".eq-mini-wrap")) {  // a gap beside the card's curve (owner saw one)
      const svg = wrap.querySelector("svg");
      if (svg && Math.abs(svg.getBoundingClientRect().width - wrap.getBoundingClientRect().width) > 1) faults.push(`an EQ card's curve does not fill its box (${Math.round(svg.getBoundingClientRect().width)} of ${Math.round(wrap.getBoundingClientRect().width)})`);
    }
    // Hidden must mean unseen: a display rule beats the hidden attribute (the drag's L/R tag once
    // stayed on screen after the drag).
    for (const el of document.querySelectorAll("[hidden]"))
      if (getComputedStyle(el).display !== "none") faults.push(`#${el.id || el.className} is hidden but still shows`);
    const eqp = document.querySelector(".eq-panel");
    if (eqp) {  // the EQ panel: its menus open above it, its band strip fits, its curves draw
      const z = (el) => parseInt(getComputedStyle(el).zIndex, 10) || 0;
      for (const m of document.querySelectorAll(".menu")) if (z(m) <= z(eqp)) faults.push("a menu opens under the EQ panel");
      const st = eqp.querySelector(".eq-strip");
      if (st && st.scrollWidth > st.clientWidth + 1) faults.push(`the EQ's band strip overflows (${st.scrollWidth} > ${st.clientWidth})`);
      for (const p of eqp.querySelectorAll("path")) if ((p.getAttribute("d") || "").includes("NaN")) faults.push("an EQ curve has NaN in it");
    }
    // Diagnostic displays that are on and have heard sound: drawn (some pixel lit).
    for (const c of document.querySelectorAll(".diag-display:not(.off) canvas")) {
      if (!DIAG.heard(c.closest("[data-key]")?.dataset.key || DIAG.openKey())) continue;
      const px = c.width && c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      if (!px || !px.some((v, i) => i % 4 === 3 && v > 0)) faults.push(`a diagnostic display drew nothing: ${c.closest("[data-key], .eq-panel")?.dataset?.key || "the panel"}`);
    }
    // Turned off: an empty box, not the last picture held (owner).
    for (const c of document.querySelectorAll(".diag-display.off canvas")) {
      if (c.width && c.getContext("2d").getImageData(0, 0, c.width, c.height).data.some((v, i) => i % 4 === 3 && v > 0)) faults.push(`a diagnostic display that is off still shows its picture: ${c.closest("[data-key]")?.dataset.key}`);
    }
    const bar = rect($("bar")), pb = rect($("problems-btn"));
    if (bar && pb && bar.right > pb.left - 8) faults.push("the bar runs into the problems button");
    for (const p of layer.querySelectorAll("path")) if ((p.getAttribute("d") || "").includes("NaN")) faults.push("a wire has NaN in its path");
    const drawn = Object.values(S.wireEls).filter((x) => x.g.style.display !== "none").length;
    if (drawn !== visibleWires().length) faults.push(`drew ${drawn} of ${visibleWires().length} wires`);
    $("audit").textContent = JSON.stringify(faults);
  }

  const debug = {
    openProblems: () => toggleProblems(true),
    // Notifications with made-up events: "mix" (a dropout going on with the GPU named as its cause,
    // a clipping 4 times, an EQ that stopped, a device gone, already seen), or none ("none").
    // Unsaved changes as the server would send them (the preview has no server): the `moved` cards
    // saved [dx, dy] cells from where they are, the `changed` ones otherwise changed; `hover`: the
    // changes card under the mouse.
    fakeUnsaved: (moved = {}, changed = [], hover = false) => {
      const unsaved = Object.fromEntries(changed.map((k) => [k, {}]));
      for (const [k, [dx, dy]] of Object.entries(moved)) unsaved[k] = { x: S.pos[k].x + dx, y: S.pos[k].y + dy };
      const n = Object.keys(moved).length, title = (k) => byKey()[k].title;
      S.profile = { ...S.profile, unsaved, changes: [...(n ? [`Moved ${n} card${n > 1 ? "s" : ""}`] : []),
                                                    ...changed.map((k) => `Changed the settings of ${title(k)}`)] };
      render();
      if (hover) $("changes").dispatchEvent(new MouseEvent("mouseenter"));
    },
    // The board dragged about by `drags` ([key, dx, dy] px) and saved so, then tidied: Tidy shows
    // where its cards came from.
    tidyMoves: (drags) => {
      for (const [k, dx, dy] of drags) { debug.drag(k, dx, dy); onUp(); }
      const was = structuredClone(S.pos);
      tidy();
      const moved = Object.keys(was).filter((k) => S.els[k] && (was[k].x !== S.pos[k].x || was[k].y !== S.pos[k].y));
      S.profile = { ...S.profile, unsaved: Object.fromEntries(moved.map((k) => [k, was[k]])),
                    changes: [`Moved ${moved.length} cards`, "Made 3 portals"] };
      render();
    },
    fakeEvents: (kind = "mix", showSeen = false) => {
      const now = Date.now() / 1000, ur = S.model.cards.find((c) => c.kind === "hardware")?.key, ev = (o) => ({ cards: [], data: {}, peak: {}, count: 1, active: false, new: true, seen: null, ...o });
      S.events = kind === "none" ? [] : [
        ev({ id: "dropouts", kind: "dropouts", severity: "warning", title: "Sound dropped out", where: titleOf(byKey()[ur] || { title: "Studio Interface" }), what: "", fix: "Give it more room: a bigger safety margin (the gear on Studio Interface).",
             cards: [ur], data: { minute: 1680, cause: "gpu", saving: 28.4, up: 0 }, peak: { minute: 1712 }, count: 3, since: now - 140, last: now, active: true }),
        ev({ id: "clipping:music_player:out", kind: "clipping", severity: "warning", title: "Too loud", where: "Studio Interface", what: "It gets sound louder than it can play, so it distorts.", fix: "Turn down what feeds it, or put a Limiter right before it.",
             cards: ["music_player"], count: 4, since: now - 600, last: now - 560, ended: now - 560 }),
        ev({ id: "stopped:eq", kind: "stopped", severity: "error", title: "Calf Vinyl couldn't load", where: "Calf Vinyl", what: "This plugin isn't installed on this computer.", fix: "Install it, or remove its card.",
             since: now - 3 * 3600, last: now - 3 * 3600, ended: now - 3 * 3600 }),
        ev({ id: "clipping:desktop_mix:out", kind: "clipping", severity: "warning", title: "Too loud", where: "Desktop Mix", what: "It gets sound louder than it can play, so it distorts.", fix: "Turn down what feeds it, or put a Limiter right before it.",
             cards: ["desktop_mix"], count: 31, since: now - 90, last: now - 80, ended: now - 80, new: false, muted: true }),
        ev({ id: "missing:pedal", kind: "missing", severity: "warning", title: "Pedal isn't connected", where: "Pedal", what: "It's unplugged or turned off; its 3 connections wait for it and come back with it.", fix: "Plug it back in, or right-click its card to use another device.",
             since: now - 26 * 3600, last: now - 25 * 3600, ended: now - 25 * 3600, new: false, seen: now - 24 * 3600 }),
      ];
      S.showSeen = S.showMuted = showSeen;
      if (kind === "mix") {  // (a plugin not installed here: a problem, not an event per launch)
        S.events = S.events.filter((e) => e.kind !== "stopped");
        S.problems = [...S.problems, { severity: "warning", title: "Calf Vinyl isn't installed", where: "Calf Vinyl", what: "PipeBoard can't load this plugin: it isn't on this computer.",
                                       fix: "Install it, or remove its card.", cards: [], wires: [] }];
      }
      renderProblems();
      toggleProblems(true);
    },
    pending: (key, sock, x, y) => { startWire(document.querySelector(`.sock[data-key="${key}"][data-sock="${sock}"]`), { clientX: x, clientY: y }); },
    select, wires: () => visibleWires(),
    // A wire from one socket held over another (not let go): the target lit, the channel tag up.
    dragTo: (key, sock, toKey, toSock) => {
      startWire(document.querySelector(`.sock[data-key="${key}"][data-sock="${sock}"]`), { clientX: 0, clientY: 0 });
      const r = document.querySelector(`.sock[data-key="${toKey}"][data-sock="${toSock}"] .dot`).getBoundingClientRect();
      moveWire(r.left + r.width / 2, r.top + r.height / 2);
      if ($("drag-side").hidden) S.errors.push("no channel tag over a mono input");
    },
    // The same wire held over empty board: the tag still says which channel it carries.
    dragFree: (key, sock, x, y) => {
      startWire(document.querySelector(`.sock[data-key="${key}"][data-sock="${sock}"]`), { clientX: 0, clientY: 0 });
      moveWire(x, y);
      if ($("drag-side").textContent !== "L") S.errors.push(`held from a stereo output, the tag says "${$("drag-side").textContent}"`);
    },
    // Opens the board menu at x, y and then the submenus named in path, as a hover would.
    menu: (x, y, path = []) => {
      boardMenu(x, y);
      for (const label of path) {
        const row = [...menus.at(-1).querySelectorAll(".item")].find((r) => r.textContent.startsWith(label));
        row?.click();
      }
    },
    cardMenu: (key) => { const r = S.els[key].getBoundingClientRect(); cardMenu(key, r.right - 20, r.top + 20); },
    // Plugins: the picker (with the fixture's list, searched for query), a card's controls, an open editor.
    pickPlugin: (list, query = "", added) => {  // added: as a Rescan's answer says
      S.pluginList = list.map((p) => ({ ...p, ref: `${p.format}:${p.path || ""}:${p.label}` }));
      pickPlugin({});
      const box = document.querySelector(".picker input");
      box.value = query; box.dispatchEvent(new Event("input"));
      if (added !== undefined) S.picker(added);
    },
    editorOpen: (key) => { S.editors = [key]; render(); },
    // The EQ: open its panel with band sel picked and a made-up spectrum behind the curve.
    eqOpen: (key, sel = 0, spectrum = true) => { openEq(key); EQ.debug.select(sel); if (spectrum) EQ.debug.spectrum(); },
    eqDrag: (key, i, dx, dy) => { openEq(key); EQ.debug.spectrum(); EQ.debug.drag(i, dx, dy); },
    eqMenu: (key, i) => { openEq(key); EQ.debug.spectrum(); EQ.debug.menu(i); },
    eqBypass: (key) => eqBypass(key),
    // A limiter or compressor: its panel with 6 s of made-up sound; drag: its line held at that dB.
    dynFlag: (key, flag) => dynFlag(key, flag),
    // Clicks the open panel's title: its card is framed on the board and flashed.
    panelTitle: () => {
      S.moved = true;  // the view frames one card on purpose: others may be off screen
      const title = document.querySelector(".eq-panel .eq-title"), r = title.getBoundingClientRect();
      const at = { clientX: r.left + 5, clientY: r.top + r.height / 2, pointerId: 9, bubbles: true, button: 0 };
      title.dispatchEvent(new PointerEvent("pointerdown", at));
      title.dispatchEvent(new PointerEvent("pointerup", at));
    },
    // Drags the open panel by its header by dx, dy (as a person would, with the pointer).
    panelMove: (dx, dy) => {
      const head = document.querySelector(".eq-panel .eq-head"), r = head.querySelector(".eq-title").getBoundingClientRect();
      const at = { clientX: r.right + 30, clientY: r.top + r.height / 2, pointerId: 9, bubbles: true, button: 0 };
      head.dispatchEvent(new PointerEvent("pointerdown", at));
      head.dispatchEvent(new PointerEvent("pointermove", { ...at, clientX: at.clientX + dx, clientY: at.clientY + dy }));
      head.dispatchEvent(new PointerEvent("pointerup", { ...at, clientX: at.clientX + dx, clientY: at.clientY + dy }));
    },
    dynOpen: (key, drag = null) => { openDyn(key); if (drag != null) DYN.debug.drag(drag); DYN.debug.fill(); },
    // Scrolls on band i (ticks up) and double-clicks band j, as a person would; the shot's audit
    // then checks what that did (Q narrower, slope steeper, band j off).
    eqGestures: (key, i, ticks, j) => {
      openEq(key); EQ.debug.spectrum();
      const q0 = EQ.debug.band(i).q, s0 = EQ.debug.band(0).slope, n0 = EQ.debug.count();
      EQ.debug.wheel(i, ticks); EQ.debug.wheel(0, 1); EQ.debug.dblclick(j);
      const want = [[EQ.debug.band(i).q < q0, `wheel up did not widen band ${i + 1}`],
                    [EQ.debug.band(0).slope > s0, "wheel up did not steepen the cut"],
                    [EQ.debug.band(j).on === false, `double-click did not turn band ${j + 1} off`],
                    [EQ.debug.count() === n0, `double-clicking a band added one (${n0} → ${EQ.debug.count()})`]];
      S.errors.push(...want.filter(([ok]) => !ok).map(([, why]) => why));
      EQ.debug.select(i);
    },
    rename: (key) => startRename(key),
    profileMenu: (path = []) => {
      const r = $("profile").getBoundingClientRect();
      profileMenu(r.left, r.bottom + 6);
      for (const label of path) [...menus.at(-1).querySelectorAll(".item")].find((x) => x.textContent.startsWith(label))?.click();
    },
    showPlan, saveAs, askDelete, askTakeOver,
    // Tidy, then every column's rows must be evenly spaced, one gap apart (owner).
    // Tidy with the columns cut where asked ([] one row; [k] before column k…); what it gives.
    tidyCuts: (cuts) => { S.forceCuts = cuts; tidy(); S.forceCuts = null; return S.foldInfo; },
    columns: () => { S.forceCuts = []; layout(visible().map((c) => c.key)); S.forceCuts = null; return S.foldInfo.n; },
    tidy: () => {
      tidy();
      const cols = {};
      for (const [k, p] of Object.entries(S.pos)) if (S.els[k]) (cols[p.x] ||= []).push({ y: p.y, h: S.sizes[k].h });
      for (const col of Object.values(cols)) {
        col.sort((a, b) => a.y - b.y);
        const gaps = col.slice(1).map((c, i) => Math.round(c.y - col[i].y - col[i].h));
        if (gaps.some((g) => g !== gaps[0])) S.errors.push(`tidy: uneven rows in a column, gaps ${gaps.join(", ")}`);
      }
      // Portals: every wire between rows is one; no badge on a card or on another badge.
      const { rowOf } = S.fold, ends = [...document.querySelectorAll(".portal-end")].map((e) => e.querySelector(".portal-disc").getBoundingClientRect());
      for (const w of visibleWires()) if (rowOf[w.from[0]] !== rowOf[w.to[0]] && !S.ws.portals?.[w.id]) S.errors.push(`tidy: a wire between rows is not a portal: ${w.id}`);
      const cardBoxes = Object.values(S.els).map((el) => el.getBoundingClientRect());
      const meet = (p, q) => p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom;
      ends.forEach((p, i) => {
        if (cardBoxes.some((q) => meet(p, q))) S.errors.push("tidy: a portal badge sits on a card");
        if (ends.some((q, j) => j > i && meet(p, q))) S.errors.push("tidy: two portal badges overlap");
      });
    },
    // Four portals by hand on neighbouring wires; one toggled off and on: every badge must be where
    // it was (owner: one moved), and none may overlap another.
    // Tidy is the same at any zoom (owner: zoom is only how it looks): positions, portals and
    // socket offsets laid out at six zooms, every one compared with zoom 1.
    // Diagnostic cards fed made-up sound (no server in the shots); one opened large; one off.
    fakeDiag: (wide = 1, rich = false, gen = null) => { DIAG.fake(Object.keys(S.ws.devices || {}).filter((k) => DIAG.TYPES[S.ws.devices[k].type]), wide, rich, gen); },
    openDiag: (key) => { openDiag(key); if (!DIAG.isOpen(key)) S.errors.push("the diagnostic panel did not open"); },
    // A quiet sound zoomed up reaches the picture's centre: a fixed level floor (−66 dBFS) once
    // left a hole there that grew as the sound got quieter (owner: "the center circle cutout").
    stereoCentre: () => setTimeout(() => {
      const c = [...document.querySelectorAll(".diag-display.stereo canvas")].sort((a, b) => b.width - a.width)[0];
      const dpr = devicePixelRatio || 1, sc = Math.min((c.width - 12 * dpr) / 200, (c.height - 6 * dpr) / 104), r = Math.round(6 * sc);
      const px = c.getContext("2d").getImageData(Math.round(c.width / 2 - r), Math.round(c.height - 2 * dpr - 3 * sc - r), 2 * r, r).data;
      if (!px.some((v, i) => i % 4 === 3 && v > 0)) S.errors.push("a quiet sound left the stereo picture's centre empty");
      PB.audit();
    }, 600),
    diagSet: (key, field, on) => setDiag(key, field, on),
    diagMono: (key) => {
      [...(S.els[key]?.querySelectorAll(".diag-mode button") || [])].find((b) => b.textContent === "Mono")?.click();
      if (!S.ws.devices[key].mono) S.errors.push("the Mono switch did not take");
    },
    // The card's Mono clicked while its panel is open: the panel's switch shows Mono too.
    diagSync: (key) => {
      openDiag(key);
      [...(S.els[key]?.querySelectorAll(".diag-mode button") || [])].find((b) => b.textContent === "Mono")?.click();
      const lit = document.querySelector(".diag-panel .diag-mode button[aria-pressed=\"true\"]")?.textContent;
      if (lit !== "Mono") S.errors.push(`the panel's switch shows ${lit}, the card's Mono`);
    },
    diagSmooth: (key) => {
      [...(S.els[key]?.querySelectorAll(".diag-toggle") || [])].find((b) => b.textContent === "Smooth")?.click();
      if (!S.ws.devices[key].smooth) S.errors.push("the Smooth toggle did not take");
    },
    diagOff: (key) => {
      S.els[key]?.querySelector(".diag-row .bypass")?.click();
      if (!S.ws.devices[key].bypass) S.errors.push("the power button did not turn it off");
    },
    tidyAnyZoom: () => {
      const run = (z) => {
        S.view = { ...S.view, z }; applyView(false); measure();
        const keys = layout(visible().map((c) => c.key));
        return JSON.stringify([keys.map((k) => [k, S.pos[k]]), foldPortals(), S.offsets]);
      };
      const want = run(1);
      for (const z of [0.35, 0.47, 0.83, 1.31, 1.75]) if (run(z) !== want) S.errors.push(`Tidy differs at zoom ${z}`);
      run(1); tidy();
    },
    noPortals: () => { setTidyPortals(false, false); if (S.ws.tidy_portals !== false) S.errors.push("Portals did not turn off"); },
    showLatency: () => { $("show-latency").click(); },
    liveInstant: () => setWires("instant"),
    // The Wires and Tidy menus open (their rows, the current one marked).
    wiresMenu: () => $("wires-btn").click(),
    tidyMenu: () => $("tidy-more").click(),
    openPath: (key) => openPath(key),
    // The path panel open and the view on its cards, room left for the panel (README picture).
    pathView: (key) => {  // (key: a card's key, or its title)
      key = S.model.cards.find((c) => c.key === key || titleOf(c) === key)?.key || key;
      openPath(key);
      const keys = S.model.latency?.[key]?.path;
      if (!keys) { S.errors.push(`pathView: no path into ${key}`); return; }
      fit(false, keys, 1.1, $("latpath").offsetHeight + 14);  // (the whole path above the panel)
      if (S.mode === "preview") { S.drops = Object.fromEntries(keys.map((k) => [k, 0])); showDrops(); }  // (no server: none counted)
    },
    // A new state arriving with the panel open: the same panel stays (no redraw, no animation).
    pathStays: () => {
      const before = $("latpath");
      load({ ...S.lastState, show_latency: true });  // (as the server would say: the box is on)
      if ($("latpath") !== before) S.errors.push("the path panel was drawn again by a state that changed nothing");
    },
    pathDrops: (counts) => { S.drops = counts; showDrops(); },
    // A new state during a trial (its dropout count): the same bar, the same Keep button.
    // The bar while the interface comes back: a step, or the moment it is back.
    starting: (step, back) => {
      S.engine.starting = back ? { back } : { step, elapsed: 8 };
      showTrial(S.engine.trial);
      if (!document.querySelector("#trial .lines b")?.textContent) S.errors.push("the starting bar names no interface");
    },
    trialStays: () => {
      const keep = document.querySelector("#trial .btn.go");
      load(S.lastState);
      if (document.querySelector("#trial .btn.go") !== keep) S.errors.push("a new state replaced the trial bar's Keep button");
    },
    pathHover: (i) => document.querySelectorAll("#latpath .seg")[i]?.dispatchEvent(new MouseEvent("mouseenter")),
    problemsAway: () => {
      toggleProblems(true);
      $("board").dispatchEvent(new MouseEvent("mousedown", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      dispatchEvent(new MouseEvent("mouseup", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      if (!$("problems").hidden) S.errors.push("a press on the board left the problems list open");
    },
    engineAway: () => {
      openEngine();
      $("board").dispatchEvent(new MouseEvent("mousedown", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      dispatchEvent(new MouseEvent("mouseup", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      if ($("engine")) S.errors.push("a press on the board left the Audio engine panel open");
    },
    // One popover at a time (owner): each top-bar dropdown opened after another leaves only itself.
    onePopover: () => {
      const open = () => [document.querySelector(".menu") && "menu", $("engine") && !S.iface && "engine", !$("problems").hidden && "problems"].filter(Boolean);
      for (const id of ["clock", "profile", "problems-btn", "wires-btn", "clock", "add", "tidy-more", "problems-btn", "clock"]) {
        $(id).click();
        if (open().length !== 1) S.errors.push(`after ${id}: ${open().join(" + ") || "nothing"} open`);
      }
    },
    // A click on empty board closes an interface's panel, as it does an effect's (owner).
    ifaceAway: () => {
      openIface(S.engine.interfaces[0].key);
      if (!$("engine")) S.errors.push("the interface's panel did not open");
      $("board").dispatchEvent(new MouseEvent("mousedown", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      dispatchEvent(new MouseEvent("mouseup", { clientX: 760, clientY: 820, bubbles: true, button: 0 }));
      if ($("engine")) S.errors.push("a click on the board left the interface's panel open");
    },
    // A plain click on empty board (no drag) closes the path panel (owner).
    clickBoard: (x, y) => {
      $("board").dispatchEvent(new MouseEvent("mousedown", { clientX: x, clientY: y, bubbles: true, button: 0 }));
      dispatchEvent(new MouseEvent("mouseup", { clientX: x, clientY: y, bubbles: true, button: 0 }));
      if ($("latpath")) S.errors.push("a click on the board left the path panel open");
    },
    // Buffer headroom made up: "ok" (the owner's, at block 512), "close" (dips near 0), "wait" (none yet).
    fakeHeadroom: (kind = "ok", engine = null) => {
      if (engine) { Object.assign(S.engine, engine); if ($("engine")) openEngine(); }
      const wob = (base, amp, dips, len = 120) => Array.from({ length: len }, (_, i) => +(base + amp * Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.6)) - (dips[i] || 0)).toFixed(2));
      // "saw": the owner's at block 128, one block of margin: the output creeping up for ~38 s and
      // dropping back (the interface's clock 6.5 ppm slow against USB's), the input spiking.
      const saw = (i) => +(0.2 + 0.25 * (((i + 26) % 76) / 75) + 0.01 * Math.sin(i * 2.3)).toFixed(2);
      if (kind === "dry") { S.headroom = { every: 0.5, out: wob(0.6, 1.2, { 30: 1.1, 61: 1.4, 62: 1.2, 90: 1.0, 101: 1.3 }), in: wob(1.6, 0.4, { 50: 1.0 }) }; showHeadroom(); return; }
      if (kind === "saw") { S.headroom = { every: 0.5, out: Array.from({ length: 120 }, (_, i) => saw(i)), in: Array.from({ length: 120 }, (_, i) => +(0.8 + (i % 9 === 4 ? 0.35 : 0.03 * Math.sin(i))).toFixed(2)) }; showHeadroom(); return; }
      // "idle": nothing plays through the output (its buffer closed all minute); "input": a card
      // with an input only (a USB mic).
      if (kind === "idle" || kind === "input") {
        S.headroom = { every: 0.5, sides: kind === "idle" ? ["in", "out"] : ["in"], out: kind === "idle" ? Array(120).fill(null) : [], in: wob(7.3, 0.6, { 40: 0.25 }) };
        showHeadroom();
        return;
      }
      S.headroom = kind === "wait" ? null : kind === "close"
        ? { every: 0.5, out: wob(0.9, 0.35, { 70: 0.62, 71: 0.4, 101: 0.3 }), in: wob(4.2, 0.5, {}) }
        : { every: 0.5, out: wob(1.6, 0.18, { 88: 0.3, 89: 0.15, 30: 0.12 }), in: wob(7.3, 0.6, { 40: 0.25 }) };
      showHeadroom();
    },
    engineOpen: (pick, advanced) => { openEngine(); Object.assign(S.pick, pick || {}, { advanced: !!advanced }); drawEngine(); },
    // A setting clicked in the docked (never moved) interface panel: no opening animation again (owner).
    ifaceRedraw: () => {
      openIface(S.engine.interfaces[0].key);
      $("engine").querySelector(".margins button:not([aria-checked=true])").click();
      if ($("engine").style.animationName !== "none") S.errors.push("a setting clicked replayed the interface panel's opening animation");
    },
    // An interface's panel (its key, else the first listed), with values picked in it.
    ifaceOpen: (key, pick) => { openIface(key || S.engine.interfaces[0].key); if (pick && $("engine")) { Object.assign(S.pick, pick); drawEngine(); } },
    // Tidy, then a fractional zoom (the owner's) and the cards measured again, then Tidy: nothing
    // may move (sizes once carried zoom-dependent float noise and a card flipped a grid step).
    tidyZoom: () => {
      const where = () => JSON.stringify(Object.values(S.els).map((el) => el.style.transform));
      tidy();
      const first = where();
      zoomAt(innerWidth / 2, innerHeight / 2, 0.7791780025683952);
      render();
      tidy();
      if (where() !== first) S.errors.push("tidy: a card moved after a zoom and a second Tidy");
    },
    // A state sent before the server saved a Tidy arrives after it: no card may move back (owner:
    // they jittered).
    tidyEcho: () => {
      const behind = structuredClone(S.lastState), where = () => JSON.stringify(Object.values(S.els).map((el) => el.style.transform));
      for (const k of Object.keys(S.els)) behind.workspace.cards[k] = { ...behind.workspace.cards[k], x: 1, y: 1 };  // its places before
      tidy();
      const after = where();
      receive(behind);
      if (where() !== after) S.errors.push("tidy: a state from before it moved cards back");
    },  // the bar's switch off (the next Tidy: one row)
    portalToggle: () => {
      const ws = visibleWires().filter((w) => !w.ghost).slice(0, 4).map((w) => w.id);
      setPortals(Object.fromEntries(ws.map((id) => [id, "hand"])));
      const spots = () => Object.fromEntries([...document.querySelectorAll(".portal-end")].map((e, i) => [`${e.dataset.wire}>${i % 2}`, e.getAttribute("transform")]));
      const before = spots();
      setPortals({ [ws[1]]: null });
      setPortals({ [ws[1]]: "hand" });
      const after = spots();
      for (const [k, v] of Object.entries(before)) if (after[k] !== v) S.errors.push(`portal toggle: ${k} moved from ${v} to ${after[k]}`);
      const discs = [...document.querySelectorAll(".portal-disc")].map((d) => d.getBoundingClientRect());
      discs.forEach((p, i) => discs.slice(i + 1).forEach((q) => {
        if (p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom) S.errors.push("portal toggle: two badges overlap");
      }));
    },
    // The pointer on portal i's exit badge: its wire shows, whole.
    portalHover: (i) => {
      const end = document.querySelectorAll(".portal-end")[2 * i];
      end?.dispatchEvent(new MouseEvent("mouseenter"));
      const we = S.wireEls[end?.dataset.wire];
      if (!we || !we.g.classList.contains("hot")) S.errors.push("portal hover: its wire did not show");
    },
    // A card appearing where another card has been moved to (owner: new app cards landed on top
    // of existing ones): its spot in a fresh layout is taken first, then it "appears".
    comeBackOver: (key, other) => {  // an app's card returning to the spot another card has now
      S.shown.delete(key);
      S.pos[key] = { ...S.pos[other] };
      render();
    },
    appearOver: (key, other) => {
      delete S.pos[key];
      const slot = layout([key]) && S.pos[key];
      S.pos[other] = { ...slot };
      delete S.pos[key];
      render();
    },
    // A device made where a wire was dropped, the spot overlapping a card: it lands right there or
    // at the nearest free spot, never at a layout spot across the board (owner).
    // A card dropped after a drag, then a board update from before the drop arrives (its old spot):
    // the card stays where it was dropped (owner: it jumped back and forth).
    // A card dragged after a Tidy that gave its wires portals: their badges come along (owner).
    portalFollow: (key, dx = 6, dy = 4) => {
      tidy();
      const mine = Object.entries(S.ws.portals || {}).filter(([id, how]) => typeof how === "object" && S.model.wires.some((w) => w.id === id && (w.from[0] === key || w.to[0] === key)));
      if (!mine.length) { S.errors.push(`portalFollow: no Tidy portal on ${key}'s wires`); return; }
      const to = { x: S.pos[key].x + dx, y: S.pos[key].y + dy };
      S.drag = { kind: "card", key, dx: 0, dy: 0, moved: true, to };
      onUp();
      for (const [id] of mine) {
        const how = S.ws.portals[id], w = S.model.wires.find((x) => x.id === id);
        if ((w.from[0] === key && how.out) || (w.to[0] === key && how.in)) S.errors.push(`a portal of ${id} stayed behind when ${key} moved`);
      }
    },
    // Portals turned off after a Tidy that folded: Tidy again, one row, none of its portals left (owner).
    portalsOff: () => {
      tidy();
      if (!Object.values(S.ws.portals || {}).some((how) => typeof how === "object")) S.errors.push("portalsOff: the Tidy made no portals to begin with");
      setTidyPortals(false);
      if (Object.values(S.ws.portals || {}).some((how) => typeof how === "object")) S.errors.push("Tidy's portals stayed after Portals was turned off");
    },
    // A column of cards that only receive, far taller than the rest, wraps (owner: Tidy could not fold).
    tidyWrap: (keys) => {
      tidy();
      if (new Set(keys.map((k) => S.pos[k]?.x)).size < 2) S.errors.push("the receive-only cards stayed in one tall column");
    },
    staleAfterDrop: (key) => {
      const from = { ...S.pos[key] }, to = { x: from.x + 6, y: from.y + 2 };
      S.pos[key] = to; placeCard(key); sendPlaces({ [key]: to });
      load({ ...S.lastState, workspace: { ...S.lastState.workspace, cards: { ...S.lastState.workspace.cards, [key]: { ...S.lastState.workspace.cards?.[key], ...from } } } });
      if (S.pos[key].x !== to.x || S.pos[key].y !== to.y) S.errors.push(`a stale update moved the dropped card back to ${S.pos[key].x},${S.pos[key].y}`);
    },
    dropOver: (key, other) => {
      S.shown.delete(key);
      S.pos[key] = { x: S.pos[other].x + 2, y: S.pos[other].y + 1 };
      const want = { ...S.pos[key] };
      render();
      const got = S.pos[key], far = Math.abs(got.x - want.x) + Math.abs(got.y - want.y);
      if (far > 30) S.errors.push(`a device dropped at ${want.x},${want.y} landed at ${got.x},${got.y}`);
      if (covered(key)) S.errors.push("the dropped device lies on another card");
    },
    askDeleteDevice: (key) => askDeleteDevice(byKey()[key]),
    // A new wire from a socket, let go on empty board at x, y.
    dropWire: (key, sock, x, y) => {
      const el = document.querySelector(`.sock[data-key="${CSS.escape(key)}"][data-sock="${sock}"]`);
      startWire(el, { clientX: x, clientY: y });
      moveWire(x, y);
      const d = S.drag;
      S.drag = null;
      finishWire(d);
      menus.at(-1)?.querySelector(".item")?.click();  // open Effects, to show what is there
      if (!S.dropPath?.isConnected) S.errors.push("the dropped wire's path is gone while its menu is open");
    },
    wireMenu: (id, x, y) => { wireMenu(id, x, y); menus.at(-1).querySelector(".item")?.click(); },
    createDevice: (type) => { S.menuAt = toWorld(800, 500); createDevice(type); },
    deleteDevice: (key) => askDeleteDevice(byKey()[key]),
    clock: () => ({ ...clock, running: liveRunning }),
    // A tooltip up on a card's title, then a right-click there: the tooltip must go.
    // A gain box: typing and stepping change nothing until confirmed; a board update waits;
    // Enter applies (clamped); Esc or clicking away cancels.
    // Scrolling a band's Q while the server's state, a step behind, comes back (owner: the curve
    // jittered back and forth): the panel must keep what was scrolled, not jump back.
    eqEcho: (key, i) => {
      openEq(key); EQ.debug.select(i);
      EQ.debug.wheel(i, 1);
      const behind = structuredClone(S.lastState), q1 = EQ.debug.band(i).q;
      behind.workspace.devices[key].bands = structuredClone(S.ws.devices[key].bands);
      behind.workspace.devices[key].bands[i].q = q1;
      EQ.debug.wheel(i, 2);
      const q3 = EQ.debug.band(i).q;
      receive(behind);
      if (EQ.debug.band(i).q !== q3) S.errors.push(`a state a step behind pulled band ${i + 1}'s Q back from ${q3} to ${EQ.debug.band(i).q}`);
    },
    fieldHold: (key) => {
      const box = () => S.els[key].querySelector(".gain-field"), dev = S.ws.devices[key], start = dev.gain_db;
      const sent = [], real = send;
      send = (m) => { if (m.op === "set-gain") sent.push(m.db); };  // eslint-disable-line no-func-assign
      try {
        const before = box();
        before.focus();
        before.value = "-6";
        before.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
        if (sent.length || dev.gain_db !== start) S.errors.push("the gain changed before it was confirmed");
        receive(S.lastState);
        if (box() !== before) S.errors.push("a board update replaced the gain box while editing");
        before.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        if (sent.join() !== "-5.5") S.errors.push(`expected -5.5 sent on Enter, sent ${sent.join() || "nothing"}`);
        if (box() === before) S.errors.push("the held update was never applied after confirming");
        const b2 = box();
        b2.focus(); b2.value = "99"; b2.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        if (sent.at(-1) !== 24) S.errors.push(`99 should be clamped to 24, sent ${sent.at(-1)}`);
        const b3 = box();
        b3.focus(); b3.value = "-20"; b3.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        if (sent.length !== 2) S.errors.push("Esc applied a value");
        const b3b = box(), shown = b3b.value;  // clicking away cancels too (owner), even after stepping
        b3b.focus(); b3b.value = "-20"; b3b.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true })); b3b.blur();
        if (sent.length !== 2) S.errors.push("clicking away applied a value");
        if (box().value !== shown) S.errors.push(`clicking away left ${box().value} in the box, not ${shown}`);
        const b4 = box();
        b4.focus(); b4.value = "-inf"; b4.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        if (sent.at(-1) !== -144 || box().value !== "−144") S.errors.push(`-inf should give −144, sent ${sent.at(-1)}, shows ${box().value}`);
        const b5 = box();
        b5.focus(); b5.value = "-500"; b5.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        if (sent.length !== 3 || box().value !== "−144") S.errors.push(`-500 should stay at −144, shows ${box().value}`);
        const b6 = box();
        b6.focus(); b6.value = "3.55"; b6.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        if (sent.at(-1) !== 3.6 || box().value !== "+3.6") S.errors.push(`3.55 should become 3.6, sent ${sent.at(-1)}, shows ${box().value}`);
        const b7 = box();
        b7.focus(); b7.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
        if (b7.value !== "+3.6") S.errors.push(`double-click changed the value to ${b7.value}`);
        b7.blur();
      } finally { send = real; }  // eslint-disable-line no-func-assign
    },
    // The tooltip of a card's title, a socket, or a wire, shown at a point (for the shots).
    showTip: (selector, x, y) => {
      const el = document.querySelector(selector), box = $("tip");
      fillTip(box, el.dataset.tip);
      box.hidden = false;
      box.style.left = `${x}px`; box.style.top = `${y}px`;
    },
    tipThenMenu: (key) => {
      const head = S.els[key].querySelector("header"), r = head.getBoundingClientRect();
      const at = { clientX: r.left + 30, clientY: r.top + 10 };
      $("tip").textContent = head.dataset.tip; $("tip").hidden = false;
      head.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, ...at }));
    },
    drawCost: () => { const t = performance.now(); for (let i = 0; i < 50; i++) drawLive(); return (performance.now() - t) / 50; },
    // Live wires with made-up sound: speech, a chord, music that clips, the rest silent.
    // n: how many sources get made-up sound; clipWire: the wire whose source is the one that clips
    // (README: the hovered portal's), else the first loud one.
    fakeLive: (n = 5, clipWire = null, clip = true) => {  // (clip: false, no source clips)
      const srcs = [...new Set(visibleWires().map((w) => w.from.join("|")))];
      const clipSrc = clipWire ? S.wireEls[clipWire]?.w.from.join("|") : null;
      // Speech in bursts, a sustained chord, loud music with a hit every 1.6 s that clips for
      // ~80 ms (owner: a short burst, not a second of red).
      const hit = (t) => Math.exp(-((((t % 1.6) - 0.3) / 0.06) ** 2));
      const shapes = [
        (t) => Math.max(0, Math.sin(t * 2.1) * 0.9 + 0.1) * Math.sin(t * 2 * Math.PI * 140) * (0.5 + 0.5 * Math.sin(t * 9)),
        (t) => Math.exp(-((t % 3) * 0.8)) * (Math.sin(t * 2 * Math.PI * 98) * 0.6 + Math.sin(t * 2 * Math.PI * 147) * 0.4),
        (t) => (0.6 + 0.6 * hit(t)) * Math.sin(t * 2 * Math.PI * 220),
        // (more, for pictures with many wires: a kick and bass line, an arpeggio, a pad with shimmer)
        (t) => Math.exp(-((t % 0.5) * 9)) * Math.sin(2 * Math.PI * (55 + 60 * Math.exp(-(t % 0.5) * 30)) * t) * 0.6 + 0.25 * Math.sin(2 * Math.PI * 82 * t),
        (t) => (0.35 + 0.45 * Math.exp(-((t % 0.25) * 12))) * Math.sin(2 * Math.PI * [262, 330, 392, 523][Math.floor(t * 4) % 4] * t),
        (t) => (0.45 + 0.15 * Math.sin(t * 1.3)) * (Math.sin(2 * Math.PI * 196 * t) * 0.6 + Math.sin(2 * Math.PI * 294 * t) * 0.3 + Math.sin(2 * Math.PI * 1170 * t) * 0.12 * (0.5 + 0.5 * Math.sin(t * 7))),
      ];
      const bytes = (x) => { const m = Math.abs(x); const v = m >= 0.999 ? 127 : m < 0.002 ? 0 : Math.round((20 * Math.log10(m) + 54) / 54 * 126); return x < 0 ? -v : v; };
      const tones = [125, 55, 168, 70, 150, 110];  // a voice, a bass chord, bright music, kick and bass, arpeggio, pad (scope.bright's scale)
      srcs.slice(0, n).forEach((k, n) => {
        const values = Int8Array.from({ length: 8000 }, (_, ms) => {
          let peak = 0;
          // (no clipping: only the steady shapes, never silent at the moment of a picture)
          const shape = clipSrc === k ? 2 : clip ? n % 3 : 2 + (n % 4), clips = clip && (clipSrc ? clipSrc === k : n === 2);
          const quiet = shape === 2 && !clips ? 0.55 : 1;  // (one source clips: one warning)
          for (let s8 = 0; s8 < 8; s8++) { const x = quiet * shapes[shape](ms / 1000 + s8 / 8000); if (Math.abs(x) > Math.abs(peak)) peak = x; }
          return bytes(peak);
        });
        feed(k, 0, values, tones[clip ? n % 3 : 2 + (n % 4)]);
        // (no warning raised here: the server warns only where a sound card receives it,
        // problems.clipping; the red on the wire is the picture)
      });
      clock.newest = clock.head = 7999;
      drawLive();
    },
    // A frame made by the capture's own encoder (scripts/shots.py: scope.encode, two sources,
    // the second bright): the page must read both, values and brightness, in that layout.
    frameCheck: (b64, tones) => {
      const raw = atob(b64), buf = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
      S.liveSources = [["frame-a", "out"], ["frame-b", "out"]];
      liveFrame(buf.buffer);
      tones.forEach((want, k) => {
        const r = rings[`frame-${"ab"[k]}|out`], got = r ? Math.max(...r.tone) : -1;
        if (got !== want) S.errors.push(`live frame: source ${k} brightness ${got}, the capture sent ${want}`);
      });
    },
    // The live wires at a given moment of the fake sound (frames of the README's animation).
    liveAt: (ms) => { clock.head = ms; drawLive(); },
    // The fake live wires moving (for the frame-time check): their 8 s played round, a frame each.
    liveLoop: () => {
      PB.debug.fakeLive();
      const t0 = performance.now();
      const step = (now) => { clock.head = 2000 + ((now - t0) % 5000); drawLive(); requestAnimationFrame(step); };
      requestAnimationFrame(step);
    },
    missingMenu: (key) => {
      const r = S.els[key].getBoundingClientRect();
      cardMenu(key, r.right - 20, r.top + 20);
      menus.at(-1).querySelector(".item")?.click();
    },
    // Half-way through moving a wire: picked up at its input, held at x, y.
    moveWire: (key, sock, toKey, toSock) => {
      const at = (k, s) => document.querySelector(`.sock[data-key="${k}"][data-sock="${s}"] .dot`).getBoundingClientRect();
      const a = at(key, sock), b = at(toKey, toSock);
      const w = wireInto(key, sock);
      startMove(w, { clientX: a.left, clientY: a.top });
      moveWire(b.left + b.width / 2, b.top + b.height / 2);
      const want = sockOf(...w.from, "out")?.ports.length === 2 ? (w.fromPorts.length === 1 ? "LR"[w.fromPorts[0]] : "L R") : "";
      const got = $("drag-side").hidden ? "" : $("drag-side").textContent;
      if (got !== want) S.errors.push(`moving a wire that carries "${want}", the tag says "${got}"`);
    },
    // Half-way through dragging a card: pressed on its title, moved by dx, dy (screen px).
    drag: (key, dx, dy) => {
      const head = S.els[key].querySelector("header"), r = head.getBoundingClientRect();
      const x = r.left + 40, y = r.top + r.height / 2;
      onDown({ target: head, button: 0, clientX: x, clientY: y });
      onMove({ clientX: x + dx, clientY: y + dy, altKey: false });
    },
    zoom: (z) => { S.moved = true; zoomAt(innerWidth / 2, innerHeight / 2, z); },
    fit: (keys, dy = 0, dx = 0) => { S.moved = true; fit(false, keys || undefined); S.view.y += dy; S.view.x += dx; applyView(false); },  // dy: room for a panel
  };

  // Frames a second and the longest frame of that second (max, not the average: owner read it as
  // the frame time), from the page's own frames; a hitch shows in max before fps moves. A hidden window draws no frames: its gap
  // starts a fresh second rather than reading as one long frame.
  function fpsMeter() {
    const el = $("fps");
    el.hidden = false;
    let n = 0, worst = 0, last = 0, since = 0;
    const tick = (t) => {
      if (t - last > 1000) { since = t; n = 0; worst = 0; }
      else { n++; worst = Math.max(worst, t - last); }
      last = t;
      if (t - since >= 1000) {
        el.textContent = `${Math.round((n * 1000) / (t - since))} fps · max ${Math.round(worst)} ms`;
        since = t; n = 0; worst = 0;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  function start() {
    bind();
    const boot = window.PB_BOOT;
    if (boot) {
      // Screenshots catch animations halfway; a preview shows every state as it settles.
      document.documentElement.classList.add("preview");
      load(boot);
      try { if (boot.after) new Function("PB", boot.after)(PB); } catch (e) { S.errors.push(`after: ${e}`); }
      setTimeout(audit, 200);
    } else { connect(); fpsMeter(); }  // (not in pictures: its numbers would differ every time)
  }

  const PB = { debug, audit };
  addEventListener("DOMContentLoaded", start);
  return PB;
})();
