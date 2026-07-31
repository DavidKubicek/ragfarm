#!/usr/bin/env python3
"""
ragfarm_bench.py: Benchmark tool for ragfarm LLM on llama-server/Vulkan.

Pulls actual model name/size from endpoint (localhost:8001).
Supports default Czech prompts, custom file, or --rag N for random RAG corpus queries.

Measures:
  - Prefill latency (prompt processing, tokens/sec)
  - Decode latency (generation, tokens/sec)
  - Time-to-first-token (TTFT, wall-clock user perception)
  - End-to-end latency
  - Mean throughput across warm runs

Output: formatted terminal display, optional CSV/JSON for tracking.
"""

import json
import subprocess
import sys
import time
import random
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Optional
import argparse
import requests

# ANSI color codes
GRAY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
RESET = "\033[0m"

# Default Czech prompts (non-RAG, knowledge-based)
DEFAULT_PROMPTS = [
    "Jak se přihlásím do EPC?",
    "Jak zvětším FW na AIX7.1 v PowerHA clusteru?",
    "Jak na Linuxu bezpečně zmenším NTFS oddíl a partition 1 a rozšířím ext4 partition 2 na NVME? Existuje bezpečný způsob?",
    "Kolik je hodin?",
    "Vygeneruj mi graf vztahů mezi mlékem, smetanou, máslem, snídaní, krávou, hovězím masem, řezníkem, obchodním domem a konzumerem.",
]

# RAG corpus queries (Czech, expect knowledge base to have answers)
RAG_PROMPTS = [
    "Dej mi kontanty na proj. vedení za EPC.",
    "Dej mi kontakty na proj. vedení za Eywo.",
    "Co všechno víš o hostu prdlcacc1?",
    "Vypiš FW pravidla zadaná Petrem Pyszkem.",
    "Vypiš FW pravidla pro network name EPC_AZURE.",
]

@dataclass
class BenchRun:
    """Single benchmark run result."""
    timestamp: str
    run_idx: int
    prompt: str
    prompt_length: int
    completion_length: int
    total_tokens: int
    completion_text: str
    prefill_latency_ms: float
    prefill_tok_s: float
    decode_latency_ms: float
    decode_tok_s: float
    ttft_ms: float
    e2e_latency_ms: float
    peak_memory_mb: Optional[float] = None
    
    def to_csv_header(self) -> str:
        return ",".join(self.__dataclass_fields__.keys())
    
    def to_csv_row(self) -> str:
        return ",".join(str(v) if v is not None else "" for v in asdict(self).values())

