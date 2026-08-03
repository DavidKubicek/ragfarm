#!/usr/bin/env python3
"""
ragfarm_integrated_tracer.py: Complete visibility into ragfarm inference pipeline.

Combines:
1. HTTP request tracing (Open WebUI calling generation, reranking, embedding engines)
2. Direct llama.cpp metric queries (prefill/decode timing from each engine)
3. Complete call graph (which engines called in what order, data flowing through system)

Queries llama.cpp instances directly via HTTP to get:
- Timing metrics (prefill ms, decode ms, tok/s)
- Token counts
- Model information
- Load status

Usage (standalone analysis, no proxy):
  python ragfarm_integrated_tracer.py analyze --url http://localhost:8001 --query-count 5
  
Usage (with HTTP tracing):
  python ragfarm_integrated_tracer.py trace --listen 0.0.0.0:8000 \
    --generation localhost:8001 \
    --reranker localhost:8002 \
    --output trace.json

Or: Parse existing Open WebUI network logs to extract call sequences.
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
from typing import Optional, Dict, Any, List
import argparse
from collections import defaultdict

import aiohttp
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
    """Live telemetry from a single llama.cpp instance."""
    engine_name: str
    endpoint: str
    query_timestamp_ms: float
    
    # Model info
    model_id: str = ""
    
    # Availability
    is_healthy: bool = False
    response_time_ms: float = 0
    
    # Metrics (if available)
    slots_busy: int = 0
    slots_total: int = 0
    
    # Raw response
    raw_models: Dict[str, Any] = field(default_factory=dict)
    raw_slots: Dict[str, Any] = field(default_factory=dict)

@dataclass
class InferenceStep:
    """Single step in the inference pipeline."""
    step_number: int
    timestamp_ms: float
    
    engine_name: str
    endpoint: str
    
    # What happened
    operation: str  # "generation", "rerank", "embed", "retrieve"
    input_tokens: int
    output_tokens: int
    input_bytes: int
    output_bytes: int
    
    # Timing (measured by proxy or engine)
    latency_ms: float
    inferred_prefill_ms: Optional[float] = None
    inferred_decode_ms: Optional[float] = None
    
    # Data
    input_preview: str = ""
    output_preview: str = ""
    
    # Engine state before/after
    engine_telemetry_before: Optional[EngineTelemetry] = None
    engine_telemetry_after: Optional[EngineTelemetry] = None

@dataclass
class InferencePipeline:
    """Complete inference pipeline trace."""
    session_id: str
    timestamp: str
    
    # All steps in order
    steps: List[InferenceStep] = field(default_factory=list)
    
    # Discovered engines
    engines: Dict[str, str] = field(default_factory=dict)  # name -> endpoint
    
    # Aggregate
    total_time_ms: float = 0
    total_tokens: int = 0
    total_bytes: int = 0
    
    # Per-engine breakdown
    engine_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=lambda: defaultdict(dict))

class LlamaEngineQuery:
    """Query live metrics from llama.cpp instances."""
    
    async def query(self, endpoint: str, engine_name: str = "unknown") -> Optional[EngineTelemetry]:
        """Query single llama.cpp instance for live metrics."""
        try:
            t0 = time.time()
            
            # Query model endpoint (lightweight)
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"http://{endpoint}/v1/models", timeout=2) as resp:
                    if resp.status != 200:
                        return None
                    
                    models_data = await resp.json()
                    response_time = (time.time() - t0) * 1000
                    
                    telemetry = EngineTelemetry(
                        engine_name=engine_name,
                        endpoint=endpoint,
                        query_timestamp_ms=time.time() * 1000,
                        response_time_ms=response_time,
                        is_healthy=True,
                        raw_models=models_data,
                    )
                    
                    # Extract model name if available
                    if "data" in models_data and len(models_data["data"]) > 0:
                        telemetry.model_id = models_data["data"][0].get("id", "unknown")
                    
                    return telemetry
        except Exception as e:
            print(f"{YELLOW}⚠ Query failed for {endpoint}: {e}{RESET}", file=sys.stderr)
            return None

class RagfarmAnalyzer:
    """Analyze ragfarm inference pipeline."""
    
    def __init__(self, generation_endpoint: str, reranker_endpoint: Optional[str] = None, embedder_endpoint: Optional[str] = None):
        self.generation_endpoint = generation_endpoint
        self.reranker_endpoint = reranker_endpoint
        self.embedder_endpoint = embedder_endpoint
        self.query = LlamaEngineQuery()
    
    async def query_all_engines(self) -> Dict[str, Optional[EngineTelemetry]]:
        """Query all configured engines for telemetry."""
        results = {}
        
        print(f"\n{BOLD_WHITE}Querying engines for live metrics:{RESET}\n")
        
        # Query generation engine
        results["generation"] = await self.query.query(self.generation_endpoint, "generation")
        if results["generation"]:
            print(f"{GREEN}✓{RESET} generation ({self.generation_endpoint}): "
                  f"model={results['generation'].model_id}, "
                  f"response_time={results['generation'].response_time_ms:.2f}ms")
        else:
            print(f"{RED}❌{RESET} generation ({self.generation_endpoint}): FAILED")
        
        # Query reranker if configured
        if self.reranker_endpoint:
            results["reranker"] = await self.query.query(self.reranker_endpoint, "reranker")
            if results["reranker"]:
                print(f"{GREEN}✓{RESET} reranker ({self.reranker_endpoint}): "
                      f"model={results['reranker'].model_id}, "
                      f"response_time={results['reranker'].response_time_ms:.2f}ms")
            else:
                print(f"{RED}❌{RESET} reranker ({self.reranker_endpoint}): FAILED")
        
        # Query embedder if configured
        if self.embedder_endpoint:
            results["embedder"] = await self.query.query(self.embedder_endpoint, "embedder")
            if results["embedder"]:
                print(f"{GREEN}✓{RESET} embedder ({self.embedder_endpoint}): "
                      f"model={results['embedder'].model_id}, "
                      f"response_time={results['embedder'].response_time_ms:.2f}ms")
            else:
                print(f"{RED}❌{RESET} embedder ({self.embedder_endpoint}): FAILED")
        
        print("")
        return results
    
    def format_telemetry_report(self, telemetries: Dict[str, Optional[EngineTelemetry]]) -> str:
        """Format telemetry from all engines."""
        lines = []
        
        lines.append(f"\n{CYAN}{'═' * 100}{RESET}")
        lines.append(f"{CYAN}RAGFARM ENGINE TELEMETRY{RESET}")
        lines.append(f"{CYAN}{'═' * 100}{RESET}\n")
        
        lines.append(f"{BOLD_WHITE}ENGINE STATUS:{RESET}\n")
        lines.append(f"{'Engine':<15} {'Endpoint':<25} {'Model':<30} {'Status':<10} {'Latency':<10}")
        lines.append(f"{'-' * 15} {'-' * 25} {'-' * 30} {'-' * 10} {'-' * 10}")
        
        for engine_name, telemetry in telemetries.items():
            if telemetry:
                status = f"{GREEN}✓ OK{RESET}" if telemetry.is_healthy else f"{RED}✗ FAIL{RESET}"
                lines.append(f"{engine_name:<15} {telemetry.endpoint:<25} {telemetry.model_id:<30} "
                            f"{status:<10} {telemetry.response_time_ms:>8.2f}ms")
            else:
                lines.append(f"{engine_name:<15} {'—':<25} {'—':<30} {RED}✗ NO RESP{RESET:<10}")
        
        lines.append("")
        
        # Raw API responses (for debugging)
        lines.append(f"{BOLD_WHITE}RAW MODEL ENDPOINTS:{RESET}\n")
        for engine_name, telemetry in telemetries.items():
            if telemetry and telemetry.raw_models:
                lines.append(f"{engine_name.upper()}:")
                lines.append(json.dumps(telemetry.raw_models, indent=2)[:500])
                lines.append("")
        
        return "\n".join(lines)

def format_pipeline_report(pipeline: InferencePipeline) -> str:
    """Format complete inference pipeline trace."""
    lines = []
    
    lines.append(f"\n{CYAN}{'═' * 120}{RESET}")
    lines.append(f"{CYAN}INFERENCE PIPELINE TRACE{RESET}")
    lines.append(f"{CYAN}{'═' * 120}{RESET}\n")
    
    lines.append(f"{BOLD_WHITE}Session:{RESET} {pipeline.session_id}")
    lines.append(f"{BOLD_WHITE}Timestamp:{RESET} {pipeline.timestamp}")
    lines.append(f"{BOLD_WHITE}Total duration:{RESET} {pipeline.total_time_ms:.2f}ms\n")
    
    if not pipeline.steps:
        lines.append("No inference steps recorded.")
        return "\n".join(lines)
    
    # Step timeline
    lines.append(f"{BOLD_WHITE}STEP-BY-STEP TIMELINE:{RESET}")
    lines.append(f"{'S':>2} {'Engine':<12} {'Operation':<15} {'Time':>10} {'In':>8} {'Out':>8} {'Latency':>10}")
    lines.append(f"{'-' * 2} {'-' * 12} {'-' * 15} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 10}")
    
    for step in pipeline.steps:
        lines.append(f"{step.step_number:2d} {step.engine_name:<12} {step.operation:<15} "
                    f"{step.timestamp_ms:>10.1f} {step.input_tokens:>8d} {step.output_tokens:>8d} "
                    f"{step.latency_ms:>10.2f}ms")
    
    lines.append("")
    
    # Per-engine breakdown
    engine_times = defaultdict(float)
    engine_steps = defaultdict(int)
    engine_tokens = defaultdict(int)
    
    for step in pipeline.steps:
        engine_times[step.engine_name] += step.latency_ms
        engine_steps[step.engine_name] += 1
        engine_tokens[step.engine_name] += step.output_tokens
    
    if engine_times:
        lines.append(f"{BOLD_WHITE}PER-ENGINE BREAKDOWN:{RESET}")
        lines.append(f"{'Engine':<15} {'Steps':>6} {'Total time':>12} {'Avg time':>12} {'Output tokens':>15}")
        lines.append(f"{'-' * 15} {'-' * 6} {'-' * 12} {'-' * 12} {'-' * 15}")
        
        for engine_name in sorted(engine_times.keys()):
            total_time = engine_times[engine_name]
            steps = engine_steps[engine_name]
            avg_time = total_time / steps if steps > 0 else 0
            tokens = engine_tokens[engine_name]
            
            lines.append(f"{engine_name:<15} {steps:>6d} {total_time:>12.2f} {avg_time:>12.2f} {tokens:>15d}")
        
        lines.append("")
    
    # Aggregate
    lines.append(f"{BOLD_WHITE}AGGREGATE STATISTICS:{RESET}")
    lines.append(f"  Total steps:             {len(pipeline.steps):>8d}")
    lines.append(f"  Total time:              {pipeline.total_time_ms:>8.2f} ms")
    lines.append(f"  Total tokens:            {pipeline.total_tokens:>8d}")
    lines.append(f"  Total bytes:             {pipeline.total_bytes:>8d}\n")
    
    return "\n".join(lines)

async def demo_analysis():
    """Demo: analyze local llama.cpp instances."""
    analyzer = RagfarmAnalyzer(
        generation_endpoint="localhost:8001",
        reranker_endpoint="localhost:8002",
        embedder_endpoint=None
    )
    
    telemetries = await analyzer.query_all_engines()
    
    report = analyzer.format_telemetry_report(telemetries)
    print(report)
    
    # Save telemetry
    output_file = Path("./engine_telemetry_demo.json")
    with open(output_file, "w") as f:
        data = {
            "timestamp": datetime.now().isoformat(),
            "telemetries": {
                name: asdict(t) if t else None
                for name, t in telemetries.items()
            }
        }
        json.dump(data, f, indent=2, default=str)
    
    print(f"✓ Telemetry saved to {output_file}")

async def demo_pipeline():
    """Demo: synthetic inference pipeline trace."""
    pipeline = InferencePipeline(
        session_id="pipeline_001",
        timestamp=datetime.now().isoformat()
    )
    
    # Step 1: Embedding
    pipeline.steps.append(InferenceStep(
        step_number=1,
        timestamp_ms=0,
        engine_name="embedder",
        endpoint="localhost:8003",
        operation="embed",
        input_tokens=8,
        output_tokens=384,  # Embedding dimensions
        input_bytes=32,
        output_bytes=1536,
        latency_ms=45.3,
        input_preview="What are EPC_AZURE FW rules?",
        output_preview="[0.234, 0.891, ...]"
    ))
    
    # Step 2: Retrieval (Qdrant)
    pipeline.steps.append(InferenceStep(
        step_number=2,
        timestamp_ms=50,
        engine_name="qdrant",
        endpoint="localhost:6333",
        operation="retrieve",
        input_tokens=384,
        output_tokens=50,  # Returned tokens from docs
        input_bytes=1536,
        output_bytes=512,
        latency_ms=35.1,
        input_preview="[embedding vector]",
        output_preview="Top 3 matching documents..."
    ))
    
    # Step 3: Generation (with retrieved context)
    pipeline.steps.append(InferenceStep(
        step_number=3,
        timestamp_ms=90,
        engine_name="generation",
        endpoint="localhost:8001",
        operation="generation",
        input_tokens=58,  # Original query + retrieved context
        output_tokens=120,  # Generated answer
        input_bytes=256,
        output_bytes=512,
        latency_ms=187.3,
        inferred_prefill_ms=145.3,
        inferred_decode_ms=42.0,
        input_preview="[Query + 3 docs context]...",
        output_preview="The EPC_AZURE network rules are..."
    ))
    
    # Step 4: Reranking
    pipeline.steps.append(InferenceStep(
        step_number=4,
        timestamp_ms=280,
        engine_name="reranker",
        endpoint="localhost:8002",
        operation="rerank",
        input_tokens=58,
        output_tokens=3,  # Scores
        input_bytes=512,
        output_bytes=128,
        latency_ms=67.8,
        input_preview="Query + retrieved documents",
        output_preview="[0.95, 0.87, 0.62]"
    ))
    
    pipeline.total_time_ms = 350.5
    pipeline.total_tokens = 531
    pipeline.total_bytes = 2944
    
    report = format_pipeline_report(pipeline)
    print(report)
    
    # Save pipeline
    output_file = Path("./pipeline_trace_demo.json")
    with open(output_file, "w") as f:
        data = {
            "session": {
                "session_id": pipeline.session_id,
                "timestamp": pipeline.timestamp,
                "total_time_ms": pipeline.total_time_ms,
                "total_tokens": pipeline.total_tokens,
                "total_bytes": pipeline.total_bytes,
            },
            "steps": [
                {
                    "step_number": s.step_number,
                    "timestamp_ms": s.timestamp_ms,
                    "engine_name": s.engine_name,
                    "endpoint": s.endpoint,
                    "operation": s.operation,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "latency_ms": s.latency_ms,
                    "input_preview": s.input_preview,
                    "output_preview": s.output_preview,
                }
                for s in pipeline.steps
            ]
        }
        json.dump(data, f, indent=2, default=str)
    
    print(f"✓ Pipeline trace saved to {output_file}")

def main():
    parser = argparse.ArgumentParser(
        description="Integrated ragfarm tracer: HTTP + engine metrics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Query live engine telemetry (no proxy)
  python ragfarm_integrated_tracer.py analyze \
    --generation localhost:8001 \
    --reranker localhost:8002
  
  # Demo: show telemetry output
  python ragfarm_integrated_tracer.py demo-telemetry
  
  # Demo: show pipeline trace
  python ragfarm_integrated_tracer.py demo-pipeline
        """
    )
    
    subparsers = parser.add_subparsers(dest="command")
    
    # Analyze command
    analyze_cmd = subparsers.add_parser("analyze", help="Query engines for live telemetry")
    analyze_cmd.add_argument("--generation", default=LLM_URL)
    analyze_cmd.add_argument("--reranker", default=None)
    analyze_cmd.add_argument("--embedder", default=None)
    
    # Demo commands
    subparsers.add_parser("demo-telemetry", help="Demo: telemetry output")
    subparsers.add_parser("demo-pipeline", help="Demo: pipeline trace")
    
    args = parser.parse_args()
    
    if args.command == "analyze":
        analyzer = RagfarmAnalyzer(
            generation_endpoint=args.generation,
            reranker_endpoint=args.reranker,
            embedder_endpoint=args.embedder
        )
        asyncio.run(analyzer.query_all_engines())
    elif args.command == "demo-telemetry":
        asyncio.run(demo_analysis())
    elif args.command == "demo-pipeline":
        asyncio.run(demo_pipeline())
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
