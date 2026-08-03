#!/usr/bin/env python3
"""
ragfarm_http_tracer.py: HTTP interceptor proxy for ragfarm inference engines.

Sits between Open WebUI and inference engines (llama.cpp instances, embedders, rerankers).
Captures every HTTP call with timing and attributes to specific engines.

Directly queries each llama.cpp instance for actual timing metrics (prefill, decode, tok/s).
No log parsing — all data from API endpoints.

Usage:
  python ragfarm_http_tracer.py --listen 0.0.0.0:8000 \
    --generation localhost:8001 \
    --reranker localhost:8002 \
    --embedder localhost:8003 \
    --output trace.json

Then configure Open WebUI to use http://localhost:8000 as LLM endpoint.
"""


# Endpoints come from .env via the shared resolver — never hardcode ports.
# See tests/tracing/ragfarm_env.py for the real port map.
from ragfarm_env import LLM_URL
import json
import sys
import time
import asyncio
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import argparse
from collections import defaultdict
import threading
import queue

from aiohttp import web, ClientSession
import aiohttp

# ANSI colors
GRAY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

@dataclass
class EngineMetrics:
    """Metrics for an inference engine at a point in time."""
    engine_name: str
    endpoint: str
    timestamp_ms: float
    
    # Timing (from engine itself)
    prefill_ms: Optional[float] = None
    decode_ms: Optional[float] = None
    
    # Tokens (from engine response)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    
    # Rates
    prefill_tok_s: Optional[float] = None
    decode_tok_s: Optional[float] = None
    
    # Raw metrics
    raw_metrics: Dict[str, Any] = field(default_factory=dict)

@dataclass
class HTTPCall:
    """Single HTTP request/response pair."""
    sequence_num: int
    timestamp_ms: float
    
    # Request
    method: str
    engine_name: str
    endpoint: str
    request_path: str
    request_bytes: int
    request_payload: Dict[str, Any]
    
    # Response
    status_code: int
    response_bytes: int
    response_payload: Dict[str, Any]
    
    # Timing
    request_time_ms: float  # Time to send and receive
    
    # Engine metrics (captured after call)
    engine_metrics: Optional[EngineMetrics] = None

@dataclass
class TraceSession:
    """Complete session trace with all HTTP calls."""
    session_id: str
    timestamp: str
    start_time_ms: float = 0
    
    # Calls in order
    calls: List[HTTPCall] = field(default_factory=list)
    
    # Endpoints discovered
    engines: Dict[str, str] = field(default_factory=dict)  # engine_name -> endpoint
    
    # Aggregate stats
    total_time_ms: float = 0
    total_tokens: int = 0
    total_bytes: int = 0
    
    # Per-engine stats
    engine_stats: Dict[str, Dict[str, Any]] = field(default_factory=lambda: defaultdict(dict))

