#!/usr/bin/env python3
"""
ragfarm_tracer_simple.py: Lightweight ragfarm inference tracer.

Simpler approach: no async, just HTTP request tracing + direct engine queries.

Usage:
  # Query engines for live telemetry
  python ragfarm_tracer_simple.py query \
    --generation localhost:8001 \
    --reranker localhost:8002

  # Parse Open WebUI network log (HAR or JSON export)
  python ragfarm_tracer_simple.py parse --har captured_requests.har

  # Demo: show what complete trace looks like
  python ragfarm_tracer_simple.py demo
"""

import json
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import argparse
from collections import defaultdict

import requests

# ANSI colors
GRAY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

@dataclass
class EngineTelemetry:
    """Telemetry snapshot from one engine."""
    engine_name: str
    endpoint: str
    timestamp: str
    
    model_id: str = ""
    is_healthy: bool = False
    response_time_ms: float = 0
    error: Optional[str] = None

@dataclass
class HTTPRequest:
    """Single HTTP request to an engine."""
    seq: int
    timestamp_ms: float
    
    method: str
    engine: str
    endpoint: str
    path: str
    
    request_bytes: int
    response_bytes: int
    response_status: int
    latency_ms: float
    
    request_preview: str = ""
    response_preview: str = ""

@dataclass
class RagfarmTrace:
    """Complete ragfarm execution trace."""
    session_id: str
    timestamp: str
    
    requests: List[HTTPRequest] = field(default_factory=list)
    engine_telemetries: Dict[str, EngineTelemetry] = field(default_factory=dict)
    
    total_time_ms: float = 0
    total_requests: int = 0
    total_bytes: int = 0

class EngineQuerier:
    """Query llama.cpp instances for metrics."""
    
    @staticmethod
    def query(endpoint: str, engine_name: str) -> Optional[EngineTelemetry]:
        """Query single llama.cpp endpoint."""
        try:
            t0 = time.time()
            r = requests.get(f"http://{endpoint}/v1/models", timeout=5)
            elapsed_ms = (time.time() - t0) * 1000
            
            if r.status_code != 200:
                return EngineTelemetry(
                    engine_name=engine_name,
                    endpoint=endpoint,
                    timestamp=datetime.now().isoformat(),
                    is_healthy=False,
                    response_time_ms=elapsed_ms,
                    error=f"HTTP {r.status_code}"
                )
            
            data = r.json()
            model_id = ""
            if "data" in data and len(data["data"]) > 0:
                model_id = data["data"][0].get("id", "unknown")
            
            return EngineTelemetry(
                engine_name=engine_name,
                endpoint=endpoint,
                timestamp=datetime.now().isoformat(),
                model_id=model_id,
                is_healthy=True,
                response_time_ms=elapsed_ms
            )
        
        except Exception as e:
            return EngineTelemetry(
                engine_name=engine_name,
                endpoint=endpoint,
                timestamp=datetime.now().isoformat(),
                is_healthy=False,
                error=str(e)
            )
    
    @staticmethod
    def query_all(engines: Dict[str, str]) -> Dict[str, EngineTelemetry]:
        """Query all configured engines."""
        results = {}
        for name, endpoint in engines.items():
            results[name] = EngineQuerier.query(endpoint, name)
        return results

def format_telemetry_report(telemetries: Dict[str, EngineTelemetry]) -> str:
    """Format engine telemetry."""
    lines = []
    
    lines.append(f"\n{CYAN}{'═' * 90}{RESET}")
    lines.append(f"{CYAN}RAGFARM ENGINE TELEMETRY{RESET}")
    lines.append(f"{CYAN}{'═' * 90}{RESET}\n")
    
    lines.append(f"{'Engine':<15} {'Endpoint':<25} {'Model':<30} {'Status':<15} {'Latency':<10}")
    lines.append(f"{'-' * 15} {'-' * 25} {'-' * 30} {'-' * 15} {'-' * 10}")
    
    for name, tel in telemetries.items():
        if tel.is_healthy:
            status = f"{GREEN}✓ OK{RESET}"
        else:
            status = f"{RED}✗ FAIL{RESET}"
        
        model = tel.model_id if tel.model_id else "—"
        lines.append(f"{name:<15} {tel.endpoint:<25} {model:<30} {status:<15} {tel.response_time_ms:>8.2f}ms")
        
        if tel.error:
            lines.append(f"  → Error: {tel.error}")
    
    lines.append("")
    return "\n".join(lines)

