# Integration Control Plane Design

## PURPOSE

Add an explicit, operator-friendly setup and diagnostics surface for platform integrations in the deployed Docker app.

The system should answer:

- what is configured
- what is missing
- what is stale
- what is degraded
- what the operator should do next

The app must remain read-only with respect to secrets. It may inspect environment variables, mounted files/directories, installed binaries, and recent sync outcomes, but it must not persist or mutate secret material.

This design targets the current Hetzner Cloud + Docker Compose deployment model, while keeping room for future backend changes that remove reliance on external tools.

## GOALS

- Provide a clear setup and health view for Steam, Epic, GOG, Nintendo, and PSN.
- Expose the same integration status through both MCP/admin tools and HTTP endpoints.
- Distinguish between overall platform health and capability-level health.
- Give remediation steps that are specific to the active backend and deployment model.
- Avoid coupling the operator surface to one permanent backend implementation.

## NON-GOALS

- No in-app browser auth flow.
- No secret storage in the app or database.
- No automatic editing of `.env`, Compose files, or host-mounted files.
- No secret manager integration in the first version.
- No implementation planning in this document.

## CONTEXT

Current setup is split across `deploy.md`, `setup_platform.py`, environment variables, and manually copied host files. Startup sync historically attempted every supported platform, which created noisy logs for optional integrations. Epic, GOG, and Nintendo also depend on implementation-specific artifacts that are not currently surfaced clearly to the operator.

The current deployment target is a Hetzner VM running Docker Compose. The app already reads credentials and mounted files from inside the container, which makes inspection feasible without changing the deployment model.

## ARCHITECTURE

Introduce a small integration control plane inside the app with three layers:

1. Integration status model
2. Platform/backend inspectors
3. Delivery surfaces

### 1. Integration status model

Define a shared internal model returned by every inspector. This model is the source of truth for both MCP and HTTP responses.

Core fields:

- `platform`
- `overall_status`
- `active_backend`
- `summary`
- `capabilities`
- `checks`
- `required_inputs`
- `detected_inputs`
- `remediation_steps`
- `last_sync`

### 2. Platform/backend inspectors

Each platform implements an inspector that evaluates current runtime state without running a full sync. Inspectors read only local runtime facts:

- env var presence and basic shape
- mounted file/directory presence
- installed binary presence
- lightweight metadata structure
- recently recorded sync outcomes

Inspectors are platform-aware and backend-aware. A platform may have multiple possible backends, but only one active backend at a time in the initial version.

### 3. Delivery surfaces

Expose the control plane through:

- MCP/admin tools for automation and terminal use
- HTTP JSON endpoints for machine and human inspection
- a simple HTTP admin page built on top of the JSON response

All surfaces must be read-only.

## STATUS MODEL

Use explicit states rather than booleans.

Supported statuses:

- `unconfigured`
- `partially_configured`
- `ready`
- `degraded`
- `stale`
- `error`

Definitions:

- `unconfigured`: required inputs are absent
- `partially_configured`: some required inputs exist, but setup is incomplete
- `ready`: required runtime inputs are present and checks pass
- `degraded`: setup exists, but one or more capabilities are impaired
- `stale`: setup exists, but auth/session data is expired or likely invalid
- `error`: the inspector failed unexpectedly

Each platform should also report capability-level health. This matters when the platform can still provide some value while one capability is impaired. Example: Epic library ownership may be ready from cached metadata while playtime is stale.

## BACKEND MODEL

The operator surface must be backend-aware, not tool-aware-only.

Each platform status should include:

- `active_backend`
- backend-specific checks
- backend-specific remediation
- capabilities provided by that backend

Examples of backend identifiers:

- `steam-web-api`
- `legendary-cache`
- `lgogdownloader`
- `nxapi`
- `vgcs-cookie`
- `psnawp`

This preserves the operator contract when implementation details change later. If GOG or Nintendo moves away from external tools, the platform model stays stable and only the backend inspector changes.

## PLATFORM DIAGNOSTICS

### Steam

Backend: `steam-web-api`

Checks:

- `STEAM_API_KEY` present
- `STEAM_ID` present
- optional format sanity for `STEAM_ID`

Capabilities:

- ownership
- playtime

Typical states:

- `unconfigured` if required env vars are absent
- `ready` if required env vars are present

### Epic

Backend: `legendary-cache`

Checks:

- mounted Legendary path exists
- `user.json` exists
- `metadata/` exists
- `metadata/` contains readable entries
- token-related fields exist if needed for playtime
- recent sync/playtime errors indicate stale auth

Capabilities:

- ownership from cached metadata
- playtime from Epic auth/session

Typical states:

- `unconfigured` if mount or required files are missing
- `ready` if cache and auth look usable
- `degraded` if ownership is ready but playtime is impaired
- `stale` if refresh token rejection or token expiry is detected

Remediation examples:

- run `legendary auth`
- run `legendary list --force-refresh`
- confirm host path is mounted read-only into the container

### GOG

Backend: `lgogdownloader`

Checks:

- `lgogdownloader` binary exists in container
- mounted config dir exists
- expected auth/session files exist
- recent subprocess failures

Capabilities:

- ownership

Typical states:

