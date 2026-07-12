// First-person walkable library: procedural shelf units, games spine-out,
// pointer-lock WASD movement, crosshair pull-out. Desktop-only v1.

import * as THREE from "three";
import { PointerLockControls } from "../vendor/PointerLockControls.js";
import { games } from "../data.js";
import {
  scene, camera, controls, renderer, figure, figureLabel,
  makeLabelSprite, labelSprites, clearLabels, clearModeObjects,
} from "../scene.js";
import { meshes, snapAll } from "../cases.js";
import { MODES, applyMode, groupGames } from "../layouts.js";
import { setHovered, pickGame } from "./orbit.js";
import { endFlythrough } from "../flythrough.js";
import { S } from "../state.js";
import { CASE_H, CASE_D, STAGGER, jitter } from "../util.js";

// ---------- shelving dimensions (scene units = 10 cm) ----------

const SHELF_LEN = 16;                    // board length per unit (1.6 m)
const SHELF_DEPTH = 1.6;                 // board depth — a case is 1.28 deep spine-out
const BOARD_T = 0.12;
const BOARD_YS = [1.6, 5.2, 8.8, 12.4, 16.0];   // board top surfaces (5 shelves, top ~1.6 m)
const PITCH = 0.28;                      // case pitch along a shelf (spine + air)
const PER_BOARD = Math.floor((SHELF_LEN - 0.6) / PITCH);
const PER_UNIT = PER_BOARD * BOARD_YS.length;
const UNIT_GAP = 1.4;                    // between units in a row
const ROW_Z_PITCH = 16;                  // aisle pitch (rows face +z, ~1.5 m clear)
const EYE = 17;                          // 1.7 m
const SPEED = 26;                        // 2.6 m/s walk; shift sprints

const walkObjects = [];                  // shelf meshes owned by walk mode
const colliders = [];                    // 2D AABBs {x0,x1,z0,z1} per unit
let plc = null;                          // PointerLockControls, lazy
let pulled = null;                       // the case pulled out to eye level
let pulledHome = null;                   // its shelf transform to reshelve to
let headless = false;                    // ?walk=1 debug: no pointer lock
const keys = new Set();
const CENTER = new THREE.Vector2(0, 0);  // crosshair NDC

const crosshair = document.getElementById("crosshair");
const hint = document.getElementById("hint");
const card = document.getElementById("card");
let savedHint = "";

const shelfMat = new THREE.MeshStandardMaterial({
  color: 0x3a3f47, roughness: 0.75, metalness: 0.15,
});

function buildShelfUnit(ux, uz) {
  const H = BOARD_YS[BOARD_YS.length - 1] + 1.2;
  const upright = new THREE.BoxGeometry(0.24, H, SHELF_DEPTH);
  for (const sx of [-1, 1]) {
    const m = new THREE.Mesh(upright, shelfMat);
    m.position.set(ux + sx * (SHELF_LEN / 2 + 0.12), H / 2, uz);
    scene.add(m);
    walkObjects.push(m);
  }
  const board = new THREE.BoxGeometry(SHELF_LEN, BOARD_T, SHELF_DEPTH);
  for (const y of BOARD_YS) {
    const m = new THREE.Mesh(board, shelfMat);
    m.position.set(ux, y - BOARD_T / 2, uz);
    scene.add(m);
    walkObjects.push(m);
  }
  colliders.push({
    x0: ux - SHELF_LEN / 2 - 0.6, x1: ux + SHELF_LEN / 2 + 0.6,
    z0: uz - SHELF_DEPTH / 2 - 0.5, z1: uz + SHELF_DEPTH / 2 + 0.5,
  });
}

