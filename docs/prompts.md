# Prompt library — demo reference and regression suite

This file has two jobs and one format.

**For a human** it is the demo script: what to ask, what a good answer looks
like, and which of those answers has actually been seen rather than hoped for.

**For a machine** it is the regression suite. `tests/promptlib.py` parses every
case out of it, `scripts/test_regressions.py` replays them against the live slot
and an LLM judge decides whether today's answer still achieves what the
`expect` block describes. One file, so the demo script and the tests cannot
drift apart.

## How to add a case

Copy the shape below. Only the heading, `~~~prompt` and `~~~expect` are
required.

    ### X1 · short title

    - **tags:** rag, czech
    - **tools:** search_corpus
    - **status:** unverified

    ~~~prompt
    The prompt, verbatim, exactly as a user would type it.
    ~~~

    ~~~expect
    What a correct answer must achieve, in prose.
    ~~~

    ~~~must
    a-substring-that-must-appear
    ~~~

Rules worth knowing before you write one:

- **Fences are tildes**, not backticks, so a prompt or expectation can itself
  contain ```` ``` ```` blocks. The draw.io and code cases do.
- **`expect` is prose, never a golden string.** Sampling is non-greedy, so two
  correct answers differ in wording every run. Describe what the answer must
  *achieve*; the judge decides whether it did. A literal diff would fail every
  time and everyone would learn to ignore the suite.
- **`must` / `must-not` are for the deterministic part** — a hostname, an
  identifier, a figure. They are checked by plain string match before any model
  is called, because a cheap check that can fail hard should not be delegated to
  a judge.
- **`status:` is a claim about evidence.** Say which model and which date, or
  say `unverified`. Several cases below are verified only on models we no longer
  run; that is recorded rather than hidden.
- `skip:` with a reason keeps a case in the library but out of the run.

Validate after editing:

```bash
.venv/bin/python tests/promptlib.py --validate
```

Metadata keys: `preset`, `slot`, `tools`, `attach`, `status`, `tags`, `skip`.

---

## Section R — Retrieval and grounding

The core of the product. Every case here must call `search_corpus` and answer
only from what comes back.

> **Provenance warning.** R1-R6 were verified on 2026-07-27 against the
> *retired* text preset (Qwen2.5-7B Q4_K_M, greedy) and have README screenshots.
> They were not re-verified on the current 30B models except where the status
> line says so. That is exactly the drift this suite exists to catch.

### R1 · FW rules for one specific host

- **tags:** rag, czech, table
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-01); tool call re-verified 4/4 on 30B-A3B Instruct 2026-08-12

~~~prompt
Jaká jsou FW pravidla pro host leadb229p.lea.piz?
~~~

~~~expect
Calls search_corpus, then returns the firewall rules for that one host as a
markdown table with every column present: source and destination network
addresses, source and destination network names, destination ports, protocol.
Around six rows. Ends with a Source: line naming the xlsx it came from. Does not
dump rules for other hosts.
~~~

~~~must
leadb229p
~~~

### R2 · Contacts for project management

- **tags:** rag, czech, list
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-02)

~~~prompt
Dej mi kontakty na projektove vedeni v EPC.
~~~

~~~expect
Calls search_corpus and returns six people as a list, each with company,
role/area, phone and e-mail. A phone number is omitted only where it is genuinely
absent from the source. No invented contacts.
~~~

### R3 · Everything about one host

- **tags:** rag, czech, table
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-03)

~~~prompt
Co vis o hostu acclcass1?
~~~

~~~expect
Calls search_corpus and returns the full record for that host — on the order of
nineteen fields, including environment, OS, virtualisation, storage, vCPU, RAM,
HDD, support, hostnames, IP addresses, netmasks, VLAN id, filesystem and UID.
For this one host only.
~~~

~~~must
acclcass1
~~~

### R4 · Where credentials live

- **tags:** rag, czech
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-04)

~~~prompt
Kde ukládáme hesla pro EPC?
~~~

~~~expect
Calls search_corpus and explains that passwords are kept in NordPass, names the
record to look up, and says how to use it with the RDP client. Does not print any
actual credential.
~~~

~~~must-not
password:
~~~

### R5 · Where the documentation repo is

- **tags:** rag, czech
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-05)

~~~prompt
Kde máme uložené GIT repo s dokumentací?
~~~

~~~expect
Calls search_corpus and returns the exact Azure DevOps URL for the sa-hosting
repository, not a paraphrase of it.
~~~

~~~must
dev.azure.com
~~~

### R6 · How to log in from ŠA to EPC

- **tags:** rag, czech, procedure
- **tools:** search_corpus
- **status:** verified 2026-07-27 (text preset, screenshot ex-06)

~~~prompt
Jak se přihlásím ze ŠA do EPC?
~~~

~~~expect
Calls search_corpus and lays out the login paths as a numbered procedure — SSH
direct, RDP direct, terminal servers, GUI, reverse proxy, CLI — with hostnames,
IP addresses, where the credentials come from, and which spreadsheets hold the
details.
~~~

### R7 · All CutOver contacts

- **tags:** rag, czech, list
- **tools:** search_corpus
- **status:** tool call verified 4/4 on 30B-A3B Instruct and 30B-A3B Thinking, 2026-08-12

~~~prompt
Úplně všechny kontakty co máme v souvislosti s CutOver plánem?
~~~

~~~expect
Calls search_corpus and returns every person listed on the CutOver contact sheet,
with role and contact details. This is an overview question, so the full table is
the right answer rather than a single record.
~~~

~~~must
CutOver
~~~

### R8 · General knowledge is answered directly

- **tags:** general, czech, negative
- **status:** rewritten 2026-08-14 — the July expectation (route everything through search_corpus) contradicts today's RULE 0 carve-out

~~~prompt
Kdo je prezident USA?
~~~

~~~expect
Answers directly from the model's own knowledge and does NOT call search_corpus:
RULE 0 makes general-knowledge questions tool-forbidden, and the corpus has
nothing to say about US presidents. Names the president its training data knows
(Joe Biden for the Qwen3-VL generation) and may add that its knowledge has a
cutoff. What must not happen is a corpus lookup, a claim that the corpus contains
the answer, or a refusal to answer at all.
~~~

### R9 · Honest miss inside the corpus domain

- **tags:** rag, czech, negative
- **tools:** search_corpus
- **status:** new 2026-08-14, unverified — replaces the anti-hallucination half of the old R8

~~~prompt
Co víš o hostu zzql-nonexistent-42?
~~~

~~~expect
This IS a corpus question, so it calls search_corpus. Finding nothing for that
host, it says plainly that the corpus contains no such host. It must not invent
an IP address, VLAN, owner or any other field, and must not silently answer about
a different host whose name merely looks similar.
~~~

---

## Section T — Tools beyond retrieval

### T1 · Clock

- **tags:** tools
- **tools:** get_current_timestamp
- **status:** verified 2026-07-27 (text preset, screenshot ex-07)

~~~prompt
Kolik je hodin?
~~~

~~~expect
Calls the clock tool and answers with one short line giving the current time and
the timezone offset. Does not call search_corpus.
~~~

### T2 · Human-gated reboot

- **tags:** tools, safety
- **tools:** reboot_host
- **status:** verified 2026-07-27 (text preset, MOCK placement backend, screenshot ex-07)
- **skip:** mutating action behind a confirmation modal; cannot run unattended

~~~prompt
Rebootuj host node-03.
~~~

~~~expect
Routes to reboot_host through the reboot_guarded confirmation wrapper and STOPS
for human approval. On approval, reports that the host was restarted and how many
VMs were drained first. It must never report success without the confirmation
step having happened.
~~~

---

## Section V — Vision

> **Provenance warning.** V1-V7 were verified on 2026-07-27 against
> Qwen3-VL-**8B**-Thinking (GGUF, old AMD box). Timings quoted in the status
> lines are from that hardware and are historical; the Spark is far faster. The
> capability claims are what matter.

### V1 · Receipt OCR, real world

- **tags:** vision, ocr
- **status:** verified 2026-07-27 on 8B-Thinking — 77 s, 613 out tokens, 8.1 tok/s (old AMD iGPU)

~~~prompt
Read all the text in this image. Preserve the original numbers and layout.
~~~

~~~expect
Transcribes every line of the receipt verbatim, including the vendor name, the
line item with its quantity and unit price, subtotal, grand total, cash tendered
and change due. All figures exact. Nothing invented, nothing translated unless
asked.
~~~

### V2 · Mixed-script OCR with identifiers

- **tags:** vision, ocr
- **status:** verified 2026-07-27 on 8B-Thinking — 59 s, 462 out tokens

~~~prompt
OCR every visible line. Keep punctuation, numbers, and IP addresses exact.
~~~

~~~expect
Every IP address, CIDR suffix, port, e-mail address and float reproduced
character for character, including negative and multi-decimal values. Czech words
kept as written. Zero hallucinated lines.
~~~

### V3 · Chinese to English

- **tags:** vision, ocr, translation
- **status:** verified 2026-07-27 on 8B-Thinking — 50 s, 394 out tokens

~~~prompt
This image contains Chinese text. Read each line and give an English translation.
~~~

~~~expect
Reads each line of Simplified Chinese and gives a correct English translation,
preserving the numbers and units exactly — server counts, bandwidth figures and
the maintenance window with its times.
~~~

### V4 · Farsi to English

- **tags:** vision, ocr, translation
- **status:** verified 2026-07-27 on 8B-Thinking — 190 s, 1500 out tokens; slowest case in the library

~~~prompt
This image contains Persian (Farsi) text. Read every line and provide an English translation next to each.
~~~

~~~expect
Recognises the Persian text, breaks it into words and translates each line into
English alongside the original. Place names and temperature units come through
correctly. Digit misreads on non-Arabic-shaping fonts are a known limit and not a
failure of this case.
~~~

### V5 · Bar chart to table

- **tags:** vision, extraction, table
- **status:** verified 2026-07-27 on 8B-Thinking — 28 s, 211 out tokens; every value correct

~~~prompt
This image is a bar chart. Extract the underlying data into a Markdown table with two columns: Region and Revenue.
~~~

~~~expect
A markdown table with exactly the two requested columns and one row per bar, the
region labels transcribed and each revenue value read correctly from the bar
height. No commentary needed beyond the table.
~~~

### V6 · Diagram photo to mermaid

- **tags:** vision, diagram
- **status:** verified 2026-07-27 on 8B-Thinking — 134 s, 1061 out tokens

~~~prompt
This image shows a small architecture diagram. Regenerate it as a mermaid graph (fenced mermaid). Preserve every box and every arrow direction.
~~~

~~~expect
One fenced mermaid block. Every box from the source appears as a node, every
arrow appears with its direction preserved, and no edges are invented. Open WebUI
renders it inline.
~~~

~~~must
mermaid
~~~

### V7 · Code photo to runnable Python

- **tags:** vision, code
- **status:** verified 2026-07-27 on 8B-Thinking — 155 s, 1232 out tokens; transcribed n**0.5 as n*0.5, still predicted the right output

~~~prompt
This image shows Python code. Transcribe it verbatim into a fenced python block. Then predict what it prints (do not use any tool).
~~~

~~~expect
A fenced python block reproducing the code from the photo, followed by a
prediction of its output. The predicted output must be correct. It must NOT call
the code interpreter, because the prompt forbids it.
~~~

---

## Section D — Diagrams the model authors

### D1 · Reverse the arrows in a supplied diagram

- **tags:** diagram, drawio, edit
- **attach:** tests/fixtures/dependency_input.drawio
- **status:** verified 2026-08-09, 10/10 structural checks on both 30B-A3B Thinking FP8 and 32B dense FP8; rendered headlessly

~~~prompt
V přiloženém Draw.io grafu obrať směr šípek mezi Frontend → API → Database opačným směrem. Prezentuj výstup opět ve formátu Draw.io.
~~~

~~~expect
One fenced html block containing the ragfarm draw.io wrapper and the user's own
XML with only the arrow directions changed: each edge's source and target
swapped. The user's cell ids, styles, geometry, labels and fill colours are
preserved exactly — the diagram must be edited in place, not redrawn. No elided
attributes, no reserved ids 0 or 1 on content cells, every cell keeps its
mxGeometry, and the page loads ragfarm-drawio.js.
~~~

~~~must
ragfarm-drawio.js
~~~

~~~must-not
...
~~~

### D2 · Author a diagram from a description

- **tags:** diagram, drawio
- **status:** verified 2026-08-09 on 30B-A3B Thinking FP8; fenced 3/3, ~11k completion tokens

~~~prompt
Vytvoř Draw.io ER diagram naší monitorovací domény: entity Incident, Problem, OBS Process, Micro Service, Cluster, Node, Data Centre, Squad, Person. Každá entita má 2-3 atributy, barevné výplně podle domény a ortogonální spoje. Prezentuj jako Draw.io.
~~~

~~~expect
One fenced html block using the wrapper. Every named entity present, each with
its attribute rows, colours grouped by domain, and orthogonal edges between
related entities. Valid draw.io XML that renders.
~~~

~~~must
ragfarm-drawio.js
~~~

### D3 · 1:1 conversion of a diagram image

- **tags:** diagram, drawio, vision, hard
- **attach:** tests/fixtures/Splunk_in_KB.png
- **status:** KNOWN HARD — single-pass fails. See docs/measurements/2026-08-09-instruct-vs-thinking-drawio.json
- **skip:** single-pass conversion is a known failure; kept as the reference for the two-pass workflow

~~~prompt
Převeď obrázek grafu z přílohy do formátu Draw.io. Zachovej fortmátování, rámečky, spoje, šipky, typy a styly šipek, velikosti, poměry, vztahy, barvy, zkrátka vše 1:1. Prezentuj výsledný Draw.io graf.
~~~

~~~expect
Twenty-eight entities as swimlanes with their attribute rows, the PoC container,
the Legenda frame, colours by domain, and roughly twenty-six edges. In one pass
neither model achieves this: Thinking stops early with a simplified 43-box
diagram, Instruct loops on edges until the budget is gone. The working route is
two passes — boxes first with edges forbidden, then edges given the box ids.
~~~

### D4 · Inventory a diagram image without drawing it

- **tags:** vision, extraction, table
- **attach:** tests/fixtures/Splunk_in_KB.png
- **status:** verified 2026-08-09 on 30B-A3B Thinking FP8 — 28/28 entities, all attribute rows correct, one placement error

~~~prompt
Vypiš úplný inventář tohoto ER diagramu jako markdown tabulku. Jeden řádek na entitu: název | barva výplně | seznam VŠECH atributových řádků uvnitř rámečku | je uvnitř modrého rámu PoC? (ano/ne). Nic nevynechávej, nic neshrnuj.
~~~

~~~expect
A markdown table with one row per entity — twenty-eight of them — listing every
attribute row inside each box, its fill colour, and whether it sits inside the
PoC frame. Source typos may be normalised. This is the perception half of D3 and
it is reliable where the drawing half is not.
~~~

---

## Section C — Coding

### C1 · Write and actually run Python

- **tags:** code, interpreter
- **status:** verified 2026-07-27 (text preset, screenshot ex-09)

~~~prompt
Vygeneruj mi kód pro quicksort a otestuj ho spuštěním nad malým polem náhodných řetězců. Prezentuj kód a výsledné pořadí tříděného pole po běhu sortu.
~~~

~~~expect
Emits complete Python implementing quicksort over a random list of strings, then
CALLS the code interpreter to run it and reports the sorted result. Per RULE 6,
Python is the one language it must execute rather than only describe.
~~~

### C2 · Non-Python code is not executed

- **tags:** code, negative
- **status:** verified 2026-08-05 as the fix for the Python/JavaScript reasoning loop

~~~prompt
Napiš mi v JavaScriptu funkci, která z pole objektů udělá mapu podle klíče id.
~~~

~~~expect
Outputs the JavaScript and stops. It must NOT call the code interpreter, must not
invent test cases for it, and must not apologise for being unable to run
JavaScript — the interpreter is Python-only and declining to run other languages
is correct behaviour, not a limitation worth narrating.
~~~

~~~must-not
code interpreter
~~~

---

## Section M · Mermaid

### M1 · Dependency tree of a sentence

- **tags:** diagram, mermaid
- **status:** verified 2026-07-27 (text preset, screenshot ex-08)

~~~prompt
Vygeneruj stromový diagram slovních vazeb ve větě: "Once upon a time there was a very little dog called Steven who owned a nice little yellow car".
~~~

~~~expect
One fenced mermaid block containing a genuine branching dependency tree rather
than a linear chain: the head noun acts as a hub with its determiners and
modifiers as children, and the relative clause branches off correctly.
~~~

~~~must
mermaid
~~~
