// Keyframed camera flythrough. Generic: Monolith uses it for the reveal climb
// and the walkable library can reuse it for entry moves.

import * as THREE from "three";
import { camera, controls, modeObjects, labelSprites } from "./scene.js";
import { ease } from "./util.js";

let fly = null;   // { keys, dur, t }

export const isFlying = () => fly !== null;

export function flythrough(keys, duration) {
  controls.enabled = false;
  fly = { keys, dur: duration, t: 0 };
}

export function endFlythrough(snapToEnd = true) {
  if (!fly) return;
  if (snapToEnd) {
    const last = fly.keys[fly.keys.length - 1];
    camera.position.copy(last.pos);
    controls.target.copy(last.target);
  }
  fly = null;
  controls.enabled = true;
  // whatever the flythrough hadn't revealed yet shows now (ESC skip included)
  for (const o of [...modeObjects, ...labelSprites]) {
    if (o.userData.gated) {
      o.userData.gated = false;
      o.visible = true;
    }
  }
}

const _p = new THREE.Vector3(), _t = new THREE.Vector3();
export function stepFlythrough(dt) {
  fly.t = Math.min(1, fly.t + dt / fly.dur);
  const k = ease(fly.t);
  const u = k * (fly.keys.length - 1);
  const i = Math.min(Math.floor(u), fly.keys.length - 2);
  const f = u - i;
  _p.lerpVectors(fly.keys[i].pos, fly.keys[i + 1].pos, f);
  _t.lerpVectors(fly.keys[i].target, fly.keys[i + 1].target, f);
  camera.position.copy(_p);
  controls.target.copy(_t);
  camera.lookAt(_t);
  // reveal height markers as the camera climbs past them; they stay revealed
  for (const o of [...modeObjects, ...labelSprites]) {
    if (o.userData.gated && camera.position.y > o.position.y - 2) {
      o.userData.gated = false;
      o.visible = true;
    }
  }
  if (fly.t >= 1) endFlythrough(false);
}

addEventListener("keydown", (e) => {
  if (e.key === "Escape") endFlythrough();   // skippable, never traps the camera
});