// Shelve the whole library in the sections of the active sort mode
// (platform wings, critic bays, …); modes without buckets fall back to
// Platform. Within a section games keep the mode's sort, so the best
// spines start each bay.
function layoutShelves() {
  const modeKey = MODES[S.currentModeKey]?.buckets ? S.currentModeKey : "platform";
  const mode = MODES[modeKey];
  const sections = groupGames(mode);

  const totalUnits = sections.reduce(
    (s, [, gs]) => s + Math.ceil(gs.length / PER_UNIT), 0);
  const unitsPerRow = Math.max(2, Math.ceil(Math.sqrt(totalUnits / 1.6)));

  colliders.length = 0;
  let unit = 0;
  for (const [key, gs] of sections) {
    const sectionStartUnit = unit;
    gs.forEach((g, i) => {
      const u = unit + Math.floor(i / PER_UNIT);
      const slot = i % PER_UNIT;
      const b = Math.floor(slot / PER_BOARD);
      const k = slot % PER_BOARD;
      const col = u % unitsPerRow, row = Math.floor(u / unitsPerRow);
      const ux = (col - (unitsPerRow - 1) / 2) * (SHELF_LEN + UNIT_GAP);
      const uz = -row * ROW_Z_PITCH;
      g._stack = null;
      g._shelf = {   // remembered so pull-out can reshelve
        pos: new THREE.Vector3(
          ux - (SHELF_LEN - 0.6) / 2 + k * PITCH + PITCH / 2,
          BOARD_YS[b] + CASE_H / 2,
          uz
        ),
        yaw: -Math.PI / 2 + jitter(g.id, 11) * 0.03,   // spine (+x face) to the aisle
      };
      g.from.copy(g.cur);
      g.fromYaw = g.curYaw;
      g.fromTilt = g.curTilt;
      g.fromScale = g.curScale;
      g.to.copy(g._shelf.pos);
      g.toYaw = g._shelf.yaw;
      g.toTilt = 0;
      g.toScale = 1;
      g.t = 0;
      g.delay = (i / gs.length) * STAGGER;
    });
    const nUnits = Math.ceil(gs.length / PER_UNIT);
    for (let u = 0; u < nUnits; u++) {
      const uu = sectionStartUnit + u;
      const col = uu % unitsPerRow, row = Math.floor(uu / unitsPerRow);
      const ux = (col - (unitsPerRow - 1) / 2) * (SHELF_LEN + UNIT_GAP);
      const uz = -row * ROW_Z_PITCH;
      buildShelfUnit(ux, uz);
      if (u === 0) {
        const spr = makeLabelSprite(
          mode.title(key), `${gs.length} game${gs.length === 1 ? "" : "s"}`, 0.06);
        spr.position.set(ux, BOARD_YS[BOARD_YS.length - 1] + 2.6, uz);
        spr.userData.prio = gs.length;
        scene.add(spr);
        labelSprites.push(spr);
      }
    }
    unit += nUnits;
  }

  // the chair waits at the end of the first aisle
  const rowW = unitsPerRow * (SHELF_LEN + UNIT_GAP);
  figure.position.set(-rowW / 2 - 5, 0, 3);
  figureLabel.position.set(-rowW / 2 - 5, 9.5, 3);
  return { unitsPerRow };
}

// ---------- enter / exit ----------

export function enterWalk(opts = {}) {
  if (S.walking) return;
  endFlythrough(false);   // a Monolith climb shouldn't fight the walk camera
  headless = !!opts.headless;
  S.walking = true;
  S.exploded = null;

  clearLabels();
  clearModeObjects();
  layoutShelves();
  if (S.SNAP) snapAll();
  else {
    S.transitionClock = 0;
    S.animating = true;
  }

  controls.enabled = false;
  // start in the front aisle, centered on the first shelf unit, ~1 m back
  const startX = -(SHELF_LEN + UNIT_GAP) / 2;
  camera.position.set(startX, EYE, 10);
  camera.lookAt(startX, EYE * 0.8, 0);

  crosshair.style.display = "";
  savedHint = hint.textContent;
  hint.textContent =
    "WASD move · shift run · look at a case to peek · click / E pull it out · ESC leave the aisles";

  if (!headless) {
    if (!plc) {
      plc = new PointerLockControls(camera, renderer.domElement);
      plc.addEventListener("unlock", () => exitWalk());
    }
    plc.lock();
  }
}

export function exitWalk() {
  if (!S.walking) return;
  S.walking = false;
  pulled = null;
  pulledHome = null;
  keys.clear();

  if (plc?.isLocked) plc.unlock();
  for (const o of walkObjects) {
    scene.remove(o);
    o.geometry.dispose();
  }
  walkObjects.length = 0;
  colliders.length = 0;
  for (const m of meshes) m.frustumCulled = false;

  crosshair.style.display = "none";
  hint.textContent = savedHint;
  card.style.transform = "";
  setHovered(null);

  // back to the orbit view of whatever mode we walked out of
  controls.enabled = true;
  camera.position.set(0, 26, 44);
  controls.target.set(0, 3, 0);
  applyMode(S.currentModeKey);
}

// ---------- movement + interaction ----------

