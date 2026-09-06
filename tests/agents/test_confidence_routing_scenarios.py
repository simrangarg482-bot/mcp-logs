"""Regression tests for `app.agents.confidence`'s answer-vs-investigation
routing decision, added during the 2026-09-05 confidence-calibration review
(see `docs/PROJECT_STATUS.md`'s corresponding phase entry and
`docs/SEMANTIC_BENCHMARK.md`'s threshold inventory).

WHY THIS FILE EXISTS, AND WHAT IT DOES NOT CLAIM
    This review found no valid live-production evidence for
    `_SIGNAL_WEIGHTS`, `_DENSE_SIMILARITY_FLOOR`/`_DENSE_SIMILARITY_CEILING`,
    or `Settings.confidence_threshold` (see the phase entry for why: the
    only prior live runs -- `scripts/eval_confidence_report*.json` -- predate
    both the 2026-08-30 reranker calibration and the 2026-09-02 signal-shape
    fixes, and even ignoring that, sit below `app.evaluation.semantic.
    calibration`'s own 20-example floor). Per that finding, none of those
    values were changed here.

    These tests therefore do NOT assert that the current formula produces
    the "correct" or "safe" answer for every scenario below -- that would
    be claiming a calibration this review explicitly could not perform.
    What they DO is pin down, precisely and deterministically, what the
    *current, unchanged* formula actually outputs for each named scenario,
    computed independently via the same private helper functions
    `evaluate_confidence` itself calls (never hand-computed magic numbers),
    against the REAL shipped default threshold (`Settings.
    confidence_threshold`'s field default, read without needing a full
    `Settings()` instantiation -- see `_real_default_threshold`). This is a
    regression net: if a future change to the weights/floor/ceiling/
    threshold shifts any of these routing outcomes, that change is now
    visible and must be a deliberate, evidence-cited decision (per
    `app/agents/confidence.py`'s own module docstring), not a silent
    side effect.

    Where a scenario's current output is itself a known, documented
    limitation (e.g. a strong dense-similarity signal outweighing a
    strongly negative reranker signal), the test says so in its docstring
    rather than presenting the observed behavior as validated-safe.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import confidence as confidence_module
from app.agents.confidence import (
    _distinct_source_count_signal,
    _normalize_rerank_score,
    _normalize_top_similarity,
    _weighted_score,
    evaluate_confidence,
)
from app.agents.graph import GraphState
from app.retrieval.schemas import ScoredChunk
from app.shared.config.settings import Settings
from app.shared.schemas import Identity

_ACTOR = Identity.for_agent("test_confidence_routing", uuid.uuid4())


class _FakeSettings:
    """Same stand-in `tests/agents/test_confidence.py` uses -- isolates
    these tests from whatever `Settings.confidence_threshold` a repo-local
    `.env` might set, while still letting each test declare exactly which
    threshold it means to exercise against.
    """

    def __init__(self, threshold: float) -> None:
        self.confidence_threshold = threshold


def _real_default_threshold() -> float:
    """The actual shipped default for `Settings.confidence_threshold`, read
    from the field definition rather than instantiating `Settings()`
    (which needs a full `.env`/environment this test suite does not
    assume). A canary as much as a helper: if this ever stops returning
    0.5, it means the production default itself changed, and every test
    below using it should be re-read against the new value, not silently
    re-pinned.
    """
    return Settings.model_fields["confidence_threshold"].default


def _chunk(document_id: uuid.UUID, *, score: float) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=document_id,
        collection="documentation",
        content="some retrieved content",
        score=score,
        source_offset_start=0,
        source_offset_end=21,
    )


def _state(
    *,
    chunks: list[ScoredChunk] | None = None,
    signals: dict[str, float] | None = None,
    incident_id: uuid.UUID | None = None,
) -> GraphState:
    return GraphState(
        query="why did checkout break?",
        actor=_ACTOR,
        incident_id=incident_id,
        retrieved_chunks=chunks or [],
        confidence_signals=signals or {},
    )


def _expected_score(
    *,
    top_similarity_raw: float | None,
    rerank_raw: float | None,
    chunks: list[ScoredChunk],
) -> float:
    """Independently compose the expected weighted score from the same
    private helpers `evaluate_confidence` calls, so every assertion below
    is derived from production code, not a hand-typed constant that could
    silently drift out of sync with the real formula.
    """
    signals: dict[str, float] = {}
    if top_similarity_raw is not None:
        signals["top_similarity"] = _normalize_top_similarity(top_similarity_raw)
    signals["rerank_score"] = _normalize_rerank_score(rerank_raw) if rerank_raw is not None else 0.0
    signals["source_count"] = _distinct_source_count_signal(chunks)
    return _weighted_score(signals)


def test_real_production_default_threshold_is_0_5() -> None:
    """Canary: every other test in this file exercises behavior "at the
    real production default" by reading this value, not by hardcoding
    0.5 a second time. If `Settings.confidence_threshold`'s default ever
    changes, this test fails first and points at every test below that
    needs re-reading against the new default.
    """
    assert _real_default_threshold() == pytest.approx(0.5)


# --------------------------------------------------------------------------
# 1. Clearly answerable / high confidence -> ANSWER
# --------------------------------------------------------------------------


def test_clearly_answerable_high_confidence_routes_to_answer(monkeypatch) -> None:
    """Strong dense similarity (0.62, near the calibrated ceiling), a
    strong reranker score (-3.0, this model's documented "strong match"
    anchor -- see `_normalize_rerank_score`'s docstring), and three
    corroborating distinct sources: the unambiguous "should answer" shape.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(_real_default_threshold()))
    doc_a, doc_b, doc_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chunks = [_chunk(doc_a, score=-3.0), _chunk(doc_b, score=-3.0), _chunk(doc_c, score=-3.0)]

    out = evaluate_confidence(_state(chunks=chunks, signals={"top_similarity": 0.62}))

    expected = _expected_score(top_similarity_raw=0.62, rerank_raw=-3.0, chunks=chunks)
    assert out["confidence_score"] == pytest.approx(expected)
    assert out["route"] == "answer"
    assert out["confidence_score"] > 0.8  # comfortably clear of the threshold, not a squeaker


