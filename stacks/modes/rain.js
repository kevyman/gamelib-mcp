// Ragdoll rain: a mannequin stands in the middle of the floor and the whole
// library rains down on it with real rigid-body physics (Rapier, vendored
// WASM build, lazy-loaded on first use so the base payload doesn't grow).

import * as THREE from "three";
import { games } from "../data.js";
import { scene, figure, figureLabel, clearLabels, clearModeObjects } from "../scene.js";
import { meshes } from "../cases.js";
import { S } from "../state.js";
import { applyMode, preApplyHooks } from "../layouts.js";
import { CASE_W, CASE_H, CASE_D, M, jitter } from "../util.js";

let RAPIER = null;          // module, resolved on first Rain click
let world = null;
let bodies = null;          // Map<game, RigidBody>
let ragdoll = [];           // [{ body, mesh, offsetY }]
let chairBody = null;
let spawnQueue = [];
let spawnTimer = 0;
let settleTimer = 0;
let acc = 0;

const SPAWN_BATCH = 150;
const SPAWN_EVERY = 0.14;   // seconds between batches — several seconds of rain
const STEP = 1 / 60;
const GRAVITY = -9.81 * M;  // scene units are 10 cm

const _m = new THREE.Matrix4();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3(1, 1, 1);
const _e = new THREE.Euler();
const _off = new THREE.Vector3();
const _chairYaw = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, Math.PI * 0.3, 0));

const ragdollMat = new THREE.MeshStandardMaterial({
  color: 0x9aa0a6, roughness: 0.9, metalness: 0.05,
});

// A procedural capsule mannequin: 11 bodies, spherical joints with the
// default limits — enough to get knocked around and buried convincingly.
// [name, radius, capsule half-height (0 = ball), x, y]
const PARTS = [
  ["pelvis", 1.35, 0.9, 0, 10.2],
  ["torso", 1.55, 2.0, 0, 13.6],
  ["head", 1.1, 0, 0, 17.6],
  ["armUL", 0.55, 1.35, -2.5, 13.6],
  ["armUR", 0.55, 1.35, 2.5, 13.6],
  ["armLL", 0.5, 1.25, -2.5, 10.4],
  ["armLR", 0.5, 1.25, 2.5, 10.4],
  ["legUL", 0.8, 1.7, -0.95, 7.2],
  ["legUR", 0.8, 1.7, 0.95, 7.2],
  ["legLL", 0.65, 1.6, -0.95, 3.3],
  ["legLR", 0.65, 1.6, 0.95, 3.3],
];
// [partA, partB, anchor on A (local), anchor on B (local)]
const JOINTS = [
  ["pelvis", "torso", [0, 1.2, 0], [0, -2.2, 0]],
  ["torso", "head", [0, 2.2, 0], [0, -1.2, 0]],
  ["torso", "armUL", [-1.8, 1.6, 0], [0, 1.5, 0]],
  ["torso", "armUR", [1.8, 1.6, 0], [0, 1.5, 0]],
  ["armUL", "armLL", [0, -1.5, 0], [0, 1.4, 0]],
  ["armUR", "armLR", [0, -1.5, 0], [0, 1.4, 0]],
  ["pelvis", "legUL", [-0.95, -1.1, 0], [0, 1.9, 0]],
  ["pelvis", "legUR", [0.95, -1.1, 0], [0, 1.9, 0]],
  ["legUL", "legLL", [0, -1.9, 0], [0, 1.8, 0]],
  ["legUR", "legLR", [0, -1.9, 0], [0, 1.8, 0]],
];

function buildRagdoll() {
  const byName = {};
  for (const [name, r, hh, x, y] of PARTS) {
    // Fixed at first: he stands his ground, games bounce off. stepRain flips
    // everything dynamic partway through the rain — THEN he goes down.
    const body = world.createRigidBody(
      RAPIER.RigidBodyDesc.fixed()
        .setTranslation(x, y, 0)
        // damping tames post-collapse joint jitter once dynamic
        .setLinearDamping(0.3)
        .setAngularDamping(1.2)
    );
    const desc = hh > 0
      ? RAPIER.ColliderDesc.capsule(hh, r)
      : RAPIER.ColliderDesc.ball(r);
    world.createCollider(desc.setFriction(0.8).setDensity(2.5), body);
    const geo = hh > 0
      ? new THREE.CapsuleGeometry(r, hh * 2, 4, 10)
      : new THREE.SphereGeometry(r, 16, 12);
    const mesh = new THREE.Mesh(geo, ragdollMat);
    mesh.position.set(x, y, 0);   // standing pose; sync takes over on collapse
    scene.add(mesh);
    ragdoll.push({ body, mesh });
    byName[name] = body;
  }
  for (const [a, b, pa, pb] of JOINTS) {
    const joint = world.createImpulseJoint(
      RAPIER.JointData.spherical(
        { x: pa[0], y: pa[1], z: pa[2] },
        { x: pb[0], y: pb[1], z: pb[2] }
      ),
      byName[a], byName[b], true
    );
    // adjacent capsules overlap at every joint; without this the solver
    // fights those contacts forever and the mannequin convulses on the floor
    joint.setContactsEnabled(false);
  }
}

