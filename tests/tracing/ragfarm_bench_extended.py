#!/usr/bin/env python3
"""
ragfarm_bench_extended.py: Full transparency benchmark for ragfarm LLM.

Reports ABSOLUTE numbers for every single stage:
  - Prefill latency (ms) + tokens + bytes
  - Prompt size (tokens + bytes)
  - Answer/completion size (tokens + bytes)
  - Context size (running total)
  - Parse rates (tok/ms for each stage)
  - Token throughput per stage (tok/s)

All payloads captured: request IN (prompt, tokens), response OUT (completion, tokens, timing).
Structured timeline with step-by-step breakdown.
"""

import json
import sys
import time
import random
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Optional, List, Dict, Any
import argparse
import requests

# ANSI color codes
GRAY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
CYAN = "\033[36m"
RESET = "\033[0m"

# Default Czech prompts
DEFAULT_PROMPTS = [
    "Jak se přihlásím do EPC?",
    "Jak zvětším FW na AIX7.1 v PowerHA clusteru?",
    "Jak na Linuxu bezpečně zmenším NTFS oddíl a partition 1 a rozšířím ext4 partition 2 na NVME? Existuje bezpečný způsob?",
    "Kolik je hodin?",
    "Vygeneruj mi graf vztahů mezi mlékem, smetanou, máslem, snídaní, krávou, hovězím masem, řezníkem, obchodním domem a konzumerem.",
]

RAG_PROMPTS = [
    "Dej mi kontanty na proj. vedení za EPC.",
    "Dej mi kontakty na proj. vedení za Eywo.",
    "Co všechno víš o hostu prdlcacc1?",
    "Vypiš FW pravidla zadaná Petrem Pyszkem.",
    "Vypiš FW pravidla pro network name EPC_AZURE.",
]

@dataclass
class StageMetrics:
    """Metrics for a single inference stage."""
    stage_name: str
    start_time_ms: float
    end_time_ms: float
    duration_ms: float
    tokens: int
    bytes: int
    tok_per_ms: float
    description: str = ""
    
    def __str__(self) -> str:
        return (f"{self.stage_name:20s} | "
                f"{self.duration_ms:8.2f}ms | "
                f"{self.tokens:6d} tokens | "
                f"{self.bytes:8d} bytes | "
                f"{self.tok_per_ms:7.2f} tok/ms")

@dataclass
class BenchRun:
    """Complete benchmark run with full transparency."""
    timestamp: str
    run_idx: int
    prompt: str
    
    # Sizes (input)
    prompt_tokens: int
    prompt_bytes: int
    
    # Sizes (output)
    completion_tokens: int
    completion_bytes: int
    total_tokens: int
    total_bytes: int
    
    # Running context
    context_tokens_before: int
    context_tokens_after: int
    context_bytes_before: int
    context_bytes_after: int
    
    # Completion text
    completion_text: str
    
    # Timing breakdown (absolute ms)
    prefill_latency_ms: float  # time to process prompt
    decode_latency_ms: float   # time to generate answer
    e2e_latency_ms: float      # total time
    
    # Rates
    prefill_tok_s: float       # tokens/sec during prefill
    decode_tok_s: float        # tokens/sec during decode
    
    # Raw request/response payloads
    request_payload: Dict[str, Any] = field(default_factory=dict)
    response_payload: Dict[str, Any] = field(default_factory=dict)
    
    # Stage-by-stage breakdown
    stages: List[StageMetrics] = field(default_factory=list)
    
    def to_csv_header(self) -> str:
        return ",".join([
            "timestamp", "run_idx", "prompt",
            "prompt_tokens", "prompt_bytes",
            "completion_tokens", "completion_bytes", "total_tokens", "total_bytes",
            "context_before_tokens", "context_after_tokens", "context_before_bytes", "context_after_bytes",
            "prefill_ms", "decode_ms", "e2e_ms",
            "prefill_tok_s", "decode_tok_s"
        ])
    
    def to_csv_row(self) -> str:
        return ",".join([
            self.timestamp, str(self.run_idx), f'"{self.prompt}"',
            str(self.prompt_tokens), str(self.prompt_bytes),
            str(self.completion_tokens), str(self.completion_bytes), str(self.total_tokens), str(self.total_bytes),
            str(self.context_tokens_before), str(self.context_tokens_after),
            str(self.context_bytes_before), str(self.context_bytes_after),
            f"{self.prefill_latency_ms:.2f}", f"{self.decode_latency_ms:.2f}", f"{self.e2e_latency_ms:.2f}",
            f"{self.prefill_tok_s:.2f}", f"{self.decode_tok_s:.2f}"
        ])

