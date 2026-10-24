from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import OrganizationCreate
from app.database.models.ingestion_models import Document
from app.database.models.retrieval_models import DocumentationChunk
from app.database.session import session_scope, set_tenant_context
from app.retrieval.pgvector.store import PgVectorStore
from app.retrieval.schemas import SearchFilters
from app.shared.schemas import Identity


def _request_times(client: TestClient, *, endpoint: str, requests: int = 25, workers: int = 8) -> tuple[list[float], float]:
    def _hit() -> tuple[int, float]:
        started = time.perf_counter()
        response = client.get(endpoint)
        elapsed = time.perf_counter() - started
        return response.status_code, elapsed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: _hit(), range(requests)))

    status_codes = [status for status, _ in results]
    timings = [elapsed for _, elapsed in results]
    assert all(code in (200, 503) for code in status_codes), status_codes
    return timings, sum(timings) / len(timings)


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="requires DATABASE_URL for a real RLS proof")
def test_p0_rls_isolation_proof() -> None:
    script = PROJECT_ROOT / "scripts" / "verify_rls_isolation.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        timeout=180,
    )
    assert result.returncode == 0, f"RLS verification failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "All checks passed" in result.stdout


def test_p0_api_health_concurrency_benchmark() -> None:
    from app.api import main as api_main

    with TestClient(api_main.app) as client:
        timings, avg = _request_times(client, endpoint="/health", requests=30, workers=8)

    p95 = statistics.quantiles(timings, n=100)[94]
    print(f"api_health_concurrency: avg={avg * 1000:.1f}ms p95={p95 * 1000:.1f}ms")
    assert avg < 0.5, f"health endpoint average latency too high: {avg:.3f}s"
    assert p95 < 1.0, f"health endpoint p95 too high: {p95:.3f}s"


@pytest.mark.asyncio
async def test_p0_pgvector_search_benchmark() -> None:
    async with session_scope() as session:
        organization = await tenancy_service.create_organization(
            session,
            OrganizationCreate(name=f"bench-{uuid.uuid4().hex[:8]}", slug=f"bench-{uuid.uuid4().hex[:8]}"),
        )
        await set_tenant_context(session, organization.id)
        project = await tenancy_service.get_default_project(
            Identity.for_agent("p0-benchmark", organization.id), organization.id
        )

        rows: list[DocumentationChunk] = []
        for i in range(200):
            document = Document(
                organization_id=organization.id,
                project_id=project.id,
                source="benchmark",
                external_id=f"doc-{i}",
                content_hash=f"hash-{i}",
                title=f"Benchmark Document {i}",
                source_url=f"https://example.com/{i}",
                status="published",
            )
            session.add(document)
            await session.flush()
            chunk = DocumentationChunk(
                organization_id=organization.id,
                project_id=project.id,
                document_id=document.id,
                chunk_index=i,
                content=f"benchmark chunk {i} for pgvector latency measurement",
                embedding=[0.0] * 384,
                source_offset_start=0,
                source_offset_end=len("benchmark chunk"),
                acl_permission_code=None,
                repo_full_name=None,
            )
            session.add(chunk)
            rows.append(chunk)

        # Make a single strong match vector distinct from the rest to ensure a
        # realistic similarity search rather than a degenerate no-op.
        query = [1.0 if j % 3 == 0 else 0.0 for j in range(384)]
        rows[0].embedding = query
        for row in rows[1:]:
            row.embedding = [0.01 * ((i + j) % 7) for j in range(384)]
        await session.flush()
        store = PgVectorStore()

        start = time.perf_counter()
        for _ in range(5):
            await store.search_all(
                session,
                query,
                SearchFilters(organization_id=organization.id, permission_codes=frozenset()),
                top_k=10,
            )
        elapsed = time.perf_counter() - start
        log_ms = elapsed / 5 * 1000
        print(f"pgvector_search_all: {log_ms:.1f}ms per search")
        assert log_ms < 1500.0, f"pgvector search too slow: {log_ms:.1f}ms"


