# Embedding Model Record

| Field    | Value |
|----------|-------|
| Model    | BAAI/bge-m3 |
| Revision | 5617a9f61b028005a4858fdac845db406aefb181 |
| Backend  | FlagEmbedding (BGEM3FlagModel), CPU, FP32 (use_fp16=False) |
| Load     | model_kwargs={"use_safetensors": True} |
| Output   | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages| 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service  | POST http://127.0.0.1:8090/embed |
| Updated  | 2026-06-16 |
