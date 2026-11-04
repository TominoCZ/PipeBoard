"use strict";
// A number box, the same everywhere (owner): on a card (a gain's level, a limiter's ceiling...)
// and in the EQ's and the dynamics panels' strips. Type, scroll or ↑/↓ to change the number
// (Shift: finer); Enter applies it; Esc or clicking away puts the applied value back. Nothing
// reaches the sound before Enter.
//   numField({ value(), show(v), parse(text) → number | NaN, step(v, dir, fine), fit(v), lo, hi,
//              apply(v), cls, aria, tip, focus(), done(), keyed() })
// fit rounds and clamps; lo/hi only decide whether a typed number was out of range (it flashes).
// focus/done: the box got / lost the focus (done after it is settled either way); keyed: it was
// left by Enter or Esc (the panels take the focus back for their own keys).

function numField(o) {
  const input = document.createElement("input");
  Object.assign(input, { type: "text", className: o.cls, value: o.show(o.value()), spellcheck: false });
  input.setAttribute("inputmode", "decimal");
  input.setAttribute("aria-label", o.aria);
  if (o.tip) input.dataset.tip = o.tip;
  let applied = false;
  const flash = (cls) => { input.classList.add(cls); setTimeout(() => input.classList.remove(cls), 600); };
  const nudge = (dir, fine) => {  // edits the number only
    const t = o.parse(input.value);
    input.value = o.show(o.fit(o.step(Number.isFinite(t) ? t : o.value(), dir, fine)));
  };
  const enter = () => {
    const t = o.parse(input.value);
    if (Number.isNaN(t)) { flash("bad"); input.select(); return; }
    const v = o.fit(t);
    if ((t < o.lo || t > o.hi) && t !== -Infinity) flash("limit");  // "-inf": the lowest, on purpose
    if (v !== o.value()) o.apply(v);
    applied = true;
    input.value = o.show(v);
    input.blur();
    o.keyed?.();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();  // typing never reaches the panel's or the board's own keys
    if (e.key === "Enter") { e.preventDefault(); enter(); }
    else if (e.key === "Escape") { e.preventDefault(); input.blur(); o.keyed?.(); }
    else if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); nudge(e.key === "ArrowUp" ? 1 : -1, e.shiftKey); }
  });
  input.addEventListener("wheel", (e) => {
    e.preventDefault(); e.stopPropagation();
    if (document.activeElement !== input) input.focus();
    nudge(-Math.sign(e.deltaY), e.shiftKey);
  }, { passive: false });
  input.addEventListener("focus", () => { applied = false; input.select(); o.focus?.(); });
  input.addEventListener("blur", () => {
    if (!applied) input.value = o.show(o.value());  // Esc or clicking away: back to what applies
    applied = false;
    o.done?.();
  });
  return input;
}

// Steps for numField: dB-like values move by 0.5 (Shift 0.1); times, Q and the like by a share of
// themselves, so 5 ms and 500 ms both move sensibly (0 steps up to 1).
const STEPS = {
  add: (v, dir, fine) => v + dir * (fine ? 0.1 : 0.5),
  share: (v, dir, fine) => (v > 0 ? v * (fine ? 1.03 : 1.15) ** dir : Math.max(0, dir)),
  semitone: (v, dir, fine) => v * 2 ** (dir / (fine ? 48 : 12)),
};
const parseNum = (t) => {
  const s = String(t).trim().toLowerCase().replace(/−/g, "-").replace("∞", "inf").replace(",", ".");
  if (/^-inf(inity)?(\s*db)?$/.test(s)) return -Infinity;
  if (/^\+?inf(inity)?(\s*db)?$/.test(s)) return Infinity;
  const m = /^[+-]?(\d+\.?\d*|\.\d+)/.exec(s);
  return m ? Number(m[0]) : NaN;
};
