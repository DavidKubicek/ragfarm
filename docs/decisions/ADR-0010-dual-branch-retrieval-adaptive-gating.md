# ADR-0010 — Dual-branch retrieval: LightRAG traversal branch + adaptive score-gating
Author: David Kubicek (david.kubicek@eywo.cz)

Status: ACCEPTED (2026-07-30) for the *architecture*; two parts carry tracked
implementation debt (below). Three decisions are accepted and binding:
(1) the candidate cut becomes an **empirically-calibrated absolute floor** plus a
**Kneedle adaptive escape hatch**, replacing the placeholder `RAG_MIN_SCORE=0.0`
left open by ADR-0008; (2) a **second, parallel LightRAG graph-traversal branch**
joins the existing hybrid branch, fused *late* (both candidate streams into the one
cross-encoder) with **no pre-retrieval router**; (3) candidate selection is
**per-branch** — structured/sparse hits are gated by the floor only and never
diversity-pruned (MMR stays retired per ADR-0008). Pending, and why this ADR still
names open items rather than being closed: the floor's *numeric* value must be read
off labelled dumps (calibration method below, blocked on volume + the prod box), and
the LightRAG branch is *specified here but not yet built*. Promote those to done as
they land; the decisions themselves are not reopened.
Date: 2026-07-30
Builds on: ADR-0002 (BGE-M3 dense+sparse embedder, CPU host on :8090), ADR-0003
(rag-retrieval owns corpus RAG in the MCP layer — the durable, HW-agnostic tool
plane), ADR-0007 (section-aware chunking + broad-in/narrow-out; **supersedes its §2
MMR selection**, already retired by 0008), ADR-0008 (cross-encoder rerank replaced
MMR; **this ADR closes its open `RAG_MIN_SCORE` calibration item** and extends the
single-branch pool into two branches).
Scope: the **candidate-selection policy** in `services/rag-retrieval/server.py`
(the cut after rerank) and a **new retrieval branch** (LightRAG-style graph
traversal) fused into the existing RRF pool before the cross-encoder. Chunking
(`ingester.py`), the embedder (`:8090/embed`), the reranker service
(`:8081/reranking`), the manifest/sync layer (ADR-0006), and Qdrant's collection
schema are untouched except for the additive graph store the traversal branch reads.

## Context

The pipeline today (post-ADR-0008, verified in `server.py`) is single-branch:

```
query
  → embed (dense + sparse)                          :8090/embed
  → Qdrant hybrid Query API, TWO prefetch branches   :6333
      dense (semantic) + sparse (exact token) → RRF fusion
  → cross-encoder rerank of the fused pool           :8081/reranking
      (bge-reranker-v2-m3; RAG_USE_RERANKER=1)
  → drop < RAG_MIN_SCORE, take top-k                  (RAG_MIN_SCORE=0.0 today)
  → same-section window expansion                     (RAG_EXPAND_*)
  → verbatim text_raw + provenance → model
```

Two gaps remain after 0008, and they are the whole subject of this ADR.

**Gap 1 — the cut is a placeholder.** `RAG_MIN_SCORE=0.0` keeps everything; the
top-k bound is the only thing trimming the tail. ADR-0008 left the floor at 0.0
explicitly "until calibrated on real dumps", and noted the separation is real
(signal ≈ 0.13–0.95 vs junk ≈ 0.002). This is the same context-blowup lever we keep
circling: a large candidate pool with a long low-relevance tail is exactly what
overflows the model's window over a multi-turn conversation. The cut is the cheapest,
highest-leverage control we have, and it is currently off.

The reason a *fixed* floor can't just be hard-coded is visible in the real corpus.
For the firewall-rules tables, a query like a host FQDN returns forward **and**
reverse rules (source↔dest swapped) plus the same host-pair on a second port. Those
rows are textually near-identical, so the cross-encoder scores the second, third,
fourth occurrences *lower* even though every one of them is an operationally required
fact. Observed on real table queries: required rows spread from ~0.97 down to ~0.56,
then a hard cliff to junk at ~0.2 and ~0.03. A naïve floor at 0.7 would silently drop
required reverse rules; a floor at 0.05 keeps junk. The floor must be calibrated, not
guessed, and it interacts with the reverse-rule penalty in a way that pushes the true
boundary *lower* than intuition suggests.

