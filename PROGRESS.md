# PROGRESS — blocker channel between the agent and Dave

This is the only file Dave writes into to steer the build. The agent appends
`BLOCKED:` entries when it needs something only Dave can supply; Dave flips them
to `UNBLOCKED:` once supplied. Linear build progress lives in `BUILD_STATE.md`,
not here — this file carries only blockers and their resolution.

See `CLAUDE.md` Chapter 2 for exactly when and how this file is read and written.
All timestamps are UTC. Newest entries are appended at the end. Resolved entries
are kept as historical record, never deleted.

## Format

```
BLOCKED: <NN-stepname> — <UTC timestamp>
  need:   <exactly what Dave must supply>
  where:  <exact path / command / .env key / BIOS field involved>
  detail: <one or two lines of context>
```

Dave clears a blocker by editing that block's first line in place:

```
UNBLOCKED: <NN-stepname> — <UTC timestamp Dave cleared it>
  supplied: <what he did — file in place, creds in .env, toggle set, etc.>
  need:   <original need, left for the record>
  where:  <original where, left for the record>
  detail: <original detail, left for the record>
```

## Entries

(none yet)
