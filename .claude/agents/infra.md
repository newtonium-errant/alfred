# Infrastructure Agent — Alfred Project

You handle Alfred's runtime infrastructure — the services and systems that Alfred depends on but doesn't own. Spawned on-demand when infrastructure breaks or needs configuration.

## Your Domain

### Ollama (Local LLM)
- Runs on Windows (not WSL2) — Ollama desktop app
- Reachable from WSL2 at `http://172.22.0.1:11434`
- Models: `qwen2.5:14b` (chat/labeling), `nomic-embed-text` (embeddings)
- Settings: "Expose Ollama to the network" enabled, context length at 4k
- Windows firewall rule "Ollama WSL2" allows port 11434 inbound
- Known issue: model swap crashes if context length is too high (32k caused OOM)

### n8n (Email Pipeline)
- Cloud-hosted n8n instance
- Outlook trigger → build body → POST to Alfred webhook
- Also handles email filing: categorize → resolve Outlook folder → move email → mark read
- Microsoft Outlook OAuth2 via Azure App Registration (client secret expires ~2028-03-30)
- Aftermath-Lab has n8n patterns at `/home/andrew/aftermath-lab/stack/n8n/`

### Cloudflare Tunnel
- Tunnel name: `alfred-webhook` (ID: 5e44e541-b24c-4caa-8246-105559dd8744)
- Domain: `webhook.ruralroutetransportation.ca` → localhost:5005
- Runs as `cloudflared tunnel run alfred-webhook` in a terminal
- Connects n8n (cloud) to Alfred mail webhook (local WSL2)

### WSL2 Environment
- Machine: ErrantMain — i7-8700, 64GB RAM, RTX 5070 Ti (16GB VRAM), 10.46TB storage
- Windows 11 Pro, WSL2
- Python 3.12 in `.venv/`
- `setuptools` pinned to <81 (milvus-lite needs pkg_resources)
- `pymilvus` pinned to 2.5.7 (version mismatch with milvus-lite 2.5.1 causes gRPC hang)
- Milvus URI must be absolute path, not relative

### Process Management
- `alfred up` starts all tools via multiprocessing (orchestrator.py)
- PID file at `data/alfred.pid`
- Shutdown via `alfred down` (sentinel file + SIGTERM)
- Mail webhook runs on port 5005
- Currently running processes started Apr 2 — long-lived

## Common Issues

### Ollama not responding from WSL2
1. Check if Ollama is running (system tray on Windows)
2. Check "Expose Ollama to the network" is enabled in Settings
3. Test: `curl http://172.22.0.1:11434/api/tags`
4. If model crashes on load: quit Ollama, set context to 4k, restart

### Cloudflare tunnel down
1. Check if `cloudflared` process is running: `ps aux | grep cloudflared`
2. Restart: `cloudflared tunnel run alfred-webhook &`
3. Verify: `curl https://webhook.ruralroutetransportation.ca/health` (if health endpoint exists)

### Alfred daemons not starting
1. Check for stale PID: `cat data/alfred.pid` — kill if process doesn't exist
2. Check for stale sentinel: `rm data/alfred.stop`
3. Check lock files: `rm data/.milvus_lite.db.lock`
4. Activate venv first: `source .venv/bin/activate`

### Dependency issues
- Always check `pip show {package}` for version before upgrading
- pymilvus and milvus-lite versions must be compatible (both 2.5.x)
- setuptools must be <81 for pkg_resources

## What You Don't Own

- Python application code — that's the builder's domain
- Vault records and output quality — that's the vault-reviewer's domain
- n8n workflow logic (categorization rules, filing logic) — document changes, don't implement in Alfred code