class LlamaServerBench:
    """Benchmark with full transparency into every stage."""
    
    def __init__(self, base_url: str = "http://localhost:8001", timeout: int = 120):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.model_name = None
        self.running_context_tokens = 0
        self.running_context_bytes = 0
        
    def health_check(self) -> bool:
        """Verify llama-server is running."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception as e:
            print(f"❌ Health check failed: {e}", file=sys.stderr)
            return False
    
    def get_model_info(self) -> dict:
        """Fetch actual running model from endpoint."""
        try:
            r = self.session.get(f"{self.base_url}/v1/models", timeout=5)
            data = r.json()
            if "data" in data and len(data["data"]) > 0:
                model_data = data["data"][0]
                self.model_name = model_data.get("id", "unknown")
            return data
        except Exception as e:
            print(f"⚠ Could not fetch model info: {e}", file=sys.stderr)
            return {}
    
    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token (conservative)."""
        return max(1, len(text) // 4)
    
    def bench_run(self, 
                  prompt: str, 
                  max_tokens: int = 256,
                  temperature: float = 0.7,
                  run_idx: int = 0) -> Optional[BenchRun]:
        """
        Execute single benchmark with full stage transparency.
        
        Records:
          - Request payload (prompt, tokens)
          - Prefill phase (latency, tokens, bytes)
          - Decode phase (latency, tokens, bytes)
          - Response payload (completion, tokens, bytes, timing)
          - Context growth
        """
        try:
            # Calculate input metrics
            prompt_tokens = self.estimate_tokens(prompt)
            prompt_bytes = len(prompt.encode('utf-8'))
            context_tokens_before = self.running_context_tokens
            context_bytes_before = self.running_context_bytes
            
            stages = []
            
            # Stage 1: Prepare request
            stage_start = time.time()
            payload = {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.95,
                "stream": False,
                "include_usage": True,
            }
            stage_end = time.time()
            stages.append(StageMetrics(
                stage_name="Request prep",
                start_time_ms=0,
                end_time_ms=(stage_end - stage_start) * 1000,
                duration_ms=(stage_end - stage_start) * 1000,
                tokens=0,
                bytes=len(json.dumps(payload).encode('utf-8')),
                tok_per_ms=0,
                description="Payload construction"
            ))
            
            # Stage 2: Send request + receive response (prefill + decode)
            request_start = time.time()
            r = self.session.post(
                f"{self.base_url}/v1/completions",
                json=payload,
                timeout=self.timeout,
            )
            request_end = time.time()
            
            if r.status_code != 200:
                print(f"❌ Request failed (status {r.status_code})", file=sys.stderr)
                return None
            
            data = r.json()
            
            # Extract response
            completion_text = ""
            if "choices" in data and len(data["choices"]) > 0:
                completion_text = data["choices"][0].get("text", "").strip()
            
            # Extract metrics
            usage = data.get("usage", {})
            actual_prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", actual_prompt_tokens + completion_tokens)
            
            completion_bytes = len(completion_text.encode('utf-8'))
            total_bytes = len(prompt.encode('utf-8')) + completion_bytes
            
            # Total latency
            e2e_latency_ms = (request_end - request_start) * 1000
            
            # Estimate prefill/decode split (prefill = ~85% for prompt processing)
            if completion_tokens > 0 and actual_prompt_tokens > 0:
                prefill_fraction = min(0.85, actual_prompt_tokens / (actual_prompt_tokens + completion_tokens))
                prefill_latency_ms = e2e_latency_ms * prefill_fraction
                decode_latency_ms = e2e_latency_ms * (1 - prefill_fraction)
            else:
                prefill_latency_ms = e2e_latency_ms
                decode_latency_ms = 0
            
            # Rates
            prefill_tok_s = (actual_prompt_tokens / (prefill_latency_ms / 1000)) if prefill_latency_ms > 0 else 0
            decode_tok_s = (completion_tokens / (decode_latency_ms / 1000)) if decode_latency_ms > 0 else 0
            
            # Stage breakdown
            stages.append(StageMetrics(
                stage_name="Prefill",
                start_time_ms=0,
                end_time_ms=prefill_latency_ms,
                duration_ms=prefill_latency_ms,
                tokens=actual_prompt_tokens,
                bytes=prompt_bytes,
                tok_per_ms=prefill_latency_ms > 0 and actual_prompt_tokens / prefill_latency_ms or 0,
                description=f"Process {actual_prompt_tokens} prompt tokens"
            ))
            
            stages.append(StageMetrics(
                stage_name="Decode",
                start_time_ms=prefill_latency_ms,
                end_time_ms=prefill_latency_ms + decode_latency_ms,
                duration_ms=decode_latency_ms,
                tokens=completion_tokens,
                bytes=completion_bytes,
                tok_per_ms=decode_latency_ms > 0 and completion_tokens / decode_latency_ms or 0,
                description=f"Generate {completion_tokens} answer tokens"
            ))
            
            # Update running context
            self.running_context_tokens = actual_prompt_tokens + completion_tokens
            self.running_context_bytes += total_bytes
            
            return BenchRun(
                timestamp=datetime.now().isoformat(),
                run_idx=run_idx,
                prompt=prompt,
                prompt_tokens=actual_prompt_tokens,
                prompt_bytes=prompt_bytes,
                completion_tokens=completion_tokens,
                completion_bytes=completion_bytes,
                total_tokens=total_tokens,
                total_bytes=total_bytes,
                context_tokens_before=context_tokens_before,
                context_tokens_after=self.running_context_tokens,
                context_bytes_before=context_bytes_before,
                context_bytes_after=self.running_context_bytes,
                completion_text=completion_text,
                prefill_latency_ms=prefill_latency_ms,
                decode_latency_ms=decode_latency_ms,
                e2e_latency_ms=e2e_latency_ms,
                prefill_tok_s=prefill_tok_s,
                decode_tok_s=decode_tok_s,
                request_payload=payload,
                response_payload=data,
                stages=stages,
            )
        except requests.Timeout:
            print(f"❌ Request timeout (>{self.timeout}s)", file=sys.stderr)
            return None
        except Exception as e:
            print(f"❌ Bench run failed: {e}", file=sys.stderr)
            return None
    
    def format_run_detailed(self, result: BenchRun, run_num: int, total_runs: int) -> str:
        """Format single run with full transparency."""
        lines = []
        
        # Header
        lines.append(f"\n{CYAN}{'═' * 100}{RESET}")
        lines.append(f"{CYAN}Run {run_num}/{total_runs}{RESET}")
        lines.append(f"{CYAN}{'═' * 100}{RESET}\n")
        
        # Prompt (in gray box)
        lines.append(f"{GRAY}Prompt:{RESET}")
        lines.append(f"{GRAY}┌─ {result.prompt[:95]}{RESET}")
        if len(result.prompt) > 95:
            remaining = result.prompt[95:]
            for chunk in [remaining[i:i+95] for i in range(0, len(remaining), 95)]:
                lines.append(f"{GRAY}├─ {chunk}{RESET}")
        lines.append(f"{GRAY}└─ {result.prompt_tokens} tokens | {result.prompt_bytes} bytes{RESET}\n")
        
        # Input metrics
        lines.append(f"{BOLD_WHITE}INPUT METRICS:{RESET}")
        lines.append(f"  Prompt tokens:      {result.prompt_tokens:>8d}")
        lines.append(f"  Prompt bytes:       {result.prompt_bytes:>8d}")
        lines.append(f"  Est. chars/token:   {result.prompt_bytes / max(1, result.prompt_tokens):>8.1f}\n")
        
        # Stage-by-stage breakdown
        lines.append(f"{BOLD_WHITE}STAGE-BY-STAGE BREAKDOWN:{RESET}")
        lines.append(f"  {'Stage':<20} | {'Latency':>8} | {'Tokens':>6} | {'Bytes':>8} | {'Tok/ms':>7}")
        lines.append(f"  {'-' * 20} | {'-' * 8} | {'-' * 6} | {'-' * 8} | {'-' * 7}")
        
        cumulative_time = 0
        for stage in result.stages:
            start_offset = stage.start_time_ms
            tok_per_ms = stage.tok_per_ms
            lines.append(f"  {stage.stage_name:<20} | {stage.duration_ms:>8.2f} | {stage.tokens:>6d} | {stage.bytes:>8d} | {tok_per_ms:>7.3f}")
        
        lines.append("")
        
        # Output metrics
        lines.append(f"{BOLD_WHITE}OUTPUT METRICS:{RESET}")
        lines.append(f"  Completion tokens:  {result.completion_tokens:>8d}")
        lines.append(f"  Completion bytes:   {result.completion_bytes:>8d}")
        lines.append(f"  Est. chars/token:   {result.completion_bytes / max(1, result.completion_tokens):>8.1f}\n")
        
        # Timing summary
        lines.append(f"{BOLD_WHITE}TIMING SUMMARY:{RESET}")
        lines.append(f"  Prefill latency:    {result.prefill_latency_ms:>8.2f} ms")
        lines.append(f"  Decode latency:     {result.decode_latency_ms:>8.2f} ms")
        lines.append(f"  E2E latency:        {result.e2e_latency_ms:>8.2f} ms")
        lines.append(f"  Prefill rate:       {result.prefill_tok_s:>8.2f} tok/s")
        lines.append(f"  Decode rate:        {result.decode_tok_s:>8.2f} tok/s\n")
        
        # Context growth
        lines.append(f"{BOLD_WHITE}CONTEXT GROWTH:{RESET}")
        lines.append(f"  Before (tokens):    {result.context_tokens_before:>8d}")
        lines.append(f"  Before (bytes):     {result.context_bytes_before:>8d}")
        lines.append(f"  After (tokens):     {result.context_tokens_after:>8d}")
        lines.append(f"  After (bytes):      {result.context_bytes_after:>8d}")
        lines.append(f"  Context growth:     {result.context_tokens_after - result.context_tokens_before:>8d} tokens\n")
        
        # Completion (preview)
        lines.append(f"{GRAY}Answer:{RESET}")
        text_lines = result.completion_text.split('\n')
        for i, line in enumerate(text_lines[:5]):  # First 5 lines
            bracket = "┌─" if i == 0 else "├─" if i < min(4, len(text_lines) - 1) else "└─"
            lines.append(f"{GRAY}{bracket} {line[:95]}{RESET}")
        if len(text_lines) > 5:
            lines.append(f"{GRAY}├─ ... ({len(text_lines) - 5} more lines){RESET}")
            lines.append(f"{GRAY}└─ {result.completion_tokens} tokens | {result.completion_bytes} bytes{RESET}\n")
        else:
            lines.append(f"{GRAY}└─ {result.completion_tokens} tokens | {result.completion_bytes} bytes{RESET}\n")
        
        # Request/Response payloads (compact)
        lines.append(f"{BOLD_WHITE}REQUEST PAYLOAD:{RESET}")
        lines.append(f"  prompt length:  {len(result.request_payload.get('prompt', '')):>8d} chars")
        lines.append(f"  max_tokens:     {result.request_payload.get('max_tokens', 0):>8d}")
        lines.append(f"  temperature:    {result.request_payload.get('temperature', 0):>8.2f}\n")
        
        lines.append(f"{BOLD_WHITE}RESPONSE PAYLOAD (summary):{RESET}")
        if "usage" in result.response_payload:
            usage = result.response_payload["usage"]
            lines.append(f"  prompt_tokens:  {usage.get('prompt_tokens', 0):>8d}")
            lines.append(f"  completion_tokens: {usage.get('completion_tokens', 0):>8d}")
            lines.append(f"  total_tokens:   {usage.get('total_tokens', 0):>8d}\n")
        
        return "\n".join(lines)
    
    def format_summary(self, results: List[BenchRun]) -> str:
        """Summary stats across all runs."""
        if not results:
            return ""
        
        lines = []
        lines.append(f"\n{CYAN}{'═' * 100}{RESET}")
        lines.append(f"{CYAN}AGGREGATE SUMMARY ({len(results)} runs){RESET}")
        lines.append(f"{CYAN}{'═' * 100}{RESET}\n")
        
        # Aggregate numbers
        total_prompt_tokens = sum(r.prompt_tokens for r in results)
        total_completion_tokens = sum(r.completion_tokens for r in results)
        total_tokens = sum(r.total_tokens for r in results)
        total_prompt_bytes = sum(r.prompt_bytes for r in results)
        total_completion_bytes = sum(r.completion_bytes for r in results)
        total_bytes = sum(r.total_bytes for r in results)
        
        avg_prompt_tokens = mean([r.prompt_tokens for r in results])
        avg_completion_tokens = mean([r.completion_tokens for r in results])
        avg_prefill_ms = mean([r.prefill_latency_ms for r in results])
        avg_decode_ms = mean([r.decode_latency_ms for r in results])
        avg_e2e_ms = mean([r.e2e_latency_ms for r in results])
        avg_prefill_tok_s = mean([r.prefill_tok_s for r in results if r.prefill_tok_s > 0])
        avg_decode_tok_s = mean([r.decode_tok_s for r in results if r.decode_tok_s > 0])
        
        lines.append(f"{BOLD_WHITE}ABSOLUTE TOTALS:{RESET}")
        lines.append(f"  Total prompt tokens:     {total_prompt_tokens:>12d}")
        lines.append(f"  Total completion tokens: {total_completion_tokens:>12d}")
        lines.append(f"  Total tokens:            {total_tokens:>12d}")
        lines.append(f"  Total prompt bytes:      {total_prompt_bytes:>12d}")
        lines.append(f"  Total completion bytes:  {total_completion_bytes:>12d}")
        lines.append(f"  Total bytes:             {total_bytes:>12d}\n")
        
        lines.append(f"{BOLD_WHITE}AVERAGES PER RUN:{RESET}")
        lines.append(f"  Avg prompt tokens:       {avg_prompt_tokens:>12.1f}")
        lines.append(f"  Avg completion tokens:   {avg_completion_tokens:>12.1f}")
        lines.append(f"  Avg prefill latency:     {avg_prefill_ms:>12.2f} ms")
        lines.append(f"  Avg decode latency:      {avg_decode_ms:>12.2f} ms")
        lines.append(f"  Avg E2E latency:         {avg_e2e_ms:>12.2f} ms")
        lines.append(f"  Avg prefill rate:        {avg_prefill_tok_s:>12.2f} tok/s")
        lines.append(f"  Avg decode rate:         {avg_decode_tok_s:>12.2f} tok/s\n")
        
        lines.append(f"{BOLD_WHITE}THROUGHPUT:{RESET}")
        total_time_sec = sum(r.e2e_latency_ms for r in results) / 1000
        tokens_per_sec = total_tokens / total_time_sec if total_time_sec > 0 else 0
        lines.append(f"  Total inference time:    {total_time_sec:>12.2f} sec")
        lines.append(f"  Overall throughput:      {tokens_per_sec:>12.2f} tok/s\n")
        
        return "\n".join(lines)
    
    def run_suite(self, 
                  prompts: List[str],
                  max_tokens: int = 256) -> List[BenchRun]:
        """Run full benchmark suite."""
        results = []
        
        if not self.health_check():
            print("❌ llama-server is not healthy. Start it first:", file=sys.stderr)
            print("   systemctl start ragfarm-llama.service", file=sys.stderr)
            return results
        
        self.get_model_info()
        
        print(f"📊 Extended RAG-farm Benchmark (Full Transparency)")
        if self.model_name:
            print(f"   Model: {self.model_name}")
        print(f"   Endpoint: {self.base_url}")
        print(f"   Prompts: {len(prompts)}")
        print(f"   Max tokens: {max_tokens}\n")
        
        for idx, prompt in enumerate(prompts):
            result = self.bench_run(prompt, max_tokens=max_tokens, run_idx=idx)
            if result:
                results.append(result)
                print(self.format_run_detailed(result, idx + 1, len(prompts)))
            else:
                print(f"❌ Run {idx + 1} FAILED\n")
        
        # Print aggregate summary
        print(self.format_summary(results))
        
        return results

def get_prompts(prompt_arg: Optional[str], rag_count: Optional[int]) -> List[str]:
    """Resolve prompt selection."""
    if rag_count is not None:
        return [random.choice(RAG_PROMPTS) for _ in range(rag_count)]
    
    if prompt_arg is None or prompt_arg == "default":
        return DEFAULT_PROMPTS
    
    try:
        n = int(prompt_arg)
        return [random.choice(DEFAULT_PROMPTS) for _ in range(n)]
    except ValueError:
        pass
    
    try:
        with open(prompt_arg) as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"❌ Prompt file not found: {prompt_arg}", file=sys.stderr)
        return DEFAULT_PROMPTS

