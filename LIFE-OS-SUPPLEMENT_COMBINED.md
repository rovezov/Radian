# Life OS — Supplement: Coding Engine, Workspaces & Personal Data Layer
> **Additive to `CODING_AGENT_INSTRUCTIONS.md`.** Build and verify the base system first.
> This supplement adds three features:
> 1. **Coding engine** — Aider-powered agent that writes code in persistent git workspaces
> 2. **Project workspaces** — git-tracked project directories managed by `WorkspaceManager`
> 3. **Personal data storage** — file upload, ingestion, embedding, and agent context retrieval
>
> **Domain design:** The `domain` field throughout this supplement is a free-form string.
> No domains are predefined. Agents and users create domains dynamically by using a new
> domain name in an upload or context entry — MinIO paths and DB records are created
> on demand. No static folder setup is required.

---

## New Directories and Files

Add to the structure from ARCHITECTURE.md Section 5:

```
backend/
  agents/
    coding/
      Dockerfile                    <- Aider coding agent container image
  app/
    agents/
      coding_agent.py               <- CodingAgent class
    workspaces/
      manager.py                    <- WorkspaceManager
    storage/
      minio_client.py               <- MinIO client factory + bucket init
    ingestion/
      worker.py                     <- BackgroundTask: extract -> chunk -> embed -> store
    memory/
      context_retriever.py          <- Agent interface for personal data queries
    api/
      projects.py                   <- Project CRUD + deploy
      data.py                       <- Upload, context CRUD, semantic search
    models/
      project.py
      personal_file.py
      personal_chunk.py
      personal_context_entry.py
frontend/
  src/
    app/
      projects/
        page.tsx
      data/
        page.tsx
```

---

## Part 1 — Infrastructure Changes

### 1.1 `docker-compose.yml`

Add the `minio` service. Add `workspaces_data` and `minio_data` to named volumes.
Add the `workspaces_data` mount to the `backend` service.

```yaml
services:
  minio:
    image: minio/minio:latest
    container_name: life-os-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes:
      - minio_data:/data
    networks:
      - internal
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 10s
      retries: 3

  # Add to existing backend service:
  backend:
    volumes:
      - workspaces_data:/data/workspaces   # add alongside existing volume mounts
    depends_on:
      minio:
        condition: service_healthy

# Add to named volumes:
volumes:
  minio_data:
  workspaces_data:

# Update networks section (replace the stub from base instructions):
networks:
  internal:
    driver: bridge
    internal: true    # no direct internet access from this network
  external:
    driver: bridge
```

Add to `docker-compose.dev.yml`:

```yaml
minio:
  ports:
    - "9000:9000"   # S3 API
    - "9001:9001"   # Web console
```

### 1.2 `.env.example` additions

```bash
# --- MinIO ---
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=changeme
MINIO_ENDPOINT=minio:9000
MINIO_BUCKET=life-os

# --- Aider (coding agent) ---
AIDER_PROVIDER=ollama
AIDER_API_BASE=http://ollama:11434/v1
AIDER_API_KEY=ollama
# Model examples:
#   Ollama:  openai/qwen2.5-coder:7b
#   Claude:  claude-sonnet-4-6
AIDER_MODEL=openai/qwen2.5-coder:7b
AIDER_EDITOR_MODEL=openai/qwen2.5-coder:7b
AIDER_MAX_TOKENS=16384
AIDER_MAP_TOKENS=2048
```

### 1.3 `backend/requirements.txt` additions

Append to the existing file:

```
# Storage
minio==7.2.10

# Data ingestion
pymupdf==1.24.0
python-docx==1.1.2
pandas==2.2.3
openpyxl==3.1.5
beautifulsoup4==4.12.3
```

### 1.4 `services/postgres/init.sql` additions

Append after the existing tables:

