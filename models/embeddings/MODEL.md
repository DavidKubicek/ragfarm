# Embedding Model

**Model:** BAAI/bge-small-en-v1.5  
**Revision:** 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a  
**Source:** https://huggingface.co/BAAI/bge-small-en-v1.5  
**Parameters:** 33M  
**Embedding dim:** 384  
**Max seq len:** 128 (fixed, static export)  
**Quantization:** Quark 0.11.2, static INT8/QDQ, `enable_npu_transformer=True`  
**Runtime:** ORT 1.23.3 (VitisAI EP → NPU RyzenAI-npu4, CPU fallback)

## Paths

| Artefact | Path |
|----------|------|
| Tokenizer + raw ONNX (FP32) | `models/embeddings/bge-small-en-v1.5-onnx-static/` |
| Quark-quantised ONNX (INT8 QDQ) | `models/embeddings/bge-small-en-v1.5-quark-static/model_quantized.onnx` |
| Quantization script | `infra/embedder/quark_quantize.py` |
| HTTP service | `services/embedder/server.py` |

## Service

Listens on `127.0.0.1:8090`.  
`POST /embed` — body `{"input": ["text1", "text2"]}`, response `{"embeddings": [[...]], "dim": 384}`.  
Embeddings are L2-normalised (norm ≈ 1.0), suitable for cosine similarity.

## Quantization notes

- Model exported with static shapes (batch=1, seq=128) — required by VitisAI EP vaiml compiler (dynamic shapes trigger MLIR assertion in libvaiml.so).
- 73 Gemm ops quantized to INT8. Remaining ops (LayerNorm, Add, Softmax, etc.) fall back to CPU.
- Calibrated on 20 domain-relevant sentences covering RAG, NPU, and infra topics.
- Quark config: `QuantFormat.QDQ`, `QuantType.QInt8`, `per_channel=False`, `include_cle=True`.
