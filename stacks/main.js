// The Stacks — sort-into-piles view of the game library.
// Every game is one jewel case: an InstancedMesh instance (one mesh per atlas
// sheet) whose front face samples the cover atlas and whose plastic is tinted
// by platform family. Sort modes retarget every case; positions tween there.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RGBELoader } from "./vendor/RGBELoader.js";
import { GLTFLoader } from "./vendor/GLTFLoader.js";

const CASE_W = 1.28, CASE_H = 1.92, CASE_D = 0.16;
const STACK_MAX = 40;          // cases per stack before a pile spills sideways
const SPACING_X = 1.75, SPACING_Z = 2.5;   // stack pitch inside a pile
const GROUP_GAP = 3.2;         // gap between piles
const TRANSITION = 1.0, STAGGER = 0.7;     // seconds
const POP_LIFT = 1.4;

// ---------- data ----------

const res = await fetch("./assets/library.json");
const data = await res.json();
const { meta } = data;
const games = data.games;
const FAMILY = meta.familyColors;
const FAMILY_LABEL = { nintendo: "Nintendo", sony: "PlayStation", xbox: "Xbox", pc: "PC" };

document.getElementById("loadmsg").textContent = "loading cover atlases & environment…";
const loader = new THREE.TextureLoader();
const loadmsg = document.getElementById("loadmsg");
const buf = async (url) => (await fetch(url)).arrayBuffer();
// fetch + parse instead of loadAsync: three's FileLoader streaming stalls in
// some headless environments, and plain fetch is equivalent here
const [atlases, hdrBuf, glbBuf] = await Promise.all([
  Promise.all(
    Array.from({ length: meta.sheets }, (_, i) =>
      loader.loadAsync(`./assets/atlas_${i}.jpg`)
    )
  ),
  buf("./assets_static/warehouse_2k.hdr"),
  buf("./assets_static/chair.glb"),
]);

loadmsg.textContent = "building environment…";
const hdrData = new RGBELoader().parse(hdrBuf);
const envTex = new THREE.DataTexture(
  hdrData.data, hdrData.width, hdrData.height, THREE.RGBAFormat, hdrData.type
);
envTex.flipY = true;
envTex.magFilter = THREE.LinearFilter;
envTex.minFilter = THREE.LinearFilter;
envTex.generateMipmaps = false;
envTex.needsUpdate = true;

loadmsg.textContent = "building scene…";
// createImageBitmap-based texture decode stalls in headless browsers; hide it
// during parse so GLTFLoader picks its <img>-element TextureLoader path
const _cib = window.createImageBitmap;
window.createImageBitmap = undefined;
let chairGltf;
try {
  chairGltf = await new GLTFLoader().parseAsync(glbBuf, "./assets_static/");
} finally {
  window.createImageBitmap = _cib;
}
for (const t of atlases) {
  t.minFilter = THREE.LinearMipmapLinearFilter;
  t.magFilter = THREE.LinearFilter;
  t.generateMipmaps = true;
  t.anisotropy = 4;
}

// ---------- scene ----------

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
envTex.mapping = THREE.EquirectangularReflectionMapping;
scene.background = envTex;
scene.backgroundIntensity = 0.7;       // keep the warehouse moody, covers pop
scene.backgroundBlurriness = 0.04;
scene.environment = envTex;            // IBL for the standard-material figure

const camera = new THREE.PerspectiveCamera(50, innerWidth / innerHeight, 0.1, 500);
camera.position.set(0, 26, 44);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI / 2 - 0.04;
controls.minDistance = 6;
controls.maxDistance = 160;
controls.target.set(0, 3, 0);

const floor = new THREE.Mesh(
  new THREE.CircleGeometry(70, 64),
  new THREE.MeshStandardMaterial({ color: 0x212328, roughness: 0.55, metalness: 0.25 })
);
floor.rotation.x = -Math.PI / 2;
floor.position.y = -0.02;
scene.add(floor);

