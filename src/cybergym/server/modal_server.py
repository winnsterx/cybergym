"""
Modal deployment for CyberGym server.

Usage:
    # Deploy server on Modal (persistent)
    uv run python -m cybergym.server --runtime modal --modal-deploy

The server will run on Modal with a persistent volume for the database.
Database is stored at /data/server_poc/poc.db on the volume.
"""

import os
from pathlib import Path

import modal

# Configuration from environment variables
VOLUME_NAME = os.environ.get("CYBERGYM_MODAL_VOLUME", "cybergym-server-data")
# Use same default salt as task generation (from cybergym.task.types.DEFAULT_SALT)
SALT = os.environ.get("CYBERGYM_SALT", "CyberGym")
API_KEY = os.environ.get("CYBERGYM_API_KEY", "cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d")

# Container paths - stable paths matching local server structure
CONTAINER_DATA_ROOT = "/data"
CONTAINER_DB_PATH = f"{CONTAINER_DATA_ROOT}/server_poc/poc.db"
CONTAINER_LOG_DIR = f"{CONTAINER_DATA_ROOT}/server_poc/logs"
CONTAINER_DATA_DIR = "/app/cybergym_data"  # Baked into image, not on volume

# Create Modal app
app = modal.App("cybergym-server")

# Create persistent volume for database
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Path to local data directory (for answers.csv files)
LOCAL_DATA_DIR = Path(__file__).parent.parent.parent.parent / "cybergym_data" / "data"

# Build image with dependencies - copy the cybergym package and data files
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi>=0.115.0",
        "uvicorn>=0.32.0",
        "sqlalchemy>=2.0.0",
        "pydantic>=2.0.0",
        "python-multipart>=0.0.9",
    )
    .add_local_dir(
        str(Path(__file__).parent.parent.parent.parent / "src"),
        remote_path="/app/src",
        copy=True,
    )
    .add_local_file(
        str(LOCAL_DATA_DIR / "google-ctf" / "answers.csv"),
        remote_path="/app/cybergym_data/google-ctf/answers.csv",
        copy=True,
    )
    .add_local_file(
        str(LOCAL_DATA_DIR / "flare-on" / "answers.csv"),
        remote_path="/app/cybergym_data/flare-on/answers.csv",
        copy=True,
    )
    .add_local_file(
        str(LOCAL_DATA_DIR / "defcon-ooo" / "answers.csv"),
        remote_path="/app/cybergym_data/defcon-ooo/answers.csv",
        copy=True,
    )
    .workdir("/app")
    .env({"PYTHONPATH": "/app/src"})
)


@app.function(
    image=image,
    volumes={"/data": volume},
    timeout=86400,  # 24 hours
    cpu=1.0,
    memory=1024,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fastapi_app():
    """Create and return the FastAPI app for Modal."""
    import sys
    sys.path.insert(0, "/app/src")

    from contextlib import asynccontextmanager
    from typing import Annotated

    from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Security, UploadFile, status
    from fastapi.security import APIKeyHeader
    from sqlalchemy.orm import Session

    from cybergym.server.pocdb import add_judge_evaluation, get_poc_by_hash, init_engine, query_ctf_submissions, query_re_submissions
    from cybergym.server.server_utils import _post_process_result, submit_flag, submit_poc, submit_pseudocode
    from cybergym.server.types import CTFSubmissionPayload, CTFSubmissionQuery, JudgeEvaluationPayload, Payload, PocQuery, RESubmissionPayload, RESubmissionQuery

    # Ensure directories exist
    Path(CONTAINER_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CONTAINER_LOG_DIR).mkdir(parents=True, exist_ok=True)

    engine_holder = {"engine": None}

    def get_session():
        with Session(engine_holder["engine"]) as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine_holder["engine"] = init_engine(Path(CONTAINER_DB_PATH))
        volume.commit()
        yield
        if engine_holder["engine"]:
            engine_holder["engine"].dispose()

    fastapi = FastAPI(lifespan=lifespan, title="CyberGym Server (Modal)")

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def get_api_key(key: str = Security(api_key_header)):
        if key == API_KEY:
            return key
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    public_router = APIRouter()
    private_router = APIRouter(dependencies=[Depends(get_api_key)])

    @public_router.get("/")
    def health():
        return {
            "status": "ok",
            "runtime": "modal",
            "volume": VOLUME_NAME,
            "db_path": CONTAINER_DB_PATH,
        }

    @public_router.post("/submit-vul")
    def submit_vul(db: SessionDep, metadata: Annotated[str, Form()], file: Annotated[UploadFile, File()]):
        try:
            payload = Payload.model_validate_json(metadata)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid metadata format") from None
        payload.data = file.file.read()
        res = submit_poc(
            db, payload, mode="vul", log_dir=Path(CONTAINER_LOG_DIR), salt=SALT,
            oss_fuzz_path=Path(CONTAINER_DATA_DIR), use_modal=True,
        )
        res = _post_process_result(res, payload.require_flag)
        volume.commit()
        return res

    @public_router.post("/submit-pseudocode")
    def submit_re_pseudocode(db: SessionDep, payload: RESubmissionPayload):
        try:
            res = submit_pseudocode(db, payload, salt=SALT)
            volume.commit()
            return res
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error submitting pseudocode: {str(e)}") from None

    @public_router.post("/submit-flag")
    def submit_ctf_flag(db: SessionDep, payload: CTFSubmissionPayload):
        try:
            res = submit_flag(db, payload, data_dir=Path(CONTAINER_DATA_DIR), salt=SALT)
            volume.commit()
            return res
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error submitting flag: {str(e)}") from None

    @private_router.post("/query-re-submissions")
    def query_re_subs(db: SessionDep, query: RESubmissionQuery):
        records = query_re_submissions(db, agent_id=query.agent_id, task_id=query.task_id)
        if not records:
            raise HTTPException(status_code=404, detail="No RE submissions found")
        return [record.to_dict() for record in records]

    @private_router.post("/query-ctf-submissions")
    def query_ctf_subs(db: SessionDep, query: CTFSubmissionQuery):
        records = query_ctf_submissions(db, agent_id=query.agent_id, task_id=query.task_id, correct=query.correct)
        if not records:
            raise HTTPException(status_code=404, detail="No CTF submissions found")
        return [record.to_dict() for record in records]

    @private_router.post("/query-poc")
    def query_db(db: SessionDep, query: PocQuery):
        records = get_poc_by_hash(db, query.agent_id, query.task_id)
        if not records:
            raise HTTPException(status_code=404, detail="Record not found")
        return [record.to_dict() for record in records]

    @private_router.post("/store-evaluation")
    def store_evaluation(db: SessionDep, payload: JudgeEvaluationPayload):
        try:
            record = add_judge_evaluation(
                db,
                submission_id=payload.submission_id,
                judge_number=payload.judge_number,
                grading_schema=payload.grading_schema,
                category_scores=payload.category_scores,
                detailed_scores=payload.detailed_scores,
            )
            volume.commit()
            return {"status": "success", "submission_id": record.submission_id}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error storing evaluation: {str(e)}") from None

    fastapi.include_router(public_router)
    fastapi.include_router(private_router)

    return fastapi
