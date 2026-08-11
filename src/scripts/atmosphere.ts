/**
 * WowRepo atmospheres — the generative, motion-led backgrounds a page can wear.
 *
 * One shared engine renders a volumetric daylight field in WebGL (dappled
 * canopy light + god-rays + a rising sun), and a small 2D foreground that
 * differs per variant. The variant is chosen by art direction (the page plan's
 * `atmosphere.variant`) to match the page's meaning.
 *
 * Principles:
 *   - Purely decorative. Every page is fully readable and usable without it.
 *   - Honours prefers-reduced-motion: renders a single calm still frame, no loop.
 *   - No external requests (all procedural), so it stays cheap and offline-safe.
 *
 * Extend: add a variant name in src/engine/page-plan.ts (ATMOSPHERES) and a
 * matching config + optional foreground below.
 */

type Variant = 'canopy-light' | 'sunbeam' | 'light-columns' | 'sheltering-glow';

interface VariantConfig {
  sun: [number, number];
  sunRise: number;
  warmth: number;
  ray: number;
  mode: number; // 0 canopy, 1 sunbeam, 2 columns, 3 shelter
  tree: boolean;
  dust: boolean;
  columns: boolean;
  rings: boolean;
}

const CONFIGS: Record<Variant, VariantConfig> = {
  'canopy-light': { sun: [0.8, 0.3], sunRise: 0.55, warmth: 1, ray: 1.05, mode: 0, tree: true, dust: false, columns: false, rings: false },
  'sunbeam': { sun: [0.82, 0.86], sunRise: 0.08, warmth: 1.05, ray: 1.5, mode: 1, tree: false, dust: true, columns: false, rings: false },
  'light-columns': { sun: [0.5, 0.9], sunRise: 0.05, warmth: 0.95, ray: 0.7, mode: 2, tree: false, dust: false, columns: true, rings: false },
  'sheltering-glow': { sun: [0.5, 0.5], sunRise: 0, warmth: 1.1, ray: 0.5, mode: 3, tree: false, dust: false, columns: false, rings: true },
};

const VERT = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';

const FRAG = `
precision highp float;
uniform vec2 res; uniform float time; uniform float scroll;
uniform vec2 sun; uniform float warm; uniform float ray; uniform float mode;
float hash(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.0-2.0*f);
  float a=hash(i),b=hash(i+vec2(1,0)),c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));
  return mix(mix(a,b,u.x),mix(c,d,u.x),u.y);}
float fbm(vec2 p){float v=0.0,a=0.55;mat2 m=mat2(1.6,1.2,-1.2,1.6);
  for(int i=0;i<5;i++){v+=a*noise(p);p=m*p;a*=0.5;}return v;}
void main(){
  vec2 uv=gl_FragCoord.xy/res;
  vec2 p=(gl_FragCoord.xy-0.5*res)/res.y;
  float t=time*0.03;
  vec2 q=vec2(fbm(p*2.2+vec2(0.0,t)),fbm(p*2.2+vec2(3.7,-t*0.7)));
  float dapple=fbm(p*3.0+1.7*q+vec2(t*0.4,-t*0.2));
  vec2 s=vec2(sun.x, sun.y + scroll*0.0);
  vec2 dir=(s-uv);
  float rays=0.0; vec2 sp=uv;
  for(int i=0;i<14;i++){sp+=dir*0.055;rays+=fbm(sp*3.2+1.2*q)*(1.0-float(i)/14.0);}
  rays/=8.0;
  vec3 base=vec3(0.945,0.905,0.815);
  vec3 shade=vec3(0.52,0.62,0.44);
  vec3 gold=mix(vec3(1.0,0.90,0.62), vec3(1.0,0.85,0.55), warm-1.0);
  float dap = mode>2.5 ? 0.25 : 0.55;
  vec3 col=mix(base, shade, dapple*dap);
  col=mix(col, gold, clamp(rays*ray,0.0,0.85));
  float breath = 0.5+0.5*sin(time*0.0007);
  if(mode>2.5){ // sheltering glow — a warm centre that breathes
    float sd=length((uv-vec2(0.5,0.46))*vec2(res.x/res.y,1.0));
    col+=gold*exp(-sd*2.2)*(0.30+breath*0.14);
    for(int i=0;i<5;i++){float r=0.12+float(i)*0.1; float ring=smoothstep(0.012,0.0,abs(sd-r*(0.94+breath*0.1))); col+=gold*ring*0.05*(1.0-float(i)/5.0);}
  } else {
    float sd=length((uv-s)*vec2(res.x/res.y,1.0));
    col+=gold*exp(-sd*2.6)*(0.5+scroll*0.25);
  }
  col=mix(col, mix(gold,shade,0.4), smoothstep(0.35,-0.15,p.y)*0.16);
  col*=1.0-0.16*dot(p*vec2(0.85,1.0),p*vec2(0.85,1.0));
  col+=(hash(uv*res+time)-0.5)*0.02;
  gl_FragColor=vec4(col,1.0);
}`;