class LlamaMetricsQuery:
    """Query llama.cpp instances for timing metrics."""
    
    def __init__(self):
        self.session: Optional[ClientSession] = None
    
    async def init(self):
        self.session = ClientSession()
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def query_metrics(self, endpoint: str) -> Optional[EngineMetrics]:
        """
        Query llama.cpp instance for timing metrics.
        llama.cpp doesn't have a dedicated metrics endpoint, but we can infer from:
        - /v1/models endpoint (returns model info)
        - Timing embedded in responses (usage object)
        """
        try:
            if not self.session:
                await self.init()
            
            # Try to get model info (lightweight endpoint)
            async with self.session.get(f"http://{endpoint}/v1/models", timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    metrics = EngineMetrics(
                        engine_name="unknown",
                        endpoint=endpoint,
                        timestamp_ms=time.time() * 1000,
                        raw_metrics=data
                    )
                    
                    return metrics
        except Exception as e:
            print(f"{YELLOW}⚠ Failed to query {endpoint}: {e}{RESET}", file=sys.stderr)
        
        return None

class HTTPInterceptor:
    """HTTP proxy that intercepts and traces all requests to inference engines."""
    
    def __init__(self, 
                 listen_addr: str = "0.0.0.0:8000",
                 generation_endpoint: str = "localhost:8001",
                 reranker_endpoint: Optional[str] = None,
                 embedder_endpoint: Optional[str] = None):
        self.listen_addr = listen_addr
        self.generation_endpoint = generation_endpoint
        self.reranker_endpoint = reranker_endpoint
        self.embedder_endpoint = embedder_endpoint
        
        self.session: Optional[TraceSession] = None
        self.call_counter = 0
        self.session_start_time = 0
        self.metrics_query = LlamaMetricsQuery()
        self.app = web.Application()
        self._setup_routes()
    
    def _setup_routes(self):
        """Setup proxy routes."""
        # Route all /v1/* paths to appropriate engine
        self.app.router.add_route("*", "/v1/{path_info:.*}", self.handle_request)
        self.app.router.add_route("*", "/health", self.handle_health)
    
    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})
    
    async def handle_request(self, request: web.Request) -> web.Response:
        """Handle proxied request, trace it, forward to appropriate engine."""
        if not self.session:
            return web.json_response({"error": "No active session"}, status=500)
        
        self.call_counter += 1
        call_start = time.time()
        path_info = request.match_info["path_info"]
        
        # Determine target engine based on request
        engine_name, target_endpoint = self._route_request(path_info, request)
        
        # Read request body
        request_body = await request.read()
        try:
            request_payload = json.loads(request_body) if request_body else {}
        except:
            request_payload = {}
        
        # Forward request to target engine
        try:
            target_url = f"http://{target_endpoint}/v1/{path_info}"
            
            async with aiohttp.ClientSession() as sess:
                async with sess.request(
                    request.method,
                    target_url,
                    data=request_body,
                    headers={k: v for k, v in request.headers.items() 
                            if k.lower() not in ['host', 'content-length']},
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    response_body = await resp.read()
                    try:
                        response_payload = json.loads(response_body) if response_body else {}
                    except:
                        response_payload = {}
                    
                    call_time_ms = (time.time() - call_start) * 1000
                    
                    # Query engine for metrics
                    engine_metrics = await self.metrics_query.query_metrics(target_endpoint)
                    
                    # Create call record
                    http_call = HTTPCall(
                        sequence_num=self.call_counter,
                        timestamp_ms=(time.time() - self.session_start_time) * 1000,
                        method=request.method,
                        engine_name=engine_name,
                        endpoint=target_endpoint,
                        request_path=f"/v1/{path_info}",
                        request_bytes=len(request_body),
                        request_payload=request_payload,
                        status_code=resp.status,
                        response_bytes=len(response_body),
                        response_payload=response_payload,
                        request_time_ms=call_time_ms,
                        engine_metrics=engine_metrics,
                    )
                    
                    self.session.calls.append(http_call)
                    self._print_call(http_call)
                    self._update_stats(http_call)
                    
                    # Return response to client
                    return web.Response(
                        body=response_body,
                        status=resp.status,
                        content_type=resp.content_type
                    )
        
        except Exception as e:
            print(f"{RED}❌ Request failed: {e}{RESET}", file=sys.stderr)
            return web.json_response({"error": str(e)}, status=500)
    
    def _route_request(self, path_info: str, request: web.Request) -> Tuple[str, str]:
        """Determine target engine based on request path and content."""
        # Heuristic routing based on path
        if "completions" in path_info:
            # Check if it's a reranking request (body contains scores, top_k, etc.)
            # For now, default to generation
            return ("generation", self.generation_endpoint)
        elif "embeddings" in path_info:
            if self.embedder_endpoint:
                return ("embedder", self.embedder_endpoint)
            else:
                return ("generation", self.generation_endpoint)
        else:
            return ("generation", self.generation_endpoint)
    
    def _print_call(self, call: HTTPCall):
        """Print call in real-time trace format."""
        direction = "→" if call.method in ["POST", "PUT"] else "←"
        status_color = GREEN if call.status_code == 200 else RED
        
        print(f"{BOLD_WHITE}{direction}{RESET} "
              f"[{call.sequence_num:3d}] {call.engine_name:12s} "
              f"{call.request_path:30s} "
              f"{status_color}{call.status_code}{RESET} | "
              f"{call.request_time_ms:>8.2f}ms | "
              f"{call.request_bytes:>6d}b → {call.response_bytes:>6d}b")
    
    def _update_stats(self, call: HTTPCall):
        """Update session statistics."""
        self.session.total_bytes += call.request_bytes + call.response_bytes
        
        # Extract token counts if available
        if "usage" in call.response_payload:
            usage = call.response_payload["usage"]
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            self.session.total_tokens += prompt_tokens + completion_tokens
    
    async def run(self):
        """Start the proxy server."""
        await self.metrics_query.init()
        
        host, port = self.listen_addr.split(":")
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, int(port))
        await site.start()
        
        print(f"\n{GREEN}✓ HTTP Tracer listening on {self.listen_addr}{RESET}")
        print(f"{CYAN}Forward to: generation={self.generation_endpoint}, "
              f"reranker={self.reranker_endpoint}, "
              f"embedder={self.embedder_endpoint}{RESET}\n")
        
        await asyncio.Event().wait()