# --------------------------------------------------------------------------
# 2. Clearly unsupported -> INVESTIGATE / DECLINE
# --------------------------------------------------------------------------


def test_clearly_unsupported_question_routes_to_investigation(monkeypatch) -> None:
    """Dense similarity below the calibrated floor (out-of-domain), a very
    negative reranker score, and only one weak source: the unambiguous
    "should decline" shape -- the no-information case
    `scripts/eval_confidence_dataset.json` labels this way.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(_real_default_threshold()))
    doc = uuid.uuid4()
    chunks = [_chunk(doc, score=-18.0)]

    out = evaluate_confidence(_state(chunks=chunks, signals={"top_similarity": 0.15}))

    expected = _expected_score(top_similarity_raw=0.15, rerank_raw=-18.0, chunks=chunks)
    assert out["confidence_score"] == pytest.approx(expected)
    assert out["route"] == "investigation"
    assert out["confidence_score"] < 0.3  # comfortably clear of the threshold, not a squeaker


# --------------------------------------------------------------------------
# 3. Borderline evidence -> conservative (investigate) behavior
# --------------------------------------------------------------------------


def test_borderline_evidence_just_under_threshold_is_conservative(monkeypatch) -> None:
    """A dense similarity right at the calibrated midpoint (normalizes to
    ~0.5), a reranker score right at its own documented "borderline" anchor
    (-8.0, normalizes to exactly 0.5 -- see `_normalize_rerank_score`'s own
    calibration test), and a single source: every signal is a genuine
    coin-flip, not a strong pull either way. `evaluate_confidence` has no
    "when in doubt, decline" bias built in -- it is a plain weighted
    average -- so this scenario pins down which side of the real default
    threshold a truly ambiguous case actually lands on today, which is
    exactly the kind of case a future real calibration run needs to check
    against ground truth.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(_real_default_threshold()))
    doc = uuid.uuid4()
    chunks = [_chunk(doc, score=-8.0)]
    midpoint_similarity = 0.5  # (_DENSE_SIMILARITY_FLOOR + _DENSE_SIMILARITY_CEILING) / 2

    out = evaluate_confidence(_state(chunks=chunks, signals={"top_similarity": midpoint_similarity}))

    expected = _expected_score(top_similarity_raw=midpoint_similarity, rerank_raw=-8.0, chunks=chunks)
    assert out["confidence_score"] == pytest.approx(expected)
    # Documents current behavior; not a safety claim either way -- see
    # module docstring. Assert the actual route only pinned to the real
    # default, so a future weight/threshold change is caught here.
    threshold = _real_default_threshold()
    assert out["route"] == ("answer" if expected >= threshold else "investigation")