- `unconfigured` if session mount is absent
- `degraded` if binary is missing inside the container
- `ready` if binary and session files are present
- `stale` if session files exist but recent auth failure indicates refresh/login needed

Important rule: missing binary must be reported as a runtime/backend problem, not an auth problem.

### Nintendo

Possible backends:

- `nxapi`
- `vgcs-cookie`

Checks:

- `NINTENDO_SESSION_TOKEN` present
- `nxapi` binary present if token mode is active
- cookie file exists and is readable for VGCS mode
- recent auth failures

Capabilities:

- `nxapi`: playtime, launched-title ownership approximation
- `vgcs-cookie`: ownership, no playtime

Typical states:

- `unconfigured` if neither mode is available
- `degraded` if token exists but `nxapi` binary is absent
- `ready` if one backend is correctly configured

The operator surface must clearly state which backend is active and what that backend can provide.

### PSN

Backend: `psnawp`

Checks:

- `PSN_NPSSO` present
- optional shape sanity
- recent auth rejection or refresh-required failures

Capabilities:

- ownership approximation via played titles
- playtime

Typical states:

- `unconfigured` if `PSN_NPSSO` is absent
- `ready` if token exists and no stale signal is present
- `stale` if recent auth failure indicates the token must be re-extracted

## OPERATOR SURFACES

### MCP/admin

Add at least one admin-facing tool:

- `get_integration_status(platforms: list[str] | None = None, verbose: bool = True) -> dict`

Optional follow-up tool if content grows too large:

- `get_setup_guide(platform: str) -> dict`

The tool response should mirror the HTTP JSON response and include remediation steps.

### HTTP JSON

Recommended endpoints:

- `GET /admin/integrations`
- `GET /admin/integrations/{platform}`

These endpoints return the shared status model and must remain read-only.

### HTTP UI

Recommended page:

- `GET /admin/integrations/ui`

This page should present:

- platform cards or rows
- overall status
- active backend
- capability health
- failing checks
- last sync status
- copyable remediation steps

The UI should be intentionally small and operational, not a full admin console.

## DATA FLOW

1. Caller hits MCP tool or HTTP endpoint.
2. Service asks the integration control plane for current status.
3. Control plane runs each requested platform inspector.
4. Inspectors collect runtime facts and recent sync state.
5. Shared model is returned.
6. MCP returns it directly; HTTP JSON returns it directly; HTTP UI renders it.

No write path exists in the first version.

## ERROR HANDLING

- Inspector failures must map to `error`, not crash the endpoint.
- Each inspector should return partial information if possible.
- Remediation steps should still be shown even when a check fails.
- Missing optional integrations should not emit alarming states if they were never configured.
- Stale auth and missing runtime dependencies should be differentiated explicitly.

## LAST SYNC AND STALENESS

Inspectors should include recent sync outcomes where available. This enables the operator surface to answer not just “what inputs exist?” but also “did the last real use of this backend succeed?”

Recommended fields:

- `last_attempt_at`
- `last_success_at`
- `last_error_summary`
- `last_error_classification`

`last_error_classification` should be normalized where possible:

- `auth_stale`
- `missing_runtime_dependency`
- `missing_configuration`
- `network`
- `unexpected`

This is especially important for Epic and PSN token freshness, and for GOG runtime packaging failures.

## RECOMMENDED FIRST IMPLEMENTATION SLICE

Build in this order:

1. Shared status model
2. Inspectors for Steam, Epic, GOG, Nintendo, PSN
3. MCP/admin tool
4. HTTP JSON endpoints
5. Simple HTTP UI

This order gives immediate operator value before any page styling work.

## TESTING STRATEGY

Tests should cover:

- inspector state classification for each platform
- backend selection and capability reporting
- stale vs degraded vs unconfigured distinctions
- MCP/admin response shape
- HTTP JSON response shape
- HTML page rendering for key states

Focus tests on deterministic local inspection behavior. Avoid live external API calls in inspector tests.

## TRADEOFFS

### Chosen approach

Build a read-only integration control plane with backend-aware inspectors and dual MCP/HTTP surfaces.

Why:

- matches the operator need for clear setup and refresh guidance
- fits Hetzner + Docker Compose cleanly
- avoids turning the app into a secret store
- survives future backend replacements

### Rejected alternative: thin status layer

Rejected because it would report too little. A simple configured/missing view does not explain stale auth, missing binaries, or partial capability loss.

### Rejected alternative: setup wizard that edits deployment artifacts

Rejected for the first version because it adds config-management complexity and creates more ways for deployment guidance to drift from the actual runtime.

## OPEN QUESTIONS RESOLVED

- Secret ownership: secrets remain outside the app
- Deployment model: optimize for Hetzner Cloud + Docker Compose
- Operator surfaces: provide both MCP/admin and HTTP
- Future backend changes: supported through backend-aware inspector design

## SUCCESS CRITERIA

- An operator can open one page or call one tool and see all platform setup states.
- The operator can tell whether a platform is missing config, stale, degraded, or ready.
- The operator can see exactly what to do next without consulting scattered docs.
- Startup/runtime logs become less important for basic setup diagnosis because the control plane explains the problem directly.
- The design remains valid if one or more integrations later stop depending on external tools.
