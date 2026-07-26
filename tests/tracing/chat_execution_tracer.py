#!/usr/bin/env python3
"""
chat_execution_tracer.py: Full transparency tracer for Open WebUI chat sessions.

Hooks into vLLM (or llama-server) to capture:
  1. Initial user prompt (size, tokens)
  2. LLM generation request (prefill phase)
  3. Tool detection & schema extraction
  4. Tool invocation execution (which tools, execution time, return values)
  5. Decision/thinking phase (time between tool return and next generation)
  6. Final generation (completion size, tokens, latency)
  
Output: Structured JSON timeline with every request/response payload, 
absolute timing in milliseconds, byte/token counts at each stage.

Usage:
  - Run as proxy: python chat_execution_tracer.py --listen 0.0.0.0:8002 --forward localhost:8001
  - Or inspect logs from a running session
  - Parse output for complete visibility into chat orchestration overhead
"""

import json
import sys
import time
import asyncio
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import argparse
from collections import defaultdict

# ANSI colors
GRAY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"

@dataclass
class RequestMetrics:
    """Metrics for a single request in the chat flow."""
    step_number: int
    step_name: str
    timestamp_ms: float
    direction: str  # "IN" (user->llm) or "OUT" (llm->tool/user)
    
    # Payload sizes
    payload_bytes: int
    estimated_tokens: int
    
    # Timing
    latency_ms: float  # Time this stage took
    cumulative_ms: float  # Total time from start
    
    # Content
    content_preview: str = ""  # First 200 chars
    tool_name: Optional[str] = None  # If tool invocation
    
    # Raw payload
    full_payload: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        tool_info = f" [{self.tool_name}]" if self.tool_name else ""
        return (f"[{self.step_number:2d}] {self.step_name:30s} {self.direction} | "
                f"{self.latency_ms:>8.2f}ms (cumul: {self.cumulative_ms:>8.1f}ms) | "
                f"{self.estimated_tokens:>6d}t {self.payload_bytes:>8d}b{tool_info}")

@dataclass
class ChatSession:
    """Complete chat session trace."""
    session_id: str
    timestamp: str
    model_name: str
    
    # Requests in order
    requests: List[RequestMetrics] = field(default_factory=list)
    
    # Tool invocations
    tool_invocations: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    
    # Aggregate stats
    total_time_ms: float = 0
    total_tokens: int = 0
    total_bytes: int = 0
    tool_execution_time_ms: float = 0  # Total time spent executing tools
    decision_time_ms: float = 0  # Time between tool return and next LLM generation
    llm_generation_time_ms: float = 0  # Total LLM inference time

