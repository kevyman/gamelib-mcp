// Free-fly through the galaxy: pointer-lock mouse look + WASD along the
// sight line (space/C for up/down, shift to boost). Unlike walk mode there
// is no ground, no collision — just drifting between the nebula islands.

import * as THREE from "three";
import { PointerLockControls } from "../vendor/PointerLockControls.js";
import { camera, controls, renderer } from "../scene.js";
import { setHovered, pickGame } from "./orbit.js";
import { endFlythrough } from "../flythrough.js";
import { S } from "../state.js";

const SPEED = 55;            // units/s — the charted galaxy is ~140 across
const BOOST = 3;
const BOUND_R = 420;         // soft cage so nobody gets lost in the void
const CENTER = new THREE.Vector2(0, 0);   // crosshair NDC

const keys = new Set();
let plc = null;              // PointerLockControls, lazy (separate from walk's)
let headless = false;        // ?fly=1 debug: no pointer lock

const crosshair = document.getElementById("crosshair");
const hint = document.getElementById("hint");
const card = document.getElementById("card");
let savedHint = "";

export function enterFly(opts = {}) {
  if (S.flying || S.walking || S.rainActive) return;
  endFlythrough(false);      // a label fly-to shouldn't fight the stick
  headless = !!opts.headless;
  S.flying = true;

  controls.enabled = false;
  crosshair.style.display = "";
  savedHint = hint.textContent;
  hint.textContent =
    "WASD fly · space / C up and down · shift boost · mouse to look · ESC returns to orbit";

  if (!headless) {
    if (!plc) {
      plc = new PointerLockControls(camera, renderer.domElement);
      plc.addEventListener("unlock", () => exitFly());
    }
    plc.lock();
  }
}

export function exitFly() {
  if (!S.flying) return;
  S.flying = false;
  keys.clear();
  if (plc?.isLocked) plc.unlock();
  crosshair.style.display = "none";
  hint.textContent = savedHint;
  setHovered(null);

  // hand back to orbit, continuing from wherever the flight ended: park the
  // orbit target a little ahead so the camera doesn't snap anywhere
  const dir = camera.getWorldDirection(new THREE.Vector3());
  controls.target.copy(camera.position).addScaledVector(dir, 40);
  controls.enabled = true;
}

addEventListener("keydown", (e) => {
  if (!S.flying) return;
  keys.add(e.code);
  // pointer-lock unlock is the normal exit; fallback for headless/lock failure
  if (e.code === "Escape" && !plc?.isLocked) exitFly();
});
addEventListener("keyup", (e) => keys.delete(e.code));

const _dir = new THREE.Vector3();
const _right = new THREE.Vector3();

export function stepFly(dt) {
  if (plc?.isLocked || headless) {
    const boost = keys.has("ShiftLeft") || keys.has("ShiftRight") ? BOOST : 1;
    const d = SPEED * boost * dt;
    camera.getWorldDirection(_dir);
    _right.crossVectors(_dir, camera.up).normalize();
    if (keys.has("KeyW") || keys.has("ArrowUp")) camera.position.addScaledVector(_dir, d);
    if (keys.has("KeyS") || keys.has("ArrowDown")) camera.position.addScaledVector(_dir, -d);
    if (keys.has("KeyA") || keys.has("ArrowLeft")) camera.position.addScaledVector(_right, -d);
    if (keys.has("KeyD") || keys.has("ArrowRight")) camera.position.addScaledVector(_right, d);
    if (keys.has("Space")) camera.position.y += d;
    if (keys.has("KeyC")) camera.position.y -= d;

    const r = Math.hypot(camera.position.x, camera.position.z);
    if (r > BOUND_R) {
      camera.position.x *= BOUND_R / r;
      camera.position.z *= BOUND_R / r;
    }
    camera.position.y = Math.min(Math.max(camera.position.y, 2), BOUND_R);
  }

  // crosshair peek, same grammar as walk mode: the case under the reticle
  // pops via the existing aGlow channel and the card parks bottom-right
  if (!S.animating) setHovered(pickGame(CENTER));
  if (S.hovered) {
    card.style.left = innerWidth - 372 + "px";
    card.style.top = innerHeight - 240 + "px";
  }
}