// Scale object: an ordinary plastic monobloc chair (~85 cm). Model is
// meter-scale; scene units are 10 cm (a case is 1.9 units = 19 cm), hence ×10.
const figure = chairGltf.scene;
figure.scale.setScalar(10);
figure.rotation.y = Math.PI * 0.3;    // casual angle toward the piles
scene.add(figure);                    // positioned per-layout in applyMode

// In-scene text sprites: depth-tested (piles in front occlude them) and big.
function makeLabelSprite(title, sub, heightFrac = 0.13) {
  const pad = 30, titleSize = 76, subSize = 42, gap = 14;
  const cv = document.createElement("canvas");
  const ctx = cv.getContext("2d");
  const titleFont = `700 ${titleSize}px system-ui, sans-serif`;
  const subFont = `500 ${subSize}px system-ui, sans-serif`;
  ctx.font = titleFont;
  const wT = ctx.measureText(title).width;
  ctx.font = subFont;
  const wS = sub ? ctx.measureText(sub).width : 0;
  cv.width = Math.ceil(Math.max(wT, wS) + pad * 2);
  cv.height = Math.ceil(pad * 2 + titleSize + (sub ? gap + subSize : 0));
  ctx.fillStyle = "rgba(8, 10, 14, 0.82)";
  ctx.beginPath();
  ctx.roundRect(0, 0, cv.width, cv.height, 22);
  ctx.fill();
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.font = titleFont;
  ctx.fillStyle = "#ffffff";
  ctx.fillText(title, cv.width / 2, pad);
  if (sub) {
    ctx.font = subFont;
    ctx.fillStyle = "#b9c1cd";
    ctx.fillText(sub, cv.width / 2, pad + titleSize + gap);
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const spr = new THREE.Sprite(
    // constant screen size: readable from afar without towering over the
    // piles up close; still depth-tested, so piles in front occlude it
    new THREE.SpriteMaterial({ map: tex, transparent: true, sizeAttenuation: false })
  );
  spr.center.set(0.5, 0);                   // anchor at the plate's bottom edge
  const h = heightFrac;                     // fraction of viewport height-ish
  spr.scale.set(h * cv.width / cv.height, h, 1);
  return spr;
}

const figureLabel = makeLabelSprite("an ordinary chair", "for scale", 0.07);
scene.add(figureLabel);                     // positioned per-layout in applyMode

// ---------- instanced jewel cases ----------

const caseGeo = new THREE.BoxGeometry(CASE_W, CASE_H, CASE_D);

const vert = /* glsl */ `
  attribute vec2 aUv;
  attribute vec3 aColor;
  attribute float aGlow;
  attribute float aDust;
  uniform vec2 uTileScale;
  varying vec2 vUv;
  varying vec2 vUvEdge;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vFront;
  varying float vGlow;
  varying float vDust;
  void main() {
    vDust = aDust;
    vUv = aUv + uv * uTileScale;
    // Edge faces sample a stripe through the middle of the cover, like a
    // printed spine: the long sides read the art vertically, top/bottom
    // read it horizontally, the back reuses the full art.
    if (abs(normal.x) > 0.5)      vUvEdge = aUv + vec2(0.5, uv.y) * uTileScale;
    else if (abs(normal.y) > 0.5) vUvEdge = aUv + vec2(uv.x, 0.5) * uTileScale;
    else                          vUvEdge = vUv;
    vColor = aColor;
    vGlow = aGlow;
    vFront = step(0.9, normal.z);                    // +z face carries the cover
    vNormal = normalize(mat3(instanceMatrix) * normal);
    // Hover pop is visual-only, applied here so the raycast target never
    // moves out from under the cursor (which caused hover flicker). It eases
    // toward the camera, so buried cases slide out of whichever side you see.
    vec3 popDir = normalize(cameraPosition - (instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz);
    vec4 wp = instanceMatrix * vec4(position * (1.0 + 0.05 * aGlow), 1.0);
    wp.xyz += popDir * (aGlow * ${POP_LIFT.toFixed(2)});
    gl_Position = projectionMatrix * modelViewMatrix * wp;
  }
`;
const frag = /* glsl */ `
  uniform sampler2D uAtlas;
  uniform float uDust;
  varying vec2 vUv;
  varying vec2 vUvEdge;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vFront;
  varying float vGlow;
  varying float vDust;
  void main() {
    vec3 art = texture2D(uAtlas, vUv).rgb;
    vec3 spine = mix(texture2D(uAtlas, vUvEdge).rgb, vColor, 0.25) * 0.9;
    vec3 base = mix(spine, art, vFront);
    // Dust film on never-touched games: cheap desaturate + grey lift, gated
    // by the global toggle so the attribute never has to be rewritten.
    base = mix(base, vec3(dot(base, vec3(0.299, 0.587, 0.114))) * 0.75 + 0.12,
               vDust * uDust * 0.55);
    vec3 n = normalize(vNormal);
    float d1 = max(dot(n, normalize(vec3(0.35, 0.9, 0.45))), 0.0);
    float d2 = max(dot(n, normalize(vec3(-0.6, 0.25, -0.5))), 0.0);
    vec3 c = base * (0.52 + 0.52 * d1 + 0.18 * d2);
    c += vGlow * vec3(0.30, 0.27, 0.18);
    gl_FragColor = vec4(c, 1.0);
  }
`;

// One InstancedMesh per atlas sheet; record where each game lives.
const meshes = [];
const bySheet = new Map();
games.forEach((g, gi) => {
  const s = Math.floor(g.tile / meta.tilesPerSheet);
  if (!bySheet.has(s)) bySheet.set(s, []);
  bySheet.get(s).push(gi);
});

const tileScale = [meta.tile[0] / meta.atlasSize, meta.tile[1] / meta.atlasSize];
for (const [sheet, indices] of [...bySheet.entries()].sort((a, b) => a[0] - b[0])) {
  const n = indices.length;
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uAtlas: { value: atlases[sheet] },
      uTileScale: { value: new THREE.Vector2(...tileScale) },
      uDust: { value: 0 },
    },
    vertexShader: vert,
    fragmentShader: frag,
  });
  const mesh = new THREE.InstancedMesh(caseGeo, mat, n);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false;

  const uvArr = new Float32Array(n * 2);
  const colArr = new Float32Array(n * 3);
  const glowArr = new Float32Array(n);
  const dustArr = new Float32Array(n);
  const col = new THREE.Color();
  indices.forEach((gi, i) => {
    const g = games[gi];
    // dusty = never touched: zero minutes AND no human-set completion status
    // (an unplayed game marked completed elsewhere shouldn't be dusty)
    dustArr[i] = g.minutes === 0 && !g.status ? 1 : 0;
    const local = g.tile % meta.tilesPerSheet;
    const cx = local % meta.cols, cy = Math.floor(local / meta.cols);
    uvArr[i * 2] = cx * tileScale[0];
    uvArr[i * 2 + 1] = 1 - (cy + 1) * tileScale[1];   // flipY texture: v from bottom
    col.set(FAMILY[g.family]);
    colArr[i * 3] = col.r; colArr[i * 3 + 1] = col.g; colArr[i * 3 + 2] = col.b;
    g._mesh = mesh;
    g._i = i;
  });
  mesh.userData.games = indices.map((gi) => games[gi]);
  mesh.geometry = caseGeo.clone();
  mesh.geometry.setAttribute("aUv", new THREE.InstancedBufferAttribute(uvArr, 2));
  mesh.geometry.setAttribute("aColor", new THREE.InstancedBufferAttribute(colArr, 3));
  mesh.geometry.setAttribute("aGlow", new THREE.InstancedBufferAttribute(glowArr, 1));
  mesh.geometry.setAttribute("aDust", new THREE.InstancedBufferAttribute(dustArr, 1));
  scene.add(mesh);
  meshes.push(mesh);
}

