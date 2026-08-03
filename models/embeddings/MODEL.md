# Embedding Model Record

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-m3 |
| Revision   | `5617a9f61b028005a4858fdac845db406aefb181` (resolved on the Spark, 2026-08-03; repo `lastModified` 2024-07-03) |
| Backend    | FlagEmbedding 1.4.0 (BGEM3FlagModel), **CUDA** (`devices="cuda:0"`), FP16 (`use_fp16=True`) — ADR-0013 §4 |
| Weights    | models/embeddings/bge-m3/ — `pytorch_model.bin` (2.2 GB) + `sparse_linear.pt` head (load-bearing for sparse); path in `.env` `EMBED_MODEL_PATH` |
| Output     | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages  | 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service    | services/embedder/server.py — POST http://127.0.0.1:8090/embed (embeddings only) |
| Unit       | manifests/ragfarm-embedder.service (host, CUDA) |
| Updated    | 2026-08-03 |

**Weight format.** This repo ships **no** `model.safetensors` — `pytorch_model.bin`
is the only weight file BAAI publishes, so `fetch-encoder.sh`'s "fastest format"
preference falls through to the pickle. That is fine on torch 2.13 (`weights_only`
defaults to True); the old safetensors-only rule was a workaround for the abandoned
NPU venv's very old torch and is retired. An earlier version of this record claimed
`model.safetensors (~2.3 GB)` — that was never true of this repo.

**Revision vs. the tested baseline.** `docs/deployment.md` → "Tested model versions"
records `50f9396f75618b3389c1fd1068a1ff58dc7b5b26` as the known-good baseline. Step
03's standing policy is to fetch **latest**, which resolved to the hash above. If
retrieval quality ever looks wrong, re-fetch the baseline rev and compare before
suspecting the pipeline.

The sibling cross-encoder reranker is recorded separately in `../reranker/MODEL.md`
(it moved out of the embedder to its own GPU service, ADR-0008); the generative LLM
in `../llm/MODEL.md`.