```sql
-- Projects — one record per coding workspace
CREATE TABLE IF NOT EXISTS projects (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name         TEXT NOT NULL,
    description  TEXT,
    task_id      UUID REFERENCES tasks(id) ON DELETE SET NULL,
    workspace_id TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'archived', 'failed')),
    test_status  TEXT CHECK (test_status IN ('passing', 'failing', NULL)),
    last_commit  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER projects_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Personal files — registry of every uploaded file
CREATE TABLE IF NOT EXISTS personal_files (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename         TEXT NOT NULL,
    original_name    TEXT NOT NULL,
    mime_type        TEXT NOT NULL,
    size_bytes       BIGINT NOT NULL,
    domain           TEXT NOT NULL,    -- free-form string; created on demand
    minio_path       TEXT NOT NULL UNIQUE,
    project_id       UUID REFERENCES projects(id) ON DELETE SET NULL,
    ingestion_status TEXT NOT NULL DEFAULT 'pending'
                         CHECK (ingestion_status IN (
                             'pending', 'processing', 'complete', 'failed'
                         )),
    chunk_count      INTEGER DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER personal_files_updated_at BEFORE UPDATE ON personal_files
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Personal chunks — searchable text units extracted from files
CREATE TABLE IF NOT EXISTS personal_chunks (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    file_id     UUID NOT NULL REFERENCES personal_files(id) ON DELETE CASCADE,
    domain      TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(768),
    chunk_index INTEGER NOT NULL,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chunks_domain_idx ON personal_chunks (domain);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON personal_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Personal context — direct text entries (not extracted from a file)
CREATE TABLE IF NOT EXISTS personal_context (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain     TEXT NOT NULL,    -- free-form string; created on demand
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(768),
    tags       TEXT[] DEFAULT '{}',
    is_pinned  BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER personal_context_updated_at BEFORE UPDATE ON personal_context
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE INDEX IF NOT EXISTS context_domain_idx ON personal_context (domain);
CREATE INDEX IF NOT EXISTS context_embedding_idx ON personal_context
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
CREATE INDEX IF NOT EXISTS context_pinned_idx ON personal_context (is_pinned)
    WHERE is_pinned = TRUE;
```

### 1.5 `services/ollama/pull-models.sh` additions

Append to the existing script, after the embedding model pull:

```bash
echo "Pulling coding agent models..."
PRIMARY_MODEL="${AIDER_MODEL#openai/}"
EDITOR_MODEL="${AIDER_EDITOR_MODEL#openai/}"
ollama pull "$PRIMARY_MODEL"
if [ "$PRIMARY_MODEL" != "$EDITOR_MODEL" ]; then
    ollama pull "$EDITOR_MODEL"
fi
```

### 1.6 `backend/app/config.py` additions

Add to the `Settings` class:

```python
# MinIO
minio_root_user: str = "minioadmin"
minio_root_password: str
minio_endpoint: str = "minio:9000"
minio_bucket: str = "life-os"

# Coding agent (Aider)
aider_provider: str = "ollama"
aider_api_base: str = "http://ollama:11434/v1"
aider_api_key: str = "ollama"
aider_model: str = "openai/qwen2.5-coder:7b"
aider_editor_model: str = "openai/qwen2.5-coder:7b"
aider_max_tokens: int = 16384
aider_map_tokens: int = 2048
```

---

## Part 2 — Coding Engine

### 2.1 `backend/agents/coding/Dockerfile`

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir aider-chat

RUN apt-get update && apt-get install -y \
    curl git build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Common packages pre-installed so agents don't wait mid-task
RUN pip install --no-cache-dir \
    fastapi uvicorn sqlalchemy alembic \
    httpx pytest pytest-asyncio ruff black pyyaml

RUN npm install -g pnpm typescript

# Docker CLI — agents can build and run containers for projects they create
RUN curl -fsSL https://get.docker.com | sh

WORKDIR /workspace
ENTRYPOINT ["aider"]
```

### 2.2 `backend/app/workspaces/manager.py`

```python
from pathlib import Path
import json
import subprocess
import uuid
from app.config import get_settings


WORKSPACES_ROOT = Path("/data/workspaces")


