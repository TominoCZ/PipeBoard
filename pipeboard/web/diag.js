// Diagnostic cards (PLAN §8d): Waveform and Stereo. The server sends what each card hears
// (diag-pcm, 48 kHz, two channels each); each card's display and its big panel draw it.
// Owner: they render on their own, apart from the board. Each display is a canvas on its own
// compositor layer (CSS contain + will-change), drawn in this module's own animation loop: a
// frame repaints that canvas only, never a card, a wire or the board's layout. Readouts change
// ten times a second; the correlation marker moves by transform.
"use strict";

const DIAG = (() => {
  const RATE = 48000, KEEP = 12000;  // 250 ms per card
  // L and R's colours: board.css's --diag-l and --diag-r (one place: the traces, the dots, the
  // labels and the dashed edge), read again each second. The stereo dots go from L's through a
  // soft white (mono) to R's: two contrasting colours mixed straight turn muddy.
  const MID = [226, 230, 236];
  let pal = { l: "#f84fcc", r: "#0fbb7c", lr: [248, 79, 204], rr: [15, 187, 124], at: -1e9 };
  const rgb = (hex) => { const n = parseInt(hex.replace("#", "").slice(0, 6), 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
  function palette(t) {
    if (t - pal.at < 1000) return pal;
    const css = getComputedStyle(document.documentElement), l = css.getPropertyValue("--diag-l").trim(), r = css.getPropertyValue("--diag-r").trim();
    if (/^#[0-9a-f]{6}$/i.test(l) && /^#[0-9a-f]{6}$/i.test(r)) pal = { l, r, lr: rgb(l), rr: rgb(r), at: t };
    return pal;
  }
  const mix = (a, b, t) => a.map((v, i) => (v + (b[i] - v) * t) | 0);
  const SPAN = 0.05 * RATE;  // the waveform's 50 ms
  const FADE_S = 0.15;
  // the stereo dots' fade (time constant; ~0.4 s until they are gone)
  const rings = {};  // key → { l, r: Float32Array(KEEP), at: frames written }
  // The displays shown, by key (and |big): { key, type, canvas, info, big }. The board draws its
  // cards anew on every state; a display keeps its canvas (moved into the new card, its picture
  // with it) and where it had got to, so nothing is replayed (owner: blips with nothing plugged
  // in were the last sound replayed by each new canvas).
  const views = new Map(), canvases = {};
  const mono = {};  // key → true: the waveform shows L and R combined (the card's Stereo/Mono switch)
  const smooth = {};  // key → true: the waveform smoothed (its Smooth toggle)
  const fixed = {};  // key → true: the waveform at full scale, not zoomed to fit (its Zoom toggle off)
  const seen = {};  // key → its last numbers: a card drawn again (any new state) shows them at once
  let raf = 0, lastInfo = -Infinity;  // (the first frame fills the readouts at once)

  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) if (k === "text") el.textContent = v; else if (v != null && v !== false) el.setAttribute(k, v);
    el.append(...kids.filter((k) => k != null));
    return el;
  };

  // ---- what the server sends ----------------------------------------------------------------
  function pcm(keys, b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    feed(keys, new Float32Array(bytes.buffer));
  }
  // The server's binary frames (owner: the visualizers' load): FFFFFFFF, then the cards' raw
  // floats, two channels each, in the order of its last diag-keys message.
  let order = [];
  const keys = (k) => { order = k; };
  const isFrame = (buf) => buf.byteLength >= 4 && new DataView(buf).getUint32(0, true) === 0xffffffff;
  const frame = (buf) => { if (order.length) feed(order, new Float32Array(buf, 4, (buf.byteLength - 4) >> 2)); };
  function feed(keys, f) {
    const n = keys.length * 2, frames = Math.floor(f.length / n);
    keys.forEach((k, i) => {
      const ring = rings[k] ||= { l: new Float32Array(KEEP), r: new Float32Array(KEEP), at: 0, play: 0, drawn: 0 };
      ring.fed = performance.now();
      for (let j = 0; j < frames; j++) {
        const p = ring.at++ % KEEP;
        ring.l[p] = f[j * n + 2 * i]; ring.r[p] = f[j * n + 2 * i + 1];
      }
    });
    kick();
  }
  // The playhead (owner: very smooth): the sound comes in 8 ms lumps, but what is drawn follows a
  // position moving on with real time, LAG behind the newest, so each screen refresh shows a
  // little more instead of a jump every lump. Too far behind (a stall), it catches up at once.
  const LAG = 0.015 * RATE;  // (the server sends every 8 ms: 15 ms covers its gaps; was 40, owner: it lagged)
  function advance(ring, dt) {
    const want = ring.at - LAG;
    ring.play = Math.min(want, ring.play + dt * RATE * (ring.play < want - LAG ? 1.15 : 1));  // (a little faster when behind)
    if (want - ring.play > 0.15 * RATE || ring.play < 0) ring.play = Math.max(0, want);
  }
  // Nothing has come for a moment (nothing plugged in: its node sleeps and sends nothing): silence,
  // not the last sound held.
  const silent = (ring) => !ring || performance.now() - ring.fed > 300;
  // The newest `count` frames up to `end` (frames written), oldest first; silence before any.
  function frames(ring, count, end = ring.at) {
    const l = new Float32Array(count), r = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      const at = end - count + i;
      if (at < 0 || at <= ring.at - KEEP) continue;
      const p = at % KEEP;
      l[i] = ring.l[p]; r[i] = ring.r[p];
    }
    return { l, r };
  }

  // ---- drawing --------------------------------------------------------------------------------
  function fitCanvas(c) {  // its backing store matches its size on screen (sharp at any zoom)
    const box = c.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    const w = Math.max(1, Math.round(box.width * dpr)), ht = Math.max(1, Math.round(box.height * dpr));
    if (c.width !== w || c.height !== ht) { c.width = w; c.height = ht; }
    return [w, ht];
  }

  // Steady, as a scope's trigger: the window starts at the newest rising zero crossing that still
  // leaves a whole span after it. Of the mid filtered below ~150 Hz (the bass's own waves, the
  // steadiest thing in most music), and with hysteresis: a crossing counts only after the wave
  // went clearly below zero first (a tenth of its size). Owner saw it jump half a wave, "as if
  // mirrored": a ripple at the bottom of a bass wave crossing zero upwards for an instant.
  const TRIG_A = Math.exp((-2 * Math.PI * 150) / RATE);
  function trigger(look) {
    const n = SPAN + 1, t = new Float32Array(n);
    let y = 0, top = 0;
    for (let i = 0; i < n; i++) { y = TRIG_A * y + (1 - TRIG_A) * (look.l[i] + look.r[i]); t[i] = y; top = Math.max(top, Math.abs(y)); }
    let armed = false, found = -1;
    for (let i = 1; i < n; i++) {
      if (t[i] < -0.1 * top) armed = true;
      else if (armed && t[i - 1] < 0 && t[i] >= 0) { found = i; armed = false; }
    }
    return found > 0 ? found : SPAN;  // (no crossing, silence or noise: the newest span as it is)
  }
  // Smooth (owner: a toggle omitting very sharp detail; at half a millisecond the crests still
  // showed as spikes): two passes of a centred 0.67 ms average, a soft bell-shaped filter fading
  // from ~600 Hz up; the shape stays, nothing shifts. And one line through each column's average
  // instead of its lowest-to-highest span. Running sums: the same work for any width.
  const SMOOTH_N = 32;
  function smoothed(x) {
    const out = new Float32Array(x.length), half = SMOOTH_N / 2;
    let sum = 0;
    for (let i = 0; i < x.length + half; i++) {
      if (i < x.length) sum += x[i];
      if (i - SMOOTH_N >= 0) sum -= x[i - SMOOTH_N];
      const c = i - half;  // (the window is samples i−N+1 … i, the ends cut off: divide by what is in it)
      if (c >= 0) out[c] = sum / (Math.min(i, x.length - 1) - Math.max(0, i - SMOOTH_N + 1) + 1);
    }
    return out;
  }
  function waveform(v, ring, dt) {
    const [w, ht] = fitCanvas(v.canvas), g = v.canvas.getContext("2d");
    g.clearRect(0, 0, w, ht);
    g.strokeStyle = "rgba(255,255,255,0.06)"; g.lineWidth = 1;
    for (const y of [0.25, 0.75]) { g.beginPath(); g.moveTo(0, ht * y); g.lineTo(w, ht * y); g.stroke(); }
    g.strokeStyle = "rgba(255,255,255,0.13)"; g.beginPath(); g.moveTo(0, ht / 2); g.lineTo(w, ht / 2); g.stroke();
    if (silent(ring)) { v.stats = { peak: 0, silent: true }; return; }
    const look = frames(ring, SPAN * 2, Math.floor(ring.play));
    const start = trigger(look);
    if (mono[v.key]) look.m = look.l.map((a, i) => (a + look.r[i]) / 2);  // (Mono: one trace, both sides)
    // The true peak of what is drawn (Mono: the combined trace), before any smoothing.
    let peak = 0;
    for (const ch of mono[v.key] ? ["m"] : ["l", "r"]) for (let i = start; i < start + SPAN; i++) peak = Math.max(peak, Math.abs(look[ch][i]));
    // Zoomed to fit (owner: a song at −12 dB used a third of the box): the loudest of the last
    // second and a half fills nine tenths of it; up at once, down slowly; never past ×250 (−48 dB).
    // Smooth keeps the wave's size (owner: it made the wave lower): taking out the sharp detail
    // takes some height off the peaks (a quarter, for music), so the line is scaled back to the
    // true peak, the ratio eased so the size does not wobble frame to frame.
    let size = 1;
    if (smooth[v.key]) {
      for (const ch of Object.keys(look)) look[ch] = smoothed(smoothed(look[ch]));
      let shown = 0;
      for (const ch of mono[v.key] ? ["m"] : ["l", "r"]) for (let i = start; i < start + SPAN; i++) shown = Math.max(shown, Math.abs(look[ch][i]));
      v.sg = ease(v.sg, shown > 1e-4 ? Math.min(4, peak / shown) : 1, dt);
      size = v.sg;
    }
    v.wz = Math.max(peak, (v.wz || peak || 0.004) * Math.exp(-dt / 1.5), 0.004);
    const gain = (fixed[v.key] ? 0.95 : 0.9 / v.wz) * size;  // (Zoom off: the edge is full scale, 0 dB)
    // L and R the same weight, added together (owner: green was hard to see under magenta): where
    // they agree the trace turns white (mono, as in the stereo picture), where they differ each
    // shows its own colour.
    const traces = mono[v.key] ? [["m", `rgb(${MID.join(",")})`]] : [["l", pal.l], ["r", pal.r]];
    g.globalCompositeOperation = mono[v.key] ? "source-over" : "lighter";
    for (const [ch, color] of traces) {
      const s = look[ch];
      g.strokeStyle = color; g.lineWidth = 1.5 * (devicePixelRatio || 1);
      g.beginPath();
      for (let x = 0; x < w; x++) {  // each column's lowest and highest sample
        const a = start + Math.floor((x / w) * SPAN), b = Math.max(a + 1, start + Math.floor(((x + 1) / w) * SPAN));
        let lo = Infinity, hi = -Infinity, sum = 0;
        for (let i = a; i < b; i++) { const s0 = s[i]; sum += s0; if (s0 < lo) lo = s0; if (s0 > hi) hi = s0; }
        if (smooth[v.key]) lo = hi = sum / (b - a);  // (one line through the column's average)
        const y1 = ht / 2 - hi * gain * (ht / 2), y2 = ht / 2 - lo * gain * (ht / 2);
        if (x === 0) g.moveTo(x, y1); else g.lineTo(x, y1);
        if (y2 - y1 > 1) g.lineTo(x, y2);
      }
      g.stroke();
    }
    g.globalCompositeOperation = "source-over";
    v.stats = { peak, zoom: gain / 0.9 };
  }

  // Polar sample, as a stereo imager: each sample a dot, its angle how far it leans to a side
  // (mono straight up, L and R on the 45° lines, out of phase flat), its distance its level.
  // Dots fade over a few frames (the canvas is cleared by a little each frame).
  function stereo(v, ring, dt) {
    const [w, ht] = fitCanvas(v.canvas), g = v.canvas.getContext("2d");
    // (fading by the time passed, not per frame: the same at 60 and 144 Hz; a dot lasts ~0.4 s,
    // owner: a little longer than the first 0.15. In steps of at least a quarter: a canvas keeps
    // whole steps of opacity, and a faint pixel dimmed by a few % a frame rounds back to what it
    // was, so old dots stayed as a grey ghost (owner saw one); a quarter at a time reaches zero.)
    v.fade = (v.fade || 0) + dt;
    const fade = 1 - Math.exp(-v.fade / FADE_S);
    if (fade >= 0.25) {
      v.fade = 0;
      g.globalCompositeOperation = "destination-out"; g.fillStyle = `rgba(0,0,0,${fade.toFixed(4)})`; g.fillRect(0, 0, w, ht);
    }
    g.globalCompositeOperation = "source-over";
    if (silent(ring)) {  // (a second of silence: wiped clean, not even a last 1/255 left)
      if (!ring || performance.now() - ring.fed > 1300) g.clearRect(0, 0, w, ht);
      v.stats = { silent: true }; v.drawn = ring ? Math.floor(ring.play) : 0;
      return;
    }
    // (the guides' geometry: viewBox 200 × 104, centre (100, 101), radius 96, fitted bottom-centre
    // into the display less 6 px a side, 4 above and 2 below)
    const dpr = devicePixelRatio || 1, sc = Math.min((w - 12 * dpr) / 200, (ht - 6 * dpr) / 104);
    // (a new display, sound live: the last 0.1 s at once, which is now, not a replay; silence
    // returned above, so old sound is never drawn)
    const end = Math.floor(ring.play), fresh = Math.min(4096, Math.max(0, end - (v.drawn ?? end - 4800)));
    v.drawn = end;
    const n = 4096, { l, r } = frames(ring, n, end), cx = w / 2, cy = ht - 2 * dpr - 3 * sc, rad = 96 * sc;
    let lr = 0, ll = 0, rr = 0, mm = 0, ss = 0, top = 0;
    for (let i = n - fresh; i < n; i++) top = Math.max(top, Math.abs(l[i]), Math.abs(r[i]));
    // (up at once to a louder sample, down over ~1.5 s; never past −48 dB, so hiss stays small)
    v.zoom = Math.max(top, (v.zoom || top || 0.004) * Math.exp(-dt / 1.5), 0.004);
    const dot = Math.max(1, Math.round(dpr));
    for (let i = 0; i < n; i++) {
      const a = l[i], b = r[i];
      lr += a * b; ll += a * a; rr += b * b;
      let m = (a + b) * Math.SQRT1_2, s = (a - b) * Math.SQRT1_2;
      mm += m * m; ss += s * s;
      if (i < n - fresh) continue;  // drawn already: only what played since the last frame
      if (m < 0) { m = -m; s = -s; }
      // distance: its level against the loudest of the last second (zoomed to fit, owner: a song
      // at −12 dB was a small blob; in dB the loud near-mono part sat at the rim, hollow under it)
      const lvl = Math.hypot(m, s) * Math.SQRT1_2, ang = Math.atan2(s, m);
      if (lvl < 0.0005) continue;
      const amp = Math.min(1, (lvl / v.zoom) * 0.95);
      // 0 full left, 1 mono, 2 full right; white only near mono itself (owner: vivid), the
      // side's colour from a little off the centre line
      const t = Math.min(1, Math.max(0, 0.5 - ang / Math.PI)) * 2, k = Math.sqrt(Math.abs(t - 1));
      g.fillStyle = `rgb(${(t < 1 ? mix(MID, pal.lr, k) : mix(MID, pal.rr, k)).join(",")})`;
      g.globalAlpha = 0.55;
      g.fillRect(cx - Math.sin(ang) * amp * rad, cy - Math.cos(ang) * amp * rad, dot, dot);
    }
    g.globalAlpha = 1;
    const corr = ll && rr ? lr / Math.sqrt(ll * rr) : 0;
    // Width: how far the picture fans out, against its guide lines (owner: "what am I supposed to
    // imagine under 240 %"). From the side's level over the mid's (RMS, level-weighted as is
    // correlation, so the two agree; owner: 168 % beside a narrow picture had been the dots'
    // spread, faint noise counted as much as loud sound), as an angle: 2·atan(side/mid), 0 % a
    // vertical line (mono), 100 % out to the L and R lines (L and R unrelated), 200 % flat
    // (fully out of phase), never more. A sine with L and R φ apart reads φ / 90°.
    const width = ss + mm < 1e-9 ? 0 : (2 * Math.atan2(Math.sqrt(ss), Math.sqrt(mm))) / (Math.PI / 2), k = 1 - Math.exp(-dt / 0.25);  // (numbers settle over ~¼ s)
    v.stats = { corr: v.stats?.corr == null ? corr : v.stats.corr + (corr - v.stats.corr) * k,
                width: v.stats?.width == null ? width : v.stats.width + (width - v.stats.width) * k };
  }

  // ---- Meter (owner): levels and loudness -------------------------------------------------------
  // What played since this view last looked (at most a second; a new view: the last 0.1 s).
  function freshFrames(v, ring) {
    const end = Math.floor(ring.play), n = Math.min(RATE, Math.max(0, end - (v.drawn ?? end - 4800)));
    v.drawn = end;
    return { ...frames(ring, n, end), n };
  }
  // Loudness as streaming platforms measure it (ITU-R BS.1770): each side K-weighted (a high shelf,
  // then a high-pass; the standard's own coefficients at 48 kHz), its mean square per 100 ms block;
  // momentary over the last 4 blocks (400 ms), short-term over the last 30 (3 s).
  const KW = [[1.53512485958697, -2.69169618940638, 1.19839281085285, -1.69065929318241, 0.73248077421585],
              [1, -2, 1, -1.99004745483398, 0.99007225036621]];
  function kSquares(x, st) {  // x through both stages (st: their state), the sum of squares out
    let sum = 0;
    for (let i = 0; i < x.length; i++) {
      let y = x[i];
      for (let k = 0; k < 2; k++) {
        const [b0, b1, b2, a1, a2] = KW[k], z = st[k];
        const o = b0 * y + b1 * z[0] + b2 * z[1] - a1 * z[2] - a2 * z[3];
        z[1] = z[0]; z[0] = y; z[3] = z[2]; z[2] = o; y = o;
      }
      sum += y * y;
    }
    return sum;
  }
  const lufs = (blocks, n) => {
    const b = blocks.slice(-n);
    return b.length ? -0.691 + 10 * Math.log10(Math.max(1e-12, b.reduce((a, x) => a + x, 0) / b.length)) : -Infinity;
  };
  const toDb = (x) => (x > 1e-6 ? 20 * Math.log10(x) : -120);
  function meter(v, ring, dt) {
    const [w, ht] = fitCanvas(v.canvas), g = v.canvas.getContext("2d"), dpr = devicePixelRatio || 1;
    const m = v.m ||= { k: [[[0, 0, 0, 0], [0, 0, 0, 0]], [[0, 0, 0, 0], [0, 0, 0, 0]]], blocks: [], acc: 0, accN: 0,
                        pk: [-120, -120], hold: [-120, -120], holdAt: [0, 0], ms: [0, 0] };
    const now = performance.now();
    if (ring && !silent(ring)) {
      const f = freshFrames(v, ring);
      for (const [c, x] of [[0, f.l], [1, f.r]]) {
        let top = 0, a = Math.exp(-1 / (0.3 * RATE)), ms = m.ms[c];
        for (let i = 0; i < x.length; i++) { const s0 = x[i]; top = Math.max(top, Math.abs(s0)); ms = ms * a + s0 * s0 * (1 - a); }
        m.ms[c] = ms;
        m.pk[c] = Math.max(toDb(top), m.pk[c] - 20 * dt);
        if (m.pk[c] >= m.hold[c]) { m.hold[c] = m.pk[c]; m.holdAt[c] = now; }
      }
      for (let i = 0; i < f.n; i += 480) {  // (in steps of 10 ms: blocks fill exactly)
        const end = Math.min(f.n, i + 480);
        m.acc += kSquares(f.l.subarray(i, end), m.k[0]) + kSquares(f.r.subarray(i, end), m.k[1]);
        m.accN += end - i;
        if (m.accN >= 4800) { m.blocks.push(m.acc / m.accN); m.acc = 0; m.accN = 0; if (m.blocks.length > 30) m.blocks.shift(); }
      }
    } else {
      for (const c of [0, 1]) { m.pk[c] -= 20 * dt; m.ms[c] *= Math.exp(-dt / 0.3); }
      v.drawn = ring ? Math.floor(ring.play) : 0;
    }
    for (const c of [0, 1]) if (now - m.holdAt[c] > 1500) m.hold[c] = Math.max(m.pk[c], m.hold[c] - 20 * dt);  // (held 1.5 s, then falls)
    // Segmented LED bars (owner's pick B), the whole width (owner: no numbers beside them): −60 …
    // 0 dB, green, then yellow from −12, red from −3; lit to the RMS, dimmer on to the peak, the
    // hold one segment lit white (red at full scale). Segments ~6 px on the card, ~10 large.
    // (inner margins all round and a column for L / R with room after it: owner, "no margins,
    // clumsy"; slightly rounded segments)
    g.clearRect(0, 0, w, ht);
    // (sizes in the card's own pixels: they follow the board's zoom as the card does; at a small
    // zoom fixed screen sizes crowded it)
    const u = dpr * (v.canvas.getBoundingClientRect().width / (v.canvas.offsetWidth || 1) || 1);
    const pad = (v.big ? 12 : 8) * u, labW = (v.big ? 16 : 12) * u, rowGap = (v.big ? 8 : 5) * u;
    // Whole screen pixels for every segment and gap (owner: on the card they came out uneven, each
    // fractional width rounded differently); the bars start after the L / R column and the few
    // pixels left over go to the right margin.
    const x0 = Math.round(pad + labW), avail = w - x0 - Math.round(pad), gap = Math.max(1, Math.round((v.big ? 3 : 2) * u));
    const sw = Math.max(2, Math.round((v.big ? 11 : 8) * u) - gap), n = Math.max(12, Math.floor((avail + gap) / (sw + gap)));
    const bh = Math.round(Math.min((ht - 2 * pad - rowGap) / 2, (v.big ? 26 : 13) * u)), rad = Math.min(sw, bh) * 0.25;
    const segOf = (d) => Math.round(((Math.min(0, Math.max(-60, d)) + 60) / 60) * n);
    const colour = (i) => { const d = -60 + ((i + 0.5) / n) * 60; return d > -3 ? [239, 95, 95] : d > -12 ? [224, 192, 80] : [63, 191, 127]; };
    g.font = `500 ${(v.big ? 12 : 10) * u}px Inter, sans-serif`; g.textBaseline = "middle"; g.textAlign = "left";
    const top = Math.round((ht - 2 * bh - rowGap) / 2);
    for (const c of [0, 1]) {
      const y = top + c * (bh + Math.round(rowGap)), rms = segOf(toDb(Math.sqrt(m.ms[c]))), pk = segOf(m.pk[c]), hold = m.hold[c] > -60 ? segOf(m.hold[c]) - 1 : -1;
      for (let i = 0; i < n; i++) {
        const [r0, g0, b0] = colour(i), x = x0 + i * (sw + gap);
        g.fillStyle = i === hold ? (m.hold[c] >= -0.1 ? "rgb(239,95,95)" : "rgb(242,244,247)")
          : i < rms ? `rgb(${r0},${g0},${b0})` : i < pk ? `rgba(${r0},${g0},${b0},0.45)` : "#24272c";
        g.beginPath(); g.roundRect(x, y, sw, bh, rad); g.fill();
      }
      g.fillStyle = c ? pal.r : pal.l; g.fillText(c ? "R" : "L", pad, y + bh / 2 + 0.5 * u);
    }
    v.stats = { mom: lufs(m.blocks, 4), short: lufs(m.blocks, 30), silent: silent(ring) };
  }

  // The loudness against the standard's own reference points (run by the page audit): a 997 Hz
  // sine at −20 dBFS in both channels reads −20.0 LUFS; at full scale in one channel, −3.01.
  function selftest() {
    const faults = [], read = (aL, aR) => {
      const st = [[[0, 0, 0, 0], [0, 0, 0, 0]], [[0, 0, 0, 0], [0, 0, 0, 0]]], blocks = [];
      for (let b = 0; b < 30; b++) {
        const l = new Float32Array(4800), r = new Float32Array(4800);
        for (let i = 0; i < 4800; i++) { const x = Math.sin(2 * Math.PI * 997 * (b * 4800 + i) / RATE); l[i] = aL * x; r[i] = aR * x; }
        blocks.push((kSquares(l, st[0]) + kSquares(r, st[1])) / 4800);
      }
      return lufs(blocks.slice(5), 25);  // (the filters settled)
    };
    for (const [aL, aR, want] of [[0.1, 0.1, -20.0], [1, 0, -3.01]]) {
      const got = read(aL, aR);
      if (Math.abs(got - want) > 0.1) faults.push(`loudness: a 997 Hz sine read ${got.toFixed(2)} LUFS, the standard says ${want}`);
    }
    return faults;
  }

  // ---- Spectrum (owner): the EQ analyser's own picture on any wire --------------------------------
  // The two channels combined, the newest 4096 samples at the playhead, once a frame per card (its
  // panel draws the same), peaks falling at the analyser's own rate.
  const SPEC = {};  // key → { at: the frame it was taken, bins }
  function spectrumBins(key, ring, t, dt) {
    const A = EQ.analyser, c = SPEC[key] ||= { at: -1, bins: new Float64Array(A.BINS).fill(-120) };
    if (c.at === t) return c.bins;
    c.at = t;
    let fresh = null;
    if (ring && !silent(ring)) {
      const { l, r } = frames(ring, A.N, Math.floor(ring.play));
      const x = new Float64Array(A.N);
      for (let i = 0; i < A.N; i++) x[i] = (l[i] + r[i]) / 2;
      fresh = A.binsOf(x);
    }
    for (let i = 0; i < A.BINS; i++) c.bins[i] = Math.max(fresh ? fresh[i] : -120, c.bins[i] - A.FALL * dt);
    return c.bins;
  }
  function spectrum(v, ring, dt, t) {
    const [w, ht] = fitCanvas(v.canvas), g = v.canvas.getContext("2d"), dpr = devicePixelRatio || 1, A = EQ.analyser;
    const bins = spectrumBins(v.key, ring, t, dt), lf0 = Math.log10(A.F0), span = Math.log10(A.F1) - lf0;
    const xOf = (f) => ((Math.log10(f) - lf0) / span) * w, yOf = (d) => ht - ((Math.min(0, Math.max(-90, d)) + 90) / 90) * ht;
    g.clearRect(0, 0, w, ht);
    g.strokeStyle = "rgba(255,255,255,0.05)"; g.lineWidth = 1;
    for (const f of A.GRID_F) { g.beginPath(); g.moveTo(xOf(f), 0); g.lineTo(xOf(f), ht); g.stroke(); }
    for (const d of [-20, -40, -60, -80]) { g.beginPath(); g.moveTo(0, yOf(d)); g.lineTo(w, yOf(d)); g.stroke(); }
    const n = bins.length, pts = Array.from(bins, (b, i) => [(i / (n - 1)) * w, yOf(b)]);
    g.beginPath(); g.moveTo(0, ht);
    for (const [x, y] of pts) g.lineTo(x, y);
    g.lineTo(w, ht); g.closePath();
    // (by level, as a meter: green low, yellow from −20 dB, red near the top; owner's pick)
    const fill = g.createLinearGradient(0, ht, 0, 0);
    fill.addColorStop(0, "#3fbf7f"); fill.addColorStop(0.55, "#3fbf7f"); fill.addColorStop(0.78, "#e0c050"); fill.addColorStop(1, "#ef5f5f");
    g.globalAlpha = 0.28; g.fillStyle = fill; g.fill();
    g.globalAlpha = 1; g.strokeStyle = fill; g.lineWidth = 1.4 * dpr; g.beginPath();
    pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); g.stroke();
    g.fillStyle = "rgba(232,230,227,0.32)"; g.font = `${9 * dpr}px Inter, sans-serif`; g.textBaseline = "bottom"; g.textAlign = "center";
    if (v.big || w > 260 * dpr) for (const f of A.GRID_F.slice(1, -1)) g.fillText(f >= 1000 ? `${f / 1000}k` : String(f), xOf(f), ht - 2 * dpr);
    // The loudest frequency, its real level (the bins carry a +4.5 dB/octave tilt for the picture).
    let best = 0, top = -Infinity;
    for (let i = 0; i < n; i++) { const f = 10 ** (lf0 + (i / (n - 1)) * span), real = bins[i] - 4.5 * Math.log2(f / 1000); if (real > top) { top = real; best = f; } }
    v.stats = { silent: top < -100 || silent(ring), peakHz: best };
  }

  const db = (x) => {  // (over full scale with a +: it clips)
    if (x <= 1e-6) return "silent";
    const v = 20 * Math.log10(x);
    return `${v > 0.05 ? "+" : ""}${v.toFixed(1).replace("-", "−")} dB`;
  };
  function info(v) {  // the words beside the picture, a few times a second
    const s = v.stats || seen[v.key];
    if (!s || !v.info) return;
    seen[v.key] = s;
    if (v.type === "waveform") {
      v.info.textContent = s.silent ? "silent" : `peak ${db(s.peak)}${s.zoom >= 1.5 ? ` · ×${s.zoom < 10 ? s.zoom.toFixed(1) : Math.round(s.zoom)}` : ""}`;
      return;
    }
    if (v.type === "spectrum") {
      v.info.textContent = s.silent ? "silent" : `loudest ${s.peakHz >= 1000 ? `${(s.peakHz / 1000).toFixed(1)} kHz` : `${Math.round(s.peakHz)} Hz`}`;
      return;
    }
    if (v.type === "meter") {
      const say = (x) => (isFinite(x) ? x.toFixed(1).replace("-", "−") : "—");
      v.mom.textContent = say(s.mom); v.short.textContent = say(s.short);
      // (what it means: around −14 is where streaming platforms set music; louder gets turned down)
      v.word.textContent = !isFinite(s.short) ? "" : s.short > -9 ? "very loud" : s.short > -16 ? "streaming loud" : s.short > -24 ? "moderate" : "quiet";
      return;
    }
    // (owner: "I still don't know what correlation means": the number, and what it means for you)
    // (around 0 the sides are unrelated: very wide, not cancelling; only clearly below it cancels)
    const verdict = s.silent ? ["", "silent"] : s.corr < -0.05 ? ["bad", "cancels in mono"] : s.corr < 0.3 ? ["wide", "very wide"] : ["ok", "fine in mono"];
    const c2 = Math.round(s.corr * 100) / 100 || 0;  // (−0.00 shown as 0.00)
    v.corrText.textContent = s.silent ? "—" : `${c2 > 0 ? "+" : c2 < 0 ? "−" : ""}${Math.abs(c2).toFixed(2)}`;
    v.corrText.className = v.corrFill.className = verdict[0];
    v.verdict.textContent = verdict[1];
    v.info.textContent = s.silent ? "—" : `${Math.round(s.width * 100)} %`;
    v.wordW.textContent = s.silent ? "" : s.width < 0.1 ? "nearly mono" : s.width < 0.8 ? "normal stereo" : s.width <= 1.05 ? "very wide"
      : s.width < 1.9 ? "beyond the sides" : "out of phase";
  }
  // The two small bars glide every frame (owner: smooth), eased toward the measured values; a
  // transform (scaleX from the bar's own origin), so only the compositor moves them. Correlation's
  // starts at the middle: right for +, left for − (a negative scale flips it).
  const ease = (from, to, dt) => (from == null ? to : from + (to - from) * (1 - Math.exp(-dt / 0.08)));
  function glide(v, dt) {
    const s = v.stats;
    if (!s) return;
    v.shown = ease(v.shown, s.silent ? 0 : s.corr ?? 0, dt);
    v.wshown = ease(v.wshown, s.silent ? 0 : s.width ?? 0, dt);
    v.corrFill.style.transform = `scaleX(${v.shown.toFixed(3)})`;
    // (the width bar's colours stay put, owner: a cover over the rest slides off from the right;
    // the bar is the whole scale, 0 … 200 %, its middle the L and R lines)
    v.widthFill.style.transform = `scaleX(${(1 - Math.min(1, v.wshown / 2)).toFixed(3)})`;
  }

  // At most ~60 pictures a second (owner: the visualizers' load): on a 144 Hz screen every other
  // refresh is skipped; the motion is the same, the drawing under half.
  let lastT = 0;
  function tick(t) {
    raf = 0;
    if (lastT && t - lastT < 14) { kick(); return; }
    const dt = Math.min(0.1, lastT ? (t - lastT) / 1000 : 1 / 60);
    palette(t);
    lastT = t;
    for (const ring of Object.values(rings)) advance(ring, dt);
    for (const [id, v] of views) {
      if (!v.canvas.isConnected) { views.delete(id); continue; }
      ({ waveform, stereo, meter, spectrum })[v.type](v, rings[v.key], dt, t);
      if (v.corrFill) glide(v, dt);
    }
    if (t - lastInfo > 100) { lastInfo = t; views.forEach(info); }  // (ten a second, owner: faster)
    if (views.size && !document.hidden) kick();
  }
  function kick() { if (!raf && views.size) raf = requestAnimationFrame(tick); }

  // ---- a card's display, and the big panel ---------------------------------------------------
  // The stereo picture's guides (the half disc, mono and the L/R lines): drawn once, an SVG under
  // the canvas, so the dots' fading never touches them.
  function guides(big) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "diag-guides"); svg.setAttribute("viewBox", "0 0 200 104"); svg.setAttribute("preserveAspectRatio", "xMidYMax meet");
    const cx = 100, cy = 101, r = 96, ln = (a, op) => `<path d="M${cx} ${cy} L${(cx - Math.sin(a) * r).toFixed(1)} ${(cy - Math.cos(a) * r).toFixed(1)}" stroke="rgba(255,255,255,${op})"/>`;
    svg.innerHTML = [r / 3, (2 * r) / 3, r].map((q) => `<path d="M${cx - q} ${cy} A${q} ${q} 0 0 1 ${cx + q} ${cy}" fill="none" stroke="rgba(255,255,255,0.06)"/>`).join("")
      + ln(Math.PI / 2, 0.06) + ln(Math.PI / 4, 0.09) + ln(0, 0.14) + ln(-Math.PI / 4, 0.09) + ln(-Math.PI / 2, 0.06)
      + `<text x="${cx - r * 0.72 - 6}" y="${cy - r * 0.72 - 3}" style="fill:var(--diag-l)" font-size="${big ? 7 : 9}">L</text>`
      + `<text x="${cx + r * 0.72 + 1}" y="${cy - r * 0.72 - 3}" style="fill:var(--diag-r)" font-size="${big ? 7 : 9}">R</text>`;
    return svg;
  }
  function view(key, type, on, big = false) {
    const id = `${key}|${big ? "big" : ""}`, was = views.get(id);
    const canvas = canvases[id] ||= h("canvas", { class: "diag-canvas" });  // (kept: its picture with it)
    const display = h("div", { class: `diag-display ${type}${on ? "" : " off"}`, "data-tip": on ? "Click to open it large" : "Off: turn it on to see it" },
      type === "stereo" ? guides(big) : null, canvas, on ? null : h("span", { class: "diag-off", text: "Off" }));
    const v = { key, type, canvas, big, info: h("span", { class: "diag-info" }), drawn: was?.drawn, stats: was?.stats, shown: was?.shown, zoom: was?.zoom,
                wshown: was?.wshown, wz: was?.wz, sg: was?.sg };
    let extra = null;
    if (type === "stereo") {  // two tiles, as the Counter's (owner's pick D)
      v.corrText = h("b", { text: "—" }); v.verdict = h("em", { text: "" });
      v.corrFill = h("i", { class: "" }); v.widthFill = h("i", { class: "cover" });
      v.info = h("b", { text: "—" });
      extra = h("div", { class: "stereo-tiles" },
        h("div", { class: "stereo-tile corr", "data-tip": "Correlation: how alike left and right are. +1 the same (mono); +0.3 … +0.9 normal music; around 0 very wide; below 0 they are opposite, and parts cancel when played in mono (a phone, a speaker, a stream mixed down)." },
          v.corrText, h("small", { text: "correlation" }), v.verdict, h("span", { class: "mini" }, h("span", { class: "zero" }), v.corrFill)),
        h("div", { class: "stereo-tile width", "data-tip": "Width: how far the picture fans out. 0 % a vertical line (mono); 100 % out to the L and R lines (left and right unrelated); 200 % lying flat (fully out of phase). Normal stereo music is about 30–80 %." },
          v.info, h("small", { text: "width" }), v.wordW = h("em", { text: "" }), h("span", { class: "mini" }, v.widthFill, h("span", { class: "zero" }))));
    }
    if (type === "meter") {  // loudness, two tiles as the others'
      v.mom = h("b", { text: "—" }); v.short = h("b", { text: "—" }); v.word = h("em", { text: "" });
      v.m = was?.m;
      extra = h("div", { class: "stereo-tiles" },
        h("div", { class: "stereo-tile", "data-tip": "Momentary loudness (LUFS, the last 0.4 s), as streaming platforms measure it" },
          v.mom, h("small", { text: "LUFS momentary" }), h("em", { text: "last 0.4 s" })),
        h("div", { class: "stereo-tile", "data-tip": "Short-term loudness (LUFS, the last 3 s). Streaming platforms play music at about −14 LUFS and turn anything louder down" },
          v.short, h("small", { text: "LUFS short-term" }), v.word));
    }
    if (on) { views.set(id, v); kick(); info(v); } else views.delete(id);
    if (type === "waveform" || type === "spectrum") { v.info.classList.add("diag-peak"); display.append(v.info); }  // (in its corner: the foot holds the switches)
    return { display, extra, info: null, v };
  }

  // The Waveform's two-way switch, Stereo | Mono (owner): set(mono) is the board's (it saves it).
  function modeSwitch(key, set) {
    const btn = (m, text) => {
      const b = h("button", { type: "button", text, "aria-pressed": String(!!mono[key] === m) });
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        for (const x of b.parentElement.children) x.setAttribute("aria-pressed", String(x === b));
        set(m);
      });
      return b;
    };
    return h("span", { class: "diag-mode", role: "group", "aria-label": "Channels", "data-key": key,
                       "data-tip": "Stereo: L and R each · Mono: the two combined" }, btn(false, "Stereo"), btn(true, "Mono"));
  }
  // Every switch of a card shows the same (owner: the card's and its panel's could disagree).
  function showSwitches(key) {
    for (const el of document.querySelectorAll(".diag-mode, .diag-toggle")) {
      if (el.dataset.key !== key) continue;
      if (el.classList.contains("diag-mode")) for (const b of el.children) b.setAttribute("aria-pressed", String((b.textContent === "Mono") === !!mono[key]));
      else el.setAttribute("aria-pressed", String(TOGGLES[el.dataset.toggle].is(key)));
    }
  }
  const setMono = (key, on) => { mono[key] = !!on; showSwitches(key); };
  // The Waveform's toggles (owner): one button each, lit while on. Smooth leaves out the very
  // sharp detail; Zoom (on unless turned off) fits the wave to its loudest recent peak.
  const TOGGLES = {
    smooth: { label: "Smooth", is: (k) => !!smooth[k], tip: "Smooth: the very sharp detail left out (above ~2 kHz), the shape kept" },
    zoom: { label: "Zoom", is: (k) => !fixed[k], tip: "Zoom: the wave fitted to its loudest recent peak (the peak in dB stays true). Off: the edge is full scale, 0 dB" },
  };
  function toggle(name, key, set) {
    const t = TOGGLES[name], b = h("button", { type: "button", class: "diag-toggle", text: t.label, "aria-pressed": String(t.is(key)), "data-tip": t.tip,
                                               "data-key": key, "data-toggle": name });
    b.addEventListener("click", (e) => { e.stopPropagation(); const on = !t.is(key); b.setAttribute("aria-pressed", String(on)); set(on); });
    return b;
  }
  const setSmooth = (key, on) => { smooth[key] = !!on; showSwitches(key); };
  const setZoom = (key, on) => { fixed[key] = on === false; showSwitches(key); };

  const P = { key: null, el: null, ctx: null };
  function open(key, ctx) {
    close(false);
    const shut = h("button", { type: "button", class: "eq-close", "aria-label": "Close", text: "✕" });
    const big = view(key, ctx.type, true, true);
    const head = h("header", { class: "eq-head" }, h("span", { class: "pin" }), h("span", { class: "eq-title", text: ctx.title }),
                   h("span", { class: "eq-space" }),
                   ctx.type === "waveform" && ctx.setMono ? modeSwitch(key, ctx.setMono) : null,
                   ctx.type === "waveform" && ctx.set ? toggle("smooth", key, (on) => ctx.set("smooth", on)) : null,
                   ctx.type === "waveform" && ctx.set ? toggle("zoom", key, (on) => ctx.set("zoom", on)) : null, shut);
    P.el = h("section", { class: `eq-panel diag-panel ${ctx.type}`, tabindex: "-1", role: "dialog", "aria-label": ctx.title },
             head, h("div", { class: "diag-big" }, big.display, big.extra));
    Object.assign(P, { key, ctx });
    shut.addEventListener("click", () => close());
    P.el.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    document.body.append(P.el);
    P.el.focus({ preventScroll: true });
    return P.el;
  }
  function close(tell = true) {
    if (!P.el) return;
    P.el.remove(); P.el = null;
    const key = P.key, ctx = P.ctx;
    P.key = null;
    if (tell) ctx?.done?.(key);
  }

  // For shots (no server): made-up stereo music on these cards, as FakeTaps makes it.
  function fake(keys, wide = 1) {  // (wide: how far apart L and R are; 1 is a typical mix)
    let t = 0;
    const step = () => {
      const n = 960, f = new Float32Array(n * keys.length * 2);
      for (let j = 0; j < n; j++, t++) {
        const s = t / RATE, mid = 0.5 * Math.sin(2 * Math.PI * 220 * s) + 0.2 * Math.sin(2 * Math.PI * 661 * s);
        const side = wide * (mid * 0.4 * Math.sin(2 * Math.PI * 3 * s) + 0.06 * Math.sin(2 * Math.PI * 330 * s) + 0.03 * (Math.random() - 0.5));
        for (let i = 0; i < keys.length; i++) { f[(j * keys.length + i) * 2] = mid + side; f[(j * keys.length + i) * 2 + 1] = mid - side; }
      }
      feed(keys, f);
    };
    for (let i = 0; i < 12; i++) step();
    return setInterval(step, 20);
  }

  return { pcm, keys, isFrame, frame, view, open, close, selftest, modeSwitch, setMono, toggle, setSmooth, setZoom, isOpen: (key) => P.key === key, openKey: () => P.key, fake, heard: (key) => !!rings[key]?.at,
           TYPES: { waveform: "Waveform", stereo: "Stereo", counter: "Counter", meter: "Meter", spectrum: "Spectrum" } };
})();
