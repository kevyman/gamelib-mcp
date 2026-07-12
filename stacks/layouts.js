// Sort modes and their layouts: the default pile system plus the custom
// Hours field and Monolith tower. applyMode is the single entry point; UI
// modules subscribe to applyHooks instead of being imported here.

import * as THREE from "three";
import { games, meta, FAMILY_LABEL } from "./data.js";
import {
  scene, camera, controls, figure, figureLabel,
  makeLabelSprite, labelSprites, clearLabels, clearModeObjects, modeObjects,
} from "./scene.js";
import { snapAll, setGalaxyShading } from "./cases.js";
import { S } from "./state.js";
import { flythrough, endFlythrough } from "./flythrough.js";
import {
  CASE_W, CASE_H, CASE_D, STACK_MAX, SPACING_X, SPACING_Z, GROUP_GAP,
  STAGGER, FLAT_TILT, M, jitter, hours, fmtH, famOf,
} from "./util.js";

const familyMinutes = {};
for (const g of games)
  for (const [p, m] of Object.entries(g.platforms))
    familyMinutes[famOf(p)] = (familyMinutes[famOf(p)] ?? 0) + m;

export const MODES = {
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
  hours: {
    label: "Hours",
    layout: () => layoutHours(),   // custom layout: size = playtime, not piles
  },
  monolith: {
    label: "Monolith",
    layout: () => layoutMonolith(),   // one tower + cinematic flythrough
  },
  galaxy: {
    label: "Galaxy",
    layout: () => layoutGalaxy(),     // taste-space fly-through (export-side embedding)
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

// the galaxy needs export-side embedding data; older library.json lacks it
if (!games.some((g) => g.pos)) delete MODES.galaxy;

// UI modules register callbacks here; called with the new mode key after a
// layout is applied (keeps this module free of DOM knowledge).
export const applyHooks = [];
// Run before the new layout reads g.cur — physics modes register a teardown
// here so any mode switch hands transforms back to the tween system first.
export const preApplyHooks = [];

export function applyMode(modeKey) {
  for (const h of preApplyHooks) h(modeKey);
  S.exploded = null;   // mode switch recomputes every target anyway
  const prevModeKey = S.currentModeKey;
  S.currentModeKey = modeKey;
  const mode = MODES[modeKey];
  clearLabels();
  clearModeObjects();
  if (prevModeKey === "monolith") endFlythrough(false);
  controls.maxDistance = 160;   // monolith/galaxy layouts raise it as needed
  setGalaxyShading(modeKey === "galaxy");
  // lights out for the galaxy: the dim warehouse turns the additive nebulae,
  // constellation lines and affinity glow into the brightest things on screen
  scene.backgroundIntensity = modeKey === "galaxy" ? 0.08 : 0.7;

  if (mode.layout) mode.layout();
  else layoutPiles(mode);

  if (S.SNAP) snapAll();
  else {
    S.transitionClock = 0;
    S.animating = true;
  }

  if (modeKey !== "monolith") S.monolithFlown = false;   // re-entry re-flies

  for (const h of applyHooks) h(modeKey);
}

function layoutPiles(mode) {
  const groups = groupGames(mode);

  // measure pile footprints, then center the row of piles on x
  const piles = [];
  for (const [k, gs] of groups) {
    const nStacks = Math.ceil(gs.length / STACK_MAX);
    const cols = Math.ceil(Math.sqrt(nStacks));
    const rows = Math.ceil(nStacks / cols);
    piles.push({ k, gs, nStacks, cols, rows, width: cols * SPACING_X });
  }
  const totalW = piles.reduce((s, p) => s + p.width, 0) + GROUP_GAP * (piles.length - 1);

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
        g.fromScale = g.curScale;
        g.toScale = 1;
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
}

// Bucket + sort a mode's games. Exported so the walk mode can shelve the
// library in the same sections the active sort mode uses.
export function groupGames(mode) {
  const groups = new Map(mode.order.map((k) => [k, []]));
  for (const g of games) {
    const k = mode.buckets(g);
    (groups.get(k) ?? groups.set(k, []).get(k)).push(g);
  }
  const out = [];
  for (const k of mode.order) {
    const gs = groups.get(k);
    if (!gs || gs.length === 0) continue;
    // ties: real covers above placeholder tiles, then alphabetical
    gs.sort((a, b) =>
      mode.sort(a, b) || (a.ph ? 1 : 0) - (b.ph ? 1 : 0) || a.name.localeCompare(b.name));
    out.push([k, gs]);
  }
  return out;
}

// Hours as mass: every case upright in one wide field, sorted by playtime,
// physically scaled by log-hours — the visceral version of get_play_history.
const HOURS_K = 6.5;         // gain into the log curve (tuned by eye)
const HOURS_MIN_S = 0.45;    // floor: unplayed carpet of slivers
const HOURS_MAX_S = 4.5;     // cap: even 1,000+ h can't leave the field

function layoutHours() {
  const effMin = (g) => (g.farmed && !S.includeFarmed ? 0 : g.minutes);
  const sorted = [...games].sort(
    (a, b) => effMin(b) - effMin(a)
      || (a.ph ? 1 : 0) - (b.ph ? 1 : 0)
      || a.name.localeCompare(b.name)
  );
  const maxH = Math.max(1, hours(effMin(sorted[0])));
  const scaleOf = (g) => {
    const h = hours(effMin(g));
    if (h <= 0) return HOURS_MIN_S;
    // log scale is non-negotiable: linear would turn farmed titles into towers
    const s = HOURS_MIN_S + 0.55 * (Math.log2(1 + h) / Math.log2(1 + maxH)) * HOURS_K;
    return Math.min(Math.max(s, HOURS_MIN_S), HOURS_MAX_S);
  };

  // variable-width row packing: walk the sorted list, wrap when the row is
  // full; each row steps back far enough to clear the tallest case before it.
  // Row width adapts to the library so the field stays square-ish.
  const GAP = 0.34, ROW_GAP = 1.1;
  const totalRowLen = sorted.reduce((s, g) => s + CASE_W * scaleOf(g) + GAP, 0);
  const TARGET_W = Math.max(30, Math.sqrt(totalRowLen * (ROW_GAP + 1.1)));
  const BANDS = [
    [500, "500+ h"], [100, "100–500 h"], [10, "10–100 h"],
    [2, "2–10 h"], [1e-9, "< 2 h"], [-1, "never played"],
  ];
  let x = -TARGET_W / 2, z = 0, rowTallest = 0, band = 0;
  const bandCounts = BANDS.map(() => 0);
  sorted.forEach((g, i) => {
    const s = scaleOf(g);
    const w = CASE_W * s;
    if (x + w > TARGET_W / 2 && x > -TARGET_W / 2) {
      x = -TARGET_W / 2;
      z += CASE_H * rowTallest * 0.12 + CASE_D * rowTallest + ROW_GAP;
      rowTallest = 0;
    }
    rowTallest = Math.max(rowTallest, s);
    g._stack = null;                     // no piles to explode in this mode
    g.from.copy(g.cur);
    g.fromYaw = g.curYaw;
    g.fromTilt = g.curTilt;
    g.fromScale = g.curScale;
    g.to.set(x + w / 2, (CASE_H * s) / 2, z + jitter(g.id, 8) * 0.06);
    g.toYaw = jitter(g.id, 9) * 0.06;
    g.toTilt = 0;                        // standing upright, cover forward
    g.toScale = s;
    g.t = 0;
    g.delay = (i / sorted.length) * STAGGER * (0.6 + 0.4 * Math.abs(jitter(g.id, 5)));
    x += w + GAP;

    const h = hours(effMin(g));
    while (band < BANDS.length && h < BANDS[band][0]) band++;
    const bi = Math.min(band, BANDS.length - 1);
    bandCounts[bi]++;
    if (bandCounts[bi] === 1) {
      const spr = makeLabelSprite(BANDS[bi][1], "", 0.075);
      spr.position.set(g.to.x, CASE_H * s + 1.4, g.to.z);
      spr.userData._band = bi;
      scene.add(spr);
      labelSprites.push(spr);
    }
  });
  for (const spr of labelSprites) {
    if (spr.userData._band != null)
      spr.userData.prio = bandCounts[spr.userData._band];
  }

  // center the field on z (giants end up at the far edge, carpet up front —
  // nothing occludes anything from the default camera)
  const depth = z + CASE_D * rowTallest;
  for (const g of sorted) g.to.z -= depth / 2;
  for (const spr of labelSprites) spr.position.z -= depth / 2;

  figure.position.set(-TARGET_W / 2 - 6, 0, -depth / 2);
  figureLabel.position.set(-TARGET_W / 2 - 6, 9.5, -depth / 2);
}

// The Monolith: every case in one physical stack, camera pulling back past
// real-world reference silhouettes until the whole tower fits in frame.
// Scene units are 10 cm, so meters × 10.
const LANDMARKS = [
  // the chair (0.85 m) is already in the scene at the base — not listed here
  ["human", 1.75],
  ["semi truck", 4.1],
  ["giraffe", 5.5],
  ["3-story house", 10],
  ["blue whale", 25],
  ["Statue of Liberty (statue)", 46],
  ["Leaning Tower of Pisa", 57],
  ["Eiffel Tower", 330],
];

function buildTower(gs, x) {
  // most-played on top: the tower's crown is the games that earned it
  gs.sort((a, b) => b.minutes - a.minutes || a.name.localeCompare(b.name));
  gs.forEach((g, level) => {
    g._stack = null;
    g.from.copy(g.cur);
    g.fromYaw = g.curYaw;
    g.fromTilt = g.curTilt;
    g.fromScale = g.curScale;
    g.to.set(
      x + jitter(g.id, 7) * 0.05,
      (gs.length - 1 - level) * CASE_D + CASE_D / 2,
      jitter(g.id, 8) * 0.05
    );
    g.toYaw = jitter(g.id, 9) * 0.07;
    g.toTilt = FLAT_TILT;
    g.toScale = 1;
    g.t = 0;
    g.delay = ((gs.length - level) / gs.length) * STAGGER * 2;
  });
  return gs.length * CASE_D;
}

function addHeightMarker(y, title, sub, gated) {
  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(3.4, 0.05, 6, 72),
    new THREE.MeshBasicMaterial({ color: 0xe6c86e, transparent: true, opacity: 0.75 })
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = y;
  ring.userData.gated = gated;
  ring.visible = !gated;
  scene.add(ring);
  modeObjects.push(ring);

  const spr = makeLabelSprite(title, sub, 0.05);
  spr.position.set(4.2, y, 0);
  spr.userData.prio = 5;
  spr.userData.gated = gated;
  scene.add(spr);
  labelSprites.push(spr);
  return [ring, spr];
}

function layoutMonolith() {
  const played = games.filter((g) => g.minutes > 0);
  const unplayed = games.filter((g) => g.minutes === 0);
  let height;
  if (S.splitMonolith) {
    const h1 = buildTower(played, -3.5);
    const h2 = buildTower(unplayed, 3.5);
    height = Math.max(h1, h2);
  } else {
    height = buildTower([...games], 0);
  }
  const meters = height / M;

  figure.position.set(-8, 0, 2);
  figureLabel.position.set(-8, 9.5, 2);

  // markers appear as the flythrough climbs past them (all visible in snap)
  const gated = !S.SNAP && !S.monolithFlown;
  for (const [name, m] of LANDMARKS) {
    if (m * M <= height * 1.15) addHeightMarker(m * M, name, `${m} m`, gated);
  }

  // closing stat plate: the multiplication spelled out, plus scale conversions
  const eiffel = ((meters / 330) * 100).toFixed(1);
  const nearMiss = LANDMARKS.find(([, m]) => m * M > height * 1.15);
  const stat = makeLabelSprite(
    `${meters.toFixed(1)} m tall`,
    `${games.length.toLocaleString()} cases × ${CASE_D * 100} mm · ` +
    `${(meters / 0.85).toFixed(1)} chairs · ${(meters / 5.5).toFixed(1)} giraffes · ` +
    (nearMiss ? `${eiffel}% of the Eiffel Tower` : "taller than everything on the list"),
    0.085
  );
  stat.position.set(-5.5, height + 2.5, 0);
  stat.userData.prio = 1e9;   // the headline always wins declutter
  scene.add(stat);
  labelSprites.push(stat);

  // let the orbit camera actually back out far enough to frame the tower
  controls.maxDistance = Math.max(160, height * 2.2);

  const finalPos = new THREE.Vector3(height * 0.55, height * 0.6, height * 1.4);
  const finalTarget = new THREE.Vector3(0, height * 0.5, 0);
  if (S.SNAP || S.monolithFlown) {
    camera.position.copy(finalPos);
    controls.target.copy(finalTarget);
  } else {
    S.monolithFlown = true;
    flythrough([
      { pos: new THREE.Vector3(7, 2.5, 13), target: new THREE.Vector3(0, 9, 0) },
      { pos: new THREE.Vector3(-14, height * 0.3, 20), target: new THREE.Vector3(0, height * 0.38, 0) },
      { pos: new THREE.Vector3(-26, height * 0.72, -26), target: new THREE.Vector3(0, height * 0.7, 0) },
      { pos: new THREE.Vector3(height * 0.2, height * 0.95, height * 0.55), target: new THREE.Vector3(0, height * 0.8, 0) },
      { pos: finalPos, target: finalTarget },
    ], 9);
  }
}

// Tag constellations: the library floats as labeled nebula islands, one per
// semantic tag cluster — clustering and layout precomputed offline by
// scripts/export_stacks.py (spherical k-means in tag space, then islands
// with guaranteed separation), so this stays a dumb placement pass. The
// visual grammar is borrowed from the readable embedding maps (Nomic
// Atlas, Map of GitHub): one hue per cluster, a soft nebula glow behind
// each island, labels at the centroids, faint constellation lines between
// nearest neighbors.
const GALAXY_LIFT = 78;   // float the cloud well clear of the floor disc

let galaxyLines = null;   // { mesh, edges, settled } while galaxy is active
const nebulaMeshes = [];
let nebulaTex = null;

function nebulaTexture() {
  if (nebulaTex) return nebulaTex;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 256;
  const ctx = cv.getContext("2d");
  const grad = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
  grad.addColorStop(0, "rgba(255,255,255,0.75)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.26)");
  grad.addColorStop(0.7, "rgba(255,255,255,0.07)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 256, 256);
  nebulaTex = new THREE.CanvasTexture(cv);   // shared; never disposed
  return nebulaTex;
}

// Constellation lines between each game and its strongest same-cluster
// neighbors (pairs exported in meta.edges). Endpoints track g.cur, so the
// lines stretch with the pile→galaxy explosion, then settle and brighten.
function buildGalaxyLines() {
  const edges = meta.edges ?? [];
  if (!edges.length) return null;
  const posArr = new Float32Array(edges.length * 6);
  const colArr = new Float32Array(edges.length * 6);
  const c = new THREE.Color();
  edges.forEach(([a], e) => {
    c.set(meta.clusters?.[games[a].cl]?.color ?? "#8899aa").multiplyScalar(0.6);
    for (const v of [0, 3]) {
      colArr[e * 6 + v] = c.r;
      colArr[e * 6 + v + 1] = c.g;
      colArr[e * 6 + v + 2] = c.b;
    }
  });
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(colArr, 3));
  const mesh = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
    vertexColors: true, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  }));
  mesh.frustumCulled = false;
  scene.add(mesh);
  modeObjects.push(mesh);
  return { mesh, edges, settled: false };
}