def format_trace_report(trace: RagfarmTrace) -> str:
    """Format HTTP trace."""
    lines = []
    
    lines.append(f"\n{CYAN}{'═' * 110}{RESET}")
    lines.append(f"{CYAN}HTTP REQUEST TRACE{RESET}")
    lines.append(f"{CYAN}{'═' * 110}{RESET}\n")
    
    lines.append(f"Session: {trace.session_id}")
    lines.append(f"Timestamp: {trace.timestamp}")
    lines.append(f"Total requests: {trace.total_requests}")
    lines.append(f"Total time: {trace.total_time_ms:.2f}ms\n")
    
    if not trace.requests:
        lines.append("No requests recorded.")
        return "\n".join(lines)
    
    # Request timeline
    lines.append(f"{'Seq':>3} {'Engine':<12} {'Endpoint':>20} {'Latency':>10} {'Req':>8} {'Resp':>8} {'Status':>7}")
    lines.append(f"{'-' * 3} {'-' * 12} {'-' * 20} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 7}")
    
    for req in trace.requests:
        status_color = GREEN if req.response_status == 200 else RED
        status = f"{status_color}{req.response_status}{RESET}"
        
        lines.append(f"{req.seq:3d} {req.engine:<12} {req.endpoint:>20} "
                    f"{req.latency_ms:>10.2f} {req.request_bytes:>8d} {req.response_bytes:>8d} {status:>7}")
    
    lines.append("")
    
    # Per-engine stats
    engine_totals = defaultdict(float)
    engine_counts = defaultdict(int)
    
    for req in trace.requests:
        engine_totals[req.engine] += req.latency_ms
        engine_counts[req.engine] += 1
    
    if engine_totals:
        lines.append(f"{BOLD_WHITE}PER-ENGINE STATISTICS:{RESET}")
        lines.append(f"{'Engine':<15} {'Requests':>10} {'Total time':>12} {'Avg latency':>12}")
        lines.append(f"{'-' * 15} {'-' * 10} {'-' * 12} {'-' * 12}")
        
        for engine in sorted(engine_totals.keys()):
            total = engine_totals[engine]
            count = engine_counts[engine]
            avg = total / count if count > 0 else 0
            
            lines.append(f"{engine:<15} {count:>10d} {total:>12.2f} {avg:>12.2f}")
        
        lines.append("")
    
    # Aggregate
    lines.append(f"{BOLD_WHITE}AGGREGATE:{RESET}")
    lines.append(f"  Total bytes exchanged: {trace.total_bytes:>12d}")
    total_latency = sum(r.latency_ms for r in trace.requests)
    lines.append(f"  Total latency:         {total_latency:>12.2f}ms")
    avg_latency = total_latency / len(trace.requests) if trace.requests else 0
    lines.append(f"  Avg request latency:   {avg_latency:>12.2f}ms\n")
    
    return "\n".join(lines)

