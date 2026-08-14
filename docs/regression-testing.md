# Prompt regression testing

## Why this exists

The system prompt is the entire behavioural specification of this assistant.
Everything the model does that we care about — when it reaches for a tool, how
much it retrieves, whether it answers with a table or a sentence, whether it
admits a miss instead of inventing — is defined in prose in one string.

That string is **fidgety**. A change that reads as local to RULE 3 shifts how
RULE 1 is obeyed. Every prompt defect found in August was of that shape:

- "call the tool at most once, do not refine" was read as *be minimal*, and the
  model set `k=1`, starved its own retrieval and answered from one wrong row
- "show records as a full table" outranked the user's "only him", so a question
  about one person returned the whole team
- a coding rule demanding interpreter verification made the model loop on "the
  user wants JavaScript, which is not Python. Wait, this is a problem."

None of those were model defects. All of them were found by a human noticing a
bad answer, usually late. This suite makes the blast radius of a prompt edit
visible in one command, before a demo does it for you.

## The library is the suite

`docs/prompts.md` is the single source. It is the human demo script *and* the
test corpus, because two copies would drift. Each case carries the prompt
verbatim and an `expect` block describing what a correct answer must achieve.
See the top of that file for how to add one.

```bash
.venv/bin/python tests/promptlib.py --validate   # lint the library
.venv/bin/python tests/promptlib.py --list       # ids, titles, tags
```

## Running it

```bash
source scripts/proxy-env.sh
.venv/bin/python scripts/test_regressions.py                 # everything
.venv/bin/python scripts/test_regressions.py --tag rag       # one slice
.venv/bin/python scripts/test_regressions.py --id R1 --id R7 # named cases
```

Exit status is the worst verdict seen, so it composes into other scripts:

| RC | verdict | meaning |
|----|---------|---------|
| 0 | `EQUAL` | every case achieves what it should |
| 1 | `DRIFTING` | something slipped; still useful answers |
| 2 | `WRONG` | at least one case failed outright |
| 3 | — | harness error: stack down, library unparsable |

Every run writes `logs/regressions-<UTC>.json` (the record to diff between
prompt versions — that is the whole point) and a matching `.log` with the full
prompts, answers and timings.

## Two phases, and why they are separate

**Collect** replays each prompt against the live slot, mimicking Open WebUI as
closely as possible: the preset's own system prompt, the preset's own sampler,
the same mcpo tools. The expectations in `prompts.md` were written from OWUI
chats, so the replay has to come from the same shape of environment or the
comparison is unfair. The preset and sampler are read out of
`setup_openwebui.py` rather than restated, because a second copy of the sampler
values is exactly how a harness drifts from the deployment it is meant to test.

**Judge** compares each answer to the case's `expect` with a second model call,
a comparator system prompt, and **no tools at all**. That last part is
deliberate. The judge's job is to reason over two given texts. Give it retrieval
and it goes and checks for itself, then grades the answer against what *it*
found rather than against `expect` — which quietly turns a regression suite into
a second opinion.

Deterministic checks run **before** the judge: `must` / `must-not` substrings,
empty answers, and whether the expected tool was called. A check that can fail
hard for free should never be delegated to a model.

## Verdicts

- **EQUAL** — achieves everything `expect` describes. Wording, ordering and
  formatting may differ completely; sampling is non-greedy, so they always do.
- **DRIFTING** — substantially right, but a required column, field or list item
  is missing, or the format slipped, or unrequested commentary buried the answer.
- **WRONG** — contradicts `expect`, is empty or truncated, refuses something it
  should do, answers from general knowledge where a lookup was required, invents
  a record, or narrates a tool call instead of making one.

## Calibrating the judge

An LLM judge is a measuring instrument and needs checking like one. Two habits:

**Read the reasons, not just the verdicts.** The judge writes at most three
sentences saying what is missing. On the first real run it returned:

> R1 DRIFTING — ACTUAL includes extra columns not specified in EXPECTED. The
> table has only five rows instead of "around six".

The row count is a fair catch. The extra columns are the judge being stricter
than the comparator prompt asks for — extra correct detail is supposed to be
EQUAL. That is a calibration note, not a code change, and it is the kind of
thing only reading the reasons will surface.

**A failing case may mean the expectation is stale, not the model.** Also on the
first run, R8 (`Kdo je prezident USA?`) was marked WRONG for not calling
`search_corpus`. The July prompt made every question route through retrieval;
the current RULE 0 has a carve-out that makes general-knowledge questions
tool-forbidden. So the model did what today's prompt says, and the case encodes
yesterday's. **Decide which behaviour you want, then change one of them** — do
not quietly relax the case, because a suite that is edited to stay green is
worse than no suite.

## What is not covered yet

- **Image cases.** V1–V7 and D3/D4 need image input; the collector's text path
  cannot send an attachment, so they are skipped with a note rather than
  silently passed.
- **Multi-turn.** Every case is a fresh conversation. Context-dependent drift —
  the kind that appears only after a model switch mid-chat — is not tested.
- **Statistical power.** One run per case. Sampling is non-greedy, so a single
  DRIFTING may be a draw rather than a trend. Re-run before believing it.

## See also

- `docs/prompts.md` — the library, and how to add a case
- `tests/promptlib.py --validate` — the linter
- `scripts/agent.py --help` — the harness both phases drive
- `docs/measurements/` — the raw numbers behind the August prompt fixes