addEventListener("keydown", (e) => {
  if (!S.walking) return;
  keys.add(e.code);
  if (e.code === "KeyE") pullOrReshelve();
  // normally the pointer-lock 'unlock' event exits; this is the fallback for
  // headless runs and pointer-lock failures so ESC never traps the walker
  if (e.code === "Escape" && !plc?.isLocked) exitWalk();
});
addEventListener("keyup", (e) => keys.delete(e.code));

renderer.domElement.addEventListener("pointerdown", () => {
  if (S.walking && (plc?.isLocked || headless)) pullOrReshelve();
});

function pullOrReshelve() {
  if (pulled) {           // put it back on the shelf
    const g = pulled;
    g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
    g.fromScale = g.curScale;
    g.to.copy(pulledHome.pos); g.toYaw = pulledHome.yaw;
    g.toTilt = 0; g.toScale = 1;
    g.t = 0; g.delay = 0;
    S.transitionClock = 0;
    S.animating = true;
    pulled = null;
    pulledHome = null;
    setHovered(null);
    return;
  }
  const g = pickGame(CENTER);
  if (!g || !g._shelf) return;
  pulled = g;
  pulledHome = g._shelf;
  const dir = camera.getWorldDirection(new THREE.Vector3());
  g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
  g.fromScale = g.curScale;
  g.to.copy(camera.position).addScaledVector(dir, 3.4);
  g.to.y = EYE - 0.8;                       // eye level, slightly below center
  g.toYaw = Math.atan2(camera.position.x - g.to.x, camera.position.z - g.to.z);
  g.toTilt = 0;
  g.toScale = 1;
  g.t = 0; g.delay = 0;
  S.transitionClock = 0;
  S.animating = true;
  setHovered(g);
  pinCard();
}

function pinCard() {
  // pointer-lock hides the cursor; park the card bottom-right instead
  card.style.left = innerWidth - 372 + "px";
  card.style.top = innerHeight - 240 + "px";
}

const _dir = new THREE.Vector3();
export function stepWalk(dt) {
  // movement (pointer lock owns the look; we own the feet)
  if (plc?.isLocked || headless) {
    const run = keys.has("ShiftLeft") || keys.has("ShiftRight") ? 1.8 : 1;
    const d = SPEED * run * dt;
    let mx = 0, mz = 0;
    if (keys.has("KeyW") || keys.has("ArrowUp")) mz += 1;
    if (keys.has("KeyS") || keys.has("ArrowDown")) mz -= 1;
    if (keys.has("KeyA") || keys.has("ArrowLeft")) mx -= 1;
    if (keys.has("KeyD") || keys.has("ArrowRight")) mx += 1;
    if (mz) (plc ?? fallbackMove()).moveForward?.(mz * d);
    if (mx) plc?.moveRight(mx * d);
    camera.position.y = EYE;

    // shelf collision: 2D AABBs, push out along the shallow axis
    for (const c of colliders) {
      const { x, z } = camera.position;
      if (x > c.x0 && x < c.x1 && z > c.z0 && z < c.z1) {
        const dx = Math.min(x - c.x0, c.x1 - x);
        const dz = Math.min(z - c.z0, c.z1 - z);
        if (dx < dz) camera.position.x = x - c.x0 < c.x1 - x ? c.x0 : c.x1;
        else camera.position.z = z - c.z0 < c.z1 - z ? c.z0 : c.z1;
      }
    }
    // stay on the floor disc
    const r = Math.hypot(camera.position.x, camera.position.z);
    if (r > 66) {
      camera.position.x *= 66 / r;
      camera.position.z *= 66 / r;
    }
  }

  // crosshair peek: the looked-at case eases out via the existing pop channel
  if (!S.animating && !pulled) setHovered(pickGame(CENTER));
  if (S.hovered) pinCard();   // pointer lock hides the cursor; park the card

  // aisles are where per-instance frustum culling starts paying for itself;
  // bounding spheres are recomputed whenever a transition settles
  if (!S.animating) {
    for (const m of meshes) if (!m.frustumCulled) m.frustumCulled = true;
  } else {
    for (const m of meshes) if (m.frustumCulled) m.frustumCulled = false;
  }
}

function fallbackMove() {
  // headless debug without PointerLockControls: move along the view axis
  return {
    moveForward: (d) => {
      camera.getWorldDirection(_dir);
      _dir.y = 0;
      _dir.normalize();
      camera.position.addScaledVector(_dir, d);
    },
  };
}
