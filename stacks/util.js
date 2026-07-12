// Shared constants and pure helpers. No imports, no DOM, no three.js.

export const CASE_W = 1.28, CASE_H = 1.92, CASE_D = 0.16;
export const STACK_MAX = 40;          // cases per stack before a pile spills sideways
export const SPACING_X = 1.75, SPACING_Z = 2.5;   // stack pitch inside a pile
export const GROUP_GAP = 3.2;         // gap between piles
export const TRANSITION = 1.0, STAGGER = 0.7;     // seconds
export const POP_LIFT = 1.4;
export const FLAT_TILT = -Math.PI / 2;   // lying flat, cover up; 0 = standing upright
export const M = 10;                  // scene units per meter (1 unit = 10 cm)

// Deterministic per-game jitter so piles look hand-stacked but stable.
export function jitter(id, salt) {
  let h = (id * 2654435761 + salt * 40503) >>> 0;
  h = ((h ^ (h >> 15)) * 2246822519) >>> 0;
  return ((h & 0xffff) / 0xffff) * 2 - 1;   // [-1, 1]
}

export const hours = (m) => m / 60;
export const fmtH = (m) => {
  const h = Math.round(hours(m));
  return h >= 1000 ? (h / 1000).toFixed(1).replace(/\.0$/, "") + "k h" : h + " h";
};

// A multi-platform game's case sits in the family it's played most on, but
// platform-pile hour labels report time on that family across the whole library.
export const famOf = (p) =>
  p.startsWith("switch") ? "nintendo"
  : p.startsWith("ps") || p === "psn" ? "sony"
  : p.startsWith("xbox") ? "xbox" : "pc";

export const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
