// The instanced jewel cases: geometry, cover/spine/dust shader, per-case
// motion state (position/yaw/tilt/scale tweens), and the transition stepper.

import * as THREE from "three";
import { games, meta, FAMILY, atlases } from "./data.js";
import { scene } from "./scene.js";
import { S } from "./state.js";
import {
  CASE_W, CASE_H, CASE_D, TRANSITION, POP_LIFT, FLAT_TILT, jitter, ease,
} from "./util.js";

const caseGeo = new THREE.BoxGeometry(CASE_W, CASE_H, CASE_D);

const vert = /* glsl */ `
  attribute vec2 aUv;
  attribute vec3 aColor;
  attribute float aGlow;
  attribute float aDust;
  attribute float aAff;
  uniform vec2 uTileScale;
  varying vec2 vUv;
  varying vec2 vUvEdge;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vFront;
  varying float vGlow;
  varying float vDust;
  varying float vAff;
  void main() {
    vDust = aDust;
    vAff = aAff;
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
  uniform float uAffMode;
  varying vec2 vUv;
  varying vec2 vUvEdge;
  varying vec3 vColor;
  varying vec3 vNormal;
  varying float vFront;
  varying float vGlow;
  varying float vDust;
  varying float vAff;
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
    // Galaxy affinity: loved-tag regions glow warm, low-affinity stays cool
    float aff = vAff * uAffMode;
    c += max(aff, 0.0) * vec3(0.34, 0.22, 0.05);
    c = mix(c, c * vec3(0.72, 0.82, 1.10), max(-aff, 0.0) * 0.55);
    gl_FragColor = vec4(c, 1.0);
  }
`;

// Taste affinity normalized to [-1, 1] for the galaxy's warm/cool glow.
const affMax = Math.max(1e-6, ...games.map((g) => Math.abs(g.aff ?? 0)));
const affNorm = (g) =>
  Math.max(-1, Math.min(1, (g.aff ?? 0) / affMax));

// One InstancedMesh per atlas sheet; record where each game lives.
export const meshes = [];
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
      uAffMode: { value: 0 },
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
  const affArr = new Float32Array(n);
  const col = new THREE.Color();
  indices.forEach((gi, i) => {
    const g = games[gi];
    affArr[i] = affNorm(g);
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
  mesh.geometry.setAttribute("aAff", new THREE.InstancedBufferAttribute(affArr, 1));
  scene.add(mesh);
  meshes.push(mesh);
}

// ---------- per-case motion state ----------

const _q = new THREE.Quaternion();
const _e = new THREE.Euler();
const _m = new THREE.Matrix4();
const _s = new THREE.Vector3(1, 1, 1);

for (const g of games) {
  // scatter in the sky for the intro drop
  g.cur = new THREE.Vector3(jitter(g.id, 1) * 60, 45 + jitter(g.id, 2) * 25, jitter(g.id, 3) * 60);
  g.from = g.cur.clone();
  g.to = g.cur.clone();
  g.curYaw = g.fromYaw = g.toYaw = jitter(g.id, 4) * Math.PI;
  g.curTilt = g.fromTilt = g.toTilt = FLAT_TILT;
  g.curScale = g.fromScale = g.toScale = 1;
  g.t = 1; g.delay = 0;
  g.pop = 0; g.popT = 0;
}

export function composeMatrix(g) {
  if (g.curQuat) {
    // quaternion fast-path: physics (or a physics→tween handoff) owns the
    // full orientation, which yaw/tilt scalars can't represent
    _s.setScalar(g.curScale);
    _m.compose(g.cur, g.curQuat, _s);
    g._mesh.setMatrixAt(g._i, _m);
    return;
  }
  _e.set(g.curTilt, g.curYaw, 0, "YXZ");   // yaw about world Y, then tilt
  _q.setFromEuler(_e);
  _s.setScalar(g.curScale);
  _m.compose(g.cur, _q, _s);
  g._mesh.setMatrixAt(g._i, _m);
}

export function setGlow(g, v) {
  const attr = g._mesh.geometry.getAttribute("aGlow");
  attr.setX(g._i, v);
  attr.needsUpdate = true;
}

export function setDust(v) {
  S.dustEnabled = !!v;
  for (const m of meshes) m.material.uniforms.uDust.value = S.dustEnabled ? 1 : 0;
}

export function setAffinityGlow(v) {
  for (const m of meshes) m.material.uniforms.uAffMode.value = v ? 1 : 0;
}

export function snapAll() {
  for (const g of games) {
    g.cur.copy(g.to);
    g.curYaw = g.toYaw;
    g.curTilt = g.toTilt;
    g.curScale = g.toScale;
    g.fromQuat = g.curQuat = null;
    g.t = 1;
    composeMatrix(g);
  }
  for (const m of meshes) {
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  }
  S.animating = false;
}

// Advance in-flight tweens; called from the main loop while S.animating.
export function stepTransition(dt) {
  S.transitionClock += dt;
  let busy = false;
  for (const g of games) {
    if (g.t >= 1) continue;
    const local = (S.transitionClock - g.delay) / TRANSITION;
    g.t = Math.max(0, Math.min(1, local));
    const k = ease(g.t);
    g.cur.lerpVectors(g.from, g.to, k);
    g.curYaw = g.fromYaw + (g.toYaw - g.fromYaw) * k;
    g.curTilt = g.fromTilt + (g.toTilt - g.fromTilt) * k;
    g.curScale = g.fromScale + (g.toScale - g.fromScale) * k;
    if (g.fromQuat) {
      // seeded by a physics handoff: slerp from the arbitrary physics pose
      // toward the layout's yaw/tilt target
      _e.set(g.toTilt, g.toYaw, 0, "YXZ");
      _q.setFromEuler(_e);
      g.curQuat = (g.curQuat ?? new THREE.Quaternion())
        .slerpQuaternions(g.fromQuat, _q, k);
      if (g.t >= 1) g.fromQuat = g.curQuat = null;
    }
    composeMatrix(g);
    if (g.t < 1) busy = true;
  }
  for (const m of meshes) m.instanceMatrix.needsUpdate = true;
  if (!busy) {
    S.animating = false;
    for (const m of meshes) m.computeBoundingSphere();
  }
}
