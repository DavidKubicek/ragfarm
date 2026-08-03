#!/usr/bin/env python3
"""
ragfarm_bench_chatid.py: Enhanced benchmark with chat session tracking and context measurement.

Adds:
1. Unique chat_id for correlating records across bench/tracer tools
2. Context size tracking from OpenAI spec (prompt_tokens, completion_tokens, total_tokens)
3. Context growth visualization (running total)
4. CSV export with chat_id for easy correlation

Usage:
  ./ragfarm_bench_chatid.py --chat-id my_chat_001 --rag 5 --csv bench.csv
  
  # Or auto-generate chat_id
  ./ragfarm_bench_chatid.py --rag 5 --csv bench_$(uuidgen).csv
"""


# Endpoints come from .env via the shared resolver — never hardcode ports.
# See tests/tracing/ragfarm_env.py for the real port map.
from ragfarm_env import LLM_URL
import json
import sys
import time
import random
import uuid
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
class BenchRun:
    """Single benchmark run with context tracking."""
    # Session tracking
    chat_id: str
    timestamp: str
    run_idx: int
    
    # Prompt info
    prompt: str
    
    # Input metrics
    prompt_tokens: int
    prompt_bytes: int
    
    # Output metrics
    completion_tokens: int
    completion_bytes: int
    total_tokens: int
    
    # Context tracking (from OpenAI spec)
    context_tokens_before: int  # Running total before this request
    context_tokens_after: int   # Running total after this request
    context_growth: int         # How much context grew this step
    
    # Timing
    prefill_latency_ms: float
    decode_latency_ms: float
    e2e_latency_ms: float
    
    # Rates
    prefill_tok_s: float
    decode_tok_s: float
    
    # Completion text
    completion_text: str
    
    # Full OpenAI response (for inspection)
    response_usage: Dict[str, Any] = field(default_factory=dict)
    
    def to_csv_header(self) -> str:
        return ",".join([
            "chat_id", "timestamp", "run_idx", "prompt",
            "prompt_tokens", "prompt_bytes",
            "completion_tokens", "completion_bytes", "total_tokens",
            "context_before", "context_after", "context_growth",
            "prefill_ms", "decode_ms", "e2e_ms",
            "prefill_tok_s", "decode_tok_s"
        ])
    
    def to_csv_row(self) -> str:
        return ",".join([
            self.chat_id, self.timestamp, str(self.run_idx), f'"{self.prompt}"',
            str(self.prompt_tokens), str(self.prompt_bytes),
            str(self.completion_tokens), str(self.completion_bytes), str(self.total_tokens),
            str(self.context_tokens_before), str(self.context_tokens_after), str(self.context_growth),
            f"{self.prefill_latency_ms:.2f}", f"{self.decode_latency_ms:.2f}", f"{self.e2e_latency_ms:.2f}",
            f"{self.prefill_tok_s:.2f}", f"{self.decode_tok_s:.2f}"
        ])