// ---------- per-case motion state ----------

// Deterministic per-game jitter so piles look hand-stacked but stable.
function jitter(id, salt) {
  let h = (id * 2654435761 + salt * 40503) >>> 0;
  h = ((h ^ (h >> 15)) * 2246822519) >>> 0;
  return ((h & 0xffff) / 0xffff) * 2 - 1;   // [-1, 1]
}

const FLAT_TILT = -Math.PI / 2;   // lying flat, cover up; 0 = standing upright
const _q = new THREE.Quaternion();
const _e = new THREE.Euler();
const _m = new THREE.Matrix4();
const _s = new THREE.Vector3(1, 1, 1);
const _v = new THREE.Vector3();

for (const g of games) {
  // scatter in the sky for the intro drop
  g.cur = new THREE.Vector3(jitter(g.id, 1) * 60, 45 + jitter(g.id, 2) * 25, jitter(g.id, 3) * 60);
  g.from = g.cur.clone();
  g.to = g.cur.clone();
  g.curYaw = g.fromYaw = g.toYaw = jitter(g.id, 4) * Math.PI;
  g.curTilt = g.fromTilt = g.toTilt = FLAT_TILT;
  g.t = 1; g.delay = 0;
  g.pop = 0; g.popT = 0;
}

function composeMatrix(g) {
  _e.set(g.curTilt, g.curYaw, 0, "YXZ");   // yaw about world Y, then tilt
  _q.setFromEuler(_e);
  _m.compose(g.cur, _q, _s);
  g._mesh.setMatrixAt(g._i, _m);
}