function updateGalaxyLinePositions() {
  const attr = galaxyLines.mesh.geometry.getAttribute("position");
  galaxyLines.edges.forEach(([a, b], e) => {
    attr.setXYZ(e * 2, games[a].cur.x, games[a].cur.y, games[a].cur.z);
    attr.setXYZ(e * 2 + 1, games[b].cur.x, games[b].cur.y, games[b].cur.z);
  });
  attr.needsUpdate = true;
}

// Per-frame galaxy work, called from the main loop: billboard the nebula
// planes, track line endpoints while cases are in flight, fade lines in.
export function galaxyFrame(dt) {
  if (S.currentModeKey !== "galaxy") return;
  for (const n of nebulaMeshes) n.quaternion.copy(camera.quaternion);
  if (!galaxyLines) return;
  if (S.animating || S.rainActive || !galaxyLines.settled) {
    updateGalaxyLinePositions();
    galaxyLines.settled = !S.animating && !S.rainActive;
  }
  const target = S.animating ? 0.12 : 0.38;
  const mat = galaxyLines.mesh.material;
  mat.opacity += (target - mat.opacity) * Math.min(1, dt * 1.5);
}

function layoutGalaxy() {
  nebulaMeshes.length = 0;   // prior mode objects were already cleared
  for (const g of games) {
    const p = g.pos ?? [0, 0, 0];
    g._stack = null;
    g.from.copy(g.cur);
    g.fromYaw = g.curYaw;
    g.fromTilt = g.curTilt;
    g.fromScale = g.curScale;
    g.to.set(p[0], p[1] + GALAXY_LIFT, p[2]);
    g.toYaw = jitter(g.id, 16) * Math.PI;
    g.toTilt = 0;
    g.toScale = 0.65;
    g.t = 0;
    g.delay = Math.abs(jitter(g.id, 17)) * STAGGER * 1.6;
  }

  // one soft additive glow plane per island, in the island's hue — the
  // clusters read as colored gas clouds from any distance
  const clusters = meta.clusters ?? [];
  const maxCount = Math.max(1, ...clusters.map((c) => c.count));
  for (const c of clusters) {
    if (c.color) {
      const size = (c.r ?? 8) * 3.1;
      const neb = new THREE.Mesh(
        new THREE.PlaneGeometry(size, size),
        new THREE.MeshBasicMaterial({
          map: nebulaTexture(), color: c.color, transparent: true,
          opacity: 0.34, blending: THREE.AdditiveBlending, depthWrite: false,
        })
      );
      neb.position.set(c.pos[0], c.pos[1] + GALAXY_LIFT, c.pos[2]);
      scene.add(neb);
      modeObjects.push(neb);
      nebulaMeshes.push(neb);
    }
    const spr = makeLabelSprite(
      c.label, `${c.count} games`,
      0.05 + 0.035 * Math.sqrt(c.count / maxCount),
      c.color ?? "#ffffff"
    );
    spr.position.set(c.pos[0], c.pos[1] + GALAXY_LIFT + (c.r ?? 8) + 1.5, c.pos[2]);
    spr.userData.prio = c.count;
    scene.add(spr);
    labelSprites.push(spr);
  }

  galaxyLines = buildGalaxyLines();
  if (galaxyLines && S.SNAP) galaxyLines.mesh.material.opacity = 0.38;

  const uncharted = games.filter((g) => g.uncharted);
  if (uncharted.length) {
    const shellR = Math.max(...uncharted.map((g) => Math.hypot(g.pos[0], g.pos[2])));
    const spr = makeLabelSprite("uncharted", `${uncharted.length} thin-tagged games`, 0.05);
    spr.position.set(shellR, GALAXY_LIFT + 18, 0);
    spr.userData.prio = 1;
    scene.add(spr);
    labelSprites.push(spr);
  }

  figure.position.set(0, 0, 0);   // the chair stays grounded under the stars
  figureLabel.position.set(0, 9.5, 0);
  controls.maxDistance = 420;     // room to pull back and see the whole galaxy

  // frame the cloud (it floats far above the default pile framing)
  const finalPos = new THREE.Vector3(70, GALAXY_LIFT + 30, 128);
  const finalTarget = new THREE.Vector3(0, GALAXY_LIFT, 0);
  if (S.SNAP) {
    camera.position.copy(finalPos);
    controls.target.copy(finalTarget);
  } else {
    flythrough([
      { pos: camera.position.clone(), target: controls.target.clone() },
      { pos: finalPos, target: finalTarget },
    ], 2.5);
  }
}
