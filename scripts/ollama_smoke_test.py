"""Ollama smoke test for Alfred local LLM evaluation.

Tests qwen2.5:14b running on Windows Ollama (reachable from WSL2 at 172.22.0.1:11434)
to determine if it's capable of the kind of work Alfred's curator would ask of it.

No changes to Alfred. Standalone. Read-only against vault.

Usage:
    python scripts/ollama_smoke_test.py

Output:
    Stdout: per-test pass/fail + timing
    File: docs/ollama-smoke-test-2026-04-10.md (results writeup)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

OLLAMA_URL = "http://172.22.0.1:11434"
MODEL = "qwen2.5:14b"
TIMEOUT_SECONDS = 300

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_EMAIL = REPO_ROOT / "vault/inbox/processed/email-live-20260402-193719-RE-Cox-Palmer-Outstanding-Account---Client-10052440.md"
RESULTS_FILE = REPO_ROOT / "docs/ollama-smoke-test-2026-04-10.md"


# --- HTTP helpers ---

def post_json(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(
        f"{OLLAMA_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(messages: list[dict], tools: list[dict] | None = None, fmt: str | None = None) -> dict:
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    if tools:
        body["tools"] = tools
    if fmt:
        body["format"] = fmt
    return post_json("/api/chat", body)


# --- Test definitions ---

def test_1_connectivity() -> dict:
    """Trivial round-trip — confirm we can reach the model."""
    print("\n[Test 1] Connectivity / cold-start latency")
    t0 = time.time()
    resp = chat([{"role": "user", "content": "Reply with exactly the word: ready"}])
    dt = time.time() - t0
    text = resp.get("message", {}).get("content", "").strip()
    ok = "ready" in text.lower()
    print(f"  latency: {dt:.2f}s")
    print(f"  response: {text!r}")
    print(f"  result: {'PASS' if ok else 'FAIL'}")
    return {"name": "connectivity", "ok": ok, "latency_s": dt, "response": text}


def test_2_structured_output() -> dict:
    """Ask the model to extract structured JSON from a fake email."""
    print("\n[Test 2] Structured JSON extraction")
    fake_email = """
    From: jane.doe@acmecorp.com
    Subject: Quarterly review meeting

    Hi Andrew, I'd like to schedule our Q2 review for next Tuesday at 2pm.
    Please confirm with my assistant Bob Smith (bob@acmecorp.com).
    Thanks, Jane Doe, VP Engineering, ACME Corp.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You extract people and organizations mentioned in emails. "
                "Return ONLY a JSON object with keys: people (list of {name, email, role}), "
                "organizations (list of {name})."
            ),
        },
        {"role": "user", "content": fake_email},
    ]
    t0 = time.time()
    resp = chat(messages, fmt="json")
    dt = time.time() - t0
    raw = resp.get("message", {}).get("content", "").strip()
    try:
        parsed = json.loads(raw)
        ok = (
            isinstance(parsed, dict)
            and "people" in parsed
            and "organizations" in parsed
            and len(parsed["people"]) >= 2
            and any("acme" in o["name"].lower() for o in parsed["organizations"])
        )
    except json.JSONDecodeError:
        parsed = None
        ok = False
    print(f"  latency: {dt:.2f}s")
    print(f"  parsed: {json.dumps(parsed, indent=2) if parsed else raw}")
    print(f"  result: {'PASS' if ok else 'FAIL'}")
    return {"name": "structured_output", "ok": ok, "latency_s": dt, "parsed": parsed, "raw": raw}


def test_3_single_tool_call() -> dict:
    """Verify the model uses Ollama's tool-calling API correctly."""
    print("\n[Test 3] Single tool call (function calling)")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "vault_create",
                "description": "Create a new vault record (person, org, note, etc).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["person", "org", "note"]},
                        "name": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["type", "name"],
                },
            },
        }
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are Alfred, a vault curator. When given an email, identify the sender and "
                "create a person record for them using the vault_create tool."
            ),
        },
        {
            "role": "user",
            "content": (
                "From: pchudnovsky@coxandpalmer.com\nSubject: Trust account update\n\n"
                "Hi Andrew, we received your payment. Best, P. Chudnovsky"
            ),
        },
    ]
    t0 = time.time()
    resp = chat(messages, tools=tools)
    dt = time.time() - t0
    msg = resp.get("message", {})
    tool_calls = msg.get("tool_calls") or []
    ok = False
    call = None
    if tool_calls:
        call = tool_calls[0].get("function", {})
        args = call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        ok = (
            call.get("name") == "vault_create"
            and args.get("type") == "person"
            and "chudnovsky" in str(args.get("name", "")).lower()
        )
        call["arguments_parsed"] = args
    print(f"  latency: {dt:.2f}s")
    print(f"  tool_call: {json.dumps(call, indent=2) if call else 'NONE'}")
    print(f"  result: {'PASS' if ok else 'FAIL'}")
    return {"name": "single_tool_call", "ok": ok, "latency_s": dt, "tool_call": call}