// ---------- pile layouts ----------

const hours = (m) => m / 60;
const fmtH = (m) => {
  const h = Math.round(hours(m));
  return h >= 1000 ? (h / 1000).toFixed(1).replace(/\.0$/, "") + "k h" : h + " h";
};

// A multi-platform game's case sits in the family it's played most on, but
// platform-pile hour labels report time on that family across the whole library.
const famOf = (p) =>
  p.startsWith("switch") ? "nintendo"
  : p.startsWith("ps") || p === "psn" ? "sony"
  : p.startsWith("xbox") ? "xbox" : "pc";
const familyMinutes = {};
for (const g of games)
  for (const [p, m] of Object.entries(g.platforms))
    familyMinutes[famOf(p)] = (familyMinutes[famOf(p)] ?? 0) + m;

const MODES = {
  platform: {
    label: "Platform",
    buckets: (g) => g.family,
    order: ["nintendo", "sony", "xbox", "pc"],
    title: (k) => FAMILY_LABEL[k],
    sort: (a, b) => b.minutes - a.minutes,
    sub: (gs) => fmtH(familyMinutes[gs[0].family] ?? 0) + " played",
  },
  critic: {
    label: "Critic score",
    buckets: (g) => g.critic == null ? "none" : g.critic >= 90 ? "90" : g.critic >= 80 ? "80" : g.critic >= 70 ? "70" : g.critic >= 60 ? "60" : "low",
    order: ["90", "80", "70", "60", "low", "none"],
    title: (k) => ({ 90: "90+", 80: "80–89", 70: "70–79", 60: "60–69", low: "< 60", none: "Unscored" }[k]),
    sort: (a, b) => (b.critic ?? -1) - (a.critic ?? -1),
    sub: () => "",
  },
  mine: {
    label: "My ratings",
    buckets: (g) => g.user == null ? "none" : g.user >= 8.5 ? "loved" : g.user >= 6.5 ? "liked" : "meh",
    order: ["loved", "liked", "meh", "none"],
    title: (k) => ({ loved: "Loved (8.5+)", liked: "Liked (6.5–8.4)", meh: "Meh (< 6.5)", none: "Unrated" }[k]),
    sort: (a, b) => (b.user ?? -1) - (a.user ?? -1),
    sub: () => "",
  },
  playtime: {
    label: "Playtime",
    buckets: (g) => {
      const h = hours(g.minutes);
      return h === 0 ? "zero" : h < 2 ? "t" : h < 10 ? "s" : h < 50 ? "m" : "l";
    },
    order: ["l", "m", "s", "t", "zero"],
    title: (k) => ({ l: "50+ h", m: "10–50 h", s: "2–10 h", t: "< 2 h", zero: "Never played" }[k]),
    sort: (a, b) => b.minutes - a.minutes,
    sub: (gs) => { const t = gs.reduce((s, g) => s + g.minutes, 0); return t ? fmtH(t) : ""; },
  },
  era: {
    label: "Release era",
    buckets: (g) => g.year == null ? "none" : g.year < 2000 ? "90s" : g.year < 2010 ? "00s" : g.year < 2020 ? "10s" : "20s",
    order: ["90s", "00s", "10s", "20s", "none"],
    title: (k) => ({ "90s": "Pre-2000", "00s": "2000s", "10s": "2010s", "20s": "2020s", none: "Unknown" }[k]),
    sort: (a, b) => (b.year ?? 0) - (a.year ?? 0),
    sub: (gs) => gs.length ? "" : "",
  },
};

