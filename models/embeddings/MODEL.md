# Embedding Model Record

| Field      | Value |
|------------|-------|
| Model      | BAAI/bge-m3 |
| Revision   | 50f9396f75618b3389c1fd1068a1ff58dc7b5b26 |
| Backend    | FlagEmbedding 1.4.0 (BGEM3FlagModel), CPU, FP32 (use_fp16=False) |
| Load flag  | model_kwargs={"use_safetensors": True} |
| Output     | dense 1024-dim (L2-normalised) + sparse (lexical weights) |
| Languages  | 100+ incl. Czech and English |
| Max tokens | 8192 |
| Service    | POST http://127.0.0.1:8090/embed |
| Updated    | 2026-06-16 |
