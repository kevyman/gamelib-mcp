// Renderer, camera, controls, environment, floor, the scale chair, and the
// in-scene label-sprite system (creation, ownership lists, decluttering).

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { envTex, chairGltf } from "./data.js";

export const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

export const scene = new THREE.Scene();
envTex.mapping = THREE.EquirectangularReflectionMapping;
scene.background = envTex;
scene.backgroundIntensity = 0.7;       // keep the warehouse moody, covers pop
scene.backgroundBlurriness = 0.04;
scene.environment = envTex;            // IBL for the standard-material figure

// far plane sized for the Monolith pull-back: a 2,609-case tower is ~420
// units tall and the final framing sits ~550 units out
export const camera = new THREE.PerspectiveCamera(50, innerWidth / innerHeight, 0.1, 3000);
camera.position.set(0, 26, 44);

export const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI / 2 - 0.04;
controls.minDistance = 6;
controls.maxDistance = 160;
controls.target.set(0, 3, 0);

export const floor = new THREE.Mesh(
  new THREE.CircleGeometry(70, 64),
  new THREE.MeshStandardMaterial({ color: 0x212328, roughness: 0.55, metalness: 0.25 })
);
floor.rotation.x = -Math.PI / 2;
floor.position.y = -0.02;
scene.add(floor);

// Scale object: an ordinary plastic monobloc chair (~85 cm). Model is
// meter-scale; scene units are 10 cm (a case is 1.9 units = 19 cm), hence ×10.
export const figure = chairGltf.scene;
figure.scale.setScalar(10);
figure.rotation.y = Math.PI * 0.3;    // casual angle toward the piles
scene.add(figure);                    // positioned per-layout in applyMode

// In-scene text sprites: depth-tested (piles in front occlude them) and big.
export function makeLabelSprite(title, sub, heightFrac = 0.13, titleColor = "#ffffff") {
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
  ctx.fillStyle = titleColor;
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

export const figureLabel = makeLabelSprite("an ordinary chair", "for scale", 0.07);
scene.add(figureLabel);                     // positioned per-layout in applyMode

// Sprites owned by the current mode (pile titles, band/height markers).
export const labelSprites = [];
export function clearLabels() {
  for (const spr of labelSprites) {
    scene.remove(spr);
    spr.material.map.dispose();
    spr.material.dispose();
  }
  labelSprites.length = 0;
}

// Non-sprite helper meshes owned by the current mode (marker rings etc.)
export const modeObjects = [];
export function clearModeObjects() {
  for (const o of modeObjects) {
    scene.remove(o);
    o.geometry?.dispose();
    o.material?.dispose();
  }
  modeObjects.length = 0;
}

// Map-style label decluttering: when plates would overlap on screen, only the
// highest-priority one stays; the rest reappear as the camera separates them.
const _v = new THREE.Vector3();
export function declutterLabels() {
  // px per sprite-scale unit for sizeAttenuation:false sprites
  const F = innerHeight / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
  const pad = 6;               // don't let plates touch edge-to-edge
  const placed = [];
  const items = [...labelSprites, figureLabel]
    .map((spr) => ({ spr, prio: spr.userData.prio ?? -1 }))
    .sort((a, b) => b.prio - a.prio);
  for (const { spr } of items) {
    if (spr.userData.gated) { spr.visible = false; continue; }   // not revealed yet
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