const labelSprites = [];
function clearLabels() {
  for (const spr of labelSprites) {
    scene.remove(spr);
    spr.material.map.dispose();
    spr.material.dispose();
  }
  labelSprites.length = 0;
}

function applyMode(modeKey) {
  exploded = null;   // mode switch recomputes every target anyway
  const mode = MODES[modeKey];
  const groups = new Map(mode.order.map((k) => [k, []]));
  for (const g of games) {
    const k = mode.buckets(g);
    (groups.get(k) ?? groups.set(k, []).get(k)).push(g);
  }

  // measure pile footprints, then center the row of piles on x
  const piles = [];
  for (const k of mode.order) {
    const gs = groups.get(k);
    if (!gs || gs.length === 0) continue;
    // ties: real covers above placeholder tiles, then alphabetical
    gs.sort((a, b) =>
      mode.sort(a, b) || (a.ph ? 1 : 0) - (b.ph ? 1 : 0) || a.name.localeCompare(b.name));
    const nStacks = Math.ceil(gs.length / STACK_MAX);
    const cols = Math.ceil(Math.sqrt(nStacks));
    const rows = Math.ceil(nStacks / cols);
    piles.push({ k, gs, nStacks, cols, rows, width: cols * SPACING_X });
  }
  const totalW = piles.reduce((s, p) => s + p.width, 0) + GROUP_GAP * (piles.length - 1);

  clearLabels();
  let x0 = -totalW / 2;
  figure.position.set(x0 - 6, 0, 0);   // sits at the head of the row
  figureLabel.position.set(x0 - 6, 9.5, 0);
  for (const p of piles) {
    const { gs, cols } = p;
    let tallest = 0;
    // deal round-robin across stacks: every stack top is a top-sorted game,
    // and placeholder tiles (sorted last) sink toward stack bottoms
    const stacks = Array.from({ length: p.nStacks }, () => []);
    gs.forEach((g, i) => stacks[i % p.nStacks].push(g));   // [0] = top of stack
    for (const st of stacks) {
      // a stack that got dealt only placeholders would still show one on
      // top — swap the first real cover up if there is one
      if (st[0]?.ph) {
        const j = st.findIndex((g) => !g.ph);
        if (j > 0) [st[0], st[j]] = [st[j], st[0]];
      }
    }
    stacks.forEach((st, s) => {
      const col = s % cols, row = Math.floor(s / cols);
      tallest = Math.max(tallest, st.length * CASE_D);
      st.forEach((g, level) => {
        const i = level * p.nStacks + s;                   // pile-wide order
        g._stack = st;
        g.from.copy(g.cur);
        g.fromYaw = g.curYaw;
        g.fromTilt = g.curTilt;
        g.to.set(
          x0 + col * SPACING_X + SPACING_X / 2 + jitter(g.id, 7) * 0.10,
          (st.length - 1 - level) * CASE_D + CASE_D / 2,
          (row - (p.rows - 1) / 2) * SPACING_Z + jitter(g.id, 8) * 0.10
        );
        g.toYaw = jitter(g.id, 9) * 0.09;
        g.toTilt = FLAT_TILT;
        g.t = 0;
        g.delay = (i / gs.length) * STAGGER * (0.6 + 0.4 * Math.abs(jitter(g.id, 5)));
      });
    });

    const sub = mode.sub(gs);
    const spr = makeLabelSprite(
      mode.title(p.k),
      `${gs.length} game${gs.length === 1 ? "" : "s"}${sub ? " · " + sub : ""}`
    );
    spr.position.set(x0 + p.width / 2, tallest + 1.2, 0);
    spr.userData.prio = gs.length;      // bigger piles win label collisions
    scene.add(spr);
    labelSprites.push(spr);
    x0 += p.width + GROUP_GAP;
  }

  if (SNAP) snapAll();
  else {
    transitionClock = 0;
    animating = true;
  }

  // dust reads best against Playtime's played/unplayed piles; elsewhere it
  // defaults off — until the user clicks the toggle, whose choice then sticks
  setDust(dustUserChoice ?? (modeKey === "playtime" ? 1 : 0));

  for (const b of modeBtns) b.classList.toggle("active", b.dataset.mode === modeKey);
}