def demo_trace() -> RagfarmTrace:
    """Create demo trace with typical ragfarm call sequence."""
    trace = RagfarmTrace(
        session_id="demo_001",
        timestamp=datetime.now().isoformat()
    )
    
    # Typical sequence: user prompt → generation → reranker
    
    # Step 1: User sends prompt to generation engine
    trace.requests.append(HTTPRequest(
        seq=1,
        timestamp_ms=0,
        method="POST",
        engine="generation",
        path="/v1/completions",
        endpoint="localhost:8001",
        request_bytes=256,
        response_bytes=512,
        response_status=200,
        latency_ms=187.3,
        request_preview="prompt: 'Jak se přihlásím do EPC?'",
        response_preview="tokens generated, usage info..."
    ))
    
    # Step 2: Reranker scores the results
    trace.requests.append(HTTPRequest(
        seq=2,
        timestamp_ms=190,
        method="POST",
        engine="reranker",
        path="/v1/rerank",
        endpoint="localhost:8002",
        request_bytes=2048,
        response_bytes=256,
        response_status=200,
        latency_ms=67.8,
        request_preview="query + 3 docs",
        response_preview="relevance scores [0.95, 0.87, 0.62]"
    ))
    
    # Step 3: Final generation with reranked context
    trace.requests.append(HTTPRequest(
        seq=3,
        timestamp_ms=260,
        method="POST",
        engine="generation",
        path="/v1/completions",
        endpoint="localhost:8001",
        request_bytes=1024,
        response_bytes=640,
        response_status=200,
        latency_ms=145.2,
        request_preview="reranked context + query",
        response_preview="final answer generated..."
    ))
    
    trace.total_requests = len(trace.requests)
    trace.total_bytes = sum(r.request_bytes + r.response_bytes for r in trace.requests)
    trace.total_time_ms = sum(r.latency_ms for r in trace.requests)
    
    return trace

def main():
    parser = argparse.ArgumentParser(
        description="Lightweight ragfarm tracer: HTTP calls + engine queries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Query engines for live metrics
  python ragfarm_tracer_simple.py query \
    --generation localhost:8001 \
    --reranker localhost:8002

  # Demo: show trace format
  python ragfarm_tracer_simple.py demo
        """
    )
    
    subparsers = parser.add_subparsers(dest="command")
    
    # Query command
    query_cmd = subparsers.add_parser("query", help="Query engines for live telemetry")
    query_cmd.add_argument("--generation", required=True)
    query_cmd.add_argument("--reranker", default=None)
    query_cmd.add_argument("--embedder", default=None)
    
    # Demo command
    subparsers.add_parser("demo", help="Demo trace output")
    
    args = parser.parse_args()
    
    if args.command == "query":
        engines = {"generation": args.generation}
        if args.reranker:
            engines["reranker"] = args.reranker
        if args.embedder:
            engines["embedder"] = args.embedder
        
        print(f"\n{BOLD_WHITE}Querying engines...{RESET}\n")
        
        telemetries = EngineQuerier.query_all(engines)
        
        for name, tel in telemetries.items():
            status = f"{GREEN}✓{RESET}" if tel.is_healthy else f"{RED}✗{RESET}"
            print(f"{status} {name:<12} ({tel.endpoint:<20}) {tel.response_time_ms:>6.2f}ms", end="")
            
            if tel.is_healthy and tel.model_id:
                print(f" → {tel.model_id}")
            elif tel.error:
                print(f" → ERROR: {tel.error}")
            else:
                print()
        
        print()
        print(format_telemetry_report(telemetries))
        
        # Save telemetry
        output_file = Path("./telemetry_snapshot.json")
        with open(output_file, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "telemetries": {k: asdict(v) for k, v in telemetries.items()}
            }, f, indent=2, default=str)
        
        print(f"✓ Saved to {output_file}\n")
    
    elif args.command == "demo":
        trace = demo_trace()
        print(format_trace_report(trace))
        
        # Save trace
        output_file = Path("./http_trace_demo.json")
        with open(output_file, "w") as f:
            json.dump({
                "session": {
                    "session_id": trace.session_id,
                    "timestamp": trace.timestamp,
                    "total_requests": trace.total_requests,
                    "total_bytes": trace.total_bytes,
                    "total_time_ms": trace.total_time_ms,
                },
                "requests": [asdict(r) for r in trace.requests]
            }, f, indent=2, default=str)
        
        print(f"✓ Saved to {output_file}\n")
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
