# CyberGym Server

The CyberGym server handles submissions from agents during evaluation. It receives PoC exploits, pseudocode for reverse engineering tasks, and flags for CTF challenges.

## Quick Start

```bash
# Local server (for local Docker agents)
PORT=8666
POC_SAVE_DIR=./server_poc
CYBERGYM_SERVER_DATA_DIR=./oss-fuzz-data
uv run python -m cybergym.server \
    --host 0.0.0.0 --port $PORT \
    --log_dir $POC_SAVE_DIR --db_path $POC_SAVE_DIR/poc.db \
    --cybergym_oss_fuzz_path $CYBERGYM_SERVER_DATA_DIR

OR 

uv run python -m cybergym.server --host 0.0.0.0 --port 8666

# Modal server (for Modal agents)
uv run python -m cybergym.server --runtime modal --modal-deploy
```

## Server Modes

### Local Mode (Default)

Runs the server locally using uvicorn. Best for:
- Local development
- Running agents with `--runtime docker`
- Testing on your machine

```bash
uv run python -m cybergym.server \
  --host 0.0.0.0 \
  --port 8666 \
  --db_path ./server_poc/poc.db \
  --log_dir ./server_poc
```

### Modal Mode

Deploys the server to Modal's cloud infrastructure. Best for:
- Running agents with `--runtime modal`
- Distributed evaluation
- No need to expose local ports

#### Development (modal serve)

Auto-reloads on code changes. Creates ephemeral apps with `-dev` URL suffix.

```bash
uv run python -m cybergym.server --runtime modal
```

#### Production (modal deploy)

Persistent deployment with stable URL. Single instance, no auto-reload.

```bash
uv run python -m cybergym.server --runtime modal --modal-deploy
```

## CLI Options

### General Options

| Option | Default | Description |
|--------|---------|-------------|
| `--runtime` | `local` | Runtime mode: `local` or `modal` |
| `--salt` | (internal) | Salt for checksum verification |

### Local Mode Options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Host to bind to |
| `--port` | `8666` | Port to listen on |
| `--db_path` | `./poc.db` | Path to SQLite database |
| `--log_dir` | `./logs` | Directory for logs and PoC files |
| `--data_dir` | `./cybergym_data/data` | Path to CyberGym data (for flag verification) |

### Modal Mode Options

| Option | Default | Description |
|--------|---------|-------------|
| `--modal-deploy` | `false` | Use `modal deploy` instead of `modal serve` |
| `--modal-volume` | `cybergym-server-data` | Modal volume name for persistent storage |
| `--run-id` | (timestamp) | Run ID for data isolation |

## Data Isolation

Each server deployment can use a separate database directory to avoid mixing data between experiments.

### Automatic (Timestamp)

```bash
# Creates /data/runs/2025-11-29_12-30-45/poc.db
uv run python -m cybergym.server --runtime modal --modal-deploy
```

### Custom Run ID

```bash
# Creates /data/runs/experiment-1/poc.db
uv run python -m cybergym.server --runtime modal --modal-deploy --run-id experiment-1
```

### Volume Structure

```
/data/                          # Modal volume root
└── runs/
    ├── 2025-11-29_00-33-25/    # Auto-generated run
    │   ├── poc.db
    │   └── logs/
    ├── 2025-11-29_00-39-50/    # Another auto-generated run
    │   ├── poc.db
    │   └── logs/
    └── my-experiment/          # Custom run ID
        ├── poc.db
        └── logs/
```

## API Endpoints

### Public Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/submit-vul` | POST | Submit PoC exploit (requires Docker, local only) |
| `/submit-pseudocode` | POST | Submit RE pseudocode |
| `/submit-flag` | POST | Submit CTF flag |

### Private Endpoints (require API key)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/query-re-submissions` | POST | Query RE submissions |
| `/query-flareon-submissions` | POST | Query flag submissions |
| `/query-poc` | POST | Query PoC records |

### Authentication

Private endpoints require the `X-API-Key` header:

```bash
curl -X POST https://server/query-re-submissions \
  -H "X-API-Key: cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "abc123", "task_id": null}'
```

## Usage with run_eval.py

### Local Server + Docker Agents

```bash
# Terminal 1: Start server
uv run python -m cybergym.server --host 0.0.0.0 --port 8666

# Terminal 2: Run evaluation
uv run python run_eval.py \
  --task-csv task_lists/tasks.csv \
  --runtime docker \
  --server http://localhost:8666 \
  ...
```

### Modal Server + Modal Agents

```bash
# Deploy server (returns URL)
uv run python -m cybergym.server --runtime modal --modal-deploy

# Run evaluation with Modal server URL
uv run python run_eval.py \
  --task-csv task_lists/tasks.csv \
  --runtime modal \
  --server https://independentsafetyresearch--cybergym-server-fastapi-app.modal.run \
  ...
```

## Supported Submission Types

| Mode | Endpoint | Modal Support | Local Support |
|------|----------|---------------|---------------|
| Exploit (PoC) | `/submit-vul` | No (needs Docker) | Yes |
| Reverse Engineering | `/submit-pseudocode` | Yes | Yes |
| CTF (Flags) | `/submit-flag` | Yes | Yes |

## Troubleshooting

### Modal: "Token missing"

```bash
modal token set --token-id <your-token-id> --token-secret <your-token-secret>
```

### Modal: "Answers file not found"

The `answers.csv` files for flag verification are baked into the Modal image. If missing, redeploy:

```bash
uv run python -m cybergym.server --runtime modal --modal-deploy
```

### Local: "Address already in use"

Another process is using the port. Either:
- Kill the existing process: `lsof -i :8666 | awk 'NR>1 {print $2}' | xargs kill`
- Use a different port: `--port 8667`

### Database locked

SQLite can have issues with concurrent writes. For high-concurrency scenarios, consider using separate run IDs or the Modal deployment which handles this better.