**Gap 2 — similarity retrieval structurally cannot answer traversal questions.**
Vector + sparse retrieval returns documents that *resemble* the query. A whole class
of operator questions is answered only by documents *connected to* the answer, which
often share no vocabulary with the query and are therefore invisible to similarity
search — not ranked low, **not retrieved at all**. Worked example on the real corpus
below.

### Worked example — why the graph branch (three real-corpus docs)

- **Doc A — FW-rules row** (a real row shape): `leadb229p.lea.piz → EPC_AZURE,
  port 445`, requester `petr.pyszko@epcmmodities.cz`.
- **Doc B — host inventory / CMDB note**: "leadb229p.lea.piz — PostgreSQL host in
  the ENDUR cluster; owner team DataPlatform; criticality high; rack R12."
- **Doc C — change policy**: "ENDUR cluster changes on high-criticality hosts
  require CAB approval; DataPlatform lead Jana Nováková."

Point lookups the current pipeline already nails: "what rules did petr submit?" → Doc
A (sparse). "what is leadb229p?" → Doc B (dense/sparse). Both single-hop, both fine.

The traversal question it **cannot** answer: *"whose approval do I need to change a
firewall rule petr requested that touches the ENDUR database host?"* The answer lives
in no single chunk. It is assembled by walking `petr's rule → leadb229p` (A) →
`leadb229p → ENDUR, high-criticality` (B) → `ENDUR + high-crit → CAB, Jana` (C). The
query contains "approval" and "petr", so similarity retrieves A and C — but **never
B**, and B is the bridge. B shares no vocabulary with the query (a host-inventory row
about PostgreSQL and rack R12 says nothing about "approval" or "petr"), so it is
invisible to similarity search *precisely because it is the connective tissue rather
than the topical match*. Without B the model answers from A+C and gives a confidently
incomplete answer — the worst failure mode for infra work.

The second, more operationally important shape is the **completeness question**:
*"which teams are affected if we take down rack R12?"* Top-k similarity returns *some*
R12 mentions ranked by resemblance; it offers no guarantee it found them **all**, and
a blast-radius answer that misses a host is worse than useless. Graph traversal
returns every node reachable from R12 (all hosts → their clusters → owning teams).
"the k most similar" is not "all connected" — and infra questions shaped like *which
/ all / every / affected-by / depends-on / who-approves / downstream* are
traversal-shaped by nature.

Why this is cheap *for this corpus specifically*: the FW-rules data is already
relational. The columns **are** typed edges — `source_host → dest_host`,
`requester → rule`, `rule → network_name`. A graph over the structured rows is built
**deterministically from the table cells**, no LLM entity-extraction pass required
for the table portion; extraction is only needed for the prose docs (inventory,
incident notes). The corpus is a graph in disguise, which is what makes a parallel
traversal branch low-cost here where it would be expensive on a pile of unstructured
PDFs.

## Decision

### 1. Adaptive candidate cut: empirical floor (workhorse) + Kneedle hatch

Replace the single `RAG_MIN_SCORE` compare with a two-stage cut, applied **after**
the cross-encoder, **before** window expansion:

1. **Absolute floor — the workhorse.** Drop every reranked hit below a calibrated
   `RAG_MIN_SCORE`. This handles ~90 % of queries by itself. From the observed
   distributions a floor near **0.35–0.45** keeps the required reverse-direction FW
   rows (which land low) while removing the ~0.2 / ~0.03 junk — but the value is set
   by calibration (below), not asserted here.
2. **Kneedle adaptive hatch — armed only when needed.** *Only* when the post-floor
   survivor set still exceeds `RAG_GATE_MIN_SET` (e.g. > 12) **and** a sharp knee
   exists, cut at the point of maximum curvature of the sorted-score curve
   (Kneedle). This catches the "still huge, with an obvious 0.3→0.03-style collapse"
   case the flat floor leaves behind. Because it runs *after* the floor, it only ever
   sees already-relevant candidates, so the knee is unambiguous when present and is
   simply ignored (weak knee) when absent.