class WorkspaceManager:

    def get_path(self, project_id: str) -> Path:
        return WORKSPACES_ROOT / project_id

    async def create(self, name: str, description: str, task_id: str) -> dict:
        project_id = str(uuid.uuid4())
        project_path = WORKSPACES_ROOT / project_id
        project_path.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init"], cwd=project_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "agent@life-os.local"],
            cwd=project_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Life OS Agent"],
            cwd=project_path, check=True
        )

        settings = get_settings()
        aider_conf = "\n".join([
            f"openai-api-base: {settings.aider_api_base}",
            f"openai-api-key: {settings.aider_api_key}",
            f"model: {settings.aider_model}",
            f"editor-model: {settings.aider_editor_model}",
            "auto-commits: true",
            "dirty-commits: true",
            "yes: true",
            "no-pretty: true",
            f"max-tokens: {settings.aider_max_tokens}",
            f"map-tokens: {settings.aider_map_tokens}",
        ])
        (project_path / ".aider.conf.yml").write_text(aider_conf)

        meta = {
            "project_id": project_id,
            "name": name,
            "description": description,
            "task_id": task_id,
            "status": "active",
            "test_status": None,
            "last_commit": None,
        }
        (project_path / "meta.json").write_text(json.dumps(meta, indent=2))

        return meta

    async def update_meta(self, project_id: str, updates: dict):
        meta_path = self.get_path(project_id) / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta.update(updates)
        meta_path.write_text(json.dumps(meta, indent=2))

    async def get_file_tree(self, project_id: str) -> list[str]:
        project_path = self.get_path(project_id)
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_path, capture_output=True, text=True
        )
        return result.stdout.strip().splitlines()
```

### 2.3 `backend/app/agents/coding_agent.py`

```python
import json
import subprocess
import docker
from pathlib import Path

from app.agents.base import BaseAgent
from app.workspaces.manager import WorkspaceManager
from app.config import get_settings
from app.core.events import publish_event, SystemEvent, EventTypes
from app.core.metrics import api_fallback_calls
from app.skills.registry import SkillRegistry