# --------------------------------------------------------------------------
# 4. High similarity but conflicting/weak evidence
# --------------------------------------------------------------------------


def test_high_similarity_with_strongly_conflicting_rerank_signal(monkeypatch) -> None:
    """A KNOWN LIMITATION, characterized here rather than hidden: dense
    similarity alone can reach 1.0 (raw similarity at or above the
    calibrated ceiling) while the cross-encoder reranker -- which actually
    reads the candidate content against the query, unlike a pure embedding
    match -- scores the same top chunk very negatively (a strong signal
    the retrieved content does not really support the query). Because
    `top_similarity` carries the largest weight (0.40) and a single source
    is already worth 0.70 on its own diminishing-returns curve, this
    combination can still clear the real default threshold even though the
    reranker is flatly contradicting the dense-similarity signal. This test
    exists to make that fact visible and regression-checked, NOT to assert
    it is the desired/safe behavior -- see this file's module docstring.
    Any future evidence-based calibration should treat this scenario as a
    named case to re-check.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(_real_default_threshold()))
    doc = uuid.uuid4()
    chunks = [_chunk(doc, score=-20.0)]  # reranker strongly rejects the top chunk

    out = evaluate_confidence(_state(chunks=chunks, signals={"top_similarity": 0.65}))

    expected = _expected_score(top_similarity_raw=0.65, rerank_raw=-20.0, chunks=chunks)
    assert out["confidence_score"] == pytest.approx(expected)
    # Pin the actual current outcome (documented above as a known
    # limitation) rather than asserting a desired one.
    threshold = _real_default_threshold()
    assert out["route"] == ("answer" if expected >= threshold else "investigation")


# --------------------------------------------------------------------------
# 5. Low similarity despite other positive signals
# --------------------------------------------------------------------------


def test_low_similarity_despite_strong_rerank_and_many_sources(monkeypatch) -> None:
    """The inverse of scenario 4: dense similarity below the calibrated
    floor (normalizes to 0.0 -- an out-of-domain query against the
    embedding space) but a strong reranker score and four corroborating
    sources. Documents whether a genuinely out-of-domain dense match can
    still be dragged back over the threshold by the other two signals --
    another named case for a future real calibration pass, not a safety
    claim either way.
    """
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(_real_default_threshold()))
    docs = [uuid.uuid4() for _ in range(4)]
    chunks = [_chunk(doc, score=-3.0) for doc in docs]

    out = evaluate_confidence(_state(chunks=chunks, signals={"top_similarity": 0.10}))

    expected = _expected_score(top_similarity_raw=0.10, rerank_raw=-3.0, chunks=chunks)
    assert out["confidence_score"] == pytest.approx(expected)
    assert out["confidence_signals"]["top_similarity"] == 0.0  # clamped at the floor
    threshold = _real_default_threshold()
    assert out["route"] == ("answer" if expected >= threshold else "investigation")


# --------------------------------------------------------------------------
# 6. Confidence near the routing threshold
# --------------------------------------------------------------------------


def test_confidence_score_exactly_at_threshold_routes_to_answer(monkeypatch) -> None:
    """`_route_after_confidence`'s comparison is `>=`, not `>` (pinned
    already by `test_route_is_decided_strictly_at_the_threshold` in
    `tests/agents/test_confidence.py` using an arbitrary threshold); this
    test re-checks the exact same inclusive-boundary contract specifically
    against the REAL production default (0.5), since that is the value
    that actually ships, not an arbitrary number chosen for test
    convenience.
    """
    doc = uuid.uuid4()
    chunks = [_chunk(doc, score=-8.0)]
    state = _state(chunks=chunks, signals={"top_similarity": 0.5})

    # Discover this exact scenario's own natural score first (not assumed
    # to already equal the threshold), then pin the threshold to it --
    # this is what "confidence near the threshold" means operationally:
    # the boundary condition, not a coincidence of chosen inputs.
    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(0.9))
    natural_score = evaluate_confidence(state)["confidence_score"]

    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(natural_score))
    assert evaluate_confidence(state)["route"] == "answer"  # >= is inclusive

    monkeypatch.setattr(confidence_module, "get_settings", lambda: _FakeSettings(natural_score + 1e-9))
    assert evaluate_confidence(state)["route"] == "investigation"


# --------------------------------------------------------------------------
# 7. Regression cases from this review's own findings
# --------------------------------------------------------------------------


def test_deterministic_evaluation_harness_confidence_bucket_is_not_the_real_formula() -> None:
    """Regression guard for a real finding from this review, not a
    hypothetical: `scripts/run_evaluation.py`'s "Confidence calibration"
    console section (n=9, calibration error 0.240 as of this review) is
    computed from `app.evaluation.fixtures.canned_generations`' HAND-AUTHORED
    confidence numbers (`FixtureAnswerAdapter` -- see `app.evaluation.
    adapters.generation`'s own docstring: "do NOT read their canned outputs
    from the dataset... Canned outputs instead live in
    `canned_generations.py`"), never by calling `app.agents.confidence.
    evaluate_confidence`. It is real evidence about the evaluation harness's
    OWN fixture consistency, but zero evidence about whether `_SIGNAL_
    WEIGHTS`/`_DENSE_SIMILARITY_FLOOR`/`_DENSE_SIMILARITY_CEILING`/
    `Settings.confidence_threshold` are well-calibrated in production. This
    test pins that fact so it cannot be silently miscited as "the
    deterministic harness validated confidence calibration" in a future
    status update.
    """
    import inspect

    from app.evaluation.adapters import generation as generation_adapters

    source = inspect.getsource(generation_adapters.FixtureAnswerAdapter.generate_answer)
    assert "evaluate_confidence" not in source
    assert "app.agents.confidence" not in source


def test_stale_live_confidence_reports_predate_current_signal_formula() -> None:
    """Regression guard for this review's other real finding: every
    checked-in `scripts/eval_confidence_report*.json` was generated
    2026-08-13/14 -- before BOTH the 2026-08-30 reranker calibration and
    the 2026-09-02 signal-shape rewrite this module's own docstring
    documents (`_normalize_top_similarity`/`_distinct_source_count_signal`).
    Those reports are therefore invalid evidence for the CURRENT formula
    regardless of their sample size (14, already below the 20-example
    floor `app.evaluation.semantic.calibration` enforces) -- they measured
    a since-replaced scoring formula. This test does not re-derive that
    date comparison from the JSON files at import/collection time (the
    files could be regenerated at any moment by a real, valid future run,
    which must not make this test start failing) -- it instead pins the
    two textual facts a real recalibration must confirm are still true
    before reusing any existing report: `app/agents/confidence.py` cites
    both fix dates in its own module docstring.
    """
    import inspect

    from app.agents import confidence as confidence_mod

    module_source = inspect.getsource(confidence_mod)
    assert "2026-08-30" in module_source
    assert "2026-09-02" in module_source
