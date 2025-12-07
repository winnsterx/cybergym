import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Security, UploadFile, status
from fastapi.security import APIKeyHeader
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cybergym.server.pocdb import get_poc_by_hash, init_engine, query_ctf_submissions, query_re_submissions
from cybergym.server.server_utils import _post_process_result, run_poc_id, submit_flag, submit_poc, submit_pseudocode
from cybergym.server.types import CTFSubmissionPayload, CTFSubmissionQuery, Payload, PocQuery, RESubmissionPayload, RESubmissionQuery, VerifyPocs
from cybergym.task.types import DEFAULT_SALT

SALT = DEFAULT_SALT
LOG_DIR = Path("./logs")
DB_PATH = Path("./poc.db")
OSS_FUZZ_PATH = Path("./oss-fuzz-data")
DATA_DIR = Path("./cybergym_data/data")
API_KEY = "cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d"
API_KEY_NAME = "X-API-Key"

engine: Engine = None


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = init_engine(DB_PATH)

    yield

    if engine:
        engine.dispose()


app = FastAPI(lifespan=lifespan)

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def get_api_key(api_key: str = Security(api_key_header)):
    if api_key == API_KEY:
        return api_key
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


public_router = APIRouter()
private_router = APIRouter(dependencies=[Depends(get_api_key)])


@public_router.post("/submit-vul")
def submit_vul(db: SessionDep, metadata: Annotated[str, Form()], file: Annotated[UploadFile, File()]):
    try:
        payload = Payload.model_validate_json(metadata)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid metadata format") from None
    payload.data = file.file.read()
    res = submit_poc(db, payload, mode="vul", log_dir=LOG_DIR, salt=SALT, oss_fuzz_path=OSS_FUZZ_PATH)
    res = _post_process_result(res, payload.require_flag)
    return res


@private_router.post("/submit-fix")
def submit_fix(db: SessionDep, metadata: Annotated[str, Form()], file: Annotated[UploadFile, File()]):
    try:
        payload = Payload.model_validate_json(metadata)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid metadata format") from None
    payload.data = file.file.read()
    res = submit_poc(db, payload, mode="fix", log_dir=LOG_DIR, salt=SALT, oss_fuzz_path=OSS_FUZZ_PATH)
    res = _post_process_result(res, payload.require_flag)
    return res


@public_router.post("/submit-pseudocode")
def submit_re_pseudocode(db: SessionDep, payload: RESubmissionPayload):
    """
    Submit pseudocode for reverse engineering evaluation.

    Request:
    {
        "task_id": "arvo:10400",
        "agent_id": "abc123...",
        "checksum": "def456...",
        "pseudocode": "int main() { ... }"
    }

    Response:
    {
        "submission_id": "sub_123...",
        "task_id": "arvo:10400",
        "agent_id": "abc123...",
        "status": "received_for_evaluation"
    }
    """
    try:
        res = submit_pseudocode(db, payload, salt=SALT)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error submitting pseudocode: {str(e)}") from None


@public_router.post("/submit-flag")
def submit_ctf_flag(db: SessionDep, payload: CTFSubmissionPayload):
    """
    Submit a flag for CTF challenge (Flare-On, Google CTF, etc.).

    Request:
    {
        "task_id": "google-ctf:some-challenge",
        "agent_id": "abc123...",
        "checksum": "def456...",
        "flag": "CTF{flag-1234}"
    }

    Response:
    {
        "submission_id": "ctf_abc123...",
        "task_id": "google-ctf:some-challenge",
        "agent_id": "abc123...",
        "correct": true,
        "message": "Correct flag!",
        "created": true
    }
    """
    try:
        res = submit_flag(db, payload, data_dir=DATA_DIR, salt=SALT)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error submitting flag: {str(e)}") from None


@private_router.post("/query-re-submissions")
def query_re_subs(db: SessionDep, query: RESubmissionQuery):
    """
    Query RE submissions by agent_id and/or task_id.

    Request:
    {
        "agent_id": "abc123...",
        "task_id": "arvo:10400"  # optional
    }

    Response:
    [
        {
            "agent_id": "abc123...",
            "task_id": "arvo:10400",
            "submission_id": "sub_123...",
            "pseudocode_hash": "sha256...",
            "semantic_similarity": 0.85,
            "correctness_score": 0.92,
            "judge_reasoning": "...",
            "strengths": "[...]",
            "weaknesses": "[...]",
            "created_at": "2025-11-17T...",
            "evaluated_at": "2025-11-17T..."
        }
    ]
    """
    records = query_re_submissions(db, agent_id=query.agent_id, task_id=query.task_id)
    if not records:
        raise HTTPException(status_code=404, detail="No RE submissions found")
    return [record.to_dict() for record in records]


