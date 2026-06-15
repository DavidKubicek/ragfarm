"""
Embedder HTTP service — :8090/embed
Runs BAAI/bge-small-en-v1.5 (Quark INT8) on the NPU via VitisAI EP.

Start: python services/embedder/server.py
  (needs /opt/ryzenai/venv + XRT env sourced)
Request: POST /embed  {"input": ["text1", "text2", ...]}
Response: {"embeddings": [[...], [...]], "dim": 384}
"""
import os
import sys
import json
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import onnxruntime as ort
from transformers import AutoTokenizer

BASE        = Path(__file__).resolve().parents[2]
MODEL_DIR   = BASE / "models/embeddings/bge-small-en-v1.5-quark-static"
TOKENIZER_DIR = BASE / "models/embeddings/bge-small-en-v1.5-onnx-static"
VAIP_CFG    = "/opt/ryzenai/venv/voe-4.0-linux_x86_64/vaip_config.json"
SEQ_LEN     = 128
HOST        = "127.0.0.1"
PORT        = 8090


def load_session():
    so = ort.SessionOptions()
    providers = [
        ("VitisAIExecutionProvider", {"config_file": VAIP_CFG}),
        "CPUExecutionProvider",
    ]
    return ort.InferenceSession(
        str(MODEL_DIR / "model_quantized.onnx"),
        sess_options=so,
        providers=providers,
    )


def embed_texts(sess, tokenizer, texts: list[str]) -> list[list[float]]:
    embeddings = []
    for text in texts:
        enc = tokenizer(
            [text], return_tensors="np",
            padding="max_length", max_length=SEQ_LEN, truncation=True,
        )
        out = sess.run(None, {
            "input_ids":      enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64),
            "token_type_ids": enc["token_type_ids"].astype(np.int64),
        })
        hidden = out[0]  # (1, SEQ_LEN, 384)
        mask = enc["attention_mask"][..., np.newaxis].astype(np.float32)
        emb = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
        # L2-normalise (matches bge-small usage)
        norm = np.linalg.norm(emb, axis=-1, keepdims=True)
        emb = emb / np.maximum(norm, 1e-12)
        embeddings.append(emb[0].tolist())
    return embeddings


class EmbedHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request noise; errors still reach stderr

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/embed":
            self._respond(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            texts = payload["input"]
            if not isinstance(texts, list) or not texts:
                raise ValueError("input must be a non-empty list of strings")
        except Exception as e:
            self._respond(400, {"error": str(e)})
            return
        embeddings = embed_texts(self.server.sess, self.server.tokenizer, texts)
        self._respond(200, {"embeddings": embeddings, "dim": len(embeddings[0])})

    def _respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    print("Loading tokenizer...", file=sys.stderr)
    tokenizer = AutoTokenizer.from_pretrained(str(TOKENIZER_DIR))
    print("Loading ORT session (VitisAI EP)...", file=sys.stderr)
    sess = load_session()
    print(f"Providers: {sess.get_providers()}", file=sys.stderr)

    server = HTTPServer((HOST, PORT), EmbedHandler)
    server.sess = sess
    server.tokenizer = tokenizer
    print(f"Embedder listening on {HOST}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