// ---------- UI ----------

const modesEl = document.getElementById("modes");
const modeBtns = [];
for (const [key, m] of Object.entries(MODES)) {
  const b = document.createElement("button");
  b.textContent = m.label;
  b.dataset.mode = key;
  b.onclick = () => applyMode(key);
  modesEl.appendChild(b);
  modeBtns.push(b);
}

// dust toggle
let dustEnabled = false;
let dustUserChoice = null;   // null = follow the mode default
function setDust(v) {
  dustEnabled = !!v;
  for (const m of meshes) m.material.uniforms.uDust.value = dustEnabled ? 1 : 0;
  dustBtn.classList.toggle("active", dustEnabled);
}
const dustBtn = document.createElement("button");
dustBtn.textContent = "dust";
dustBtn.title = "grey film on never-played games";
dustBtn.onclick = () => {
  dustUserChoice = !dustEnabled;
  setDust(dustUserChoice);
};
document.getElementById("opts").appendChild(dustBtn);

const totalMin = games.reduce((s, g) => s + g.minutes, 0);
// Backlog measured in years: HLTB story hours over unplayed games, with the
// same exclusions as get_backlog_stats (completed/abandoned/evergreen out).
const BACKLOG_EXCLUDE = new Set(["completed", "abandoned", "evergreen"]);
const backlogGames = games.filter(
  (g) => g.minutes === 0 && !BACKLOG_EXCLUDE.has(g.status)
);
const backlogH = backlogGames.reduce((s, g) => s + (g.hltb ?? 0), 0);
const hltbCovered = backlogGames.filter((g) => g.hltb != null).length;
const backlogYears = backlogH / (2 * 365);   // at 2 h/day
const statsEl = document.getElementById("stats");
statsEl.textContent =
  `${games.length.toLocaleString()} games · ${Math.round(hours(totalMin)).toLocaleString()} hours on record` +
  (backlogH > 0
    ? ` · backlog ~${Math.round(backlogH).toLocaleString()} h ≈ ${backlogYears.toFixed(1)} years at 2 h/day`
    : "");
statsEl.title =
  `backlog estimated from ${hltbCovered.toLocaleString()} of ` +
  `${backlogGames.length.toLocaleString()} unplayed games with story lengths ` +
  `(games without HLTB data count as 0; completed/abandoned/evergreen excluded)`;

// ---------- explode a stack ----------

let exploded = null;   // { games: [...], home: Map<game, {pos, yaw, tilt}> }

function explodeStack(stack) {
  collapseStack();
  const center = new THREE.Vector3();
  for (const g of stack) center.add(g.cur);
  center.divideScalar(stack.length).setY(0);

  // wall of upright cases facing the camera, pulled toward it a little so
  // it clears the neighboring stacks
  const az = Math.atan2(camera.position.x - center.x, camera.position.z - center.z);
  const right = new THREE.Vector3(Math.cos(az), 0, -Math.sin(az));
  const out = new THREE.Vector3(Math.sin(az), 0, Math.cos(az));
  const n = stack.length;
  const cols = Math.min(Math.ceil(Math.sqrt(n * 2.2)), 10);
  const rows = Math.ceil(n / cols);
  const sx = CASE_W + 0.4, sy = CASE_H + 0.45;

  exploded = { games: stack, home: new Map() };
  stack.forEach((g, i) => {
    exploded.home.set(g, { pos: g.to.clone(), yaw: g.toYaw, tilt: g.toTilt });
    const cx = (i % cols) - (cols - 1) / 2;
    const cy = rows - 1 - Math.floor(i / cols);   // best game top-left
    g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
    g.to.copy(center).addScaledVector(right, cx * sx).addScaledVector(out, 10);
    g.to.y = 2.2 + cy * sy + CASE_H / 2;
    g.toYaw = az;
    g.toTilt = 0;
    g.t = 0;
    g.delay = i * 0.012;
  });
  transitionClock = 0;
  animating = true;
}

