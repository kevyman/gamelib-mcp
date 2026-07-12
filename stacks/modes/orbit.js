// Orbit-mode interactions: hover raycast + detail card, hover pop easing,
// and the click-to-explode stack wall.

import * as THREE from "three";
import { games, FAMILY, FAMILY_LABEL } from "../data.js";
import { camera, renderer } from "../scene.js";
import { meshes, setGlow } from "../cases.js";
import { S } from "../state.js";
import { CASE_W, CASE_H, fmtH } from "../util.js";

const card = document.getElementById("card");
const raycaster = new THREE.Raycaster();
export const mouse = new THREE.Vector2(-2, -2);
let mousePx = { x: 0, y: 0 };

addEventListener("pointermove", (e) => {
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  mousePx = { x: e.clientX, y: e.clientY };
});

// ---------- explode a stack ----------

export function explodeStack(stack) {
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

  S.exploded = { games: stack, home: new Map() };
  stack.forEach((g, i) => {
    S.exploded.home.set(g, { pos: g.to.clone(), yaw: g.toYaw, tilt: g.toTilt, scale: g.toScale });
    const cx = (i % cols) - (cols - 1) / 2;
    const cy = rows - 1 - Math.floor(i / cols);   // best game top-left
    g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
    g.fromScale = g.curScale; g.toScale = 1;      // wall reads at uniform size
    g.to.copy(center).addScaledVector(right, cx * sx).addScaledVector(out, 10);
    g.to.y = 2.2 + cy * sy + CASE_H / 2;
    g.toYaw = az;
    g.toTilt = 0;
    g.t = 0;
    g.delay = i * 0.012;
  });
  S.transitionClock = 0;
  S.animating = true;
}

export function collapseStack() {
  if (!S.exploded) return;
  S.exploded.games.forEach((g, i) => {
    const h = S.exploded.home.get(g);
    g.from.copy(g.cur); g.fromYaw = g.curYaw; g.fromTilt = g.curTilt;
    g.fromScale = g.curScale;
    g.to.copy(h.pos); g.toYaw = h.yaw; g.toTilt = h.tilt; g.toScale = h.scale;
    g.t = 0;
    g.delay = i * 0.008;
  });
  S.exploded = null;
  S.transitionClock = 0;
  S.animating = true;
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
  if (moved > 6 || S.animating || S.walking) return;   // walk has its own clicks
  mouse.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  raycaster.setFromCamera(mouse, camera);
  const hit = raycaster.intersectObjects(meshes, false)[0];
  const g = hit?.instanceId != null ? hit.object.userData.games[hit.instanceId] : null;
  if (!g) collapseStack();                                  // clicked empty space
  else if (S.exploded?.games.includes(g)) return;           // inside the wall
  else if (g._stack) explodeStack(g._stack);
});

// ---------- hover card ----------

export function showCard(g) {
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

export function hideCard() {
  card.classList.remove("show");
}

function placeCard() {
  const pad = 18;
  let x = mousePx.x + pad, y = mousePx.y + pad;
  if (x + 350 > innerWidth) x = mousePx.x - 350 - pad;
  if (y + 190 > innerHeight) y = mousePx.y - 190 - pad;
  card.style.left = x + "px";
  card.style.top = y + "px";
}

// Change the hovered game (shared with the walk mode's crosshair picking).
export function setHovered(g) {
  if (g === S.hovered) return;
  if (S.hovered) S.hovered.popT = 0;
  S.hovered = g;
  if (S.hovered) { S.hovered.popT = 1; showCard(S.hovered); }
  else hideCard();
}

// Raycast from an NDC point and return the game under it, if any.
export function pickGame(ndc) {
  raycaster.setFromCamera(ndc, camera);
  const hit = raycaster.intersectObjects(meshes, false)[0];
  return hit?.instanceId != null
    ? hit.object.userData.games[hit.instanceId] ?? null
    : null;
}

// Pop easing (hovered case rises, released cases settle) — drives the
// aGlow attribute only; instance matrices stay put for stable raycasting.
export function updatePop(dt) {
  for (const g of games) {
    const target = g.popT ?? 0;
    if (g.pop !== target) {
      const dir = Math.sign(target - g.pop);
      g.pop = Math.max(0, Math.min(1, g.pop + dir * dt * 7));
      setGlow(g, g.pop);
    }
  }
}

// Per-frame picking + pop easing; called from the main loop in orbit mode.
export function updatePicking(dt) {
  // hover raycast (skip during big transitions and while the rain is live —
  // matrices churn every frame until >95 % of the bodies sleep)
  if (!S.animating && !(S.rainActive && !S.rainSettled)) {
    setHovered(pickGame(mouse));
  } else if (S.hovered) {
    setHovered(null);
  }
  if (S.hovered) placeCard();

  updatePop(dt);
}