// He resists this many simulated seconds of bombardment before giving in
// (most of the pile has already come down on and around him by then).
const COLLAPSE_AT_T = 2.8;
let ragdollLive = false;
let rainClock = 0;

function collapseRagdoll() {
  for (const part of ragdoll) {
    part.body.setBodyType(RAPIER.RigidBodyType.Dynamic, true);
  }
  ragdollLive = true;
}

export async function enterRain() {
  if (S.rainActive || S.walking) return;
  if (!RAPIER) {
    RAPIER = await import("../vendor/rapier3d-compat.js");
    await RAPIER.init();
  }
  if (S.rainActive) return;   // double-click while the WASM was loading

  S.rainActive = true;
  S.rainSettled = false;
  S.animating = false;        // physics owns the transforms now
  S.exploded = null;
  clearLabels();              // the piles those labels named are airborne now
  clearModeObjects();
  figureLabel.userData.gated = true;   // the chair is about to stop being ordinary

  world = new RAPIER.World({ x: 0, y: GRAVITY, z: 0 });
  // 2 solver iterations (default 4) read fine for a junk pile and roughly
  // halve step cost at 2.6k live boxes — the frame budget matters more than
  // stacking precision here
  world.numSolverIterations = 2;
  world.createCollider(
    RAPIER.ColliderDesc.cuboid(85, 0.5, 85).setTranslation(0, -0.5, 0).setFriction(0.7)
  );
  ragdollLive = false;
  rainClock = 0;
  buildRagdoll();

  // the chair deserves to be buried too
  figure.position.set(7, 0, 1);
  figure.rotation.set(0, Math.PI * 0.3, 0);
  chairBody = world.createRigidBody(
    RAPIER.RigidBodyDesc.dynamic().setTranslation(7, 4.3, 1)
  );
  world.createCollider(
    RAPIER.ColliderDesc.cuboid(2.4, 4.3, 2.4).setFriction(0.7).setDensity(0.4),
    chairBody
  );

  // park every case out of frame above the drop zone; bodies spawn in
  // batches so the framerate never craters and the rain lasts a while
  for (const g of games) {
    g._stack = null;
    g.curScale = 1;
    g.fromQuat = g.curQuat = null;
    g.cur.set(jitter(g.id, 1) * 55, 160 + jitter(g.id, 2) * 30, jitter(g.id, 3) * 55);
    _e.set(jitter(g.id, 12) * 1.2, jitter(g.id, 13) * Math.PI, jitter(g.id, 14) * 1.2);
    _q.setFromEuler(_e);
    _m.compose(g.cur, _q, _s);
    g._mesh.setMatrixAt(g._i, _m);
  }
  for (const m of meshes) m.instanceMatrix.needsUpdate = true;

  bodies = new Map();
  spawnQueue = [...games].sort((a, b) => jitter(a.id, 21) - jitter(b.id, 21));
  spawnTimer = 0;
  settleTimer = 0;
  acc = 0;
}

function spawnBatch(n) {
  for (const g of spawnQueue.splice(0, n)) {
    _e.set(jitter(g.id, 12) * 1.2, jitter(g.id, 13) * Math.PI, jitter(g.id, 14) * 1.2);
    _q.setFromEuler(_e);
    const body = world.createRigidBody(
      RAPIER.RigidBodyDesc.dynamic()
        .setTranslation(jitter(g.id, 1) * 12, 48 + jitter(g.id, 2) * 18, jitter(g.id, 3) * 12)
        .setRotation({ x: _q.x, y: _q.y, z: _q.z, w: _q.w })
        .setLinvel(0, -10, 0)
        .setCanSleep(true)
    );
    world.createCollider(
      RAPIER.ColliderDesc.cuboid(CASE_W / 2, CASE_H / 2, CASE_D / 2)
        .setRestitution(0.1)
        .setFriction(0.6),
      body
    );
    bodies.set(g, body);
  }
}