function collapseStack() {
  if (!exploded) return;
  exploded.games.forEach((g, i) => {
    const h = exploded.home.get(g);
    g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
    g.to.copy(h.pos); g.toYaw = h.yaw; g.toTilt = h.tilt;
    g.t = 0;
    g.delay = i * 0.008;
  });
  exploded = null;
  transitionClock = 0;
  animating = true;
}

// click = press+release without dragging (leaves OrbitControls alone)
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => {
  downAt = { x: e.clientX, y: e.clientY };
});
addEventListener("pointerup", (e) => {
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
  downAt = null;
  if (moved > 6 || animating) return;
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  raycaster.setFromCamera(mouse, camera);
  const hit = raycaster.intersectObjects(meshes, false)[0];
  const g = hit?.instanceId != null ? hit.object.userData.games[hit.instanceId] : null;
  if (!g) collapseStack();                                  // clicked empty space
  else if (exploded?.games.includes(g)) return;             // inside the wall
  else if (g._stack) explodeStack(g._stack);
});

// ---------- hover ----------

const card = document.getElementById("card");
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2(-2, -2);
let mousePx = { x: 0, y: 0 };
let hovered = null;

addEventListener("pointermove", (e) => {
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  mousePx = { x: e.clientX, y: e.clientY };
});

function setGlow(g, v) {
  const attr = g._mesh.geometry.getAttribute("aGlow");
  attr.setX(g._i, v);
  attr.needsUpdate = true;
}

function showCard(g) {
  document.getElementById("card-img").src = g.cover ?? "";
  document.getElementById("card-img").style.display = g.cover ? "" : "none";
  document.getElementById("card-name").textContent = g.name;
  document.getElementById("card-meta").textContent =
    [g.year, FAMILY_LABEL[g.family]].filter(Boolean).join(" · ");
  const rows = [];
  for (const [p, m] of Object.entries(g.platforms)) {
    rows.push(`<div class="c-row"><span class="dot" style="background:${FAMILY[g.family]}"></span>` +
      `${p}${m ? ` — <b>${fmtH(m)}</b>` : ""}</div>`);
  }
  if (g.critic != null) rows.push(`<div class="c-row">critics <b>${g.critic}</b></div>`);
  if (g.user != null) rows.push(`<div class="c-row">my rating <b>${g.user}</b>/10</div>`);
  if (g.hltb != null) rows.push(`<div class="c-row">story <b>${Math.round(g.hltb)} h</b></div>`);
  if (g.farmed) rows.push(`<div class="c-row">🚜 farmed — playtime inflated</div>`);
  if (g.status) rows.push(`<div class="c-row">status <b>${g.status}</b></div>`);
  document.getElementById("card-rows").innerHTML = rows.join("");
  card.classList.add("show");
}

function placeCard() {
  const pad = 18;
  let x = mousePx.x + pad, y = mousePx.y + pad;
  if (x + 350 > innerWidth) x = mousePx.x - 350 - pad;
  if (y + 190 > innerHeight) y = mousePx.y - 190 - pad;
  card.style.left = x + "px";
  card.style.top = y + "px";
}

// ---------- loop ----------

const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
let transitionClock = 0;
let animating = false;

function snapAll() {
  for (const g of games) {
    g.cur.copy(g.to);
    g.curYaw = g.toYaw;
    g.curTilt = g.toTilt;
    g.t = 1;
    composeMatrix(g);
  }
  for (const m of meshes) {
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  }
  animating = false;
}
const clock = new THREE.Clock();