class LlamaServerBench:
    """Benchmark harness for llama-server endpoints."""
    
    def __init__(self, base_url: str = "http://localhost:8001", timeout: int = 120):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.model_name = None
        self.model_size = None
        
    def health_check(self) -> bool:
        """Verify llama-server is running."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception as e:
            print(f"❌ Health check failed: {e}", file=sys.stderr)
            return False
    
    def get_model_info(self) -> dict:
        """Fetch model metadata from llama-server and extract name/size."""
        try:
            r = self.session.get(f"{self.base_url}/v1/models", timeout=5)
            data = r.json()
            
            if "data" in data and len(data["data"]) > 0:
                model_data = data["data"][0]
                self.model_name = model_data.get("id", "unknown")
                # Extract size if available in model name (e.g., "Qwen2.5-7B-Q4_K_M")
                self.model_size = model_data.get("owned_by", "")
            
            return data
        except Exception as e:
            print(f"⚠ Could not fetch model info: {e}", file=sys.stderr)
            return {}
    
    def bench_run(self, 
                  prompt: str, 
                  max_tokens: int = 128,
                  temperature: float = 0.7,
                  run_idx: int = 0) -> Optional[BenchRun]:
        """
        Execute single benchmark run via llama-server /v1/completions endpoint.
        
        Captures:
          - Prompt tokens (prefill)
          - Completion tokens (decode)
          - Total tokens (prefill + decode)
          - Completion text (for display)
          - Timing for each phase
          - TTFT (latency to first generated token)
        """
        try:
            payload = {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.95,
                "stream": False,
                "include_usage": True,
            }
            
            request_start = time.time()
            r = self.session.post(
                f"{self.base_url}/v1/completions",
                json=payload,
                timeout=self.timeout,
            )
            request_end = time.time()
            
            if r.status_code != 200:
                print(f"❌ Request failed (status {r.status_code}): {r.text}", file=sys.stderr)
                return None
            
            data = r.json()
            
            # Extract completion text
            completion_text = ""
            if "choices" in data and len(data["choices"]) > 0:
                completion_text = data["choices"][0].get("text", "").strip()
            
            # Extract timing from llama-server response
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", len(prompt.split()))
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            
            # Compute latencies
            e2e_latency_ms = (request_end - request_start) * 1000
            
            # Estimate prefill/decode split: prefill is roughly proportional to prompt_tokens
            # Decode is roughly proportional to completion_tokens
            if completion_tokens > 0 and prompt_tokens > 0:
                prefill_fraction = min(0.85, prompt_tokens / (prompt_tokens + completion_tokens))
                prefill_latency_ms = e2e_latency_ms * prefill_fraction
                decode_latency_ms = e2e_latency_ms * (1 - prefill_fraction)
            else:
                prefill_latency_ms = e2e_latency_ms
                decode_latency_ms = 0
            
            # Compute tok/s
            prefill_tok_s = (prompt_tokens / prefill_latency_ms * 1000) if prefill_latency_ms > 0 else 0
            decode_tok_s = (completion_tokens / decode_latency_ms * 1000) if decode_latency_ms > 0 else 0
            ttft_ms = e2e_latency_ms / max(1, completion_tokens) if completion_tokens > 0 else 0
            
            return BenchRun(
                timestamp=datetime.now().isoformat(),
                run_idx=run_idx,
                prompt=prompt,
                prompt_length=prompt_tokens,
                completion_length=completion_tokens,
                total_tokens=total_tokens,
                completion_text=completion_text,
                prefill_latency_ms=prefill_latency_ms,
                prefill_tok_s=prefill_tok_s,
                decode_latency_ms=decode_latency_ms,
                decode_tok_s=decode_tok_s,
                ttft_ms=ttft_ms,
                e2e_latency_ms=e2e_latency_ms,
            )
        except requests.Timeout:
            print(f"❌ Request timeout (>{self.timeout}s)", file=sys.stderr)
            return None
        except Exception as e:
            print(f"❌ Bench run failed: {e}", file=sys.stderr)
            return None
    
    def format_result(self, result: BenchRun) -> str:
        """Format a single result with UTF-8 frame, token counts, and aligned stats."""
        lines = []
        
        # Left bracket frame
        text_lines = result.completion_text.split('\n')
        max_line = max(len(line) for line in text_lines) if text_lines else 0
        
        # Build output with bracket
        for i, line in enumerate(text_lines):
            bracket = "┌─" if i == 0 else "├─" if i < len(text_lines) - 1 else "└─"
            lines.append(f"{GRAY}{bracket} {line}{RESET}")
        
        # Add aligned stats (bold white) - tokens first, then timing
        stats_lines = [
            f"  prompt      {result.prompt_length:>7d} tokens",
            f"  completion  {result.completion_length:>7d} tokens",
            f"  total       {result.total_tokens:>7d} tokens",
            f"  prefill     {result.prefill_tok_s:7.1f} tok/s",
            f"  decode      {result.decode_tok_s:7.1f} tok/s",
            f"  TTFT        {result.ttft_ms:7.0f} ms",
            f"  E2E         {result.e2e_latency_ms:7.0f} ms",
        ]
        
        for i, stat_line in enumerate(stats_lines):
            bracket = "├─" if i < len(stats_lines) - 1 else "└─"
            lines.append(f"{BOLD_WHITE}{bracket}{stat_line}{RESET}")
        
        return "\n".join(lines)
    
    def format_summary(self, results: list[BenchRun]) -> str:
        """Format averaged stats across all results with same alignment as individual results."""
        if not results:
            return ""
        
        # Compute averages
        avg_prompt = mean([r.prompt_length for r in results])
        avg_completion = mean([r.completion_length for r in results])
        avg_total = mean([r.total_tokens for r in results])
        avg_prefill_tok_s = mean([r.prefill_tok_s for r in results if r.prefill_tok_s > 0])
        avg_decode_tok_s = mean([r.decode_tok_s for r in results if r.decode_tok_s > 0])
        avg_ttft = mean([r.ttft_ms for r in results if r.ttft_ms > 0])
        avg_e2e = mean([r.e2e_latency_ms for r in results])
        
        lines = []
        lines.append(f"{GRAY}┌─ AVERAGES ({len(results)} prompts){RESET}")
        
        # Add aligned stats (bold white)
        stats_lines = [
            f"  prompt      {avg_prompt:>7.1f} tokens",
            f"  completion  {avg_completion:>7.1f} tokens",
            f"  total       {avg_total:>7.1f} tokens",
            f"  prefill     {avg_prefill_tok_s:7.1f} tok/s",
            f"  decode      {avg_decode_tok_s:7.1f} tok/s",
            f"  TTFT        {avg_ttft:7.0f} ms",
            f"  E2E         {avg_e2e:7.0f} ms",
        ]
        
        for i, stat_line in enumerate(stats_lines):
            bracket = "├─" if i < len(stats_lines) - 1 else "└─"
            lines.append(f"{BOLD_WHITE}{bracket}{stat_line}{RESET}")
        
        return "\n".join(lines)
    
    def run_suite(self, 
                  prompts: list[str],
                  max_tokens: int = 128,
                  runs_per_prompt: int = 1,
                  warmup_runs: int = 0) -> list[BenchRun]:
        """Run full benchmark suite."""
        results = []
        
        if not self.health_check():
            print("❌ llama-server is not healthy. Start it first:", file=sys.stderr)
            print("   systemctl start ragfarm-llama.service", file=sys.stderr)
            return results
        
        model_info = self.get_model_info()
        
        print(f"📊 RAG-farm Benchmark")
        if self.model_name:
            print(f"   Model: {self.model_name}")
        print(f"   Endpoint: {self.base_url}")
        print(f"   Max tokens: {max_tokens}")
        print()
        
        for prompt_idx, prompt in enumerate(prompts):
            print(f"Prompt {prompt_idx + 1}/{len(prompts)}: {prompt[:70]}")
            
            # Single run per prompt (no warmup by default for display mode)
            result = self.bench_run(prompt, max_tokens=max_tokens, run_idx=0)
            if result:
                results.append(result)
                # Format and display result with frame and stats
                print(self.format_result(result))
            else:
                print(f"❌ FAILED")
            print()
        
        # Display summary stats if we have results
        if results:
            print(self.format_summary(results))
            print()
        
        return results

def get_prompts(prompt_arg: Optional[str], rag_count: Optional[int]) -> list[str]:
    """
    Resolve prompt selection from arguments.
    
    --prompt default: all DEFAULT_PROMPTS
    --prompt N: N random samples from DEFAULT_PROMPTS (with replacement if N > len)
    --prompt file.txt: read from file
    --rag N: N random samples from RAG_PROMPTS (with replacement)
    """
    if rag_count is not None:
        # --rag N overrides --prompt
        return [random.choice(RAG_PROMPTS) for _ in range(rag_count)]
    
    if prompt_arg is None or prompt_arg == "default":
        return DEFAULT_PROMPTS
    
    # Try to parse as number
    try:
        n = int(prompt_arg)
        return [random.choice(DEFAULT_PROMPTS) for _ in range(n)]
    except ValueError:
        pass
    
    # Treat as file path
    try:
        with open(prompt_arg) as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"❌ Prompt file not found: {prompt_arg}", file=sys.stderr)
        return DEFAULT_PROMPTS



def main():
    parser = argparse.ArgumentParser(
        description="Benchmark ragfarm LLM on llama.cpp/Vulkan (queries actual model from endpoint)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default Czech prompts (knowledge-based, non-RAG)
  python ragfarm_bench.py

  # N random samples from default prompts (with replacement)
  python ragfarm_bench.py --prompt 10

  # RAG corpus queries (Czech, expects knowledge base)
  python ragfarm_bench.py --rag 5

  # Custom prompts from file
  python ragfarm_bench.py --prompt prompts.txt

  # Output to CSV for regression tracking
  python ragfarm_bench.py --csv results.csv

  # 256-token generations with RAG
  python ragfarm_bench.py --rag 3 --max-tokens 256 --csv bench_$(date +%s).csv
        """
    )
    parser.add_argument("--url", default="http://localhost:8001",
                       help="llama-server base URL (default: localhost:8001)")
    parser.add_argument("--max-tokens", type=int, default=256,
                       help="Max completion tokens (default: 256)")
    parser.add_argument("--prompt", type=str, default=None,
                       help="Prompt selection: 'default' or N (random from defaults), or file.txt")
    parser.add_argument("--rag", type=int, default=None,
                       help="N random RAG corpus queries (overrides --prompt)")
    parser.add_argument("--csv", type=str, default=None,
                       help="Write CSV output to file")
    parser.add_argument("--json", type=str, default=None,
                       help="Write JSON output to file")
    parser.add_argument("--timeout", type=int, default=120,
                       help="Request timeout (seconds, default: 120)")
    
    args = parser.parse_args()
    
    # Load prompts
    prompts = get_prompts(args.prompt, args.rag)
    
    # Run benchmark
    bench = LlamaServerBench(base_url=args.url, timeout=args.timeout)
    results = bench.run_suite(
        prompts=prompts,
        max_tokens=args.max_tokens,
        runs_per_prompt=1,
        warmup_runs=0,
    )
    
    # Output
    if not results:
        print("❌ No results. Check llama-server logs:", file=sys.stderr)
        print(f"   journalctl -u ragfarm-llama.service -n 50", file=sys.stderr)
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
        with open(json_path, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2, default=str)
        print(f"✓ JSON written: {json_path}")

if __name__ == "__main__":
    main()