function rndf(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Branch { x1: number; y1: number; x2: number; y2: number; gS: number; gE: number; w: number; d: number; }
interface Leaf { x: number; y: number; ang: number; g: number; size: number; color: string; back: boolean; phase: number; }
interface Mote { x: number; y: number; r: number; sp: number; drift: number; ph: number; a: number; }
interface Column { x: number; w: number; lit: number; g: number; }

function initAtmosphere(root: HTMLElement): void {
  const variant = (root.dataset.variant as Variant) || 'canopy-light';
  const cfg = CONFIGS[variant] ?? CONFIGS['canopy-light'];
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const glcMaybe = root.querySelector<HTMLCanvasElement>('canvas[data-layer="gl"]');
  const fxcMaybe = root.querySelector<HTMLCanvasElement>('canvas[data-layer="fx"]');
  if (!glcMaybe || !fxcMaybe) return;
  const glc = glcMaybe;
  const fxc = fxcMaybe;
  const glCtx = glc.getContext('webgl', { antialias: true });
  const fxCtx = fxc.getContext('2d');
  if (!glCtx || !fxCtx) {
    // No WebGL — leave the paper background; the page is fully usable regardless.
    root.style.display = 'none';
    return;
  }
  // Bind to non-null locals so the render closures keep the narrowed types.
  const gl = glCtx;
  const fx = fxCtx;

  let W = 0, H = 0, DPR = 1;
  const mouse = { x: -9999, y: -9999, active: false };
  window.addEventListener('pointermove', (e) => { mouse.x = e.clientX; mouse.y = e.clientY; mouse.active = true; }, { passive: true });
  const prog = (): number => { const m = document.body.scrollHeight - innerHeight; return m > 0 ? Math.min(1, scrollY / m) : 0; };

  // --- compile shader ---
  const mk = (type: number, src: string): WebGLShader => {
    const sh = gl.createShader(type)!;
    gl.shaderSource(sh, src); gl.compileShader(sh);
    return sh;
  };
  const prgm = gl.createProgram()!;
  gl.attachShader(prgm, mk(gl.VERTEX_SHADER, VERT));
  gl.attachShader(prgm, mk(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prgm); gl.useProgram(prgm);
  const buf = gl.createBuffer()!;
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prgm, 'p');
  gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = (n: string) => gl.getUniformLocation(prgm, n);
  const uRes = U('res'), uTime = U('time'), uScroll = U('scroll'), uSun = U('sun'), uWarm = U('warm'), uRay = U('ray'), uMode = U('mode');

  // --- foreground state ---
  const LC = ['#4d7358', '#5f8560', '#6f9463', '#7fa06d', '#3d6247'];
  const AU = ['#cf9a44', '#c17b3c'];
  let branches: Branch[] = [], leaves: Leaf[] = [], motes: Mote[] = [], columns: Column[] = [];
  let growth = 0, target = reduce ? 1 : 0.02, introUntil = 0;
  const leafSprites: Record<string, HTMLCanvasElement> = {};
  let glowSprite: HTMLCanvasElement | null = null;

  function makeLeaf(color: string): HTMLCanvasElement {
    const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
    const g = c.getContext('2d')!;
    g.translate(s * 0.15, s * 0.5);
    g.fillStyle = color;
    g.beginPath(); g.moveTo(0, 0); g.quadraticCurveTo(s * 0.4, -s * 0.28, s * 0.72, 0); g.quadraticCurveTo(s * 0.4, s * 0.28, 0, 0); g.fill();
    g.globalCompositeOperation = 'destination-over'; g.filter = 'blur(1.4px)';
    g.beginPath(); g.moveTo(0, 0); g.quadraticCurveTo(s * 0.4, -s * 0.32, s * 0.76, 0); g.quadraticCurveTo(s * 0.4, s * 0.32, 0, 0); g.fill();
    return c;
  }
  function makeGlow(): HTMLCanvasElement {
    const s = 64, c = document.createElement('canvas'); c.width = c.height = s;
    const g = c.getContext('2d')!;
    const gr = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    gr.addColorStop(0, 'rgba(255,238,190,1)'); gr.addColorStop(0.25, 'rgba(250,216,150,0.7)'); gr.addColorStop(1, 'rgba(250,216,150,0)');
    g.fillStyle = gr; g.fillRect(0, 0, s, s); return c;
  }

  function build(): void {
    branches = []; leaves = []; motes = []; columns = [];
    growth = 0; target = reduce ? 1 : 0.02; introUntil = performance.now() + 3800;
    const rnd = rndf(20260717), scale = Math.min(H, 1000);
    [...LC, ...AU].forEach((c) => (leafSprites[c] = makeLeaf(c)));
    glowSprite = makeGlow();

    if (cfg.tree) {
      const rootX = W * (W > 820 ? 0.6 : 0.5), rootY = H * 1.04, baseLen = scale * 0.2;
      const cluster = (x: number, y: number, ang: number, d: number): void => {
        const n = 4 + ((rnd() * 4) | 0);
        for (let i = 0; i < n; i++) {
          const au = rnd() < 0.12;
          leaves.push({ x, y, ang: ang + (rnd() - 0.5) * 1.9, g: -1, size: scale * 0.03 + rnd() * scale * 0.03 + (6 - d) * 1.4, color: au ? AU[(rnd() * AU.length) | 0] : LC[(rnd() * LC.length) | 0], back: rnd() < 0.42, phase: rnd() * 6.28 });
        }
      };
      const grow = (x: number, y: number, ang: number, len: number, d: number, gS: number, gSp: number): void => {
        if (d > 5 || len < scale * 0.012) return;
        const x2 = x + Math.cos(ang) * len, y2 = y + Math.sin(ang) * len, gE = gS + gSp;
        branches.push({ x1: x, y1: y, x2, y2, gS, gE, w: Math.max(1.2, (6 - d) * 1.9), d });
        const n = d === 0 ? 3 : rnd() < 0.4 ? 3 : 2, spread = 0.4 + d * 0.06, tip = d >= 4 || len * 0.7 < scale * 0.012;
        if (d >= 2) cluster(x2, y2, ang, d);
        for (let k = leaves.length - 1; k >= 0 && leaves[k].g === -1; k--) leaves[k].g = gE;
        if (tip) return;
        for (let i = 0; i < n; i++) {
          const tt = i / (n - 1) - 0.5;
          grow(x2, y2, ang + tt * spread * 2 + (rnd() - 0.5) * 0.28, len * (0.66 + rnd() * 0.14), d + 1, gE, gSp * 0.82);
        }
      };
      grow(rootX, rootY, -Math.PI / 2 + 0.04, baseLen * 1.5, 0, 0, 0.3);
      let mg = 0; branches.forEach((b) => (mg = Math.max(mg, b.gE)));
      if (mg > 0) { branches.forEach((b) => { b.gS /= mg; b.gE /= mg; }); leaves.forEach((l) => (l.g /= mg)); }
    }

    if (cfg.dust) for (let i = 0; i < (W > 820 ? 150 : 80); i++) motes.push({ x: rnd() * W, y: rnd() * H, r: 1 + rnd() * 2.4, sp: 0.04 + rnd() * 0.12, drift: (rnd() - 0.5) * 0.25, ph: rnd() * 6.28, a: 0.3 + rnd() * 0.5 });
    if (cfg.tree) for (let i = 0; i < (W > 820 ? 60 : 32); i++) motes.push({ x: rnd() * W, y: rnd() * H, r: 2 + rnd() * 4, sp: 0.12 + rnd() * 0.4, drift: (rnd() - 0.5) * 0.35, ph: rnd() * 6.28, a: 0.4 + rnd() * 0.5 });
    if (cfg.rings) for (let i = 0; i < 6; i++) motes.push({ x: 0, y: 0, r: 4 + rnd() * 6, sp: 0.00006 + rnd() * 0.00004, drift: 0.14 + i * 0.05, ph: rnd() * 6.28, a: 0.4 });
    if (cfg.columns) { const n = 6; for (let i = 0; i < n; i++) columns.push({ x: (i + 0.5) / n * W, w: scale * 0.05, lit: 0, g: i / n }); }
  }

  function sway(x: number, y: number, t: number): number {
    const h = Math.max(0, H - y) / H, amp = 10 * h * h;
    return Math.sin(t * 0.00055 + x * 0.006 + y * 0.0018) * amp + Math.sin(t * 0.0011 + x * 0.011) * amp * 0.28;
  }

  function drawForeground(t: number): void {
    const p = prog();
    fx.clearRect(0, 0, W, H);

    if (cfg.tree) {
      if (reduce) {
        growth = 1; // fully grown, one calm still frame — no animation
      } else {
        if (t < introUntil) target = Math.max(target, 0.46 * (1 - (introUntil - t) / 3800));
        target = Math.max(target, 0.46 + p * 0.54);
        growth += (target - growth) * 0.055;
      }
      fx.lineCap = 'round';
      for (let pass = 0; pass < 2; pass++) {
        for (const b of branches) {
          if (growth <= b.gS) continue;
          const pp = Math.min(1, (growth - b.gS) / (b.gE - b.gS)), ex = b.x1 + (b.x2 - b.x1) * pp, ey = b.y1 + (b.y2 - b.y1) * pp, s1 = sway(b.x1, b.y1, t), s2 = sway(ex, ey, t);
          fx.beginPath(); fx.moveTo(b.x1 + s1, b.y1); fx.lineTo(ex + s2, ey);
          if (pass === 0) { fx.strokeStyle = 'rgba(30,45,32,0.28)'; fx.lineWidth = b.w + 3; } else { fx.strokeStyle = b.d < 2 ? '#33513a' : '#456a4d'; fx.lineWidth = b.w; }
          fx.stroke();
        }
      }
      for (let layer = 0; layer < 2; layer++) {
        for (const l of leaves) {
          const front = !l.back; if ((layer === 0) === front) continue; if (growth <= l.g) continue;
          const ap = Math.min(1, (growth - l.g) / 0.06), s = l.size * ap, sx = sway(l.x, l.y, t), spr = leafSprites[l.color];
          if (!spr) continue;
          fx.save(); fx.translate(l.x + sx, l.y); fx.rotate(l.ang + (reduce ? 0 : Math.sin(t * 0.0013 + l.phase) * 0.16));
          fx.globalAlpha = (l.back ? 0.62 : 0.98) * ap; fx.drawImage(spr, -s * 0.15, -s * 0.5, s, s); fx.restore();
        }
      }
      fx.globalAlpha = 1;
    }

    if (cfg.columns && glowSprite) {
      fx.globalCompositeOperation = 'lighter';
      for (const c of columns) {
        const lit = Math.max(0, Math.min(1, (p - c.g) / 0.12));
        c.lit += (lit - c.lit) * 0.08;
        if (c.lit < 0.01) continue;
        const grad = fx.createLinearGradient(c.x, H, c.x, 0);
        grad.addColorStop(0, `rgba(252,226,168,${0.05 * c.lit})`);
        grad.addColorStop(0.45, `rgba(252,228,172,${0.30 * c.lit})`);
        grad.addColorStop(1, 'rgba(252,230,180,0)');
        fx.fillStyle = grad;
        const w = c.w * (1.2 + c.lit * 0.8);
        fx.fillRect(c.x - w / 2, 0, w, H);
        // a soft seed of light at the base
        const sz = 34 * c.lit; fx.globalAlpha = c.lit; fx.drawImage(glowSprite, c.x - sz / 2, H * 0.82 - sz / 2, sz, sz);
      }
      fx.globalCompositeOperation = 'source-over'; fx.globalAlpha = 1;
    }

    if ((cfg.dust || cfg.tree) && glowSprite && motes.length) {
      fx.globalCompositeOperation = 'lighter';
      for (const m of motes) {
        if (cfg.rings) continue;
        m.y -= m.sp; m.x += m.drift + Math.sin(t * 0.0004 + m.ph) * 0.25;
        const dx = mouse.x - m.x, dy = mouse.y - m.y, d2 = dx * dx + dy * dy;
        if (mouse.active && d2 < 40000 && d2 > 1) { const f = (1 - d2 / 40000) * 0.02; m.x += dx * f; m.y += dy * f; }
        if (m.y < -16) { m.y = H + 16; m.x = Math.random() * W; }
        const a = m.a * (cfg.dust ? 0.5 : 0.35 + p * 0.65) * (0.6 + 0.4 * Math.sin(t * 0.002 + m.ph));
        const sz = m.r * (cfg.dust ? 5 : 8);
        fx.globalAlpha = Math.min(1, a); fx.drawImage(glowSprite, m.x - sz / 2, m.y - sz / 2, sz, sz);
      }
      fx.globalCompositeOperation = 'source-over'; fx.globalAlpha = 1;
    }

    if (cfg.rings && glowSprite) {
      const cx = W * 0.5, cy = H * 0.46, breath = reduce ? 0.5 : 0.5 + 0.5 * Math.sin(t * 0.0007);
      fx.globalCompositeOperation = 'lighter';
      for (const m of motes) {
        m.ph += m.sp * (reduce ? 0 : 16);
        const R = Math.min(W, H) * m.drift * (0.96 + breath * 0.08);
        const x = cx + Math.cos(m.ph) * R * 1.5, y = cy + Math.sin(m.ph) * R, sz = m.r * 5;
        fx.globalAlpha = 0.5 * (0.5 + breath * 0.5); fx.drawImage(glowSprite, x - sz / 2, y - sz / 2, sz, sz);
      }
      fx.globalCompositeOperation = 'source-over'; fx.globalAlpha = 1;
    }
  }

  function resize(): void {
    DPR = Math.min(devicePixelRatio || 1, 2);
    // clientWidth (not innerWidth) so the fixed canvas never exceeds the content
    // area when a scrollbar is present — otherwise it forces horizontal overflow.
    W = document.documentElement.clientWidth;
    H = document.documentElement.clientHeight;
    // CSS keeps the elements at 100%×100%; we only set the backing buffer.
    for (const c of [glc, fxc]) { c.width = Math.round(W * DPR); c.height = Math.round(H * DPR); }
    gl.viewport(0, 0, glc.width, glc.height);
    fx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }
  resize();
  window.addEventListener('resize', resize);

  const t0 = performance.now();
  function render(now: number): void {
    // Pause the loop while the tab is hidden — no reason to burn cycles.
    if (!reduce && document.hidden) {
      window.setTimeout(() => requestAnimationFrame(render), 250);
      return;
    }
    const t = now - t0, p = prog();
    gl.useProgram(prgm);
    gl.uniform2f(uRes, glc.width, glc.height);
    gl.uniform1f(uTime, reduce ? 8000 : t / 1000);
    gl.uniform1f(uScroll, p);
    gl.uniform2f(uSun, cfg.sun[0], cfg.sun[1]);
    gl.uniform1f(uWarm, cfg.warmth);
    gl.uniform1f(uRay, cfg.ray);
    gl.uniform1f(uMode, cfg.mode);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    drawForeground(t);
    if (!reduce) requestAnimationFrame(render);
  }
  if (reduce) {
    // A few frames via rAF (so the canvas is laid out and the WebGL context is
    // ready), then stop — a single calm, fully-grown still frame. No loop.
    let n = 0;
    const settle = (): void => {
      render(t0 + 5000);
      if (++n < 4) requestAnimationFrame(settle);
    };
    requestAnimationFrame(settle);
  } else {
    requestAnimationFrame(render);
  }
}

const el = document.getElementById('wr-atmosphere');
if (el) initAtmosphere(el);
