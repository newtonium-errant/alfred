# Ollama Smoke Test — 2026-04-10

## Setup

- Model: `qwen2.5:14b` (Q4_K_M, ~9GB)
- Host: Windows Ollama desktop, exposed via `OLLAMA_HOST=0.0.0.0`
- Reached from WSL2 at `http://172.22.0.1:11434`
- Hardware: RTX 5070 Ti (16GB VRAM), i7-8700, 64GB RAM

## Results

| Test | Result | Latency |
|------|--------|---------|
| connectivity | PASS | 72.37s |
| structured_output | PASS | 26.32s |
| single_tool_call | PASS | 10.66s |
| multi_turn_tool_use | PASS | 72.77s |

## Per-test detail

### connectivity

```json
{
  "name": "connectivity",
  "ok": true,
  "latency_s": 72.36514401435852,
  "response": "ready"
}
```

### structured_output

```json
{
  "name": "structured_output",
  "ok": true,
  "latency_s": 26.322556018829346,
  "parsed": {
    "people": [
      {
        "name": "Jane Doe",
        "email": "jane.doe@acmecorp.com",
        "role": "VP Engineering"
      },
      {
        "name": "Andrew",
        "email": "",
        "role": ""
      },
      {
        "name": "Bob Smith",
        "email": "bob@acmecorp.com",
        "role": "Assistant to Jane Doe"
      }
    ],
    "organizations": [
      {
        "name": "ACME Corp."
      }
    ]
  },
  "raw": "{\n  \"people\": [\n    {\n      \"name\": \"Jane Doe\",\n      \"email\": \"jane.doe@acmecorp.com\",\n      \"role\": \"VP Engineering\"\n    },\n    {\n      \"name\": \"Andrew\",\n      \"email\": \"\",\n      \"role\": \"\"\n    },\n    {\n      \"name\": \"Bob Smith\",\n      \"email\": \"bob@acmecorp.com\",\n      \"role\": \"Assistant to Jane Doe\"\n    }\n  ],\n  \"organizations\": [\n    {\n      \"name\": \"ACME Corp.\"\n    }\n  ]\n}"
}
```

### single_tool_call

```json
{
  "name": "single_tool_call",
  "ok": true,
  "latency_s": 10.66079068183899,
  "tool_call": {
    "index": 0,
    "name": "vault_create",
    "arguments": {
      "email": "pchudnovsky@coxandpalmer.com",
      "name": "P. Chudnovsky",
      "type": "person"
    },
    "arguments_parsed": {
      "email": "pchudnovsky@coxandpalmer.com",
      "name": "P. Chudnovsky",
      "type": "person"
    }
  }
}
```

### multi_turn_tool_use

```json
{
  "name": "multi_turn_tool_use",
  "ok": true,
  "latency_s": 72.77408242225647
}
```

**Trace:**

- `vault_search({"query": "P Chudnovsky"})`
- `vault_search({"query": "pchudnovsky@coxandpalmer.com"})`
- `vault_search({"query": "Cox and Palmer Outstanding Account - Client 10052440"})`
- `vault_search({"query": "Cox and Palmer Outstanding Account - Client 10052440"})`
- `vault_create({"type": "note", "body": "Equalization payment of $19,729.66 received from Ms. Newton for Client 10052440.", "frontmatter": {}, "name": "Cox and Palme)`
- `done({"summary": "Created a note for the equalization payment received from Ms. Newton, searched for existing records related to P Chudnovsky and Cox & Pal)`
- `done({"summary": "Created a note for the equalization payment received from Ms. Newton, searched for existing records related to P Chudnovsky and Cox & Pal)`

## Recommendation

All tests passed. qwen2.5:14b on this hardware is capable of the tool-use pattern Alfred requires. Building a full OllamaBackend is feasible. **Defer until Mac arrives** for production use; quality on a 14B model is below Claude for nuanced extraction, and the vault is the source of truth.