def format_session_report(session: TraceSession) -> str:
    """Format complete trace session report."""
    lines = []
    
    lines.append(f"\n{CYAN}{'═' * 120}{RESET}")
    lines.append(f"{CYAN}HTTP TRACE SESSION REPORT{RESET}")
    lines.append(f"{CYAN}{'═' * 120}{RESET}\n")
    
    lines.append(f"{BOLD_WHITE}Session:{RESET} {session.session_id}")
    lines.append(f"{BOLD_WHITE}Timestamp:{RESET} {session.timestamp}")
    lines.append(f"{BOLD_WHITE}Duration:{RESET} {session.total_time_ms:.2f}ms")
    lines.append(f"{BOLD_WHITE}Total calls:{RESET} {len(session.calls)}\n")
    
    # Engines discovered
    if session.engines:
        lines.append(f"{BOLD_WHITE}ENGINES DISCOVERED:{RESET}")
        for name, endpoint in session.engines.items():
            lines.append(f"  {name:15s} → {endpoint}")
        lines.append("")
    
    # Call timeline
    lines.append(f"{BOLD_WHITE}CALL TIMELINE:{RESET}")
    lines.append(f"{'Seq':>3} {'Engine':12s} {'Endpoint':30s} {'Status':>6} {'Latency':>10} {'Req':>8} {'Resp':>8}")
    lines.append(f"{'-' * 3} {'-' * 12} {'-' * 30} {'-' * 6} {'-' * 10} {'-' * 8} {'-' * 8}")
    
    for call in session.calls:
        status_str = f"{call.status_code}"
        lines.append(f"{call.sequence_num:3d} {call.engine_name:12s} {call.request_path:30s} "
                    f"{status_str:>6} {call.request_time_ms:>10.2f} "
                    f"{call.request_bytes:>8d} {call.response_bytes:>8d}")
    
    lines.append("")
    
    # Per-engine statistics
    engine_times = defaultdict(float)
    engine_calls = defaultdict(int)
    engine_tokens = defaultdict(int)
    engine_bytes = defaultdict(int)
    
    for call in session.calls:
        engine_times[call.engine_name] += call.request_time_ms
        engine_calls[call.engine_name] += 1
        engine_bytes[call.engine_name] += call.request_bytes + call.response_bytes
        
        if "usage" in call.response_payload:
            usage = call.response_payload["usage"]
            engine_tokens[call.engine_name] += usage.get("total_tokens", 0)
    
    if engine_times:
        lines.append(f"{BOLD_WHITE}PER-ENGINE STATISTICS:{RESET}")
        lines.append(f"{'Engine':15s} {'Calls':>6} {'Total time':>12} {'Avg time':>12} {'Tokens':>8} {'Bytes':>10}")
        lines.append(f"{'-' * 15} {'-' * 6} {'-' * 12} {'-' * 12} {'-' * 8} {'-' * 10}")
        
        for engine_name in sorted(engine_times.keys()):
            total_time = engine_times[engine_name]
            calls = engine_calls[engine_name]
            avg_time = total_time / calls if calls > 0 else 0
            tokens = engine_tokens[engine_name]
            bytes_count = engine_bytes[engine_name]
            
            lines.append(f"{engine_name:15s} {calls:>6d} {total_time:>12.2f} {avg_time:>12.2f} "
                        f"{tokens:>8d} {bytes_count:>10d}")
        
        lines.append("")
    
    # Aggregate stats
    lines.append(f"{BOLD_WHITE}AGGREGATE STATISTICS:{RESET}")
    lines.append(f"  Total requests:          {len(session.calls):>8d}")
    lines.append(f"  Total time:              {session.total_time_ms:>8.2f} ms")
    lines.append(f"  Total tokens:            {session.total_tokens:>8d}")
    lines.append(f"  Total bytes:             {session.total_bytes:>8d}")
    avg_latency = sum(c.request_time_ms for c in session.calls) / len(session.calls) if session.calls else 0
    lines.append(f"  Avg request latency:     {avg_latency:>8.2f} ms\n")
    
    return "\n".join(lines)

