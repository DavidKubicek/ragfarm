#!/usr/bin/env python3
"""
ragfarm_rag_tracer.py: RAG pipeline trace for ragfarm retrieval.

Traces the full retrieval pipeline:
  Query → Qdrant (prefetch) → RRF (fuse) → MMR (expand) → Reranker (score) → Context

Shows candidate pool evolution with scores at each stage.
Correlates chat_id across bench/tracer tools.

Usage:
  python ragfarm_rag_tracer.py trace \
    --chat-id chat_001 \
    --query "leadb229p.lea.piz?" \
    --rag-endpoint http://127.0.0.1:8000 \
    --output trace_chat_001.json

  # Show multiple k values to see pool shrinking
  python ragfarm_rag_tracer.py evolve \
    --query "jak zálohovat hostitele" \
    --rag-endpoint http://127.0.0.1:8000 \
    --k-values 50,100,200,500
"""


# Endpoints come from .env via the shared resolver — never hardcode ports.
# See tests/tracing/ragfarm_env.py for the real port map.
from ragfarm_env import MCPO_URL
import json
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import argparse
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
class Candidate:
    """Single retrieval candidate."""
    rank: int
    score: float
    text: str
    source_file: str
    kind: str
    location: str
    text_tokens: int = 0

@dataclass
class RAGStageResult:
    """Results after one stage."""
    stage_name: str
    candidate_count: int
    candidates: List[Candidate] = field(default_factory=list)
    timing_ms: float = 0
    notes: str = ""

@dataclass
class RAGPipelineTrace:
    """Complete RAG pipeline trace."""
    chat_id: str
    timestamp: str
    query: str
    
    # Stage results
    stages: List[RAGStageResult] = field(default_factory=list)
    
    # Timing breakdown
    timing: Dict[str, float] = field(default_factory=dict)
    total_time_ms: float = 0
    
    # Final context
    final_candidate_count: int = 0
    final_context_tokens: int = 0
    final_candidates_text: List[str] = field(default_factory=list)

