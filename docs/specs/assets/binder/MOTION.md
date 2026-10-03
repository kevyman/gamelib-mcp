# Motion spec for The Binder (candidate set, for demo and review)

Principles the demo must embody:
- Motion is feedback, orientation or continuity. Exactly ONE moment of
  delight per surface: the card being dealt onto the table. Everything else
  is quick and quiet.
- Compositor-only: animate `transform`, `opacity`, `clip-path` and
  `background-position` only. Never width/height/margin/top. No
  `backdrop-filter`. `will-change: transform` only on the element currently
  animating (on hover-capable devices only).
- Enter = ease-out (`cubic-bezier(.2,.8,.2,1)`), 320–420ms for the whole
  card, 160–220ms for parts. Exit = ease-in, shorter (120–160ms). Press
  feedback 80–100ms. Hover 160–200ms. Stagger 40ms per sibling, capped at
  6 siblings (later ones share the 6th delay).
- Nothing moves on first paint that would delay reading: the text is in
  place at t=0 (opacity 0→1 only), the card's rise is ≤12px, the whole deal
  completes in <600ms.
- `prefers-reduced-motion: reduce`: every movement becomes an opacity-only
  crossfade of the same duration (never remove the feedback entirely);
  shimmer/sheen/tilt/stagger are off; skeleton pulse is off.
- Touch-first: no effect depends on hover. Hover tilt and sheen exist only
  under `@media (hover:hover) and (pointer:fine)`.

## The moments (each is a demo panel)

M1 **Deal-in** (every card, once, when its tool-result arrives or the
skeleton is replaced): card `opacity 0→1`, `translateY(12px)→0`,
`rotate(-1deg)→0`, 380ms ease-out. Ribbon: 120ms after the card starts,
`scaleX(.6)→1` from its left notch with `opacity 0→1`, 220ms, overshoot
(`cubic-bezier(.34,1.4,.64,1)`) — a stamp, not a slide. Badge: pops
`scale(.6)→1` 200ms ease-out at +180ms. Pips: light up left→right, 40ms
apart (opacity + scale .6→1). Stat rows: the dotted leaders are drawn in
(`background-size` or `clip-path inset(0 100% 0 0)→inset(0)`) 240ms,
staggered 40ms — the one flourish that says "a stat block". Reduced motion:
card fades 380ms, ribbon/badge/pips fade with it, no stagger, no leader draw.

M2 **Grid deal** (discover grid): cards dealt in reading order, 40ms
stagger (cap 6), each with M1's card motion minus the ribbon/badge pops
(only the card body); the set line fades in first (0ms).

M3 **Press** (any tappable card or button): `scale(.985)` 90ms ease-out on
`:active`, back 160ms. On a card the specular line shifts 12px with it.
Buttons: primary darkens 6% in addition. Works on touch (`:active`).

M4 **Hover tilt + sheen** (pointer:fine only): the card tilts toward the
pointer up to ±6° (`perspective(900px) rotateX/rotateY`), the grain layer
stays, the specular line sweeps across (`background-position`) following
the pointer; `transition: transform 160ms ease-out` when the pointer
leaves. In the demo (no JS allowed) approximate with a fixed `:hover` tilt
of rotateY(-5deg) rotateX(3deg) + a 700ms sheen sweep keyframe once per
hover. In production it is pointer-tracked JS, throttled to rAF.

M5 **Skeleton → card** (every widget): the skeleton pulses 1.6s
(opacity .55↔1); when the result arrives the skeleton fades out 120ms and
the card deals in (M1) starting at +60ms — the overlap is what makes it feel
like one object resolving, not two swapped. Reduced motion: crossfade 240ms.

M6 **Verdict stamp emphasis** (eval only): after M1 completes, the ribbon
does ONE 2px settle (`translateY(-2px)→0`, 160ms) — the stamp landing. Skip
under reduced motion.

M7 **Meter fills** (price-gap bar if ever used; Steam chip meter; match bar
in strips): `scaleX(0)→1` from the left, 420ms ease-out, +120ms after M1.

Not animated, ever: text (no typewriter, no counting numbers), layout
reflow (disclosure opens via `grid-template-rows: 0fr→1fr` is OK: it is
the one layout animation allowed, 240ms), covers (no Ken Burns), the grain
(static), colours (no tier colour transitions).

Durations table: press 90/160 · hover 160 · part enter 180–200 · card
enter 300 (`cubic-bezier(.2,0,0,1)`; overshoot on the ribbon only) ·
stagger 40 between cards, 36 between parts inside a card (cap 6) · M1 at
rest by 500 (ribbon +100/200, badge +140/180, pips +160 at 36 apart,
leaders +80/200, settle +340/140) · meter fill 300 · exit 120–160, ease-out
(never ease-in) · skeleton pulse 1600 · disclosure 240 · sheen sweep 700.
