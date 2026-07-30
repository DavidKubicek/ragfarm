#!/usr/bin/env python3
"""owui_trace_proxy — minimal streaming proxy for OWUI <-> llama-server.

Wire: OWUI OPENAI_API_BASE_URL -> http://127.0.0.1:PROXY_PORT/v1
This forwards every request to LLAMA_URL, streams SSE back byte-for-byte
(so OWUI never even sees a proxy), AND emits a checkpointed log per chat
to logs/owui-trace-<chatid>-YYYY-MM-DD.log.

Checkpoints (wall-clock timestamps from request start):
  [1] submit — full request JSON dump (messages, tools, params, token counts)
  [2] pre-tool CoT — reasoning_content accumulated before any tool_call
  [3] tool_calls — the emitted tool call names + arguments
  [4] answer — final content (reasoning still streaming interleaved)
  [ERR] any HTTP or streaming failure with body
  [SUMMARY] final wall time + token counts + finish_reason

Streams SSE straight through — OWUI sees zero latency added.

DISABLE:  point OWUI back at llama-server directly:
  edit infra/compose.yaml — OPENAI_API_BASE_URL: http://127.0.0.1:8080/v1
  docker compose -f infra/compose.yaml up -d open-webui
"""
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8095"))
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080").rstrip("/")
LOG_DIR = Path(os.environ.get("LOG_DIR", "/home/dave/ragfarm/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _chat_id_from_payload(payload):
    """OWUI passes chat_id in payload metadata; fall back to id or timestamp."""
    if isinstance(payload, dict):
        for k in ("chat_id", "id"):
            if payload.get(k):
                return re.sub(r"[^\w-]", "_", str(payload[k]))[:32]
        meta = payload.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("chat_id"):
            return re.sub(r"[^\w-]", "_", str(meta["chat_id"]))[:32]
    return dt.datetime.now().strftime("adhoc-%H%M%S")


def _open_log(chat_id):
    fname = f"owui-trace-{chat_id}-{dt.date.today().isoformat()}.log"
    return (LOG_DIR / fname).open("a", buffering=1)  # line-buffered


def _cp(fh, t0, label, body=""):
    dt_ms = (time.time() - t0) * 1000
    fh.write(f"\n[{dt_ms:8.1f}ms] {label}\n")
    if body:
        fh.write(body.rstrip() + "\n")


async def _forward_stream(request, target_url, fh, t0):
    """POST/streaming pass-through with SSE accumulation for the log."""
    body = await request.read()
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    # -- CHECKPOINT 1 -- submit
    dump = {
        "model": payload.get("model"),
        "n_messages": len(payload.get("messages", [])),
        "n_tools": len(payload.get("tools", [])),
        "max_tokens": payload.get("max_tokens"),
        "temperature": payload.get("temperature"),
        "stream": payload.get("stream"),
        "tool_choice": payload.get("tool_choice"),
    }
    # last user message text preview (skip image content parts for brevity)
    for m in reversed(payload.get("messages", [])):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                dump["last_user_text"] = c[:400]
            elif isinstance(c, list):
                dump["last_user_text"] = " ".join(
                    p.get("text", "[image]")[:200] for p in c if isinstance(p, dict)
                )[:400]
            break
    tool_names = [t.get("function", {}).get("name") for t in payload.get("tools", []) if isinstance(t, dict)]
    dump["tool_names"] = tool_names
    _cp(fh, t0, "[1] SUBMIT", json.dumps(dump, ensure_ascii=False, indent=2))

    # forward to llama-server, stream response back to OWUI + capture for log
    hdrs = {k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "content-encoding")}
    timeout = ClientTimeout(total=1200)
    try:
        async with ClientSession(timeout=timeout) as sess:
            async with sess.request(request.method, target_url, data=body,
                                    headers=hdrs) as upstream:
                resp = web.StreamResponse(status=upstream.status,
                                          headers={k: v for k, v in upstream.headers.items()
                                                   if k.lower() != "content-encoding"})
                await resp.prepare(request)
                if upstream.status != 200:
                    err_body = await upstream.read()
                    await resp.write(err_body)
                    await resp.write_eof()
                    _cp(fh, t0, f"[ERR] upstream HTTP {upstream.status}", err_body.decode("utf-8", "replace")[:2000])
                    return resp

                reasoning_buf, content_buf, tool_calls, finish_reason, usage = [], [], [], None, None
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)  # stream to OWUI in real time
                    # parse SSE lines from this chunk
                    for line in chunk.split(b"\n"):
                        if not line.startswith(b"data: "):
                            continue
                        data = line[6:].strip()
                        if not data or data == b"[DONE]":
                            continue
                        try:
                            ev = json.loads(data)
                        except Exception:
                            continue
                        choice = (ev.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            reasoning_buf.append(delta["reasoning_content"])
                        if "content" in delta and delta["content"]:
                            content_buf.append(delta["content"])
                        if "tool_calls" in delta and delta["tool_calls"]:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                while len(tool_calls) <= idx:
                                    tool_calls.append({"name": "", "arguments": ""})
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    tool_calls[idx]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    tool_calls[idx]["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        if ev.get("usage"):
                            usage = ev["usage"]

                await resp.write_eof()

        reasoning = "".join(reasoning_buf)
        content = "".join(content_buf)
        if reasoning:
            _cp(fh, t0, f"[2] PRE-TOOL CoT / <think> ({len(reasoning)} chars)", reasoning)
        if tool_calls:
            _cp(fh, t0, f"[3] tool_calls ({len(tool_calls)})",
                json.dumps(tool_calls, ensure_ascii=False, indent=2))
        if content:
            _cp(fh, t0, f"[4] ANSWER ({len(content)} chars)", content)
        _cp(fh, t0, "[SUMMARY]",
            json.dumps({"finish_reason": finish_reason, "usage": usage}, ensure_ascii=False, indent=2))
        return resp

    except asyncio.CancelledError:
        _cp(fh, t0, "[ERR] client cancelled (browser closed?)")
        raise
    except Exception as e:
        _cp(fh, t0, f"[ERR] proxy exception: {type(e).__name__}: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle(request):
    """Router: chat/completions goes through the log path; anything else is transparent."""
    path = request.match_info["path"]
    target = f"{LLAMA_URL}/v1/{path}"

    # only chat/completions needs the SSE tracing; other endpoints (/models) transparent
    if request.method == "POST" and "chat/completions" in path:
        body_preview = await request.read()
        try:
            payload = json.loads(body_preview) if body_preview else {}
        except Exception:
            payload = {}
        chat_id = _chat_id_from_payload(payload)
        fh = _open_log(chat_id)
        fh.write(f"\n{'=' * 78}\nrequest at {dt.datetime.now().isoformat()}\n{'=' * 78}\n")
        # re-inject body since we consumed it
        request._read_bytes = body_preview
        t0 = time.time()
        try:
            return await _forward_stream(request, target, fh, t0)
        finally:
            _cp(fh, t0, "[END] total wall time")
            fh.close()

    # transparent GET/other for /v1/models, /v1/embeddings, etc.
    body = await request.read()
    hdrs = {k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")}
    async with ClientSession(timeout=ClientTimeout(total=60)) as sess:
        async with sess.request(request.method, target, data=body, headers=hdrs) as up:
            data = await up.read()
            return web.Response(body=data, status=up.status,
                                content_type=up.content_type)


async def main():
    app = web.Application(client_max_size=1024 * 1024 * 200)  # 200 MB (image uploads)
    app.router.add_route("*", "/v1/{path:.*}", handle)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
    await site.start()
    print(f"owui trace proxy on http://{PROXY_HOST}:{PROXY_PORT} -> {LLAMA_URL}  (logs -> {LOG_DIR})",
          file=sys.stderr)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