def test_4_multi_turn_tool_use() -> dict:
    """Realistic curator scenario: read a real email, decide what records to create.

    We define vault_search and vault_create as fake tools and trace the model's behavior
    across multiple turns. Tools are not actually executed — we return canned responses
    so we can see if the model would do the right thing.
    """
    print("\n[Test 4] Multi-turn tool use (realistic curator flow)")
    if not TEST_EMAIL.exists():
        print(f"  SKIP — test email not found: {TEST_EMAIL}")
        return {"name": "multi_turn_tool_use", "ok": False, "skip": True}

    email_text = TEST_EMAIL.read_text()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "vault_search",
                "description": "Search the vault for existing records by query string.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "vault_create",
                "description": "Create a new vault record.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["person", "org", "note", "task"]},
                        "name": {"type": "string"},
                        "frontmatter": {"type": "object"},
                        "body": {"type": "string"},
                    },
                    "required": ["type", "name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": "Call when finished processing this email.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        },
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are Alfred, a vault curator. Process the email by:\n"
                "1. Searching for existing records (sender, organization).\n"
                "2. Creating any missing records.\n"
                "3. Calling 'done' when finished.\n"
                "Vault types: person, org, note, task. Use wikilinks like [[org/Cox and Palmer]]."
            ),
        },
        {"role": "user", "content": f"Process this email:\n\n{email_text}"},
    ]

    trace = []
    max_turns = 8
    t0 = time.time()
    for turn in range(max_turns):
        resp = chat(messages, tools=tools)
        msg = resp.get("message", {})
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content", "") or ""
        if not tool_calls:
            trace.append({"turn": turn, "type": "text", "content": content[:300]})
            break
        # Append assistant turn
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            trace.append({"turn": turn, "type": "tool_call", "name": name, "args": args})
            # Canned tool responses
            if name == "vault_search":
                tool_result = json.dumps({"matches": []})
            elif name == "vault_create":
                tool_result = json.dumps({"status": "created", "path": f"{args.get('type', 'note')}/{args.get('name', 'unknown')}.md"})
            elif name == "done":
                tool_result = json.dumps({"status": "ok"})
            else:
                tool_result = json.dumps({"error": "unknown tool"})
            messages.append({"role": "tool", "content": tool_result})
            if name == "done":
                trace[-1]["final"] = True
        if any(tc.get("function", {}).get("name") == "done" for tc in tool_calls):
            break
    dt = time.time() - t0

    # Heuristic pass: did the model search OR create records, then call done?
    searched_or_created = any(t.get("name") in ("vault_search", "vault_create") for t in trace if t["type"] == "tool_call")
    called_done = any(t.get("final") for t in trace)
    ok = searched_or_created and called_done
    print(f"  latency: {dt:.2f}s ({len(trace)} steps)")
    for step in trace:
        if step["type"] == "tool_call":
            args_short = json.dumps(step["args"])[:120]
            print(f"    -> {step['name']}({args_short})")
        else:
            print(f"    text: {step['content'][:120]}")
    print(f"  result: {'PASS' if ok else 'FAIL'}")
    return {"name": "multi_turn_tool_use", "ok": ok, "latency_s": dt, "trace": trace}


# --- Results writeup ---

def write_results(results: list[dict]) -> None:
    lines = [
        "# Ollama Smoke Test — 2026-04-10",
        "",
        "## Setup",
        "",
        f"- Model: `{MODEL}` (Q4_K_M, ~9GB)",
        f"- Host: Windows Ollama desktop, exposed via `OLLAMA_HOST=0.0.0.0`",
        f"- Reached from WSL2 at `{OLLAMA_URL}`",
        f"- Hardware: RTX 5070 Ti (16GB VRAM), i7-8700, 64GB RAM",
        "",
        "## Results",
        "",
        "| Test | Result | Latency |",
        "|------|--------|---------|",
    ]
    for r in results:
        status = "PASS" if r.get("ok") else ("SKIP" if r.get("skip") else "FAIL")
        lines.append(f"| {r['name']} | {status} | {r.get('latency_s', 0):.2f}s |")
    lines.append("")
    lines.append("## Per-test detail")
    lines.append("")
    for r in results:
        lines.append(f"### {r['name']}")
        lines.append("")
        lines.append("```json")
        printable = {k: v for k, v in r.items() if k != "trace"}
        lines.append(json.dumps(printable, indent=2, default=str))
        lines.append("```")
        if "trace" in r:
            lines.append("")
            lines.append("**Trace:**")
            lines.append("")
            for step in r["trace"]:
                if step["type"] == "tool_call":
                    lines.append(f"- `{step['name']}({json.dumps(step['args'])[:150]})`")
                else:
                    lines.append(f"- text: {step['content'][:200]}")
        lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    all_passed = all(r.get("ok") for r in results)
    if all_passed:
        lines.append(
            "All tests passed. qwen2.5:14b on this hardware is capable of the tool-use pattern "
            "Alfred requires. Building a full OllamaBackend is feasible. **Defer until Mac arrives** "
            "for production use; quality on a 14B model is below Claude for nuanced extraction, "
            "and the vault is the source of truth."
        )
    else:
        lines.append(
            "Not all tests passed. Review failures before considering Ollama as a backend. "
            "Sticking with Claude API is the right call for current production."
        )
    RESULTS_FILE.write_text("\n".join(lines))
    print(f"\n  Wrote results to {RESULTS_FILE}")


# --- Main ---

def main() -> int:
    print(f"Ollama smoke test — {MODEL} @ {OLLAMA_URL}")
    try:
        results = [
            test_1_connectivity(),
            test_2_structured_output(),
            test_3_single_tool_call(),
            test_4_multi_turn_tool_use(),
        ]
    except (HTTPError, URLError) as e:
        print(f"\nFATAL: cannot reach Ollama at {OLLAMA_URL}: {e}", file=sys.stderr)
        return 2
    write_results(results)
    failed = [r for r in results if not r.get("ok") and not r.get("skip")]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