**Kneedle, not Otsu.** Otsu minimises intra-class variance assuming a cleanly
**bimodal** distribution — perfect for image histograms, wrong for us: flat semantic
queries produce a single smeared mode, and Otsu forced onto a unimodal set invents a
split that isn't there. Kneedle finds maximum curvature and degrades gracefully to
"no strong knee" on flat curves, where the `> RAG_GATE_MIN_SET` guard then leaves the
set alone. Interpretable, defensible, and appropriate to the actual score shapes.

Floor first is what makes the hatch safe: neither absolute-gap nor ratio-gap
detection is robust alone on raw scores (absolute-gap over-keeps flat tails;
ratio-gap wrongly keeps a 0.2 next to a 0.03). Removing junk *before* looking for a
knee removes both failure modes.

### 2. Second retrieval branch: LightRAG-style graph traversal, fused late

Add a parallel **traversal branch** beside the existing hybrid branch. Both feed the
**same** cross-encoder; there is **no pre-retrieval router**.

- **No router — deliberate.** A router that picks "similarity *or* graph" upfront is
  a hard decision point that can be wrong and is hard to calibrate, and it adds a
  failure mode and a latency hop. Running both branches and letting the cross-encoder
  arbitrate across the merged candidate stream makes the reranker — which already
  does cross-source semantic scoring — the judge. Worst case is a few extra pairs to
  score (linear, cheap on CUDA), **not** a wrong routing decision. This is ensemble
  retrieval with late fusion; it composes with the RRF → cross-encoder pool we
  already run.
- **Graph construction.** Structured rows (FW rules, tables) build a graph
  deterministically from typed columns — no LLM pass. Prose docs contribute
  entities/relations via LightRAG-style extraction. The graph store is **additive**
  and read-only at query time; it does not touch the Qdrant collection schema or the
  manifest.
- **Traversal at query time.** Resolve query entities → walk edges (bounded hop
  depth) → collect reachable chunks → hand them into the fused candidate pool
  alongside the hybrid hits, de-duplicated by point id, then rerank the union.
- **Cost is bounded by the cut in §1.** More branches ⇒ bigger pool ⇒ more rerank
  cost — which is exactly the context/latency pressure the adaptive cut controls. Cap
  the traversal branch's contribution (`RAG_LIGHTRAG_MAX`, hop depth) so the union
  handed to the reranker stays within the same order as today's `RAG_CANDIDATES=40`.

### 3. Per-branch selection policy

Selection differs by candidate origin, because "redundant-looking" means different
things per source:

- **Structured / sparse hits: floor only, never diversity-pruned.** Forward+reverse
  FW rules and same-host-different-port rules are textually near-identical but are
  **distinct required facts**. MMR (already retired, ADR-0008) would evict them as
  redundant; the cross-encoder keeps each on its own merit; the §1 floor is the only
  cut applied. This is the codification of ADR-0007/0008's "list all X" lesson.
- **Prose / dense hits: floor, and diversity is *permissible* here** — a near-
  duplicate paragraph genuinely is redundant. We do **not** re-introduce MMR by
  default (0008 stands); if a diversity step ever returns it lives on the prose
  branch only, never on structured rows. Recorded as the boundary, not switched on.
- **Traversal hits: floor only.** A graph-reached bridge doc (Doc B above) may score
  modestly on direct query relevance yet be structurally essential; the floor keeps
  it, and its *reason for inclusion* is the edge, not the score. (Open question 3.)

### New / changed env knobs (`services/rag-retrieval/server.py`, `.env.example`)

| var | default | meaning |
|-----|---------|---------|
| `RAG_MIN_SCORE` | **calibrated** (was 0.0) | absolute reranker-score floor; workhorse cut |
| `RAG_GATE_KNEEDLE` | `1` | enable the Kneedle adaptive hatch |
| `RAG_GATE_MIN_SET` | `12` | arm Kneedle only when post-floor survivors exceed this |
| `RAG_LIGHTRAG` | `0` → `1` when built | enable the traversal branch (off until implemented) |
| `RAG_LIGHTRAG_MAX` | `20` | max traversal-branch candidates folded into the pool |
| `RAG_LIGHTRAG_HOPS` | `2` | bounded traversal depth |

