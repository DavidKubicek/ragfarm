// service-topology.typ — the thirteen services, their runtime and their ports.
//
// SOURCE OF TRUTH IS THIS FILE, not the PNG beside it. Regenerate with
// assets/src/build.sh after changing anything. The previous diagrams were
// opaque PNGs with no source: when the deployment moved from llama.cpp to vLLM
// they silently became wrong and stayed wrong for six weeks, because nobody
// could edit them. Keep the source in git and that cannot happen twice.
//
// Cross-check against `scripts/stack.sh list`, which is the machine-readable
// inventory. If the two disagree, stack.sh is right.

#set page(width: auto, height: auto, margin: 10pt, fill: white)
#set text(font: "DejaVu Sans", size: 8.5pt)

#let svc(name, port, note, fill) = box(
  width: 152pt, fill: fill, stroke: 0.6pt + luma(90), radius: 3pt, inset: 6pt,
  [
    #text(weight: "bold", name) \
    #text(size: 7.5pt, fill: luma(60), port) \
    #text(size: 7pt, fill: luma(90), note)
  ]
)

#let group(title, subtitle, body) = box(
  stroke: 0.8pt + luma(150), radius: 4pt, inset: 8pt,
  [
    #text(weight: "bold", size: 9.5pt, title)
    #h(6pt) #text(size: 7.5pt, fill: luma(110), subtitle) \
    #v(4pt)
    #body
  ]
)

#let gpu   = rgb("#f8cecc")   // shares the GPU memory budget
#let cpu   = rgb("#d5e8d4")   // CPU-side
#let bridge= rgb("#fff2cc")   // gateway / UI
#let off   = rgb("#f0f0f0")   // defined but not deployed

#align(center, text(size: 12pt, weight: "bold")[ragfarm service topology])
#v(2pt)
#align(center, text(size: 8pt, fill: luma(100))[
  13 services · host systemd + docker compose · every port loopback except 3000 and 80
])
#v(8pt)

#grid(columns: 2, gutter: 10pt,

  group("HOST — systemd", "GPU consumers share ONE 121.7 GiB memory pool")[
    #grid(columns: 1, gutter: 5pt,
      svc("vllm-slot0", "127.0.0.1:8080/v1", "ragfarm-vllm@0 · primary MoE · enabled at boot", gpu),
      svc("vllm-slot1", "127.0.0.1:8082/v1", "ragfarm-vllm@1 · second model · manual start", gpu),
      svc("reranker", "127.0.0.1:8081/reranking", "llama.cpp · bge-reranker-v2-m3 · lowered priority", gpu),
      svc("embedder", "127.0.0.1:8090/embed", "BGE-M3 dense+sparse · CUDA", gpu),
      svc("ingester-watcher", "no port", "inotify corpus watcher", cpu),
    )
  ],

  group("CONTAINERS — docker compose", "infra/compose.yaml")[
    #grid(columns: 1, gutter: 5pt,
      svc("open-webui", "0.0.0.0:3000", "the ONLY LAN-exposed service · login-gated", bridge),
      svc("mcpo", "127.0.0.1:8000", "MCP → OpenAPI gateway · mounts /rag, /placement", bridge),
      svc("rag-retrieval", "127.0.0.1:8104", "search_corpus · owns the retrieval chain", cpu),
      svc("mcp-placement", "127.0.0.1:8101", "OpenNebula lookups", cpu),
      svc("mcp-host-control", "127.0.0.1:8102", "SAFETY-GATED · dry-run, allowlist, confirm", cpu),
      svc("mcp-fs", "127.0.0.1:8103", "stub · deliberately NOT wired to the UI", off),
      svc("qdrant", "127.0.0.1:6333", "vector store", cpu),
      svc("drawio-viewer", "0.0.0.0:80", "local draw.io mirror · air-gap safe", bridge),
    )
  ],
)

#v(8pt)
#align(center, box(stroke: 0.6pt + luma(170), radius: 3pt, inset: 6pt, width: 330pt, text(size: 7.5pt)[
  #text(weight: "bold")[Request path.] browser → open-webui:3000 → vllm slot → tool call →
  mcpo:8000 → rag-retrieval:8104 → embedder:8090 + qdrant:6333 → reranker:8081 → back
]))

#v(6pt)
#align(center, text(size: 7pt, fill: luma(110))[
  pink = shares the GPU memory budget · green = CPU · yellow = gateway/UI · grey = not deployed \
  generated from assets/src/service-topology.typ · verify with `scripts/stack.sh status`
])