function tick() {
  const dt = Math.min(clock.getDelta(), 0.05);
  controls.update();

  if (animating) {
    transitionClock += dt;
    let busy = false;
    for (const g of games) {
      if (g.t >= 1) continue;
      const local = (transitionClock - g.delay) / TRANSITION;
      g.t = Math.max(0, Math.min(1, local));
      const k = ease(g.t);
      g.cur.lerpVectors(g.from, g.to, k);
      g.curYaw = g.fromYaw + (g.toYaw - g.fromYaw) * k;
      g.curTilt = g.fromTilt + (g.toTilt - g.fromTilt) * k;
      composeMatrix(g);
      if (g.t < 1) busy = true;
    }
    for (const m of meshes) m.instanceMatrix.needsUpdate = true;
    if (!busy) {
      animating = false;
      for (const m of meshes) m.computeBoundingSphere();
    }
  }

  // hover raycast (skip during big transitions)
  if (!animating) {
    raycaster.setFromCamera(mouse, camera);
    const hit = raycaster.intersectObjects(meshes, false)[0];
    let g = null;
    if (hit && hit.instanceId != null) {
      g = hit.object.userData.games[hit.instanceId] ?? null;
    }
    if (g !== hovered) {
      if (hovered) hovered.popT = 0;
      hovered = g;
      if (hovered) { hovered.popT = 1; showCard(hovered); }
      else card.classList.remove("show");
    }
  } else if (hovered) {
    hovered.popT = 0; hovered = null;
    card.classList.remove("show");
  }
  if (hovered) placeCard();

  // pop animation (hovered case rises, released cases settle) — drives the
  // aGlow attribute only; instance matrices stay put for stable raycasting
  for (const g of games) {
    const target = g.popT ?? 0;
    if (g.pop !== target) {
      const dir = Math.sign(target - g.pop);
      g.pop = Math.max(0, Math.min(1, g.pop + dir * dt * 7));
      setGlow(g, g.pop);
    }
  }

  declutterLabels();
  renderer.render(scene, camera);
}

// Map-style label decluttering: when plates would overlap on screen, only the
// highest-priority one stays; the rest reappear as the camera separates them.
function declutterLabels() {
  // px per sprite-scale unit for sizeAttenuation:false sprites
  const F = innerHeight / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
  const pad = 6;               // don't let plates touch edge-to-edge
  const placed = [];
  const items = [...labelSprites, figureLabel]
    .map((spr) => ({ spr, prio: spr.userData.prio ?? -1 }))
    .sort((a, b) => b.prio - a.prio);
  for (const { spr } of items) {
    _v.copy(spr.position).project(camera);
    if (_v.z >= 1) { spr.visible = false; continue; }   // behind the camera
    const w = spr.scale.x * F, h = spr.scale.y * F;
    const cx = (_v.x * 0.5 + 0.5) * innerWidth;
    const by = (-_v.y * 0.5 + 0.5) * innerHeight;       // plate bottom edge
    const r = { x0: cx - w / 2 - pad, x1: cx + w / 2 + pad, y0: by - h - pad, y1: by + pad };
    const hit = placed.some(
      (o) => o.x0 < r.x1 && r.x0 < o.x1 && o.y0 < r.y1 && r.y0 < o.y1
    );
    spr.visible = !hit;
    if (!hit) placed.push(r);
  }
}

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

// initial placement (sky scatter), then drop into platform piles
for (const g of games) composeMatrix(g);
for (const m of meshes) m.instanceMatrix.needsUpdate = true;
renderer.setAnimationLoop(tick);
document.getElementById("loading").classList.add("done");
const params = new URLSearchParams(location.search);
const SNAP = params.get("snap") === "1";   // skip transitions (also: screenshots)
const startMode = params.get("mode");
if (params.has("dust")) dustUserChoice = params.get("dust") === "1";
applyMode(MODES[startMode] ? startMode : "platform");
if (SNAP && params.get("explode")) {   // deterministic explode, for screenshots
  const g = games.find((x) => x._stack && x._stack.length >= 20);
  if (g) { explodeStack(g._stack); snapAll(); }
}