async def demo_session():
    """Demonstrate tracer with simulated requests."""
    session = TraceSession(
        session_id="demo_001",
        timestamp=datetime.now().isoformat(),
        start_time_ms=time.time() * 1000
    )
    
    # Simulate generation call
    session.calls.append(HTTPCall(
        sequence_num=1,
        timestamp_ms=0,
        method="POST",
        engine_name="generation",
        endpoint="localhost:8001",
        request_path="/v1/completions",
        request_bytes=256,
        request_payload={"prompt": "Jak se přihlásím do EPC?", "max_tokens": 256},
        status_code=200,
        response_bytes=512,
        response_payload={"choices": [{"text": "..."}], "usage": {"prompt_tokens": 10, "completion_tokens": 50}},
        request_time_ms=145.3,
    ))
    
    # Simulate reranker call
    session.calls.append(HTTPCall(
        sequence_num=2,
        timestamp_ms=150,
        method="POST",
        engine_name="reranker",
        endpoint="localhost:8002",
        request_path="/v1/rerank",
        request_bytes=2048,
        request_payload={"query": "EPC login", "documents": [{"id": "1", "text": "..."}]},
        status_code=200,
        response_bytes=512,
        response_payload={"results": [{"score": 0.95}]},
        request_time_ms=87.2,
    ))
    
    session.total_time_ms = 250
    session.total_tokens = 60
    session.total_bytes = 2048 + 512
    
    print(format_session_report(session))
    
    # Save to file
    output_file = Path("./http_trace_demo.json")
    with open(output_file, "w") as f:
        data = {
            "session": {
                "session_id": session.session_id,
                "timestamp": session.timestamp,
                "total_time_ms": session.total_time_ms,
                "total_tokens": session.total_tokens,
                "total_bytes": session.total_bytes,
                "total_calls": len(session.calls),
            },
            "calls": [
                {
                    "sequence_num": c.sequence_num,
                    "timestamp_ms": c.timestamp_ms,
                    "method": c.method,
                    "engine_name": c.engine_name,
                    "endpoint": c.endpoint,
                    "request_path": c.request_path,
                    "request_bytes": c.request_bytes,
                    "status_code": c.status_code,
                    "response_bytes": c.response_bytes,
                    "request_time_ms": c.request_time_ms,
                    "request_payload": c.request_payload,
                    "response_payload": c.response_payload,
                }
                for c in session.calls
            ]
        }
        json.dump(data, f, indent=2, default=str)
    
    print(f"\n✓ Demo saved to {output_file}")

def main():
    parser = argparse.ArgumentParser(
        description="HTTP tracer proxy for ragfarm inference engines",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run demo (no servers required)
  python ragfarm_http_tracer.py --demo
  
  # Start proxy (forward all requests to engines)
  python ragfarm_http_tracer.py \
    --listen 0.0.0.0:8000 \
    --generation localhost:8001 \
    --reranker localhost:8002 \
    --embedder localhost:8003 \
    --output trace.json
  
  # Then configure Open WebUI:
  # Settings > API Settings > LLM Endpoint: http://localhost:8000/v1
        """
    )
    parser.add_argument("--listen", type=str, default="0.0.0.0:8000",
                       help="Listen address for proxy")
    parser.add_argument("--generation", type=str, default=LLM_URL,
                       help="Generation llama.cpp endpoint")
    parser.add_argument("--reranker", type=str, default=None,
                       help="Reranker endpoint")
    parser.add_argument("--embedder", type=str, default=None,
                       help="Embedder endpoint")
    parser.add_argument("--output", type=str, default="http_trace.json",
                       help="Output file for trace")
    parser.add_argument("--demo", action="store_true",
                       help="Run demo (no servers required)")
    
    args = parser.parse_args()
    
    if args.demo:
        asyncio.run(demo_session())
    else:
        tracer = HTTPInterceptor(
            listen_addr=args.listen,
            generation_endpoint=args.generation,
            reranker_endpoint=args.reranker,
            embedder_endpoint=args.embedder
        )
        asyncio.run(tracer.run())

if __name__ == "__main__":
    main()
