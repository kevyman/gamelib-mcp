// Cross-module mutable state. One flat singleton keeps the modules honest:
// everything that more than one module writes lives here, nothing else does.

export const S = {
  SNAP: false,             // ?snap=1 — skip transitions (screenshots)
  animating: false,        // a case transition is in flight
  transitionClock: 0,
  currentModeKey: null,
  exploded: null,          // { games: [...], home: Map } — spread-out stack
  hovered: null,
  includeFarmed: false,    // Hours mode: farmed playtime counts
  splitMonolith: false,    // Monolith mode: played vs never-played twin towers
  monolithFlown: false,    // fly the Monolith camera only on first entry
  dustEnabled: false,
  dustUserChoice: null,    // null = follow the mode default
  walking: false,          // first-person walk mode active
  flying: false,           // free-fly through the galaxy active
  rainActive: false,       // Rapier owns the case transforms
  rainSettled: false,      // >95 % of rain bodies are asleep
};