class ChatTracer:
    """Tracer for chat execution flows."""
    
    def __init__(self):
        self.sessions: Dict[str, ChatSession] = {}
        self.current_session: Optional[ChatSession] = None
        self.step_counter = 0
        self.session_start_time: Optional[float] = None
        
    def start_session(self, session_id: str, model_name: str = "unknown"):
        """Start tracing a new chat session."""
        self.current_session = ChatSession(
            session_id=session_id,
            timestamp=datetime.now().isoformat(),
            model_name=model_name,
        )
        self.sessions[session_id] = self.current_session
        self.step_counter = 0
        self.session_start_time = time.time()
        print(f"\n{GREEN}✓ Session started: {session_id} ({model_name}){RESET}")
    
    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(1, len(text) // 4)
    
    def log_request(self,
                    step_name: str,
                    direction: str,  # "IN" or "OUT"
                    payload: Any,
                    tool_name: Optional[str] = None,
                    latency_ms: float = 0) -> RequestMetrics:
        """Log a single request/response in the chat flow."""
        if not self.current_session:
            raise RuntimeError("No active session. Call start_session first.")
        
        self.step_counter += 1
        
        # Serialize payload
        if isinstance(payload, str):
            payload_str = payload
            payload_bytes = len(payload.encode('utf-8'))
            payload_dict = {"text": payload}
        elif isinstance(payload, dict):
            payload_str = json.dumps(payload)
            payload_bytes = len(payload_str.encode('utf-8'))
            payload_dict = payload
        else:
            payload_str = str(payload)
            payload_bytes = len(payload_str.encode('utf-8'))
            payload_dict = {"raw": str(payload)}
        
        # Estimate tokens
        estimated_tokens = self.estimate_tokens(payload_str)
        
        # Timing
        cumulative_time = time.time() - self.session_start_time if self.session_start_time else 0
        
        # Preview
        preview = payload_str[:200]
        if len(payload_str) > 200:
            preview += "..."
        
        metric = RequestMetrics(
            step_number=self.step_counter,
            step_name=step_name,
            timestamp_ms=cumulative_time * 1000,
            direction=direction,
            payload_bytes=payload_bytes,
            estimated_tokens=estimated_tokens,
            latency_ms=latency_ms,
            cumulative_ms=cumulative_time * 1000,
            content_preview=preview,
            tool_name=tool_name,
            full_payload=payload_dict,
        )
        
        self.current_session.requests.append(metric)
        
        # Print real-time
        bracket = "→" if direction == "IN" else "←"
        print(f"{BOLD_WHITE}{bracket}{RESET} {metric}")
        
        return metric
    
    def log_tool_invocation(self,
                           tool_name: str,
                           schema: Dict[str, Any],
                           input_params: Dict[str, Any],
                           output: Any,
                           execution_time_ms: float):
        """Log a tool invocation with schema and execution details."""
        if not self.current_session:
            raise RuntimeError("No active session.")
        
        if tool_name not in self.current_session.tool_invocations:
            self.current_session.tool_invocations[tool_name] = []
        
        invocation = {
            "timestamp_ms": (time.time() - self.session_start_time) * 1000,
            "tool_name": tool_name,
            "schema": schema,
            "input": input_params,
            "output": output,
            "execution_time_ms": execution_time_ms,
            "input_bytes": len(json.dumps(input_params).encode('utf-8')),
            "output_bytes": len(json.dumps(output).encode('utf-8')) if isinstance(output, (dict, list)) else len(str(output).encode('utf-8')),
        }
        
        self.current_session.tool_invocations[tool_name].append(invocation)
        
        # Print real-time
        input_bytes = invocation["input_bytes"]
        output_bytes = invocation["output_bytes"]
        print(f"{YELLOW}  🔧 {tool_name:20s} | {execution_time_ms:>8.2f}ms | in:{input_bytes:>6d}b out:{output_bytes:>6d}b{RESET}")
    
    def end_session(self) -> ChatSession:
        """Finalize session and compute aggregate stats."""
        if not self.current_session:
            raise RuntimeError("No active session.")
        
        session = self.current_session
        
        # Compute aggregates
        session.total_time_ms = (time.time() - self.session_start_time) * 1000 if self.session_start_time else 0
        session.total_tokens = sum(r.estimated_tokens for r in session.requests)
        session.total_bytes = sum(r.payload_bytes for r in session.requests)
        
        # Sum tool execution times
        for tool_calls in session.tool_invocations.values():
            for call in tool_calls:
                session.tool_execution_time_ms += call.get("execution_time_ms", 0)
        
        # Estimate decision time (time between tool returns and LLM generation)
        # This is approximate: gaps between OUT (tool result) and next IN (LLM request)
        for i in range(len(session.requests) - 1):
            if session.requests[i].direction == "OUT" and session.requests[i].tool_name:
                # This was a tool invocation output
                # Next step might be decision phase
                if i + 1 < len(session.requests):
                    gap = session.requests[i + 1].cumulative_ms - session.requests[i].cumulative_ms
                    if gap > 0 and session.requests[i + 1].direction == "IN":
                        session.decision_time_ms += gap
        
        # LLM generation time is total - tool time
        session.llm_generation_time_ms = session.total_time_ms - session.tool_execution_time_ms - session.decision_time_ms
        
        self.current_session = None
        
        return session
    
    def format_session_report(self, session: ChatSession) -> str:
        """Format complete session trace report."""
        lines = []
        
        lines.append(f"\n{CYAN}{'═' * 120}{RESET}")
        lines.append(f"{CYAN}CHAT EXECUTION TRACE REPORT{RESET}")
        lines.append(f"{CYAN}{'═' * 120}{RESET}\n")
        
        lines.append(f"{BOLD_WHITE}Session:{RESET} {session.session_id}")
        lines.append(f"{BOLD_WHITE}Model:{RESET} {session.model_name}")
        lines.append(f"{BOLD_WHITE}Timestamp:{RESET} {session.timestamp}")
        lines.append(f"{BOLD_WHITE}Total duration:{RESET} {session.total_time_ms:.2f}ms\n")
        
        # Timeline
        lines.append(f"{BOLD_WHITE}EXECUTION TIMELINE:{RESET}")
        lines.append(f"{'Seq':>3} {'Step':30s} {'Dir':3s} {'Latency':>10} {'Cumul':>10} {'Tokens':>8} {'Bytes':>10} {'Tool':<20}")
        lines.append(f"{'-' * 3} {'-' * 30} {'-' * 3} {'-' * 10} {'-' * 10} {'-' * 8} {'-' * 10} {'-' * 20}")
        
        for req in session.requests:
            tool_col = f"[{req.tool_name}]" if req.tool_name else ""
            lines.append(f"{req.step_number:3d} {req.step_name:30s} {req.direction:>3} "
                        f"{req.latency_ms:>10.2f} {req.cumulative_ms:>10.1f} "
                        f"{req.estimated_tokens:>8d} {req.payload_bytes:>10d} {tool_col:<20}")
        
        lines.append("")
        
        # Tool invocations detailed
        if session.tool_invocations:
            lines.append(f"{BOLD_WHITE}TOOL INVOCATIONS:{RESET}")
            for tool_name, calls in session.tool_invocations.items():
                lines.append(f"\n  {tool_name}:")
                for call in calls:
                    lines.append(f"    Execution time:  {call['execution_time_ms']:>8.2f} ms")
                    lines.append(f"    Input bytes:     {call['input_bytes']:>8d}")
                    lines.append(f"    Output bytes:    {call['output_bytes']:>8d}")
                    lines.append(f"    Input schema:    {json.dumps(call['input'], indent=22)[:200]}")
            lines.append("")
        
        # Aggregate stats
        lines.append(f"{BOLD_WHITE}AGGREGATE STATISTICS:{RESET}")
        lines.append(f"  Total tokens:            {session.total_tokens:>12d}")
        lines.append(f"  Total bytes:             {session.total_bytes:>12d}")
        lines.append(f"  Total time:              {session.total_time_ms:>12.2f} ms")
        lines.append(f"  LLM generation time:     {session.llm_generation_time_ms:>12.2f} ms")
        lines.append(f"  Tool execution time:     {session.tool_execution_time_ms:>12.2f} ms")
        lines.append(f"  Decision/thinking time:  {session.decision_time_ms:>12.2f} ms")
        lines.append(f"  Overhead (decision):     {session.decision_time_ms / session.total_time_ms * 100:>12.1f}%")
        lines.append(f"  Overhead (tools):        {session.tool_execution_time_ms / session.total_time_ms * 100:>12.1f}%\n")
        
        lines.append(f"{BOLD_WHITE}THROUGHPUT:{RESET}")
        tok_per_sec = session.total_tokens / (session.total_time_ms / 1000) if session.total_time_ms > 0 else 0
        bytes_per_sec = session.total_bytes / (session.total_time_ms / 1000) if session.total_time_ms > 0 else 0
        lines.append(f"  Tokens/sec:              {tok_per_sec:>12.2f}")
        lines.append(f"  Bytes/sec:               {bytes_per_sec:>12.2f}\n")
        
        return "\n".join(lines)

def demo_session():
    """Demo: simulate a typical chat session with tool calls."""
    tracer = ChatTracer()
    
    # Start session
    tracer.start_session(session_id="chat_20250725_001", model_name="Qwen2.5-7B-Q4_K_M")
    
    # Step 1: User sends prompt
    user_prompt = "What are the FW rules for the EPC_AZURE network? Give me the details."
    tracer.log_request(
        step_name="User prompt",
        direction="IN",
        payload=user_prompt,
        latency_ms=0
    )
    
    # Step 2: LLM processes prompt (prefill)
    time.sleep(0.1)  # Simulate prefill time
    prefill_request = {
        "prompt": user_prompt,
        "max_tokens": 512,
        "temperature": 0.7,
        "tools": [
            {
                "name": "query_fw_rules",
                "description": "Query firewall rules from database",
                "parameters": {"network_name": "string"}
            }
        ]
    }
    tracer.log_request(
        step_name="LLM prefill request",
        direction="OUT",
        payload=prefill_request,
        latency_ms=145.3
    )
    
    # Step 3: LLM decides to use tool
    time.sleep(0.05)
    llm_decision = {
        "type": "tool_call",
        "tool_name": "query_fw_rules",
        "tool_input": {"network_name": "EPC_AZURE"}
    }
    tracer.log_request(
        step_name="LLM tool decision",
        direction="IN",
        payload=llm_decision,
        latency_ms=87.2
    )
    
    # Step 4: Tool execution
    time.sleep(0.15)  # Simulate tool execution
    tool_schema = {
        "name": "query_fw_rules",
        "input_schema": {"network_name": "str"}
    }
    tool_output = {
        "network": "EPC_AZURE",
        "rules": [
            {"src": "10.0.0.0/8", "dst": "192.168.0.0/16", "action": "ALLOW", "ports": "80,443"},
            {"src": "172.16.0.0/12", "dst": "10.0.0.0/8", "action": "DENY", "ports": "*"}
        ]
    }
    tracer.log_tool_invocation(
        tool_name="query_fw_rules",
        schema=tool_schema,
        input_params={"network_name": "EPC_AZURE"},
        output=tool_output,
        execution_time_ms=150.5
    )
    
    tracer.log_request(
        step_name="Tool result",
        direction="OUT",
        payload=tool_output,
        tool_name="query_fw_rules",
        latency_ms=0
    )
    
    # Step 5: LLM decision phase (process tool result)
    time.sleep(0.08)
    tracer.log_request(
        step_name="LLM decision phase",
        direction="IN",
        payload={"role": "assistant", "content": "processing tool result..."},
        latency_ms=78.3
    )
    
    # Step 6: LLM generates final answer
    time.sleep(0.12)
    final_answer = """The EPC_AZURE network has the following firewall rules:

1. ALLOW traffic from 10.0.0.0/8 to 192.168.0.0/16 on ports 80, 443
2. DENY traffic from 172.16.0.0/12 to 10.0.0.0/8 on all ports

These rules prioritize external web traffic while blocking internal cross-network access."""
    
    tracer.log_request(
        step_name="LLM final generation",
        direction="IN",
        payload=final_answer,
        latency_ms=120.7
    )
    
    # End session and get report
    session = tracer.end_session()
    
    print(tracer.format_session_report(session))
    
    # Save to file
    output_file = Path("./chat_trace_demo.json")
    with open(output_file, "w") as f:
        json.dump({
            "session": asdict(session),
            "requests": [asdict(r) for r in session.requests],
            "tool_invocations": session.tool_invocations,
        }, f, indent=2, default=str)
    
    print(f"\n✓ Trace saved to {output_file}")

def main():
    parser = argparse.ArgumentParser(
        description="Chat execution tracer: full transparency into chat flows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run demo with simulated chat session
  python chat_execution_tracer.py --demo
  
  # Start as proxy (intercept Open WebUI <-> vLLM calls)
  python chat_execution_tracer.py --listen 0.0.0.0:8002 --forward localhost:8001
        """
    )
    parser.add_argument("--demo", action="store_true",
                       help="Run demo with simulated chat session")
    parser.add_argument("--listen", type=str, default="0.0.0.0:8002",
                       help="Listen address for proxy mode")
    parser.add_argument("--forward", type=str, default="localhost:8001",
                       help="Forward to vLLM address")
    parser.add_argument("--output", type=str, default=None,
                       help="Output file for trace JSON")
    
    args = parser.parse_args()
    
    if args.demo:
        demo_session()
    else:
        print(f"Proxy mode not yet implemented. Use --demo to see example trace.")
        print(f"Listen: {args.listen} -> Forward: {args.forward}")

if __name__ == "__main__":
    main()