class CodingAgent(BaseAgent):
    role = "coding_agent"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace_manager = WorkspaceManager()
        self.docker_client = docker.from_env()
        self.settings = get_settings()
        self.skill_registry = SkillRegistry()

    async def execute(self, instructions: str, project_id: str | None = None) -> str:
        if project_id is None:
            meta = await self.workspace_manager.create(
                name=self._extract_project_name(instructions),
                description=instructions[:200],
                task_id=self.task_id
            )
            project_id = meta["project_id"]

        workspace_path = self.workspace_manager.get_path(project_id)
        message = await self._build_message(instructions, project_id, workspace_path)

        await publish_event(self.redis, SystemEvent(
            event_type=EventTypes.AGENT_STARTED,
            agent_id=self.agent_id,
            task_id=self.task_id,
            payload={"project_id": project_id}
        ))

        output = await self._run_aider(message, workspace_path)
        test_result = await self._run_tests(workspace_path)

        # Retry once on the same provider if tests fail
        if not test_result["passed"] and test_result["passed"] is not None:
            retry_message = (
                f"Fix these test failures:\n\n{test_result['output']}\n\n"
                f"Original task: {instructions}"
            )
            output = await self._run_aider(retry_message, workspace_path)
            test_result = await self._run_tests(workspace_path)

        # Escalate to cloud fallback if still failing
        if not test_result["passed"] and test_result["passed"] is not None:
            if self.settings.anthropic_api_key:
                output = await self._run_aider_cloud_fallback(instructions, workspace_path)
                test_result = await self._run_tests(workspace_path)

        await self.workspace_manager.update_meta(project_id, {
            "status": "testing" if test_result["passed"] else "needs_review",
            "test_status": "passing" if test_result["passed"] else "failing",
            "last_commit": await self._get_last_commit(workspace_path)
        })

        return json.dumps({
            "project_id": project_id,
            "output": output,
            "test_result": test_result,
            "file_tree": await self.workspace_manager.get_file_tree(project_id),
        })

    async def _run_aider(self, message: str, workspace_path: Path) -> str:
        container = self.docker_client.containers.run(
            image="life-os-coding-agent",
            command=["--message", message, "--yes", "--no-pretty"],
            volumes={
                str(workspace_path): {"bind": "/workspace", "mode": "rw"},
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}
            },
            environment={
                "OPENAI_API_BASE": self.settings.aider_api_base,
                "OPENAI_API_KEY": self.settings.aider_api_key,
            },
            working_dir="/workspace",
            network_mode="host",    # must reach Ollama on host network
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            detach=True,
            remove=False
        )
        return await self._stream_container_logs(container)

    async def _run_aider_cloud_fallback(self, message: str, workspace_path: Path) -> str:
        api_fallback_calls.labels(provider="anthropic").inc()
        container = self.docker_client.containers.run(
            image="life-os-coding-agent",
            command=["--model", "claude-sonnet-4-6", "--message", message, "--yes", "--no-pretty"],
            volumes={str(workspace_path): {"bind": "/workspace", "mode": "rw"}},
            environment={"ANTHROPIC_API_KEY": self.settings.anthropic_api_key},
            working_dir="/workspace",
            detach=True,
            remove=False
        )
        return await self._stream_container_logs(container)

    async def _stream_container_logs(self, container) -> str:
        output_lines = []
        try:
            for log_line in container.logs(stream=True, follow=True):
                line = log_line.decode("utf-8", errors="replace")
                output_lines.append(line)
                await publish_event(self.redis, SystemEvent(
                    event_type=EventTypes.LLM_STREAM_TOKEN,
                    agent_id=self.agent_id,
                    task_id=self.task_id,
                    payload={"token": line}
                ))
            container.wait()
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass
        return "".join(output_lines)

    async def _run_tests(self, workspace_path: Path) -> dict:
        """Run pytest in workspace. Returns {passed: bool|None, output: str}."""
        result = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=workspace_path, capture_output=True, text=True, timeout=120
        )
        if result.returncode == 5:  # pytest exit code: no tests collected
            return {"passed": None, "output": "No tests found"}
        return {
            "passed": result.returncode == 0,
            "output": result.stdout + result.stderr
        }

    async def _get_last_commit(self, workspace_path: Path) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace_path, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    async def _get_git_log(self, workspace_path: Path, lines: int = 5) -> str:
        result = subprocess.run(
            ["git", "log", f"-{lines}", "--oneline"],
            cwd=workspace_path, capture_output=True, text=True
        )
        return result.stdout

    def _extract_project_name(self, instructions: str) -> str:
        first_line = instructions.strip().split("\n")[0]
        return first_line[:60]

    async def _build_message(
        self, instructions: str, project_id: str, workspace_path: Path
    ) -> str:
        git_log = await self._get_git_log(workspace_path, lines=5)
        relevant_skills = await self.skill_registry.search(instructions, limit=2)
        skills_hint = ""
        if relevant_skills:
            skill_names = ", ".join(s.name for s in relevant_skills)
            skills_hint = (
                f"\n\nThe skill registry has potentially relevant patterns: "
                f"{skill_names}. Use them if appropriate."
            )
        history = f"\n\nRECENT COMMITS:\n{git_log}" if git_log.strip() else ""
        return (
            f"{instructions}\n\n"
            f"CONVENTIONS:\n"
            f"- Conventional commits (feat:, fix:, refactor:, test:, chore:)\n"
            f"- Tests for all non-trivial logic\n"
            f"- Dockerfile + docker-compose.yml if creating a web service\n"
            f"- Environment variables for secrets -- document in .env.example\n"
            f"{history}{skills_hint}"
        )
```

---

## Part 3 — Personal Data Storage

### 3.1 `backend/app/storage/minio_client.py`

```python
import io
from functools import lru_cache
from minio import Minio
from app.config import get_settings


@lru_cache
def get_minio_client() -> Minio:
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=False
    )


async def init_minio():
    """Create the bucket if it does not exist. Call from app lifespan."""
    settings = get_settings()
    client = get_minio_client()
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
```

Call `await init_minio()` in `backend/app/main.py` inside the lifespan startup block.

### 3.2 `backend/app/ingestion/worker.py`

```python
import io
import pymupdf
from docx import Document
import pandas as pd
from bs4 import BeautifulSoup
import httpx

from app.models.personal_file import PersonalFile
from app.models.personal_chunk import PersonalChunk
from app.core.events import publish_event, SystemEvent
from app.config import get_settings


CHUNK_SIZE_CHARS = 1600    # ~400 tokens at 4 chars/token
CHUNK_OVERLAP_CHARS = 200


