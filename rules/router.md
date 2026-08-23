# Model routing map

Who does what in a multi-model session. The always-on rules live in
`CLAUDE.md` ("Model orchestration"); the per-model payloads live in
`rules/model-postures.md`; the dispatchable agents live in
`.claude/agents/`.

| Work | Route to | Effort |
|------|----------|--------|
| Requirements, judgment, spec-writing, integration, final verification | Main session (Fable 5) | high |
| Bounded, difficult implementation against a written spec | `opus-executor` agent | high |
| Fan-out needing per-item judgment: blind reader panels, audits, workspace sweeps | `sonnet-worker` agent | inherit |
| Bounded mechanical reads and transforms, data pulls, extraction | `haiku-worker` agent | model default |

## Dispatch rules

- The main session writes the spec before dispatching `opus-executor` and
  verifies the returned work against that spec before integrating it. The
  executor executes; it does not renegotiate scope.
- Every dispatch brief states: the exact delta, scope, expected output,
  stopping condition, and exclusions. A brief missing any of these is not
  ready to send.
- Workers return extracted key numbers and paths, never raw dumps.
- No recursive delegation: dispatched workers do not spawn further
  subagents.
- Escalation goes up, not sideways: a worker that hits something outside
  its brief reports it and stops — it does not fix it, and it does not
  re-scope itself.