export function stepRain(dt) {
  spawnTimer -= dt;
  if (spawnTimer <= 0 && spawnQueue.length) {
    spawnBatch(SPAWN_BATCH);
    spawnTimer = SPAWN_EVERY;
  }

  // fixed timestep with an accumulator, capped at 2 steps/frame: on slow
  // machines the sim runs slightly slow-motion instead of eating the frame
  acc = Math.min(acc + dt, STEP * 2);
  while (acc >= STEP) {
    world.step();
    rainClock += STEP;
    acc -= STEP;
  }

  // he stands his ground through the worst of it, then gives in and
  // ragdolls under whatever is still falling
  if (!ragdollLive && rainClock >= COLLAPSE_AT_T) collapseRagdoll();

  // physics → instance matrices (and g.cur, so raycasting/handoff stay
  // true). Sleeping bodies haven't moved, and by the settling tail that's
  // most of the pile — skipping them saves thousands of WASM-boundary calls
  // (each translation()/rotation() also allocates) per frame.
  for (const [g, body] of bodies) {
    if (body.isSleeping()) continue;
    const t = body.translation();
    const r = body.rotation();
    g.cur.set(t.x, t.y, t.z);
    _q.set(r.x, r.y, r.z, r.w);
    _m.compose(g.cur, _q, _s);
    g._mesh.setMatrixAt(g._i, _m);
  }
  for (const m of meshes) m.instanceMatrix.needsUpdate = true;

  for (const part of ragdoll) {
    if (!ragdollLive || part.body.isSleeping()) continue;
    const t = part.body.translation();
    const r = part.body.rotation();
    part.mesh.position.set(t.x, t.y, t.z);
    part.mesh.quaternion.set(r.x, r.y, r.z, r.w);
  }
  if (chairBody) {
    const t = chairBody.translation();
    const r = chairBody.rotation();
    _q.set(r.x, r.y, r.z, r.w);
    figure.quaternion.copy(_q).multiply(_chairYaw);
    // the figure's origin is the chair's base — rotate the offset with the body
    _off.set(0, -4.3, 0).applyQuaternion(_q);
    figure.position.set(t.x + _off.x, t.y + _off.y, t.z + _off.z);
  }

  // hover comes back once the pile has gone quiet
  if (!S.rainSettled && spawnQueue.length === 0) {
    settleTimer -= dt;
    if (settleTimer <= 0) {
      settleTimer = 0.5;
      let asleep = 0;
      for (const body of bodies.values()) if (body.isSleeping()) asleep++;
      if (asleep / bodies.size > 0.95) S.rainSettled = true;
    }
  }
}

// Hand the transforms back to the tween system. Called by the Restack
// button (which then re-applies the current mode) and by preApplyHooks
// when any mode button is clicked mid-rain.
export function teardownRain() {
  if (!S.rainActive) return;
  for (const [g, body] of bodies) {
    const r = body.rotation();
    g.fromQuat = new THREE.Quaternion(r.x, r.y, r.z, r.w);
    g.curScale = 1;
  }
  // cases never spawned still hang in the park zone with a written-in random
  // orientation; seed the same quat so their tween starts where they look
  for (const g of spawnQueue) {
    _e.set(jitter(g.id, 12) * 1.2, jitter(g.id, 13) * Math.PI, jitter(g.id, 14) * 1.2);
    g.fromQuat = new THREE.Quaternion().setFromEuler(_e);
  }
  world.free();
  world = null;
  bodies = null;
  chairBody = null;
  spawnQueue = [];
  for (const part of ragdoll) {
    scene.remove(part.mesh);
    part.mesh.geometry.dispose();
  }
  ragdoll = [];
  figure.rotation.set(0, Math.PI * 0.3, 0);   // layouts only set its position
  figureLabel.userData.gated = false;
  S.rainActive = false;
  S.rainSettled = false;
}

export function restack() {
  if (!S.rainActive) return;
  const key = S.currentModeKey;
  applyMode(key);   // preApplyHooks runs teardownRain first
}

preApplyHooks.push(() => teardownRain());