class LlamaServerBench:
    """Benchmark with context tracking."""
    
    def __init__(self, base_url: str = LLM_URL, chat_id: Optional[str] = None, timeout: int = 120):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.model_name = None
        
        # Context tracking
        self.chat_id = chat_id or str(uuid.uuid4())[:8]
        self.running_context_tokens = 0  # Cumulative context
    
    def health_check(self) -> bool:
        """Verify llama-server is running."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception as e:
            print(f"❌ Health check failed: {e}", file=sys.stderr)
            return False
    
    def get_model_info(self) -> dict:
        """Fetch actual model from endpoint."""
        try:
            r = self.session.get(f"{self.base_url}/v1/models", timeout=5)
            data = r.json()
            if "data" in data and len(data["data"]) > 0:
                self.model_name = data["data"][0].get("id", "unknown")
            return data
        except Exception as e:
            print(f"⚠ Could not fetch model info: {e}", file=sys.stderr)
            return {}
    
    def bench_run(self, 
                  prompt: str, 
                  max_tokens: int = 256,
                  temperature: float = 0.7,
                  run_idx: int = 0) -> Optional[BenchRun]:
        """Execute single benchmark run with context tracking."""
        try:
            # Input metrics
            prompt_tokens_est = max(1, len(prompt) // 4)
            prompt_bytes = len(prompt.encode('utf-8'))
            
            # Track context before this request
            context_tokens_before = self.running_context_tokens
            
            # Build payload with include_usage=True (OpenAI spec)
            payload = {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.95,
                "stream": False,
                "include_usage": True,  # Standard OpenAI spec
            }
            
            # Send request
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
            
            # Extract completion text
            completion_text = ""
            if "choices" in data and len(data["choices"]) > 0:
                completion_text = data["choices"][0].get("text", "").strip()
            
            # Extract metrics from OpenAI spec response
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens_est)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            
            completion_bytes = len(completion_text.encode('utf-8'))
            
            # Timing
            e2e_latency_ms = (request_end - request_start) * 1000
            
            # Estimate prefill/decode split
            if completion_tokens > 0 and prompt_tokens > 0:
                prefill_fraction = min(0.85, prompt_tokens / (prompt_tokens + completion_tokens))
                prefill_latency_ms = e2e_latency_ms * prefill_fraction
                decode_latency_ms = e2e_latency_ms * (1 - prefill_fraction)
            else:
                prefill_latency_ms = e2e_latency_ms
                decode_latency_ms = 0
            
            # Rates
            prefill_tok_s = (prompt_tokens / (prefill_latency_ms / 1000)) if prefill_latency_ms > 0 else 0
            decode_tok_s = (completion_tokens / (decode_latency_ms / 1000)) if decode_latency_ms > 0 else 0
            
            # Update running context (for tracking blowup)
            self.running_context_tokens += total_tokens
            context_tokens_after = self.running_context_tokens
            context_growth = context_tokens_after - context_tokens_before
            
            return BenchRun(
                chat_id=self.chat_id,
                timestamp=datetime.now().isoformat(),
                run_idx=run_idx,
                prompt=prompt,
                prompt_tokens=prompt_tokens,
                prompt_bytes=prompt_bytes,
                completion_tokens=completion_tokens,
                completion_bytes=completion_bytes,
                total_tokens=total_tokens,
                context_tokens_before=context_tokens_before,
                context_tokens_after=context_tokens_after,
                context_growth=context_growth,
                prefill_latency_ms=prefill_latency_ms,
                decode_latency_ms=decode_latency_ms,
                e2e_latency_ms=e2e_latency_ms,
                prefill_tok_s=prefill_tok_s,
                decode_tok_s=decode_tok_s,
                completion_text=completion_text,
                response_usage=usage,
            )
        except requests.Timeout:
            print(f"❌ Request timeout (>{self.timeout}s)", file=sys.stderr)
            return None
        except Exception as e:
            print(f"❌ Bench run failed: {e}", file=sys.stderr)
            return None
    
    def format_result(self, result: BenchRun) -> str:
        """Format single result with context tracking."""
        lines = []
        
        # Answer preview
        text_lines = result.completion_text.split('\n')
        for i, line in enumerate(text_lines[:5]):
            bracket = "┌─" if i == 0 else "├─" if i < min(4, len(text_lines) - 1) else "└─"
            lines.append(f"{GRAY}{bracket} {line[:95]}{RESET}")
        if len(text_lines) > 5:
            lines.append(f"{GRAY}└─ ... ({len(text_lines) - 5} more lines){RESET}")
        
        # Stats with context tracking
        stats = [
            f"  prompt      {result.prompt_tokens:>7d} tokens",
            f"  completion  {result.completion_tokens:>7d} tokens",
            f"  total       {result.total_tokens:>7d} tokens",
            f"  prefill     {result.prefill_tok_s:7.1f} tok/s",
            f"  decode      {result.decode_tok_s:7.1f} tok/s",
            f"  TTFT        {result.e2e_latency_ms / max(1, result.completion_tokens):7.0f} ms",
            f"  E2E         {result.e2e_latency_ms:7.0f} ms",
        ]
        
        for i, stat in enumerate(stats):
            bracket = "├─" if i < len(stats) - 1 else "└─"
            lines.append(f"{BOLD_WHITE}{bracket}{stat}{RESET}")
        
        # Context tracking
        lines.append(f"{GRAY}context: {result.context_tokens_before:>6d} → {result.context_tokens_after:>6d} (+{result.context_growth:>5d} tokens){RESET}")
        
        return "\n".join(lines)
    
    def format_summary(self, results: List[BenchRun]) -> str:
        """Format summary with context blowup visualization."""
        lines = []
        
        lines.append(f"\n{CYAN}{'═' * 100}{RESET}")
        lines.append(f"{CYAN}CONTEXT GROWTH TRACKING (Chat ID: {self.chat_id}){RESET}")
        lines.append(f"{CYAN}{'═' * 100}{RESET}\n")
        
        # Context growth timeline
        lines.append(f"{BOLD_WHITE}CONTEXT ACCUMULATION:{RESET}")
        lines.append(f"{'Run':>3} {'Prompt':>8} {'Completion':>12} {'Total':>8} {'Running':>10} {'Growth':>8}")
        lines.append(f"{'-' * 3} {'-' * 8} {'-' * 12} {'-' * 8} {'-' * 10} {'-' * 8}")
        
        for r in results:
            lines.append(f"{r.run_idx + 1:3d} {r.prompt_tokens:>8d} {r.completion_tokens:>12d} "
                        f"{r.total_tokens:>8d} {r.context_tokens_after:>10d} {r.context_growth:>8d}")
        
        lines.append("")
        
        # Context blowup analysis
        if results:
            initial_context = results[0].context_tokens_before
            final_context = results[-1].context_tokens_after
            total_growth = final_context - initial_context
            avg_growth = total_growth / len(results) if results else 0
            
            lines.append(f"{BOLD_WHITE}CONTEXT BLOWUP ANALYSIS:{RESET}")
            lines.append(f"  Initial context:      {initial_context:>8d} tokens")
            lines.append(f"  Final context:        {final_context:>8d} tokens")
            lines.append(f"  Total growth:         {total_growth:>8d} tokens")
            lines.append(f"  Avg growth/prompt:    {avg_growth:>8.1f} tokens")
            lines.append(f"  Growth rate:          {(total_growth / max(1, initial_context) * 100):>8.1f}%\n")
            
            # Warning if blowup is severe
            if final_context > 4000:
                lines.append(f"{BOLD_WHITE}⚠ WARNING: Context growing rapidly!{RESET}")
                lines.append(f"  Current: {final_context} tokens")
                lines.append(f"  Action: Review retrieval (Qdrant candidates), RRF/MMR filters, reranker output\n")
        
        # Aggregate stats
        avg_prompt = mean([r.prompt_tokens for r in results])
        avg_completion = mean([r.completion_tokens for r in results])
        avg_prefill_tok_s = mean([r.prefill_tok_s for r in results if r.prefill_tok_s > 0])
        avg_decode_tok_s = mean([r.decode_tok_s for r in results if r.decode_tok_s > 0])
        avg_e2e = mean([r.e2e_latency_ms for r in results])
        
        lines.append(f"{BOLD_WHITE}PERFORMANCE AVERAGES:{RESET}")
        lines.append(f"  Avg prompt:           {avg_prompt:>8.1f} tokens")
        lines.append(f"  Avg completion:       {avg_completion:>8.1f} tokens")
        lines.append(f"  Avg prefill rate:     {avg_prefill_tok_s:>8.1f} tok/s")
        lines.append(f"  Avg decode rate:      {avg_decode_tok_s:>8.1f} tok/s")
        lines.append(f"  Avg E2E:              {avg_e2e:>8.1f} ms\n")
        
        return "\n".join(lines)
    
    def run_suite(self, 
                  prompts: List[str],
                  max_tokens: int = 256) -> List[BenchRun]:
        """Run benchmark suite."""
        results = []
        
        if not self.health_check():
            print("❌ llama-server is not healthy.", file=sys.stderr)
            return results
        
        self.get_model_info()
        
        print(f"📊 RAG-farm Benchmark (Chat ID: {self.chat_id})")
        if self.model_name:
            print(f"   Model: {self.model_name}")
        print(f"   Endpoint: {self.base_url}")
        print(f"   Prompts: {len(prompts)}\n")
        
        for idx, prompt in enumerate(prompts):
            print(f"Prompt {idx + 1}/{len(prompts)}: {prompt[:70]}")
            
            result = self.bench_run(prompt, max_tokens=max_tokens, run_idx=idx)
            if result:
                results.append(result)
                print(self.format_result(result))
            else:
                print(f"❌ FAILED")
            print()
        
        # Summary
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
        description="Benchmark with chat session tracking and context measurement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # With explicit chat ID (for correlation with tracers)
  ./ragfarm_bench_chatid.py --chat-id my_chat_001 --rag 5 --csv bench.csv

  # Auto-generate chat ID
  ./ragfarm_bench_chatid.py --rag 5 --csv bench_$(date +%s).csv

  # Monitor context blowup
  ./ragfarm_bench_chatid.py --rag 10 --max-tokens 512 --csv bench_long.csv
        """
    )
    parser.add_argument("--chat-id", type=str, default=None,
                       help="Chat session ID (default: auto-generate)")
    parser.add_argument("--url", default=LLM_URL,
                       help="llama-server URL")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--prompt", type=str, default=None)
    parser.add_argument("--rag", type=int, default=None)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    
    args = parser.parse_args()
    
    prompts = get_prompts(args.prompt, args.rag)
    
    bench = LlamaServerBench(base_url=args.url, chat_id=args.chat_id, timeout=args.timeout)
    results = bench.run_suite(prompts, max_tokens=args.max_tokens)
    
    if not results:
        print("❌ No results.", file=sys.stderr)
        sys.exit(1)
    
    # Export
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
