"""
ingester — walk CORPUS_ROOT, chunk text, embed via the NPU embedder HTTP endpoint
(EMBED_ENDPOINT), upsert into Qdrant. Runs periodically (cron/systemd timer).
STATUS: skeleton. TODO: real chunking, dedup by content hash, incremental runs
(store mtime+hash), batching to the NPU endpoint.
"""
import os, hashlib, pathlib, requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED = os.environ.get("EMBED_ENDPOINT", "http://localhost:8090/embed")
ROOT = pathlib.Path(os.environ.get("CORPUS_ROOT", "/data/corpus"))
COLL = os.environ.get("QDRANT_COLLECTION", "corpus")

def embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(EMBED, json={"inputs": texts}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]

def chunks(text: str, size: int = 1200, overlap: int = 150):
    i = 0
    while i < len(text):
        yield text[i:i+size]
        i += size - overlap

def main():
    qc = QdrantClient(url=QDRANT_URL)
    dim = len(embed(["dimension probe"])[0])
    if COLL not in {c.name for c in qc.get_collections().collections}:
        qc.create_collection(COLL, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    pts, n = [], 0
    for f in ROOT.rglob("*"):
        if not f.is_file():
            continue
        try:
            text = f.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        cs = list(chunks(text))
        if not cs:
            continue
        for vec, ch in zip(embed(cs), cs):
            pid = int(hashlib.sha1(f"{f}:{n}".encode()).hexdigest()[:15], 16)
            pts.append(PointStruct(id=pid, vector=vec,
                                   payload={"path": str(f.relative_to(ROOT)), "text": ch}))
            n += 1
        if len(pts) >= 256:
            qc.upsert(COLL, pts); pts = []
    if pts:
        qc.upsert(COLL, pts)
    print(f"ingested {n} chunks into {COLL}")

if __name__ == "__main__":
    main()