@pytest.mark.asyncio
async def test_p0_rag_latency_benchmark() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("requires OPENAI_API_KEY for the real LLM-backed RAG path")

    async with session_scope() as session:
        organization = await tenancy_service.create_organization(
            session,
            OrganizationCreate(name=f"rag-{uuid.uuid4().hex[:8]}", slug=f"rag-{uuid.uuid4().hex[:8]}"),
        )
        await set_tenant_context(session, organization.id)
        project = await tenancy_service.get_default_project(
            Identity.for_agent("p0-rag-benchmark", organization.id), organization.id
        )
        doc = Document(
            organization_id=organization.id,
            project_id=project.id,
            source="benchmark",
            external_id="rag-doc-1",
            content_hash="rag-hash-1",
            title="Benchmark RAG Document",
            source_url="https://example.com/rag-doc",
            status="published",
        )
        session.add(doc)
        await session.flush()
        chunk = DocumentationChunk(
            organization_id=organization.id,
            project_id=project.id,
            document_id=doc.id,
            chunk_index=0,
            content="The system handles incident response, deployment safety, and multi-tenant isolation for enterprise operations.",
            embedding=[0.1 if i % 2 == 0 else -0.1 for i in range(384)],
            source_offset_start=0,
            source_offset_end=120,
            acl_permission_code=None,
            repo_full_name=None,
        )
        session.add(chunk)
        await session.flush()

    from app.agents.llm import get_llm
    from app.agents.retrieval.context_assembly import assemble_context
    from app.agents.retrieval.reranking import rerank
    from app.agents.retrieval.rewriting import rewrite_query
    from app.retrieval import service as retrieval_service
    from app.retrieval.schemas import SearchFilters

    actor = Identity.for_agent("rag-latency-test", organization.id)
    question = "How does the system handle enterprise incident response and deployment safety?"

    start = time.perf_counter()
    async with session_scope() as session:
        rewritten = await rewrite_query(
            session,
            query=question,
            incident_id=None,
            actor=actor,
            llm=get_llm(),
            retry_count={},
        )
        filters = SearchFilters(organization_id=organization.id, permission_codes=frozenset())
        candidates = await retrieval_service.search(session, rewritten, filters, 12)
        reranked = await rerank(rewritten, candidates, top_k=5)
        assembled = assemble_context(reranked)
    elapsed = time.perf_counter() - start
    print(f"rag_end_to_end: {elapsed:.3f}s")
    assert elapsed < 20.0, f"RAG benchmark exceeded budget: {elapsed:.3f}s"
    assert len(assembled) > 0, "no context assembled"


@pytest.mark.asyncio
async def test_p0_redis_queue_lock_and_retry_benchmark(monkeypatch) -> None:
    from app.ingestion.workers import tasks as tasks_module

    class _FakeRedis:
        def __init__(self):
            self.set_calls = 0
            self.eval_calls = 0

        async def set(self, *args, **kwargs):
            self.set_calls += 1
            return True

        async def eval(self, *args):
            self.eval_calls += 1
            return 1

    class _FakeJob:
        status = "completed"
        id = uuid.uuid4()
        organization_id = uuid.uuid4()
        failed_stage = None

    redis = _FakeRedis()
    ctx = {"job_id": "benchmark-job", "job_try": 1, "redis": redis}

    async def fake_run_ingestion_job(_session, _connector_config_id, **_kwargs):
        return _FakeJob()

    @asynccontextmanager
    async def _fake_session_scope():
        yield object()

    monkeypatch.setattr(tasks_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(tasks_module.service, "run_ingestion_job", fake_run_ingestion_job)

    start = time.perf_counter()
    for _ in range(50):
        try:
            await tasks_module.run_ingestion_job_task(ctx, str(uuid.uuid4()))
        except Exception:  # noqa: BLE001
            pytest.fail("queue benchmark should not raise for successful jobs")
    elapsed = time.perf_counter() - start
    print(f"queue_lock_retry_path: {elapsed:.3f}s for 50 jobs")
    assert elapsed < 2.0, f"queue benchmark exceeded budget: {elapsed:.3f}s"