async def ingest_file(file_id: str, db, minio_client, redis):
    """FastAPI BackgroundTask. Runs after upload response is sent."""
    async with db() as session:
        file_record = await session.get(PersonalFile, file_id)
        if not file_record:
            return

        file_record.ingestion_status = "processing"
        await session.commit()

        try:
            settings = get_settings()
            response = minio_client.get_object(
                settings.minio_bucket, file_record.minio_path
            )
            file_bytes = response.read()

            text_blocks = _extract_text(file_bytes, file_record.mime_type)
            chunks = _chunk_text(text_blocks)

            for i, chunk in enumerate(chunks):
                embedding = await _get_embedding(chunk["content"])
                session.add(PersonalChunk(
                    file_id=file_record.id,
                    domain=file_record.domain,
                    content=chunk["content"],
                    embedding=embedding,
                    chunk_index=i,
                    metadata=chunk.get("metadata", {})
                ))

            file_record.ingestion_status = "complete"
            file_record.chunk_count = len(chunks)
            await session.commit()

            await publish_event(redis, SystemEvent(
                event_type="file.ingested",
                payload={"file_id": file_id, "chunk_count": len(chunks)}
            ))

        except Exception:
            file_record.ingestion_status = "failed"
            await session.commit()
            raise


def _extract_text(file_bytes: bytes, mime_type: str) -> list[dict]:
    if mime_type == "application/pdf":
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
        return [
            {"content": page.get_text(), "metadata": {"page": i + 1}}
            for i, page in enumerate(doc)
            if page.get_text().strip()
        ]
    elif mime_type in ("text/plain", "text/markdown"):
        return [{"content": file_bytes.decode("utf-8", errors="replace")}]
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc = Document(io.BytesIO(file_bytes))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return [{"content": text}]
    elif mime_type == "text/csv":
        df = pd.read_csv(io.BytesIO(file_bytes))
        rows = df.apply(lambda r: " | ".join(f"{c}: {v}" for c, v in r.items()), axis=1)
        return [{"content": "\n".join(rows)}]
    elif mime_type in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel"
    ):
        df = pd.read_excel(io.BytesIO(file_bytes))
        rows = df.apply(lambda r: " | ".join(f"{c}: {v}" for c, v in r.items()), axis=1)
        return [{"content": "\n".join(rows)}]
    elif mime_type == "text/html":
        soup = BeautifulSoup(file_bytes, "html.parser")
        return [{"content": soup.get_text(separator="\n")}]
    return []


def _chunk_text(blocks: list[dict]) -> list[dict]:
    chunks = []
    for block in blocks:
        text = block["content"]
        meta = block.get("metadata", {})
        start = 0
        while start < len(text):
            end = start + CHUNK_SIZE_CHARS
            chunks.append({"content": text[start:end], "metadata": meta})
            start = end - CHUNK_OVERLAP_CHARS
    return chunks


async def _get_embedding(text: str) -> list[float]:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.ollama_host}/api/embeddings",
            json={"model": settings.ollama_embed_model, "prompt": text},
            timeout=30.0
        )
        return response.json()["embedding"]
```

### 3.3 `backend/app/memory/context_retriever.py`

```python
from app.ingestion.worker import _get_embedding


class PersonalContextRetriever:
    def __init__(self, agent_role: str, db, redis):
        self.agent_role = agent_role
        self.db = db
        self.redis = redis
        self.permitted_domains = self._get_permitted_domains()

    def _get_permitted_domains(self) -> list[str] | None:
        """
        Returns domain filter for this agent role.
        Phase 4: replace None with a role -> domains mapping once domain agents exist.
        None = no domain filter applied (all domains accessible).
        """
        return None

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        query_embedding = await _get_embedding(query)
        domain_filter = self.permitted_domains

        chunk_results = await self.db.execute(
            """
            SELECT content, domain, metadata, file_id,
                   1 - (embedding <=> $1) AS similarity
            FROM personal_chunks
            WHERE ($2::text[] IS NULL OR domain = ANY($2))
            ORDER BY embedding <=> $1
            LIMIT $3
            """,
            query_embedding, domain_filter, limit
        )

        context_results = await self.db.execute(
            """
            SELECT content, domain, title, tags,
                   1 - (embedding <=> $1) AS similarity
            FROM personal_context
            WHERE ($2::text[] IS NULL OR domain = ANY($2))
            ORDER BY embedding <=> $1
            LIMIT $3
            """,
            query_embedding, domain_filter, limit // 2
        )

        all_results = list(chunk_results) + list(context_results)
        all_results.sort(key=lambda r: r["similarity"], reverse=True)
        return all_results[:limit]

    async def get_pinned(self) -> list[dict]:
        domain_filter = self.permitted_domains
        return await self.db.execute(
            """
            SELECT title, content, domain, tags
            FROM personal_context
            WHERE ($1::text[] IS NULL OR domain = ANY($1)) AND is_pinned = TRUE
            ORDER BY domain, created_at
            """,
            domain_filter
        )

    async def get_project_brief(self, project_id: str) -> str | None:
        result = await self.db.execute(
            """
            SELECT content FROM personal_chunks
            WHERE file_id IN (
                SELECT id FROM personal_files WHERE project_id = $1
            )
            ORDER BY chunk_index
            """,
            project_id
        )
        if result:
            return "\n\n".join(row["content"] for row in result)
        return None