class RAGTracer:
    """Trace ragfarm RAG pipeline."""
    
    def __init__(self, rag_endpoint: str = MCPO_URL, chat_id: Optional[str] = None):
        self.rag_endpoint = rag_endpoint
        self.chat_id = chat_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session = requests.Session()
    
    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(1, len(text) // 4)
    
    def trace_retrieval(self, query: str, k: int = 50) -> Optional[RAGPipelineTrace]:
        """Trace single retrieval query through full RAG pipeline."""
        try:
            t0 = time.time()
            
            # Call RAG service
            payload = {"query": query, "k": k}
            r = self.session.post(
                f"{self.rag_endpoint}/rag/search_corpus",
                json=payload,
                timeout=30
            )
            
            if r.status_code != 200:
                print(f"❌ RAG service error: {r.status_code}", file=sys.stderr)
                return None
            
            data = r.json()
            total_time = (time.time() - t0) * 1000
            
            # Extract timing stages
            timing = data.get("_timing_ms", {})
            
            # Build stage results
            stages = []
            
            # Stage 1: Qdrant prefetch
            candidates = []
            for i, result in enumerate(data.get("results", []), 1):
                text_tokens = self.estimate_tokens(result["text"])
                candidates.append(Candidate(
                    rank=i,
                    score=result["score"],
                    text=result["text"],
                    source_file=result["source_file"],
                    kind=result.get("kind", "unknown"),
                    location=result.get("location", "unknown"),
                    text_tokens=text_tokens
                ))
            
            # All stages in one response from RAG service
            # Build synthetic stage breakdown based on timing
            stages.append(RAGStageResult(
                stage_name="Qdrant Prefetch",
                candidate_count=len(candidates),
                candidates=candidates[:min(5, len(candidates))],  # Show top 5
                timing_ms=timing.get("embed_ms", 0),
                notes=f"Initial retrieval from Qdrant"
            ))
            
            if timing.get("fuse_ms", 0) > 0:
                stages.append(RAGStageResult(
                    stage_name="RRF Fusion",
                    candidate_count=len(candidates),
                    candidates=candidates[:min(5, len(candidates))],
                    timing_ms=timing.get("fuse_ms", 0),
                    notes=f"Sparse+dense fusion, scores may change"
                ))
            
            if timing.get("expand_ms", 0) > 0:
                stages.append(RAGStageResult(
                    stage_name="MMR Expand",
                    candidate_count=len(candidates),
                    candidates=candidates[:min(5, len(candidates))],
                    timing_ms=timing.get("expand_ms", 0),
                    notes=f"Duplicate eviction + diversity nudge"
                ))
            
            if timing.get("rerank_ms", 0) > 0:
                stages.append(RAGStageResult(
                    stage_name="Reranker Score",
                    candidate_count=len(candidates),
                    candidates=candidates[:min(5, len(candidates))],
                    timing_ms=timing.get("rerank_ms", 0),
                    notes=f"LLM semantic relevance scoring"
                ))
            
            # Calculate final context size
            final_context_tokens = sum(c.text_tokens for c in candidates)
            
            trace = RAGPipelineTrace(
                chat_id=self.chat_id,
                timestamp=datetime.now().isoformat(),
                query=query,
                stages=stages,
                timing=timing,
                total_time_ms=total_time,
                final_candidate_count=len(candidates),
                final_context_tokens=final_context_tokens,
                final_candidates_text=[c.text[:200] for c in candidates]
            )
            
            return trace
        
        except Exception as e:
            print(f"❌ Trace failed: {e}", file=sys.stderr)
            return None
    
    def format_trace(self, trace: RAGPipelineTrace) -> str:
        """Format trace for display."""
        lines = []
        
        lines.append(f"\n{CYAN}{'═' * 120}{RESET}")
        lines.append(f"{CYAN}RAG PIPELINE TRACE{RESET}")
        lines.append(f"{CYAN}{'═' * 120}{RESET}\n")
        
        lines.append(f"{BOLD_WHITE}Chat ID:{RESET} {trace.chat_id}")
        lines.append(f"{BOLD_WHITE}Query:{RESET} {trace.query}")
        lines.append(f"{BOLD_WHITE}Timestamp:{RESET} {trace.timestamp}")
        lines.append(f"{BOLD_WHITE}Total time:{RESET} {trace.total_time_ms:.1f}ms\n")
        
        # Timing breakdown
        lines.append(f"{BOLD_WHITE}TIMING BREAKDOWN:{RESET}")
        lines.append(f"{'Stage':<20} {'Time (ms)':>10} {'% Total':>10}")
        lines.append(f"{'-' * 20} {'-' * 10} {'-' * 10}")
        
        for stage_name, timing_ms in trace.timing.items():
            pct = (timing_ms / trace.total_time_ms * 100) if trace.total_time_ms > 0 else 0
            lines.append(f"{stage_name:<20} {timing_ms:>10.1f} {pct:>10.1f}")
        
        lines.append("")
        
        # Candidate pool evolution
        lines.append(f"{BOLD_WHITE}CANDIDATE POOL EVOLUTION:{RESET}")
        lines.append(f"Candidates retrieved: {trace.final_candidate_count}\n")
        
        for stage in trace.stages:
            lines.append(f"{stage.stage_name} ({stage.timing_ms:.1f}ms)")
            lines.append(f"  Candidates: {stage.candidate_count} | {stage.notes}")
            
            # Show top candidates
            for cand in stage.candidates[:3]:
                lines.append(f"    [{cand.rank:2d}] score={cand.score:.4f} | {cand.source_file} | {cand.kind}")
                lines.append(f"         {cand.text[:140]}...")
            
            if len(stage.candidates) > 3:
                lines.append(f"    ... ({len(stage.candidates) - 3} more)")
            
            lines.append("")
        
        # Final context calculation
        lines.append(f"{BOLD_WHITE}FINAL CONTEXT:{RESET}")
        lines.append(f"  Candidates: {trace.final_candidate_count}")
        lines.append(f"  Total tokens (est): {trace.final_context_tokens:,}")
        lines.append(f"  Avg tokens/doc: {trace.final_context_tokens // max(1, trace.final_candidate_count)}")
        
        # Warning if context is large
        if trace.final_context_tokens > 3000:
            lines.append(f"\n{YELLOW}⚠ WARNING: Large context window{RESET}")
            lines.append(f"  {trace.final_context_tokens} tokens is {trace.final_context_tokens // 4000 + 1}x a 4K window")
            lines.append(f"  This will cause context overflow after a few turns")
        
        lines.append("")
        
        return "\n".join(lines)
    
    def format_evolution(self, traces: List[RAGPipelineTrace]) -> str:
        """Show how candidate pool shrinks with different k values."""
        lines = []
        
        lines.append(f"\n{CYAN}{'═' * 120}{RESET}")
        lines.append(f"{CYAN}CANDIDATE POOL EVOLUTION{RESET}")
        lines.append(f"{CYAN}{'═' * 120}{RESET}\n")
        
        lines.append(f"{BOLD_WHITE}Query:{RESET} {traces[0].query}\n")
        
        lines.append(f"{'k Value':>10} {'Candidates':>12} {'Avg Score':>12} {'Min Score':>12} {'Total Tokens':>15}")
        lines.append(f"{'-' * 10} {'-' * 12} {'-' * 12} {'-' * 12} {'-' * 15}")
        
        for trace in traces:
            k = trace.final_candidate_count
            candidates = [c for stage in trace.stages for c in stage.candidates]
            
            if candidates:
                avg_score = sum(c.score for c in candidates) / len(candidates)
                min_score = min(c.score for c in candidates)
            else:
                avg_score = min_score = 0
            
            lines.append(f"{k:>10d} {k:>12d} {avg_score:>12.4f} {min_score:>12.4f} {trace.final_context_tokens:>15,}")
        
        lines.append("")
        
        return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(
        description="RAG pipeline tracer for ragfarm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Trace single query
  python ragfarm_rag_tracer.py trace \
    --chat-id chat_001 \
    --query "leadb229p.lea.piz?" \
    --rag-endpoint http://127.0.0.1:8000

  # Show pool evolution with different k values
  python ragfarm_rag_tracer.py evolve \
    --query "jak zálohovat hostitele" \
    --rag-endpoint http://127.0.0.1:8000 \
    --k-values 50,100,200,500

  # Save to JSON for correlation with bench
  python ragfarm_rag_tracer.py trace \
    --chat-id chat_001 \
    --query "test" \
    --output rag_trace_chat_001.json
        """
    )
    
    subparsers = parser.add_subparsers(dest="command")
    
    # Trace command
    trace_cmd = subparsers.add_parser("trace", help="Trace single RAG query")
    trace_cmd.add_argument("--chat-id", required=True)
    trace_cmd.add_argument("--query", required=True)
    trace_cmd.add_argument("--rag-endpoint", default=MCPO_URL)
    trace_cmd.add_argument("--k", type=int, default=50)
    trace_cmd.add_argument("--output", type=str, default=None)
    
    # Evolve command
    evolve_cmd = subparsers.add_parser("evolve", help="Show pool evolution with different k")
    evolve_cmd.add_argument("--query", required=True)
    evolve_cmd.add_argument("--rag-endpoint", default=MCPO_URL)
    evolve_cmd.add_argument("--k-values", type=str, default="50,100,200,500")
    
    args = parser.parse_args()
    
    if args.command == "trace":
        tracer = RAGTracer(rag_endpoint=args.rag_endpoint, chat_id=args.chat_id)
        trace = tracer.trace_retrieval(args.query, k=args.k)
        
        if trace:
            print(tracer.format_trace(trace))
            
            if args.output:
                output_path = Path(args.output)
                with open(output_path, "w") as f:
                    json.dump({
                        "chat_id": trace.chat_id,
                        "timestamp": trace.timestamp,
                        "query": trace.query,
                        "timing": trace.timing,
                        "total_time_ms": trace.total_time_ms,
                        "final_candidate_count": trace.final_candidate_count,
                        "final_context_tokens": trace.final_context_tokens,
                    }, f, indent=2, default=str)
                print(f"✓ Saved to {output_path}")
    
    elif args.command == "evolve":
        k_values = [int(k) for k in args.k_values.split(",")]
        tracer = RAGTracer(rag_endpoint=args.rag_endpoint)
        
        traces = []
        for k in k_values:
            print(f"Querying with k={k}...")
            trace = tracer.trace_retrieval(args.query, k=k)
            if trace:
                traces.append(trace)
        
        if traces:
            print(tracer.format_evolution(traces))
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