Everything defaults to *today's behaviour with a calibrated floor*; the traversal
branch is dark (`RAG_LIGHTRAG=0`) until §2 lands, so shipping the gating change does
not wait on LightRAG.

## Calibration method (how the floor's number gets set)

Not guessed — read off data, the same discipline ADR-0008 named:

1. Over a batch of real Czech/English queries (tables **and** prose, deliberately
   including FW-rule FQDN queries that trigger reverse rules), dump **every**
   candidate with its cross-encoder score to CSV via the existing per-stage
   `_timing_ms`/dump path (extend the dump to emit per-candidate score + text head).
2. Hand-label each candidate required / junk.
3. Plot score vs label. The floor is the highest value that keeps all *required*
   (including reverse-rule) rows while cutting junk. Expectation from observed
   spreads: **~0.35–0.45**, lower than intuition because of the reverse-rule penalty.
4. Set `RAG_MIN_SCORE` to that value; re-run the batch; confirm no required row is
   lost and the junk tail is gone.

Blocked on: enough accumulated real queries + the prod box (CUDA rerank makes a wide
sweep cheap — ADR-0008's ~1.9 s iGPU rerank becomes ~0.2 s, so calibrating over
hundreds of queries is minutes, not an afternoon).

## Consequences

Positive:
- The cut finally does work: the low-relevance tail stops entering context, directly
  attacking multi-turn context blowup at its cheapest lever.
- Required same-template rows (reverse FW rules, "list all X") survive, because the
  floor is calibrated around them and structured rows are never diversity-pruned.
- Traversal/completeness questions ("who approves…", "blast radius of rack R12")
  become answerable — a capability similarity retrieval structurally lacks — at the
  cost of one extra branch, not a re-architecture, because fusion is late and the
  reranker already arbitrates.
- The graph is nearly free to build over the table corpus (columns = typed edges).

Negative / cost:
- Rerank cost is linear in pool size; the traversal branch enlarges the pool. Bounded
  by `RAG_LIGHTRAG_MAX` + hop depth + the §1 cut; trivial once rerank is on CUDA,
  watch it while still on the iGPU.
- A new graph store to build, sync, and keep consistent with the manifest's
  content-addressed corpus (open question 2). Additive, but it is new surface.
- Kneedle adds a small, well-understood computation on the sorted scores; negligible.

Neutral / open:
- Floor value pending calibration (above). Ship `RAG_GATE_*` now; set the number when
  dumps exist.
- LightRAG branch specified, not built; `RAG_LIGHTRAG=0` keeps it dark meanwhile.

## Open questions

1. **Graph extraction for prose.** Table edges are deterministic; prose entities need
   LightRAG-style extraction whose quality (and Czech handling) needs its own eval on
   the real inventory/incident docs before the branch is trusted for prose bridges.
2. **Graph store ↔ manifest consistency.** The traversal store must stay in step with
   ADR-0006's content-addressed alias switch (rebuild the graph on `--recreate`;
   incrementally patch it on in-place sync). Design it to ride the manifest, not
   drift beside it.
3. **Scoring a graph-reached bridge doc.** Doc B is essential *because of the edge*,
   yet may score modestly on direct query relevance. Does it enter the pool with its
   raw cross-encoder score (and risk the floor), or with an edge-derived boost? Decide
   during implementation; leaning toward "traversal hits bypass the floor but are
   capped in count", so the bridge is never silently gated out.
4. **Whether prose diversity ever returns.** Left as a permissible prose-only step,
   switched off; only revisit if prose near-duplicates measurably pollute context.

## References
- `services/rag-retrieval/server.py` — pipeline + `_rerank`, `_timing_ms`, the cut.
- `scripts/rag_pool_inspect.py` — candidate-pool inspection (extend for the dump).
- ADR-0007 (chunking + broad-in/narrow-out; MMR learned-failure appendix),
  ADR-0008 (cross-encoder; the `RAG_MIN_SCORE` open item this ADR closes).
- `.env.example` — RAG knobs (add the `RAG_GATE_*` / `RAG_LIGHTRAG_*` block).
- LightRAG (graph-augmented retrieval) as the traversal-branch reference design.