```

---

## Part 4 — New API Routes

### 4.1 `backend/app/api/projects.py`

```
GET    /projects              -- list all with status, test_status, last_commit
GET    /projects/{id}         -- detail + file tree (from WorkspaceManager.get_file_tree)
GET    /projects/{id}/diff    -- git diff of last 5 commits
POST   /projects/{id}/deploy  -- run tests -> docker build -> docker-compose up
DELETE /projects/{id}         -- archive (status=archived; does not delete files)
```

### 4.2 `backend/app/api/data.py`

**Upload route:**

```python
import io
import uuid
from fastapi import APIRouter, UploadFile, BackgroundTasks, Depends
from app.models.personal_file import PersonalFile
from app.storage.minio_client import get_minio_client
from app.ingestion.worker import ingest_file
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.config import get_settings

router = APIRouter(prefix="/data", tags=["data"])


@router.post("/upload", status_code=202)
async def upload_file(
    file: UploadFile,
    domain: str,
    project_id: str | None = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db=Depends(get_db),
    redis=Depends(get_redis)
):
    settings = get_settings()
    minio = get_minio_client()

    file_data = await file.read()
    minio_path = f"{domain}/{uuid.uuid4()}/{file.filename}"
    minio.put_object(
        settings.minio_bucket, minio_path,
        io.BytesIO(file_data), length=len(file_data),
        content_type=file.content_type
    )

    async with db() as session:
        file_record = PersonalFile(
            filename=minio_path.split("/")[-1],
            original_name=file.filename,
            mime_type=file.content_type,
            size_bytes=len(file_data),
            domain=domain,
            minio_path=minio_path,
            project_id=project_id,
        )
        session.add(file_record)
        await session.commit()
        file_id = str(file_record.id)

    background_tasks.add_task(ingest_file, file_id, db, minio, redis)
    return {"file_id": file_id, "status": "pending"}
```

**Additional routes:**

```
GET    /data/files             -- list files with ingestion_status + chunk_count
GET    /data/files/{id}        -- file detail
DELETE /data/files/{id}        -- delete DB record + MinIO object + all chunks

POST   /data/context           -- create entry: {domain, title, content, tags, is_pinned}
GET    /data/context           -- list entries (optional ?domain= filter)
PATCH  /data/context/{id}      -- update (including toggle is_pinned)
DELETE /data/context/{id}      -- delete

POST   /data/search            -- semantic search: {query, domain?, limit?} -> ranked chunks
```

On `POST /data/context` and `PATCH /data/context/{id}`, compute and store the embedding
for `content` using `_get_embedding()` from `ingestion/worker.py`.

---

## Part 5 — Orchestrator Update

In `backend/app/orchestrator/nodes.py`, update `delegate_node` to enrich task
instructions with retrieved personal context before delegating to an agent:

```python
from app.memory.context_retriever import PersonalContextRetriever


async def delegate_node(state: OrchestratorState) -> OrchestratorState:
    current_step = state["plan"][state["current_step"]]
    agent_role = current_step["agent_role"]
    instructions = current_step["instructions"]

    retriever = PersonalContextRetriever(agent_role, db, redis)
    pinned = await retriever.get_pinned()
    relevant = await retriever.search(instructions, limit=5)

    context_block = _format_context(pinned, relevant)
    enriched_instructions = f"{context_block}\n\n---\n\nTASK:\n{instructions}"

    # use enriched_instructions in place of instructions for the rest of this node


