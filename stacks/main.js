// The Stacks — sort-into-piles view of the game library.
// Every game is one jewel case: an InstancedMesh instance (one mesh per atlas
// sheet) whose front face samples the cover atlas and whose plastic is tinted
// by platform family. Sort modes retarget every case; positions tween there.
//
// Module map:
//   util.js       constants + pure helpers
//   state.js      cross-module mutable state (S)
//   data.js       library.json + atlases + HDRI + chair (top-level await)
//   scene.js      renderer/camera/controls/floor/labels/declutter
//   cases.js      instanced cases, shader, motion state, transition stepper
//   flythrough.js keyframed camera moves
//   layouts.js    MODES + pile/hours/monolith layouts + applyMode
//   modes/orbit.js  hover card, pop, click-to-explode

import * as THREE from "three";
import { games } from "./data.js";
import { renderer, scene, camera, controls, declutterLabels } from "./scene.js";
import { meshes, composeMatrix, setDust, snapAll, stepTransition } from "./cases.js";
import { MODES, applyMode, applyHooks } from "./layouts.js";
import { isFlying, stepFlythrough } from "./flythrough.js";
import { updatePicking, updatePop, explodeStack } from "./modes/orbit.js";
import { enterWalk, stepWalk } from "./modes/walk.js";
import { S } from "./state.js";
import { hours } from "./util.js";

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

// include-farmed toggle (Hours mode only): farmed titles' playtime is
// inflated by idling, so it's treated as unplayed unless opted in
const farmedBtn = document.createElement("button");
farmedBtn.textContent = "include farmed";
farmedBtn.title = "count playtime from farmed (idled) games";
farmedBtn.style.display = "none";
farmedBtn.onclick = () => {
  S.includeFarmed = !S.includeFarmed;
  farmedBtn.classList.toggle("active", S.includeFarmed);
  if (S.currentModeKey === "hours") applyMode("hours");   // re-layout
};
document.getElementById("opts").appendChild(farmedBtn);

// twin-towers toggle (Monolith mode only): played vs never played
const splitBtn = document.createElement("button");
splitBtn.textContent = "played vs unplayed";
splitBtn.title = "split the tower into played / never-played twin towers";
splitBtn.style.display = "none";
splitBtn.onclick = () => {
  S.splitMonolith = !S.splitMonolith;
  splitBtn.classList.toggle("active", S.splitMonolith);
  if (S.currentModeKey === "monolith") applyMode("monolith");
};
document.getElementById("opts").appendChild(splitBtn);

// first-person walk (desktop-only: pointer lock + WASD makes no sense on touch)
const walkBtn = document.createElement("button");
walkBtn.textContent = "Walk";
walkBtn.title = "walk the aisles in first person (WASD + mouse, ESC to leave)";
walkBtn.onclick = () => enterWalk();
if (matchMedia("(hover: none), (pointer: coarse)").matches)
  walkBtn.style.display = "none";
document.getElementById("opts").appendChild(walkBtn);

// dust toggle
const dustBtn = document.createElement("button");
dustBtn.textContent = "dust";
dustBtn.title = "grey film on never-played games";
dustBtn.onclick = () => {
  S.dustUserChoice = !S.dustEnabled;
  setDustUI(S.dustUserChoice);
};
document.getElementById("opts").appendChild(dustBtn);
function setDustUI(v) {
  setDust(v);
  dustBtn.classList.toggle("active", S.dustEnabled);
}

// per-mode UI state (button highlights, toggle visibility, dust default)
applyHooks.push((modeKey) => {
  // dust reads best against Playtime's played/unplayed piles; elsewhere it
  // defaults off — until the user clicks the toggle, whose choice then sticks
  setDustUI(S.dustUserChoice ?? (modeKey === "playtime" ? 1 : 0));
  farmedBtn.style.display = modeKey === "hours" ? "" : "none";
  splitBtn.style.display = modeKey === "monolith" ? "" : "none";
  for (const b of modeBtns) b.classList.toggle("active", b.dataset.mode === modeKey);
});

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

// ---------- loop ----------

const clock = new THREE.Clock();

function tick() {
  const dt = Math.min(clock.getDelta(), 0.05);
  if (S.walking) stepWalk(dt);
  else if (isFlying()) stepFlythrough(dt);
  else controls.update();

  if (S.animating) stepTransition(dt);

  if (S.walking) updatePop(dt);   // walk does its own crosshair picking
  else updatePicking(dt);
  declutterLabels();
  renderer.render(scene, camera);
}

// initial placement (sky scatter), then drop into platform piles
for (const g of games) composeMatrix(g);
for (const m of meshes) m.instanceMatrix.needsUpdate = true;
renderer.setAnimationLoop(tick);
document.getElementById("loading").classList.add("done");
const params = new URLSearchParams(location.search);
S.SNAP = params.get("snap") === "1";   // skip transitions (also: screenshots)
const startMode = params.get("mode");
if (params.has("dust")) S.dustUserChoice = params.get("dust") === "1";
applyMode(MODES[startMode] ? startMode : "platform");
if (S.SNAP && params.get("explode")) {   // deterministic explode, for screenshots
  const g = games.find((x) => x._stack && x._stack.length >= 20);
  if (g) { explodeStack(g._stack); snapAll(); }
}
if (params.get("walk") === "1") {   // deterministic walk view, for screenshots
  enterWalk({ headless: true });
  if (S.SNAP) snapAll();
}