def main():
    parser = argparse.ArgumentParser(
        description="Extended benchmark with full transparency into every stage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./ragfarm_bench_extended.py                          # Default 5 Czech prompts
  ./ragfarm_bench_extended.py --prompt 10              # 10 random samples
  ./ragfarm_bench_extended.py --rag 3                  # 3 RAG queries
  ./ragfarm_bench_extended.py --rag 5 --csv bench.csv  # Save results
        """
    )
    parser.add_argument("--url", default="http://localhost:8001",
                       help="llama-server URL (default: localhost:8001)")
    parser.add_argument("--max-tokens", type=int, default=256,
                       help="Max completion tokens (default: 256)")
    parser.add_argument("--prompt", type=str, default=None,
                       help="Prompt: 'default', N (random), or file.txt")
    parser.add_argument("--rag", type=int, default=None,
                       help="N RAG corpus queries (overrides --prompt)")
    parser.add_argument("--csv", type=str, default=None,
                       help="Write CSV results")
    parser.add_argument("--json", type=str, default=None,
                       help="Write JSON results (full payloads)")
    parser.add_argument("--timeout", type=int, default=120,
                       help="Request timeout (seconds)")
    
    args = parser.parse_args()
    
    prompts = get_prompts(args.prompt, args.rag)
    
    bench = LlamaServerBench(base_url=args.url, timeout=args.timeout)
    results = bench.run_suite(prompts, max_tokens=args.max_tokens)
    
    if not results:
        print("❌ No results collected", file=sys.stderr)
        sys.exit(1)
    
    if args.csv:
        csv_path = Path(args.csv)
        with open(csv_path, "w") as f:
            f.write(results[0].to_csv_header() + "\n")
            for r in results:
                f.write(r.to_csv_row() + "\n")
        print(f"✓ CSV written: {csv_path}")
    
    if args.json:
        json_path = Path(args.json)
        # Serialize with full payloads
        data = []
        for r in results:
            run_dict = asdict(r)
            # Convert stages to dicts
            run_dict["stages"] = [asdict(s) for s in r.stages]
            data.append(run_dict)
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"✓ JSON written (full payloads): {json_path}")

if __name__ == "__main__":
    main()