def _format_context(pinned: list, relevant: list) -> str:
    lines = ["PERSONAL CONTEXT (retrieved for this task):"]

    if pinned:
        lines.append("\nSTANDING RULES (always apply):")
        for entry in pinned:
            lines.append(
                f"  [{entry['domain'].upper()}] {entry['title']}: {entry['content']}"
            )

    if relevant:
        lines.append("\nRELEVANT BACKGROUND:")
        for chunk in relevant:
            source = chunk.get("title") or \
                f"Document (p.{chunk.get('metadata', {}).get('page', '?')})"
            lines.append(f"\n  [{chunk['domain'].upper()} -- {source}]")
            content = chunk["content"]
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"  {content}")

    if not pinned and not relevant:
        lines.append("  No relevant personal context found.")

    return "\n".join(lines)
```

---

## Part 6 — New SQLAlchemy Models

Create one file per table using SQLAlchemy 2.0 declarative syntax with type annotations.
Import all new models in `models/__init__.py`.

| File | Table | Key columns |
|---|---|---|
| `models/project.py` | `projects` | `id`, `name`, `description`, `task_id` (FK->tasks), `workspace_id`, `status`, `test_status`, `last_commit` |
| `models/personal_file.py` | `personal_files` | `id`, `filename`, `original_name`, `mime_type`, `size_bytes`, `domain`, `minio_path`, `project_id` (FK->projects), `ingestion_status`, `chunk_count` |
| `models/personal_chunk.py` | `personal_chunks` | `id`, `file_id` (FK->personal_files, cascade delete), `domain`, `content`, `embedding` (Vector(768)), `chunk_index`, `metadata` (JSONB) |
| `models/personal_context_entry.py` | `personal_context` | `id`, `domain`, `title`, `content`, `embedding` (Vector(768)), `tags` (ARRAY), `is_pinned` |

---

## Part 7 — Frontend Pages

### 7.1 `frontend/src/app/projects/page.tsx`

List all projects from `GET /projects`. Each card displays: name, status badge,
test_status badge, last commit hash. Actions: [View Diff] (calls `GET /projects/{id}/diff`,
renders in a modal), [Deploy] (calls `POST /projects/{id}/deploy`, streams output via
WebSocket). Clicking a card expands the file tree from `GET /projects/{id}`.

### 7.2 `frontend/src/app/data/page.tsx`

Two side-by-side panels:

**Upload panel:** Drag-and-drop or browse. Free-text `domain` input (no dropdown -- users
type any domain name). Optional project selector populated from `GET /projects`.
On submit, calls `POST /data/upload`. Renders a status badge that updates in real time
via WebSocket: `pending` -> `processing` -> `complete` (with chunk count).
Lists uploaded files below with ingestion status and a delete button.

**Context panel:** Form with title, domain (free-text), content (textarea), tags, and
"Pin this" toggle. Submits to `POST /data/context`. Lists existing entries below with
edit/delete controls. Pinned entries display a pin icon -- they always appear in agent
context regardless of semantic relevance. Includes a search box that calls
`POST /data/search` and renders ranked results inline.

---

## Verification Checklist

- [ ] `docker compose up` includes `minio`, starts healthy
- [ ] MinIO console accessible at `http://localhost:9001`, bucket `life-os` exists
- [ ] All 4 new SQL tables present: `projects`, `personal_files`, `personal_chunks`, `personal_context`
- [ ] `POST /data/upload` returns `202` immediately
- [ ] After upload, polling `GET /data/files/{id}` transitions to `complete` with `chunk_count > 0`
- [ ] `POST /data/search` with a relevant query returns ranked results
- [ ] Creating a task that uses `CodingAgent` creates a directory under `/data/workspaces/`
- [ ] Workspace directory contains `.git/`, `.aider.conf.yml`, `meta.json`
- [ ] Aider container runs, commits appear in workspace git log
- [ ] `GET /projects` returns the created project with `test_status`
- [ ] `delegate_node` prepends context block when personal data exists