@private_router.post("/query-ctf-submissions")
def query_ctf_subs(db: SessionDep, query: CTFSubmissionQuery):
    """
    Query CTF submissions by agent_id, task_id, and/or correctness.

    Request:
    {
        "agent_id": "abc123...",
        "task_id": "google-ctf:some-challenge",  # optional
        "correct": 1  # optional: 1 = correct, 0 = incorrect
    }

    Response:
    [
        {
            "agent_id": "abc123...",
            "task_id": "google-ctf:some-challenge",
            "submission_id": "ctf_abc123...",
            "flag_hash": "sha256...",
            "correct": 1,
            "created_at": "2025-11-20T..."
        }
    ]
    """
    records = query_ctf_submissions(
        db, agent_id=query.agent_id, task_id=query.task_id, correct=query.correct
    )
    if not records:
        raise HTTPException(status_code=404, detail="No CTF submissions found")
    return [record.to_dict() for record in records]


@private_router.post("/query-poc")
def query_db(db: SessionDep, query: PocQuery):
    records = get_poc_by_hash(db, query.agent_id, query.task_id)
    if not records:
        raise HTTPException(status_code=404, detail="Record not found")
    return [record.to_dict() for record in records]


@private_router.post("/verify-agent-pocs")
def verify_all_pocs_for_agent_id(db: SessionDep, query: VerifyPocs):
    """
    Verify all PoCs for a given agent_id.
    """
    records = get_poc_by_hash(db, query.agent_id)
    if not records:
        raise HTTPException(status_code=404, detail="No records found for this agent_id")

    for record in records:
        run_poc_id(db, LOG_DIR, record.poc_id, oss_fuzz_path=OSS_FUZZ_PATH)

    return {
        "message": f"All {len(records)} PoCs for this agent_id have been verified",
        "poc_ids": [record.poc_id for record in records],
    }


app.include_router(public_router)
app.include_router(private_router)


def run_local_server(args):
    """Run the server locally with uvicorn."""
    global SALT, LOG_DIR, DB_PATH, OSS_FUZZ_PATH, DATA_DIR

    SALT = args.salt
    LOG_DIR = args.log_dir
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    DB_PATH = Path(args.db_path)
    OSS_FUZZ_PATH = Path(args.cybergym_oss_fuzz_path)
    DATA_DIR = Path(args.data_dir)

    uvicorn.run(app, host=args.host, port=args.port)


def run_modal_server(args):
    """Deploy and run the server on Modal."""
    import subprocess
    import sys

    modal_script = Path(__file__).parent / "modal_server.py"

    print("=" * 60)
    print("Deploying CyberGym server to Modal...")
    print("=" * 60)
    print(f"Volume: {args.modal_volume}")
    print(f"DB Path: /data/server_poc/poc.db")
    print()

    # Set environment variables for the modal script
    env = {
        **dict(os.environ),
        "CYBERGYM_MODAL_VOLUME": args.modal_volume,
        "CYBERGYM_SALT": args.salt,
    }

    # Use `modal deploy` for persistent deployment (stable URL, single instance)
    # Use `modal serve` for development (auto-reload, ephemeral)
    modal_cmd = "deploy" if args.modal_deploy else "serve"
    cmd = ["uv", "run", "modal", modal_cmd, str(modal_script)]

    print(f"Running: {' '.join(cmd)}")
    print()
    if args.modal_deploy:
        print("Deploying persistent server (use --no-modal-deploy for dev mode)")
    else:
        print("Starting dev server (use --modal-deploy for persistent deployment)")
        print("Press Ctrl+C to stop.")
    print("=" * 60)
    print()

    try:
        subprocess.run(cmd, env=env, check=True)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except subprocess.CalledProcessError as e:
        print(f"Error running Modal server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import os

    parser = argparse.ArgumentParser(description="CyberGym Server")

    # Runtime selection
    parser.add_argument("--runtime", type=str, default="local", choices=["local", "modal"],
                        help="Runtime: 'local' (uvicorn) or 'modal' (Modal cloud)")

    # Local server options
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to run the server on (local only)")
    parser.add_argument("--port", type=int, default=8666, help="Port to run the server on (local only)")
    parser.add_argument("--log_dir", type=Path, default=LOG_DIR, help="Directory to store logs")
    parser.add_argument("--db_path", type=Path, default=DB_PATH, help="Path to SQLite DB")
    parser.add_argument("--cybergym_oss_fuzz_path", type=Path, default=OSS_FUZZ_PATH, help="Path to OSS-Fuzz")
    parser.add_argument("--data_dir", type=Path, default=DATA_DIR, help="Path to CyberGym data directory")
    parser.add_argument("--salt", type=str, default=SALT, help="Salt for checksum")

    # Modal-specific options
    parser.add_argument("--modal-volume", type=str, default="cybergym-server-data",
                        help="Modal volume name for persistent storage (modal only)")
    parser.add_argument("--modal-deploy", action="store_true",
                        help="Use 'modal deploy' for persistent deployment (default: 'modal serve' for dev)")

    args = parser.parse_args()

    if args.runtime == "modal":
        run_modal_server(args)
    else:
        run_local_server(args)
