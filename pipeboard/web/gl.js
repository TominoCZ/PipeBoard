// The GPU renderer (PLAN §4a): one WebGL2 context draws every picture that moves: the wires and
// their live waves, the scopes, the effect panels' live parts. Each picture is a surface, a plain
// 2D canvas in its own place in the page, handed a copy of what was drawn for it (drawImage from
// this context's canvas: a copy on the GPU). One context, so the browser's limit on contexts never
// comes into it; plain canvases in the page, so the layering with cards, menus and panels is what
// it was. (Measured on the owner's GPU, 2026-11-09: nine surfaces at 60 fps cost 15 ms/s of the
// page's time this way; a bitmap handed to each instead cost 430, the context resized for each.)
// WebGL2 only (owner): the app runs in its own Chrome window; the shots draw on the GPU too.
"use strict";

const GL = (() => {
  const canvas = document.createElement("canvas");
  let gl = null, lost = false, gen = 0;
  const cache = new Map();  // this context's programs and the modules' own buffers and textures
  // A lost context (a driver reset) comes back empty: everything is made again from the page's own
  // state, each module re-uploading what it keeps once it sees a new generation.
  canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); lost = true; });
  canvas.addEventListener("webglcontextrestored", () => { lost = false; gen++; cache.clear(); extensions(); dispatchEvent(new Event("gl-restored")); });
  // (asked again after a loss: a context comes back without them)
  const extensions = () => { if (!gl.getExtension("EXT_color_buffer_float")) throw new Error("this GPU cannot draw into float textures"); };

  function context() {
    if (!gl) {
      gl = canvas.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false });
      if (!gl) throw new Error("this browser has no WebGL2");
      extensions();
    }
    return lost ? null : gl;
  }

  // Made once per context (key: what it is), again after a loss.
  function make(key, create) {
    if (!cache.has(key)) cache.set(key, create(gl));
    return cache.get(key);
  }

  // A program from its two shaders; its uniforms by name (u.name), looked up once.
  function program(vs, fs) {
    return make(vs + fs, () => {
      const one = (kind, src) => {
        const sh = gl.createShader(kind);
        // (samplers too: GLSL's default for them is lowp, and the owner's NVIDIA read float textures
        // at 16 bits then: slot numbers past 2048 lost their odd ones and the live waves went wrong
        // half a minute in; positions past 2048 px kinked. SwiftShader, as the shots draw, had hidden it.)
        gl.shaderSource(sh, `#version 300 es\nprecision highp float;\nprecision highp int;\nprecision highp sampler2D;\n${src}`);
        gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS) && !gl.isContextLost()) throw new Error(`shader: ${gl.getShaderInfoLog(sh)}`);
        return sh;
      };
      const p = gl.createProgram();
      gl.attachShader(p, one(gl.VERTEX_SHADER, vs));
      gl.attachShader(p, one(gl.FRAGMENT_SHADER, fs));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(`program: ${gl.getProgramInfoLog(p)}`);
      const u = {};
      for (let i = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS) - 1; i >= 0; i--) {
        const name = gl.getActiveUniform(p, i).name.replace(/\[0\]$/, "");
        u[name] = gl.getUniformLocation(p, name);
      }
      return { p, u };
    });
  }

  // A float texture of w × h texels of four values (data: a Float32Array or null), read with
  // texelFetch: no filtering, nothing wraps. half: 16-bit floats (a picture drawn into and kept).
  function texture(w, h, data = null, half = false) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, half ? gl.RGBA16F : gl.RGBA32F, w, h, 0, gl.RGBA, half ? gl.HALF_FLOAT : gl.FLOAT, data);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST],
                          [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
    return t;
  }

  // A float texture by name holding these values now (w × h texels of four), made again when it
  // must grow. Bound to texture unit `unit`.
  function data(key, w, h, values, unit = 0) {
    const d = make(`data ${key}`, () => ({ tex: null, w: 0, h: 0 }));
    if (d.w < w || d.h < h) {
      if (d.tex) gl.deleteTexture(d.tex);
      Object.assign(d, { w: Math.max(w, d.w), h: Math.max(h, d.h) });
      d.tex = texture(d.w, d.h);
    }
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, d.tex);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, w, h, gl.RGBA, gl.FLOAT, values);
    return d.tex;
  }

  // A picture kept from frame to frame (the stereo dots fading): a half-float texture and its
  // framebuffer, by name, the surface's size (emptied when that changes or the context is lost).
  function kept(key, w, h) {
    const k = make(`kept ${key}`, () => ({ fb: gl.createFramebuffer(), tex: null, w: 0, h: 0 }));
    if (k.w !== w || k.h !== h) {
      if (k.tex) gl.deleteTexture(k.tex);
      Object.assign(k, { w, h, tex: texture(w, h, null, true) });
      gl.bindFramebuffer(gl.FRAMEBUFFER, k.fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, k.tex, 0);
      gl.clearColor(0, 0, 0, 0); gl.disable(gl.SCISSOR_TEST); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }
    return k;
  }

  // One triangle over the whole viewport: for shaders that draw every pixel (gl.drawArrays(gl.TRIANGLES, 0, 3)).
  const FULL_VS = `void main() { gl_Position = vec4(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0, 0.0, 1.0); }`;
  // CSS colour → [r, g, b] in 0…1: from the colour's own text (a browser against fingerprinting
  // puts noise in what a canvas reads back: a friend's wires came out green and blue).
  const rgbs = {};
  function rgb(css) {
    if (!rgbs[css]) {
      let text = css.trim();
      if (!/^#[0-9a-f]{6}$/i.test(text)) {  // (any other form: the page's own reading of it, rgb(…))
        const el = document.createElement("i");
        el.style.color = text; document.body.append(el); text = getComputedStyle(el).color; el.remove();
      }
      rgbs[css] = text.startsWith("#") ? [1, 3, 5].map((i) => parseInt(text.slice(i, i + 2), 16) / 255)
        : (text.match(/[\d.]+/g) || [0, 0, 0]).slice(0, 3).map((v) => +v / 255);
    }
    return rgbs[css];
  }

  // A surface's backing store the size it has on screen, in device pixels (sharp at any zoom).
  function fit(c, w = null, h = null) {
    const dpr = devicePixelRatio || 1;
    if (w == null) { const box = c.getBoundingClientRect(); w = box.width; h = box.height; }
    const pw = Math.max(1, Math.round(w * dpr)), ph = Math.max(1, Math.round(h * dpr));
    if (c.width !== pw || c.height !== ph) { c.width = pw; c.height = ph; }
    return [pw, ph];
  }

  // Draws one surface: paint(gl, w, h, tile) into a cleared w × h corner of this context's canvas
  // (premultiplied alpha, blending on), then that corner copied onto the surface. A surface bigger
  // than this GPU's drawing buffer may be (Firefox and LibreWolf against fingerprinting: 2 048 px,
  // a friend's wires drew nothing) is drawn in tiles, each its own viewport (one past the drawing
  // buffer drew nothing there). tile: [x, y, w, h] of this one in device px from the surface's
  // bottom left; a shader that places things in the surface maps them into the tile, one that
  // reads gl_FragCoord adds x, y. Only the wires get that big; the scopes are one tile.
  // False when there is no context to draw with (lost, coming back).
  function draw(surface, paint) {
    const g = context();
    if (!g) return false;
    const w = surface.width, h = surface.height, lim = make("limit", () => g.getParameter(g.MAX_RENDERBUFFER_SIZE));
    const cw = Math.min(lim, w), ch = Math.min(lim, h);
    if (canvas.width < cw || canvas.height < ch) {  // (grown only: a resize empties it and costs)
      canvas.width = Math.max(canvas.width, cw); canvas.height = Math.max(canvas.height, ch);
    }
    const out = surface.getContext("2d");
    out.clearRect(0, 0, w, h);
    for (let y0 = 0; y0 < h; y0 += ch) {
      for (let x0 = 0; x0 < w; x0 += cw) {
        const tw = Math.min(cw, w - x0), th = Math.min(ch, h - y0);
        g.bindFramebuffer(g.FRAMEBUFFER, null);
        g.viewport(0, 0, tw, th);
        g.enable(g.SCISSOR_TEST); g.scissor(0, 0, tw, th);
        g.clearColor(0, 0, 0, 0); g.clear(g.COLOR_BUFFER_BIT);
        g.enable(g.BLEND); g.blendFunc(g.ONE, g.ONE_MINUS_SRC_ALPHA);
        paint(g, w, h, [x0, y0, tw, th]);
        out.drawImage(canvas, 0, canvas.height - th, tw, th, x0, h - y0 - th, tw, th);  // (GL's rows start at the bottom)
      }
    }
    return true;
  }

  return { context, make, program, texture, data, kept, fit, draw, rgb, FULL_VS, get gen() { return gen; } };
})();
