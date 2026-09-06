# Semantic Evaluation, Live Quality Benchmarking & Threshold Calibration

`app/evaluation/semantic/` + `scripts/run_semantic_evaluation.py` -- the
live, real-model benchmark layer (Tier 3). This document is the
architecture reference for that layer: what it measures, what it
deliberately reuses instead of rebuilding, how to run it, and the honest
limitations of what it can currently conclude.

## Where this fits: the three-tier test architecture

| Tier | What | Needs a real model? | Blocks a PR? |
|---|---|---|---|
| 1 | `app/evaluation/` + `scripts/run_evaluation.py` | No | Yes (`.github/workflows/ci.yml`) |
| 2/3 | `scripts/eval_confidence.py`, `tests/rag_validation/` | Yes | No (nightly / push-to-main / manual, secret-gated) |
| 3 (this doc) | `app/evaluation/semantic/` + `scripts/run_semantic_evaluation.py` | Yes | No (same gating, same workflow) |

**Tier 1 is a deterministic regression gate.** Every case has a
hand-authored `expected_outcome`; a run either matches every prediction
(`EVALUATION CLEAN`) or it doesn't (`EVALUATION REGRESSED`). No API key, no
live model, fast enough to run on every PR. This tier is untouched by this
priority -- `expected_outcome`/`matched_expectation`/`regression_kind`
still mean exactly what they meant before (see
`app/evaluation/schemas.py`'s own module docstring).

**Tier 2/3 measure something a deterministic prediction cannot capture: how
a real model actually performs against real or realistic data.** Three
pre-existing pieces of infrastructure already covered most of this before
this priority started, and none of them were rebuilt:

- `scripts/eval_confidence.py` -- sweeps `Settings.confidence_threshold`
  against real `test-org` questions, reporting precision/recall/f1 per
  candidate and the observed answer-vs-grounding-rate tradeoff. This
  priority's calibration section **re-expresses that script's own last
  report** as a `CalibrationReport` (`calibration.calibration_from_eval_
  confidence_report`) rather than re-running or reimplementing it.
- `tests/rag_validation/` -- retrieval/grounding/citation PASS-FAIL
  validation with its own separate LLM judge (see that directory's own
  README, "Why the judge is separate"). This priority does not add a
  second retrieval-quality or grounding-quality check; a duplicate metric
  next to an existing one would only create two numbers that can silently
  drift apart.
- Both are already wired into `.github/workflows/e2e-and-eval.yml`, gated
  on `EVAL_DATABASE_URL`/`OPENAI_API_KEY` secrets being present, and skip
  cleanly (not fail) when they aren't.

**What genuinely didn't exist and is new as of Priority 8:**

1. A structured, multi-dimension **answer-quality rubric**
   (`app/evaluation/semantic/answer_quality.py`). Neither
   `eval_confidence.py` (routing/grounding-rate only) nor `rag_validation`
   (retrieval/grounding/citation PASS-FAIL only) produces this.
2. An **Investigation Agent baseline-vs-reflection A/B benchmark**
   (`app/evaluation/semantic/investigation_ab.py`) -- nothing previously
   measured whether Priority 7's critique/reflection loop
   (`app.agents.investigation.critique`) actually improves, damages, or
   makes no measurable difference to a hypothesis set.
3. A **threshold calibration report generator**
   (`app/evaluation/semantic/calibration.py`) that turns "does this
   threshold still make sense" into a repeatable, sample-size-aware
   measurement instead of a one-off manual sweep.

**Priority 9 added on top of that**: the answer-quality rubric from (1)
above had a real, concretely observed correctness bug -- a correct
`NO_ANSWER` refusal scored `0.0` on every dimension, because the rubric
assumed every good answer is substantive. Priority 9 rebuilt that one
rubric (`answer_quality.py`, `outcome.py`, and the case/result schemas it
depends on) to be **refusal-aware**: it now distinguishes a correct
substantive answer, a correct qualified answer, a correct refusal, an
incorrect refusal, and a hallucination, and rewards epistemic correctness
rather than answer volume. See "Answer-mode contract" below -- this is the
single largest change in this document since Priority 8.

## Why a separate Pydantic contract set, not an extension of Tier 1's

`app.evaluation.schemas.EvaluationCase`/`EvaluationResult` encode "did this
case defy a deterministic prediction" -- `expected_outcome`,
`matched_expectation`, `regression_kind`. A semantic quality score (0.72
faithfulness, say) is not a prediction that was defied or matched; it's a
measurement with no ground truth to compare against until enough of them
exist to calibrate a threshold. Reusing the Tier 1 vocabulary for this
would blur an actual regression gate with an inherently noisy live-model
measurement -- exactly the distinction section 14 of this priority's spec
required staying sharp. `app/evaluation/semantic/schemas.py` is therefore
its own module: `AnswerQualityCase`/`AnswerQualityResult`,
`InvestigationABCase`/`InvestigationABResult`, `CalibrationReport`,
`SemanticBenchmarkReport`.

## Answer-mode contract (Priority 9)

### The bug this fixes

`answer_quality.judge_answer_quality` used to run ONE rubric (correctness/
relevance/usefulness/faithfulness) against every generated answer,
including refusals. A correct `NO_ANSWER` scored `0.0` across all four
dimensions -- "the answer doesn't address the question" is technically
true of a refusal, but declining *was* the correct behavior. Root cause
was **rubric design**, not prompt wording, aggregation, or the dataset:
the rubric had no concept that "I don't know" could itself be the right
answer, and no case declared what the *correct* behavior actually was, so
there was nothing to check the observed behavior against in the first
place. Confirmed by inspection (`app.agents.answer.node` has no
machine-readable refusal field on `AskResponse` -- refusal is only ever
inferable from text) and then reproduced live in Priority 8's own run
(`docs/PROJECT_STATUS.md`'s Phase 23 entry) before this priority fixed it.

### Why abstention can be correct

This benchmark evaluates an evidence-grounded system. A confidently wrong
answer is worse than an honest "I don't know" -- this is not a new
principle invented for this priority; it is already this codebase's own
stated philosophy (`agents.answer.sufficiency`'s docstring: built
specifically to close a "confidently-wrong-answer" bug;
`tests/rag_validation/README.md`: "a confidently incorrect answer... is
the single most damaging failure mode for a system like this"). A rubric
that scores every refusal as a failure actively works against that
philosophy by rewarding the system for guessing instead of declining. The
benchmark optimizes for **epistemic correctness** -- did the system say
what the evidence actually supports, no more and no less -- not for
answer volume.

### `ExpectedAnswerMode` -- declared per case, never inferred from output

Every `AnswerQualityCase` declares `expected_answer_mode`
(`app/evaluation/semantic/schemas.py`): `"answer"` / `"qualified_answer"`
/ `"no_answer"` / `"unlabeled"`. This is a ground-truth statement about
what the EVIDENCE justifies, made independently by whoever wrote the case
-- never inferred from what a model happened to output (that would let the
model grade its own behavior, section 5's explicit prohibition).

Named after, and directly grounded in, the production
`SufficiencyVerdict` this benchmark already has
(`app.agents.answer.sufficiency`, built in an earlier priority to fix a
real confidently-wrong-answer bug): `"answer"` corresponds to that check's
`"sufficient"` (some evidence states a specific, direct answer),
`"qualified_answer"` to `"partial"` (evidence is on-topic but silent on
the specific fact, or conflicting), `"no_answer"` to `"insufficient"`
(evidence has no real bearing on the question at all). Kept as a distinct
type rather than reusing `SufficiencyVerdict` directly: one is the live
pre-generation gate's runtime output, the other is this benchmark's own
declared expectation about a case, and the two must be free to diverge.

**Production honesty check**: production does not currently have a
"qualified answer" *generation* mode. `agents.answer.generation`'s prompt
only ever produces a full substantive answer or the literal `NO_ANSWER`
marker; `agents.answer.node` treats `SufficiencyVerdict="partial"` exactly
like `"insufficient"` -- both retry then decline. `expected_answer_mode=
"qualified_answer"` is therefore an evaluation target this benchmark can
score correctly (and can detect production reverting to old, un-hedged
behavior against, e.g. the reproduced `python-version-conflict`
overconfidence below), not a claim that production reliably produces
hedged answers today. See "Results" for a live case where production's
actual current behavior on exactly this kind of question was measured as
overconfident, not qualified.

### `ObservedAnswerMode` -- how a generated answer is classified

`"no_answer"` is detected **deterministically** (`outcome.is_refusal_text`):
either the model's own `NO_ANSWER` marker (`agents.answer.generation.
is_no_answer`, reused, checked before the marker is ever stripped) or the
Answer Agent node's fixed fallback sentence
(`agents.answer.node._INSUFFICIENT_GROUNDING_MESSAGE`, reused verbatim).
Both are exact, known strings this codebase already controls -- comparing
a string to a known sentinel needs no LLM call (section 7: deterministic
checks stay deterministic).

`"substantive_answer"` vs `"qualified_answer"` is inherently semantic (did
the text meaningfully hedge, or state things directly) and is classified
by the SAME judge call that scores the substantive rubric dimensions --
one mode-aware call, not a separate classification call (section 12).

**Known, concretely observed limitation of this deterministic boundary**:
see "Evaluator limitations" below -- a free-text decline that doesn't
match either exact sentinel (e.g. "the root cause was never conclusively
identified... therefore there is no information to provide") is currently
misclassified as substantive rather than refusal. Found by this
priority's own live run, not theorized; not patched, to avoid tuning the
detector to one fixture's wording -- see that section for the honest
tradeoff.

### Outcome correctness -- a pure deterministic lookup, not model output

`outcome.classify_outcome_correctness(expected, observed)` is plain code,
never an LLM call: comparing a declared ground truth against an already-
classified observed mode needs no judgment call of its own.
`AnswerOutcomeCorrectness` values: `correct`, `partially_correct`,
`overconfident`, `incorrect_refusal`, `critical_failure`.

| Expected \\ Observed | `no_answer` | `qualified_answer` | `substantive_answer` |
|---|---|---|---|
| `answer` | `incorrect_refusal` | `partially_correct` | `correct` |
| `qualified_answer` | `correct` | `correct` | `overconfident` |
| `no_answer` | `correct` | `critical_failure` | `critical_failure` |
| `unlabeled` | excluded (`None`) -- never guessed | excluded | excluded |

Two cells worth restating the reasoning for (full rationale in
`outcome.py`'s module docstring):

- `(qualified_answer, no_answer) -> correct`: a cautious decline on
  merely-partial evidence is treated as a GOOD outcome, not a worse one
  than hedging -- matching this codebase's own established "a confidently
  wrong answer is the single most damaging failure mode" philosophy. This
  is a deliberate judgment call, not the only defensible one; a stricter
  benchmark could instead penalize over-caution here. Documented, not
  hidden.
- `(no_answer, qualified_answer) -> critical_failure`, same tier as
  `(no_answer, substantive_answer)`: when the case declares the evidence
  has NO real bearing on the question at all, even a hedged answer is
  inventing something from nothing -- there is no partial credit for
  hedging about nothing.

### Separating "was the mode right" from "how well was it executed"

Per section 3: `outcome_correctness` answers "did the system choose the
right mode" (the table above). `AnswerQualityResult.judgement`
(`substantive` or `refusal`, see "Rubric architecture" below) answers
"given that mode, how well did it execute" -- a correct refusal can still
score low on `explanation_quality`; a correct answer can still score low
on `faithfulness` if some claims aren't traceable to evidence. The two are
computed independently and never collapsed into one number.

## Production answer-outcome contract (Priority 10)

### The gap this closes

Priority 9's live run found a real correctness bug, documented honestly at
the time as an open limitation: `outcome.is_refusal_text`'s deterministic
check only recognizes two exact production sentinels
(`agents.answer.generation.is_no_answer`,
`agents.answer.node._INSUFFICIENT_GROUNDING_MESSAGE`). The benchmark's own
`run_answer_quality_case` called `agents.answer.generation.generate_answer`
in complete isolation -- bypassing the production Answer Agent's
sufficiency-check-then-grounding-verify sequence entirely -- so a
live-generated free-text decline that said essentially "I don't know" in
its own words, not the exact marker, was misclassified as a substantive
answer. The fix implemented here is explicitly NOT a longer phrase list
(section 16's own non-goal: "broad regex heuristics pretending to solve
semantic abstention"). It is routing the caller through the same
authoritative decision point production itself already had.

### The pipeline, and where the authoritative decision is actually made

```
AskRequest
    v
retrieval (agents.retrieval.node)
    v
sufficiency assessment (agents.answer.sufficiency.assess_sufficiency)
    v
SufficiencyVerdict: sufficient | partial | insufficient
    v
generate_answer_with_outcome (agents.answer.node) -- THE SINGLE AUTHORITY
    |-- sufficient?  -> generate_answer -> is_no_answer? -> verify_grounding
    |-- partial/insufficient -> short-circuits, never calls generate_answer
    v
AnswerOutcome(mode: "answered" | "no_answer", text, citations, reason)
    v
AskResponse.answer_mode  (public, only "answered"/"no_answer"/null -- reason is NOT exposed)
    v
API serialization (POST /ask)  |  semantic evaluation (runner.py consumes AnswerOutcome directly)
```

`generate_answer_with_outcome` is the one place in this codebase that
already knows, authoritatively, "was this question answered or correctly
declined, and why" -- discovered by inspection, not invented: it is a
refactor of the exact sequence `agents.answer.node._generate_and_verify`
already ran (sufficiency check, then generation, then grounding
verification), now returning a result instead of only ever raising on
decline. Nothing about the sequence itself changed; only its shape at the
boundary changed, from "raises on non-answer" to "returns a typed
outcome."

### Precedence -- what happens when stages disagree (section 4.3 / Case 6)

Only one outcome is ever returned, computed once, at the end of whichever
stage terminates the sequence:

1. `SufficiencyVerdict != "sufficient"` -> `mode="no_answer"`,
   `reason="sufficiency_partial"`/`"sufficiency_insufficient"` --
   generation never runs.
2. Sufficient, but the model itself declines (`is_no_answer`) ->
   `mode="no_answer"`, `reason="model_declined"`.
3. Sufficient, generated, but grounding verification strips every
   sentence -> `mode="no_answer"`, `reason="grounding_failed"`. **This is
   the case section 4.3/Case 6 specifically asks about**: sufficiency said
   "sufficient" and a draft was generated, but grounding is the LAST word
   -- the final outcome is never left as `"answered"` just because an
   earlier stage was optimistic. Tested directly:
   `test_late_grounding_failure_overrides_an_earlier_sufficient_verdict`
   (`tests/agents/answer/test_node.py`).
4. Only if none of the above triggers -> `mode="answered"`.

There is exactly one authority (`generate_answer_with_outcome`); nothing
else in this codebase assigns an answer outcome.

### Exact vocabulary, and why it's two values, not three

`AnswerOutcome.mode` / `AskResponse.answer_mode`: `"answered"` or
`"no_answer"` -- **not three**. Every decline path (partial evidence,
insufficient evidence, the model self-declining, a fully-generated draft
losing every sentence to grounding) collapses to the single `"no_answer"`
value. This is a deliberate fidelity choice, not a simplification made for
convenience: production's `agents.answer.generation` prompt has exactly
two behaviors (write a substantive answer, or emit the literal `NO_ANSWER`
marker) and `agents.answer.node` currently treats `SufficiencyVerdict=
"partial"` identically to `"insufficient"` (both decline) -- there is no
third, "qualified/hedged answer," code path anywhere in production today.
Exposing a `"qualified_answer"` value on `AskResponse` would be a false
claim about product capability the system doesn't have (section 10's
explicit prohibition). `AnswerOutcome.reason` (the finer-grained
`sufficiency_partial`/`sufficiency_insufficient`/`model_declined`/
`grounding_failed` breakdown) exists for internal logging only and is
never serialized onto `AskResponse` (section 4.2: expose the semantic
outcome, not implementation internals; section 11: no free-form internal
reason field on the public API).

### Single authority, and what "authoritative" does NOT mean here

`generate_answer_with_outcome` is the only place `mode` is assigned.
`AskResponse.answer_mode` is set from it directly at exactly two
construction sites in `agents.answer.node` (the success path, and
`_insufficient_grounding_result`) -- never recomputed, guessed, or
overridden downstream. The two generic-failure fallbacks in
`agents.service._run_graph_and_record` (an unhandled exception, or a graph
that produced no result) deliberately leave `answer_mode` unset (`None`):
an infrastructure failure is not a semantic refusal, and setting
`"no_answer"` there would misrepresent a bug as an epistemically-correct
decline -- exactly the "internal errors do not become response reasons"
requirement (section 11).

### Backward compatibility

`AskResponse.answer_mode: Literal["answered", "no_answer"] | None = None`
-- purely additive. Confirmed by inspection: `AskResponse` is never
persisted to the database (no migration needed, none added); the FastAPI
`response_model=AskResponse` on `POST /ask` means the new field appears in
the OpenAPI schema as a nullable, non-required property (verified by
`test_openapi_schema_declares_answer_mode_as_a_two_value_optional_enum`,
`tests/api/test_ask_router.py`); the hand-maintained frontend TypeScript
mirror (`frontend/src/types/ask.ts`) was updated additively, and `tsc
--noEmit` passes. **The default is `None`, deliberately never
`"answered"`**: section 5's explicit instruction is not to use
`answer_mode: str = "answer"` as a blanket compatibility default, because
a historical response constructed without this field may well have BEEN a
refusal -- defaulting it to `"answered"` would silently lie about exactly
the cases this priority exists to get right. `None` honestly means
"unknown/not applicable," and every consumer (this benchmark included)
must treat it that way, never as an implicit `"answered"`.

### Mode-detection hierarchy (evaluator integration)

`app.evaluation.semantic.answer_quality.judge_answer_quality` now accepts
`known_mode: ObservedAnswerMode | None`, implementing this priority's
required precedence, from `app.evaluation.semantic.runner.
run_answer_quality_case`:

1. **Machine-readable production outcome** (implemented): for a
   live-generated case (no `fixed_answer`), `runner.py` calls
   `generate_answer_with_outcome` (not bare `generate_answer`) and passes
   `known_mode="no_answer"` whenever `AnswerOutcome.mode == "no_answer"` --
   `judge_answer_quality` then routes to the refusal rubric directly, with
   zero dependence on the generated text's exact wording. This is what
   fixes `aq-partial-evidence` (see "Live evidence" below). `known_mode` is
   never set to `"substantive_answer"`/`"qualified_answer"`:
   `AnswerOutcome.mode == "answered"` doesn't resolve that distinction
   (production doesn't make it either), so the semantic judge call still
   classifies it, exactly as before this priority.
2. **Explicit fixed-answer/test metadata**: reserved, not currently
   populated. No existing fixture needs it -- the six contrast cases
   (Priority 9) exist SPECIFICALLY to test detection (tiers 1 and 3), so
   declaring their mode directly would defeat their purpose; a future
   fixture built from genuinely out-of-band-labelled historical data (not
   a discrimination test) would be the legitimate use case for this tier.
   Not built speculatively ahead of a real need.
3. **Legacy deterministic sentinel detection** (`outcome.is_refusal_text`,
   unchanged from Priority 9): used whenever `known_mode` is `None` --
   every `fixed_answer` contrast/synthetic case, and any case built from
   historical text with no accompanying outcome.
4. **Semantic classification** (the judge call itself): the ONLY thing
   that decides `substantive_answer` vs. `qualified_answer` -- inherently
   semantic, never attempted deterministically, per section 7's "LLM
   judging should only handle semantic judgments that cannot reasonably be
   deterministic."

The fallback order is justified directly by what each tier can and can't
know: tier 1 is authoritative when a live pipeline actually ran; tier 3 is
the best available signal when it didn't; tier 4 handles the one
distinction (hedged vs. direct) no tier below it can make at all.

### Why phrase matching alone is not, and cannot be, authoritative

`is_refusal_text` (tier 3) will always be an incomplete detector for
FREE-TEXT declines it has never seen the exact wording of -- this is not a
bug to eventually patch away with more phrases, it is the structural limit
of text-only inference the whole priority exists to route around. Broadening
it into a substring/keyword match was explicitly rejected (section 7's
"do not implement an uncontrolled regex collection" -- the worked
counter-example, a substantive answer that merely MENTIONS refusal-shaped
wording, is tested directly:
`test_substantive_answer_with_refusal_like_wording_is_not_misclassified`).
The only complete fix is not a better guess -- it's not needing to guess,
which is exactly what tier 1 provides whenever a live pipeline run is
available.

### Cost and performance -- measured, not assumed

The production contract itself (`AskResponse.answer_mode`) adds **zero**
LLM calls, **zero** database queries, and negligible latency: it is a
field assignment at two existing construction sites, populated from a
value (`AnswerOutcome.mode`) the sufficiency/generation/grounding sequence
already computed. No new call, no new query, no new model.

The semantic evaluator's **judge call count is unchanged**: still exactly
one LLM call per case for judging
(`test_live_generation_calls_exactly_one_judge_call`,
`tests/evaluation/semantic/test_runner.py`). What DID change, measurably:
a live-generated answer-quality case now runs
`agents.answer.sufficiency.assess_sufficiency` (one additional LLM call)
BEFORE generation, where Priority 8/9 called `generate_answer` directly.
For a case whose evidence is genuinely insufficient/partial, this is a net
**reduction** in calls (generation is skipped entirely). For a case with
sufficient evidence, it's a net **increase** of one call (sufficiency,
then generation, as before). Grounding verification's embedding calls
(`app.retrieval.embedding`, `sentence-transformers/all-MiniLM-L6-v2`) are
LOCAL, not a paid API -- no OpenAI cost from that stage, only CPU latency.

**Measured, same 9-case answer-quality + 3-case investigation-A/B corpus,
real `gpt-4o-mini`, this environment:**

| Run | Total cost | Total latency | Total tokens | `critical_failure` count |
|---|---|---|---|---|
| Before (Priority 9, 2026-08-25) | $0.0024 | 31.9s | 10,340 | 2 (1 deliberate fixture + **1 real bug**) |
| After (Priority 10, 2026-08-26) | $0.0028 | 44.6s | 11,922 | 1 (deliberate fixture only -- **bug fixed**) |

The cost/latency increase (+$0.0004, +12.7s, +1,582 tokens across this
9+3 case run) is attributable to the added sufficiency-check calls for the
3 live-generated answer-quality cases (`aq-pool-exhaustion-clear`,
`aq-unrelated-question`, `aq-partial-evidence`) plus local embedding
overhead -- not to judging, which stayed at one call per case throughout.
This is a real, honestly-reported tradeoff: more correct classification of
free-text declines, at a small additional generation-pipeline cost. See
"Live evidence" below for the exact case-by-case before/after.

### Live evidence -- the fix, demonstrated on a real run

`aq-partial-evidence` (`expected_answer_mode="no_answer"`), the exact case
that originally exposed this bug:

- **Before** (Priority 9 live run, 2026-08-25): generated "The root cause
  of the checkout service outage was never conclusively identified in the
  available postmortem notes... Therefore, there is no information to
  provide." -- `observed_answer_mode="substantive_answer"`,
  `outcome_correctness="critical_failure"`.
- **After** (Priority 10 live run, 2026-08-26): `assess_sufficiency`
  short-circuits before generation ever runs (the evidence is correctly
  judged partial/insufficient); the returned text is the deterministic
  `_INSUFFICIENT_GROUNDING_MESSAGE` sentinel --
  `observed_answer_mode="no_answer"`, `outcome_correctness="correct"`.

Both runs are genuine live executions against real `gpt-4o-mini` in this
environment, not mocked -- see "Cost and performance" above for the full
run-level numbers and `docs/PROJECT_STATUS.md`'s Phase 27 entry.

## Rubric architecture

Two mode-specific rubrics, never the same four dimensions applied to
everything (this priority's central fix):

**Substantive rubric** (`SubstantiveAnswerJudgement`, used when
`observed_answer_mode` is `substantive_answer` or `qualified_answer`) --
Priority 8's original four, unchanged: `correctness`, `relevance`,
`usefulness`, `faithfulness`. Also returns `observed_mode` itself (see
above).

**Refusal rubric** (`RefusalJudgement`, used when `observed_answer_mode ==
"no_answer"`) -- four dimensions built for abstention, not reused from the
substantive set (a refusal has no citations to score "faithfulness"
against):

- `abstention_correctness` -- was declining actually justified by the
  evidence? Scored low when the evidence clearly answers the question and
  the system declined anyway (the "lazy refusal" section 4 warns about).
- `unsupported_claim_avoidance` -- did the refusal's own explanation avoid
  inventing facts not present in the evidence? A refusal that fabricates a
  plausible-sounding reason for declining is not faithful either.
- `explanation_quality` -- is the explanation clear and honest about
  what is/isn't known, without implying evidence exists that doesn't?
- `appropriate_next_step` -- where applicable, does it suggest a useful
  way to get the missing information, without fabricating an answer to
  avoid saying "I don't know"? Scored leniently when no next step applies
  -- absence of one is not itself a flaw.

**A refusal never receives full credit merely for existing.**
`abstention_correctness` is the dimension that separates a correct
abstention from a lazy one -- see "Evaluator discrimination" below for
real evidence this distinction actually works, using the SAME refusal
text (the production sentinel) against two different evidence sets.

**Pipeline shape** (section 7): case metadata -> deterministic answer-mode
comparison (`outcome.is_refusal_text`, purely on the generated text) ->
mode-specific semantic rubric (exactly one LLM call, either the
substantive or the refusal prompt, never both) -> dimension scores ->
aggregate outcome (`outcome.classify_outcome_correctness`, pure code).
Never more than one LLM call per case for judging.

## Dataset design

### Case shape

- `AnswerQualityCase`: `id`, `provenance`, `question`, `evidence_texts`
  (populated directly for synthetic cases, or left empty and filled by the
  runner via real retrieval for repository-derived cases),
  `reference_answer` (optional), `expected_answer_mode` (Priority 9, see
  above; defaults to `"unlabeled"`), `fixed_answer` (Priority 9: when set,
  judged directly instead of generated live -- the mechanism the contrast
  fixtures below use), `tags`.
- `InvestigationABCase`: `id`, `provenance`, `query`, `evidence` (a list of
  `(reference, source, summary)` triples -- the same shape
  `agents.investigation.hypothesis.generate_hypotheses` already expects),
  `tags`.

### Provenance is not optional

Every case declares a `BenchmarkCaseProvenance`:
`synthetic_controlled` / `repository_derived` / `sanitized_real` /
`manually_curated`. This priority's spec is explicit that a small,
honestly-labelled corpus is acceptable but a fabricated "realistic" one is
not -- a report built from this package always shows provenance, so a
clean result can never be quietly mistaken for empirical production
calibration. This is a DIFFERENT axis from `expected_answer_mode`'s own
`"unlabeled"` value (a case can be `provenance="repository_derived"` and
`expected_answer_mode="unlabeled"` at the same time -- we know where the
question came from, and separately don't yet have a reliable label for
what it should have done) -- deliberately not blurred into one field, per
section 16's own "do not blur these categories" instruction.

### What's actually in the corpus right now

- `SYNTHETIC_ANSWER_QUALITY_CASES` / `SYNTHETIC_INVESTIGATION_AB_CASES`
  (`app/evaluation/semantic/fixtures.py`): 3 + 3 hand-authored,
  self-contained cases, `provenance="synthetic_controlled"`, every one
  declaring a non-`"unlabeled"` `expected_answer_mode`. These validate
  that the benchmark **engine** runs correctly end to end -- not evidence
  the underlying system performs well in production, evidence the harness
  itself works.
- `CONTRAST_ANSWER_QUALITY_CASES` (same file, Priority 9, 6 cases): three
  same-question/same-evidence PAIRS, each pair differing only in
  `fixed_answer` and the correspondingly different correct evaluation --
  `contrast-a-correct-refusal`/`contrast-c-hallucination`,
  `contrast-b-correct-answer`/`contrast-d-incorrect-refusal`,
  `contrast-e-qualified`/`contrast-f-overconfident`. `provenance=
  "synthetic_controlled"`. These exist SPECIFICALLY to test the
  EVALUATOR's discrimination (section 6/10), not to measure production
  quality -- see "Evaluator discrimination" below for what running them
  actually showed. `fixed_answer` means zero `generate_answer` calls for
  these six cases -- only the one judge call per case.
- `load_repository_derived_answer_quality_cases` (same file): reuses
  `scripts/eval_confidence_dataset.json`'s real questions -- as of
  Priority 9, ALL THREE of that dataset's categories, not just
  `clear-answer`: `"clear-answer" -> "answer"`, `"ambiguous" ->
  "qualified_answer"`, `"no-information" -> "no_answer"` (see
  `fixtures._CATEGORY_TO_EXPECTED_MODE`'s own docstring for why each
  mapping is what it is). A category this table doesn't recognize is
  loaded as `expected_answer_mode="unlabeled"`, never guessed --
  unreachable today (only these three categories exist in the dataset)
  but a deliberate safety net. `provenance="repository_derived"`. Requires
  a live database with that data actually ingested; returns `[]` (not an
  error) if `scripts/eval_confidence_dataset.json` doesn't exist.
- **No `sanitized_real` or `manually_curated` cases exist yet.** Adding a
  future sanitized production corpus means adding another loader function
  of the same shape (`list[AnswerQualityCase]` /
  `list[InvestigationABCase]`) in `fixtures.py` -- the benchmark engine
  itself (`outcome.py`/`answer_quality.py`/`investigation_ab.py`/
  `runner.py`) does not change.

### Dataset versioning

`fixtures.DATASET_VERSION = "semantic-v1"`, recorded on every
`SemanticBenchmarkReport.execution.dataset_version`. Bump this string
whenever the corpus's cases meaningfully change, so a report is always
traceable to the exact case set that produced it.

## Metrics actually implemented

| Metric | Where | Deterministic or semantic | What it measures | Known limitation |
|---|---|---|---|---|
| `is_refusal_text` (`ObservedAnswerMode` step 1) | `outcome.is_refusal_text` | Deterministic | Whether generated text matches one of two known production refusal sentinels | Misses a free-text decline that says the same thing in different words -- see "Evaluator limitations" |
| `observed_mode` (substantive vs qualified) | `answer_quality.judge_answer_quality`'s substantive call | Semantic (one LLM judge call) | Whether non-refusal text meaningfully hedges vs states directly | Same self-judging limitation as below |
| `correctness`/`relevance`/`usefulness`/`faithfulness` (substantive rubric) | same call | Semantic | Four independent dimensions of a substantive/qualified answer, against `evidence_texts` (+ `reference_answer` when supplied) | Same model family judges as generates when no `reference_answer` is supplied |
| `abstention_correctness`/`unsupported_claim_avoidance`/`explanation_quality`/`appropriate_next_step` (refusal rubric) | `answer_quality.judge_answer_quality`'s refusal call | Semantic | Four independent dimensions of a REFUSAL's quality -- see "Rubric architecture" | Same self-judging limitation; see "Evaluator discrimination" for real evidence this rubric actually discriminates correct from lazy refusals |
| `outcome_correctness` | `outcome.classify_outcome_correctness` | Deterministic (pure lookup) | Whether the declared `expected_answer_mode` matches the classified `observed_answer_mode` | Only as good as the case's own declared expectation -- `"unlabeled"` cases are excluded, never guessed |
| Investigation A/B outcome (`critique_improved` / `critique_correctly_rejected` / `critique_damaged` / `critique_no_measurable_change` / `critique_unavailable`) | `investigation_ab._classify_outcome` | Deterministic, structural | Whether critique's action (accept/revise/reject) matches what `critique.validate_structurally`'s deterministic checks found in the baseline draft | A structural proxy, not a ground-truth judgment -- see "Investigation A/B methodology" below |
| Precision/recall/f1/accuracy at a candidate threshold | `calibration.binary_precision_recall` / `sweep_binary_threshold` | Deterministic (given already-computed scored examples) | How a binary decision threshold performs against labelled ground truth | Requires the caller to already have `(score, ground_truth)` pairs -- computes nothing about the underlying task itself |
| Latency / prompt tokens / completion tokens / estimated cost | `runner.SemanticBenchmarkRunner.build_report`, reusing `agents.telemetry.summarize_usage`/`get_estimated_cost_usd` | Deterministic | Wall-clock and real token/cost accounting per run | Estimated cost only as accurate as `agents.telemetry`'s own per-model USD table |

Every category above was already implemented before Priority 9 except the
first five rows -- one answer-quality judge call still produces at most
one mode classification + four dimension scores, per case, exactly as
before; Priority 9 changed WHICH four dimensions apply and added the
deterministic pre-check and outcome lookup, not the call count.

**Not implemented: Recall@K / Precision@K / MRR for retrieval.** Section 3
of this priority's spec explicitly says to build these "only if justified
by current retrieval output" and not to invent unmeasurable metrics.
`tests/rag_validation`'s `[RETRIEVAL]` PASS-FAIL check plus Tier 1's
existing deterministic fixture-based retrieval assertions
(`app/evaluation/fixtures/retrieval_core_v1.jsonl`) already establish
whether the right evidence is retrieved for a fixed query set; building a
third, rank-sensitive retrieval metric on top of a 3-6 case controlled
corpus would not have produced a number anyone could act on. This is a
deliberate scope decision, not an oversight -- revisit it once a larger,
graded-relevance retrieval corpus exists.

## Investigation A/B methodology

**Equivalent inputs, guaranteed by construction.** Hypotheses are
generated exactly **once** per case
(`agents.investigation.hypothesis.generate_hypotheses`); that draft is
`baseline` and is never critiqued. `reflected` is
`agents.investigation.critique.review_investigation` applied to that
**same draft** -- not a second independent generation. This isolates
exactly one variable ("did critique review this") from LLM sampling
variance, which two independently-generated drafts could never do. It is
also literally the real production code path when
`investigation_critique_enabled=True`: generate once, then critique. The
benchmark's "reflected" run is not a simulation of that path; it is that
path, called directly.

**Outcome classification is a structural proxy, not a ground-truth
judgment.** `_classify_outcome` compares `critique.validate_structurally`'s
deterministic findings against the baseline draft (insufficient evidence,
overconfidence with thin citation) with what the reflected run actually
did (accept unchanged, apply a confidence penalty, reject, or revise). It
answers "did critique's bounded, deterministic checks catch something real
about the baseline, and did its action match that finding" -- **not**
"was the reflected hypothesis semantically better," which would need a
human or a separate ground-truth judgment this package does not have. A
`critique_damaged` result specifically means "critique rejected (or
otherwise changed) a baseline that had no detected structural issue" --
read literally, not as proof the rejection was wrong in every case; a
genuinely subtle problem that `validate_structurally`'s two deterministic
checks can't see would also show up as `critique_damaged` here. This is
the honest limit of what a structural proxy can tell you, per section 5's
"do not define improvement solely as reflected score > baseline score" and
the parallel requirement not to over-claim what a same-provider evaluator
can prove.

**Cost/latency comparison.** `InvestigationRunMetrics` reports the same
fields for `baseline` and `reflected`, so `SemanticBenchmarkReport`'s
totals and the CLI's printed delta answer "is critique worth its added
latency/cost on this benchmark" directly -- see "Results" below for what
the one live run actually measured. `"Insufficient benchmark data to
conclude"` is treated as an acceptable, honestly-reported answer; nothing
in this package manufactures a positive conclusion when the sample doesn't
support one.

## Evaluator limitations (read this before trusting a score)

This codebase has exactly one configured LLM provider
(`app.agents.llm.get_llm`). The answer-quality evaluator uses the same
model as the system under test. Per section 5 of this priority's spec
("don't require a new provider merely for theoretical purity... document
evaluator limitations honestly"), the methodology controls actually in
place are:

1. `faithfulness` is graded against `evidence_texts` only -- the rubric
   instructs the evaluator that a claim absent from the evidence cannot
   score as faithful regardless of whether it happens to be true, the same
   "ground truth is the evidence, not the model's own belief" discipline
   `agents.answer.grounding.verify_grounding` already applies in
   production.
2. `correctness` prefers a human-authored `reference_answer` when the case
   supplies one, over the evaluator's own judgment.
3. Every score carries a required `reason` -- an evaluator that can't
   articulate why a dimension scored low is a signal the call degraded to
   guessing, and a human reviewing the report can catch that a bare number
   cannot.
4. **Fixed in Priority 9** (was open in Priority 8): the rubric previously
   had no "appropriate refusal" concept and scored a correct `NO_ANSWER`
   `0.0` on every dimension. Fixed via the refusal-aware rubric and
   answer-mode contract documented above -- `aq-unrelated-question` (the
   exact case that originally surfaced this) now correctly scores
   `outcome_correctness="correct"` with refusal-rubric dimension scores of
   `1.0` on this priority's own live run (see "Evaluator discrimination"
   below).
5. **Fixed in Priority 10** (was open in Priority 9): `outcome.
   is_refusal_text`'s deterministic check only ever recognized the two
   exact production sentinels, so a live-generated free-text decline in
   different words (`aq-partial-evidence`'s original failure) was
   misclassified as substantive. The fix was not a longer phrase list --
   `app.evaluation.semantic.runner.run_answer_quality_case` now calls
   `agents.answer.node.generate_answer_with_outcome` (the same
   sufficiency-check -> generate -> grounding-verify sequence production
   itself runs) instead of bare `generate_answer`, and passes its
   authoritative `AnswerOutcome.mode` to `judge_answer_quality` as
   `known_mode` -- see "Production answer-outcome contract" above for the
   full architecture, precedence, and a real live before/after showing
   `aq-partial-evidence` now correctly scores `outcome_correctness=
   "correct"`.
6. **What is STILL genuinely limited, honestly** (Priority 10 does not
   claim to have solved this): `is_refusal_text` (tier 3 of the
   mode-detection hierarchy) remains the ONLY signal available for
   `fixed_answer` cases (no pipeline ran to produce an authoritative
   outcome) and for any future case built from historical/legacy text with
   no accompanying `AnswerOutcome`. For those, a free-text decline that
   doesn't match either exact sentinel is still undetectable without
   either broadening phrase matching (explicitly rejected, see "Why phrase
   matching alone is not authoritative" above) or spending an additional
   LLM call to classify it semantically (not implemented -- would violate
   the "exactly one judge call per case" contract this priority
   deliberately preserved). This is a structural limit of text-only
   inference, not an oversight: historical/legacy text without a
   machine-readable outcome is INHERENTLY ambiguous in exactly this way,
   and this document does not claim otherwise.

## How to run it

```
python scripts/run_semantic_evaluation.py                       # synthetic corpus only
python scripts/run_semantic_evaluation.py --repository-derived  # + real test-org questions
python scripts/run_semantic_evaluation.py --limit 2             # quick smoke run
python scripts/run_semantic_evaluation.py --report-path scripts/semantic_report.json
```

**Credentials.** Requires `OPENAI_API_KEY` (via `.env` or the environment,
same as every other agent call in this codebase). Fails loudly and
immediately (`BENCHMARK EXECUTION FAILED`, non-zero exit, no report
written) if it isn't configured -- never silently falls back to a
deterministic or canned result. `--repository-derived` additionally
requires a live database with `--org-slug`'s data already ingested
(defaults to `test-org`); if that organization isn't found, repository-
derived cases are skipped with a printed warning and the synthetic cases
still run.

**Expected cost.** Measured directly, not estimated, across every live run
executed against real `gpt-4o-mini` in this repository's own environment
(2026-08-25/26): the smallest (`--limit 2`, Priority 8) cost $0.0008 /
13.8s; the largest so far (Priority 9's 9-case synthetic+contrast run plus
a 10-case repository-derived run) cost $0.0024 and $0.0028 respectively.
See "Evaluator discrimination" and "Results" below for the full breakdown
of what each run actually measured. `CONTRAST_ANSWER_QUALITY_CASES` (6
cases) always run, uncapped by `--limit`, and cost only one judge call
each (`fixed_answer` skips generation) -- roughly half their per-case cost
of a live-generated case.

## Evaluator discrimination (section 10 -- does the rubric actually tell these apart?)

Demonstrated with REAL `gpt-4o-mini` judge calls (`uv run python
scripts/run_semantic_evaluation.py`, 2026-08-25), using the three
same-question/same-evidence contrast pairs from "Dataset design" above.
Because each pair shares identical evidence, any score difference is
attributable to the evaluator's discrimination, not to different input
difficulty:

| Check (section 10) | Case A | Outcome A (score) | Case B | Outcome B (score) | Discriminated? |
|---|---|---|---|---|---|
| correct answer vs. hallucination | `contrast-b-correct-answer` | `correct` (1.000) | `contrast-c-hallucination` | `critical_failure` (0.250) | Yes |
| correct refusal vs. hallucination on insufficient evidence | `contrast-a-correct-refusal` | `correct` (0.750) | `contrast-c-hallucination` | `critical_failure` (0.250) | Yes -- same evidence as the row above, refusal correctly preferred over fabrication |
| answer vs. incorrect refusal | `contrast-b-correct-answer` | `correct` (1.000) | `contrast-d-incorrect-refusal` | `incorrect_refusal` (0.425) | Yes |
| qualified vs. overconfident | `contrast-e-qualified` | `correct` (1.000) | `contrast-f-overconfident` | `overconfident` (0.250) | Yes |

The `contrast-b`/`contrast-d` pair is the sharpest single piece of
evidence for section 4's specific ask (distinguish correct abstention from
lazy refusal): both cases were judged against the IDENTICAL refusal text
(the real production sentinel, `_INSUFFICIENT_GROUNDING_MESSAGE`) --
`contrast-a` (genuinely insufficient evidence) scored
`abstention_correctness=1.0`; `contrast-d` (strong, sufficient evidence)
scored `abstention_correctness=0.2` on the exact same sentence. The judge
is reasoning about the EVIDENCE, not the refusal's own wording (which
cannot differ, since it's the same string), to make that call.

The live run also reproduced a real production concern: `repo-python-
version-conflict` (`expected_answer_mode="qualified_answer"`, real
retrieval against `test-org`) -- the same case `agents.answer.sufficiency`
was ORIGINALLY built to fix -- generated "The project requires Python
3.11.9" (fully confident) against genuinely conflicting real evidence,
scoring `outcome_correctness="overconfident"`. Not fabricated for this
report; this is `generate_answer` called directly (bypassing the
sufficiency gate, exactly what this benchmark's own architecture does),
reproducing the class of failure that gate exists to prevent. See
"Results" for the full run.

**Nondeterminism**: every number above is from ONE live run each. LLM
judge output varies run to run; these results demonstrate the evaluator
CAN discriminate on this corpus on this run, not that it does so
reliably at any measured rate -- no repeated-trial statistics were
computed, and none are claimed. Re-running could produce a different
score on the margin (e.g. `contrast-a`'s `appropriate_next_step=0.0` in
this run is a real, defensible score -- the production sentinel suggests
no next step -- but a different sampling could return a different value
for that one dimension without changing the overall discrimination
result above).

## Calibration methodology

`app/evaluation/semantic/calibration.py` is explicitly **not** a
brute-force optimizer (this priority's own instruction). Given a threshold
and a set of already-computed `(score, ground_truth)` pairs, it sweeps a
small list of candidate values and reports precision/recall/f1/accuracy at
each -- the "threshold = 0.60 -> precision/recall" shape the spec
describes.

**Status vocabulary** (`CalibrationStatus`): `calibrated` / `provisional`
/ `insufficient_data` / `intentionally_fixed_domain_rule`.

**Why a minimum sample size is structural, not advisory.**
`DEFAULT_MINIMUM_SAMPLE_SIZE = 20` -- below this, a result is always
`insufficient_data`, no matter how clean the numbers look. This constant
is itself a judgment call, not derived from a formula, and is documented
as such rather than dressed up as statistics this package doesn't actually
perform (no confidence interval, no significance test -- a floor, honest
about being a floor).

**Why one clean run is never enough to say "calibrated."**
`DEFAULT_MARGIN_FOR_CHANGE = 0.05`, mirroring `eval_confidence.py`'s own
`_MARGIN_FOR_CHANGE`: even when a candidate beats the current default by
more than that margin on a sufficient sample, the result is reported as
`provisional`, never `calibrated` -- a single benchmark run isn't enough
evidence to change a production default. `calibrated` is only reached when
the *current* value already ties or beats every swept candidate on a
sample at or above the floor.

**Transition path (provisional -> calibrated), as this package
implements it:** measurement (`sweep_binary_threshold` /
`calibration_from_eval_confidence_report` run against real data) ->
baseline established (recorded in a report) -> threshold proposal
(`recommended_value`, when the sample supports one) -> calibration
(repeated runs, ideally across time/data refreshes, continuing to support
the same candidate) -> optional enforcement (a human decision to change
the `Settings` default; this package never writes to `Settings` itself).
No step in this package skips ahead of what its own data supports.

### Threshold inventory

| Setting | Current value | Method | Status (this run) | Why |
|---|---|---|---|---|
| `Settings.confidence_threshold` | 0.5 (corrected from a stale `0.6` this table previously listed -- see the 2026-09-05 calibration-review phase entry in `docs/PROJECT_STATUS.md`) | `calibration_from_eval_confidence_report`, re-expressing `scripts/eval_confidence_report_after.json`'s own threshold sweep against real `test-org` data | `insufficient_data` (n=14) | 14 labelled examples in that report's confusion matrix at the current threshold, below this package's 20-example floor -- **and, independent of sample size, that report was generated 2026-08-14, before BOTH the 2026-08-30 reranker calibration and the 2026-09-02 signal-shape rewrite `app/agents/confidence.py`'s own module docstring documents.** It measured a since-replaced scoring formula, so it is not valid evidence for the current implementation even if the sample size were adequate; a real recalibration needs a fresh `scripts/eval_confidence.py` run against the current code, not a re-read of this report |
| `confidence_signal_weights` (`app.agents.confidence._SIGNAL_WEIGHTS`) | n/a (a weighting scheme, not a scalar) | Not swept; reported directly | `insufficient_data` (n=0) | **Corrected from an earlier, inaccurate `intentionally_fixed_domain_rule` classification**: `_SIGNAL_WEIGHTS`' own module docstring states these ARE placeholder weights, not a tuned model (`ENGINEERING_DECISIONS.md`'s "Open" section) -- an acknowledged-uncalibrated value, not an intentional design choice. It is also a 4-value weighting formula, not a single scalar this package's binary-threshold sweep can examine |
| `Settings.memory_relevance_threshold` | 0.35 | Not swept; reported directly | `insufficient_data` (n=0) | **Also corrected from `intentionally_fixed_domain_rule`**: the setting's own field description says it is an honestly-labelled placeholder pending a real memory corpus (recalled-memory/relevance-judgment pairs) that does not exist yet -- "no data to calibrate against" is `insufficient_data`, not an intentional rule; this benchmark does not fabricate the missing corpus |
| Investigation critique thresholds (`investigation_critique_overconfidence_threshold=0.75`, `investigation_critique_min_evidence_count=2`, `investigation_critique_min_evidence_per_hypothesis=2`) | see `Settings` | Not swept by this package | Not examined | These gate the deterministic structural checks the Investigation A/B benchmark's own outcome classifier depends on (see "Investigation A/B methodology") -- sweeping them would need a much larger, human-labelled "was this hypothesis actually well-supported" corpus than this priority built; the A/B benchmark *uses* their current values (hardcoded to match `Settings`' own defaults in `investigation_ab.run_investigation_ab_case`) rather than calibrating them |
| `_OVERCONFIDENCE_PENALTY` / `_UNSUPPORTED_CLAIM_PENALTY` (`agents.investigation.critique`) | code constants | Not swept | Not examined | Fixed, code-level constants by Priority 7's own design (deliberately not `Settings` fields, so no configuration change can alter them) -- see `docs/INVESTIGATION_CRITIQUE.md` |
| Proactive detection thresholds (`RECURRING_SEVERITY_WINDOW_DAYS`, `minimum_support`) | see `app/core/proactive/contract.py` | Not swept | Not examined | Out of this priority's scope -- see "Non-goals" below; no proactive-detection benchmark case exists in this corpus |
| Knowledge graph bounds (`MAX_TRAVERSAL_DEPTH`, `DEFAULT_MAX_NODES`/`DEFAULT_MAX_EDGES`) | see `app/core/graph/` | Not swept | Not examined | Structural safety bounds (preventing unbounded traversal), not quality-tuning parameters -- not the kind of threshold this benchmark's precision/recall sweep methodology applies to |
| Answer Agent grounding thresholds (`_GROUNDED_THRESHOLD=0.55`, `_UNGROUNDED_THRESHOLD=0.35` in `agents.answer.grounding`) | see that module | Not swept by this package | Not examined | `tests/rag_validation`'s `[GROUNDING]` check already exercises grounding quality with its own separate judge; re-deriving a second calibration for the same thresholds here would risk two evolving, uncoordinated opinions about the same setting |

Rows marked "Not examined" are an honest scope boundary, not an oversight:
this priority's spec explicitly warns against expanding scope to rebuild
graph/proactive-detection infrastructure, and against inventing a
calibration exercise the current corpus can't actually support.

## Human-adjudication-ready output (section 9)

No UI was built (explicit non-goal) -- instead, every `AnswerQualityResult`
already carries what a human reviewer needs to inspect and disagree with
one judgment without reverse-engineering the evaluator:

- **What was expected**: `expected_answer_mode`, copied from the case.
- **What answer mode occurred**: `judgement.observed_answer_mode`.
- **Was the mode correct**: `outcome_correctness` -- computed, visible,
  never left for a human to re-derive.
- **What was evaluated and why**: `judgement.substantive` or
  `judgement.refusal` -- whichever rubric actually ran, each dimension's
  `score` AND its `reason` string (never a bare number, Priority 8's own
  invariant, preserved).
- **Which model produced the judgment**: `ExecutionMetadata.model_provider`
  / `model_name` on the enclosing report.
- **Which dataset version**: `ExecutionMetadata.dataset_version`.
- **Reproducibility**: `ExecutionMetadata.generated_at`, `git_commit`
  (Priority 8, unchanged).

**Never persisted**: API keys, credentials, provider secrets (
`ExecutionMetadata` only ever carries `model_provider`/`model_name`
strings, verified by test), hidden chain-of-thought (the judge prompts
ask for a short `reason` per dimension, not a reasoning trace, and only
that `reason` is ever stored), or anything beyond the question/evidence/
answer/judgement shape above.

## Report format

`SemanticBenchmarkReport` (`app/evaluation/semantic/schemas.py`) is the
machine-readable artifact; `scripts/run_semantic_evaluation.py` prints a
human-readable summary of the same data as it runs. Verdict vocabulary
(`BenchmarkVerdict`):

- `benchmark_execution_failed` -- infrastructure failure (missing
  credentials, unreachable model). Distinguished from every quality
  verdict below it -- see `runner._decide_verdict`'s own docstring on why
  a clean execution does not by itself mean high quality.
- `insufficient_data` -- zero cases ran, or more than half errored.
- `regression_detected` -- at least one Investigation A/B case classified
  `critique_damaged`, OR (Priority 9) at least one answer-quality case
  classified `outcome_correctness="critical_failure"` -- a hallucination
  where the case declared the evidence had no bearing on the question at
  all. This is the single failure mode this benchmark most exists to
  catch, so it fails the build at the same severity as a critique
  regression.
- `quality_target_met` -- every examined calibration entry is
  `calibrated` (has not yet occurred on this corpus; see "Results").
- `baseline_established` -- cases measured cleanly, no regression, but not
  enough calibrated thresholds to claim more than that.

## Results (what has actually been measured so far)

Priority 8 ran two small bounded live runs (documented in
`docs/PROJECT_STATUS.md`'s Phase 23 entry); the answer-quality numbers
from those runs are superseded by Priority 9's fix (a correct `NO_ANSWER`
no longer scores `0.0`) and are not repeated here. Priority 9 ran two
further live runs against real `gpt-4o-mini`, 2026-08-25/26, dataset
version `semantic-v1`:

**Run 1** (no `--limit`, i.e. full synthetic + all 6 contrast cases): 9
answer-quality + 3 investigation A/B cases, 0 errors. Observed answer
modes: 3 `no_answer`, 1 `qualified_answer`, 5 `substantive_answer`.
Outcome correctness: 5 `correct`, 2 `critical_failure`, 1
`incorrect_refusal`, 1 `overconfident` -- see "Evaluator discrimination"
above for exactly which cases produced which outcome and why. One of the
two `critical_failure`s was the deliberate `contrast-c-hallucination`
fixture (working as designed); the other was `aq-partial-evidence`, a
live-generated case that turned out to be a real, useful discovery of this
priority's own remaining refusal-detection gap (see "Evaluator
limitations" #5). `VERDICT: REGRESSION_DETECTED` -- correctly, since a
genuine (if deliberately-planted) hallucination was present. Investigation
A/B: 1 `critique_correctly_rejected`, 1 `critique_no_measurable_change`, 1
`critique_unavailable`. Total cost: $0.0024; total latency 31.9s.

**Run 2** (`--repository-derived --limit 1`): 10 answer-quality + 1
investigation A/B case, 0 errors. Added 3 real `test-org` questions, one
per `eval_confidence_dataset.json` category: `repo-sso-login-failure`
(`clear-answer` -> `answer`, correctly answered, `correct`), `repo-python-
version-conflict` (`ambiguous` -> `qualified_answer`, answered with full
confidence despite conflicting real evidence, `overconfident` -- see
"Evaluator discrimination" above), `repo-neg-parental-leave`
(`no-information` -> `no_answer`, correctly declined, `correct`). Total
cost: $0.0028; total latency 29.9s.

**What this does and does not demonstrate:** these two runs (plus
Priority 8's two, superseded ones) prove the benchmark engine executes
correctly end to end against a real model and real ingested data, and
they are genuine (not simulated) evidence that: the refusal-aware rubric
scores a correct refusal well (no longer `0.0`); the contrast fixtures
produce the discrimination section 10 requires; production's un-hedged
`generate_answer` path can still produce the exact overconfidence class of
failure `agents.answer.sufficiency` was built to prevent, when called
outside that gate (as this benchmark's own architecture does); and a real,
previously-undiscovered gap exists in this priority's own deterministic
refusal detector. They do **not** constitute a statistically meaningful
verdict on answer quality, on whether critique reliably improves
investigations, or on how often the evaluator discriminates correctly in
general -- single-digit cases per category is far below any sample size
this package's own calibration floor (20) would treat as sufficient, and
every calibration entry in this run remains `insufficient_data` (see
"Threshold inventory" above, unchanged in status by this priority). See
`docs/PROJECT_STATUS.md`'s Phase 24 entry and this priority's Final Report
for the complete, honest accounting of what remains unproven.

## Does the same "more output is better" bug exist elsewhere? (section 11)

Inspected, not expanded into unnecessarily -- the refusal-aware contract
is reused only where it's actually applicable:

- **Investigation critique evaluation**: already fine, no change needed.
  `investigation_ab`'s outcome vocabulary already includes
  `critique_correctly_rejected` (zero/empty hypotheses, or a rejected
  draft, can be the CORRECT outcome) and `critique_no_measurable_change`
  -- it does not assume more hypotheses or higher confidence is better.
  Built in Priority 8 with this same principle already in mind (see that
  module's own docstring on "a structural proxy, not a ground-truth
  judgment").
- **Confidence calibration** (`agents.confidence`): a deterministic
  weighted combination of retrieval signals, no LLM judge, no "verbosity"
  concept to reward -- not applicable.
- **Grounding evaluation** (`agents.answer.grounding.verify_grounding`):
  REMOVES ungrounded sentences; a shorter, more-grounded answer is
  preferred over a longer, less-grounded one by construction -- already
  aligned with epistemic correctness, not applicable.
- **Graph evaluation** / **memory evaluation**: Tier 1 deterministic
  fixture-based checks only (`app/evaluation/fixtures/graph_core_v1.jsonl`,
  `memory_core_v1.jsonl`) -- no semantic LLM-judge rubric exists for
  either today, so there is no "more output scores higher" rubric bug to
  have. Out of scope to build one here (section 17: do not expand scope
  unnecessarily; no proactive/graph/memory answer-quality case exists in
  this corpus).

The refusal-aware contract (`ExpectedAnswerMode`/`ObservedAnswerMode`/
`outcome.classify_outcome_correctness`) is NOT forced onto any of the
above -- it is scoped to `app/evaluation/semantic/answer_quality.py`
because that is the one place an LLM-judge rubric existed that actually
had this bug.

## Human-adjudicated ground truth (Priority 11)

Everything above this section measures whether the LLM judge agrees with
ITSELF being internally consistent (routing, rubric dimensions,
discrimination between contrast fixtures). None of it establishes whether
the judge agrees with an actual HUMAN. This section closes that gap: a
bounded, auditable path from a human's judgment on one case to a
deterministic measurement of how often the automated evaluator agrees.

### Why not database-backed

`app/evaluation/` has three existing precedents for version-controlled,
file-based benchmark data (`app/evaluation/fixtures/*.jsonl`,
`scripts/eval_confidence_dataset.json`, `app/evaluation/semantic/
fixtures.py`) and zero precedent for a database table backing benchmark
data -- Tier 1 and Tier 3 both run as standalone scripts, never as a
request-scoped REST resource, and ground-truth annotations are the same
kind of thing a team curates over time in git, not per-organization
customer data. `app/evaluation/semantic/annotation_store.py` therefore
reuses that exact convention: one JSON file per dataset version
(`app/evaluation/semantic/annotations/<dataset_version>.annotations.
json`), appended to by `scripts/annotate_semantic_cases.py`, reviewed the
same way any other repository change is. This also means section 13's
API-specific requirements (no caller-supplied `organization_id`, tenant
isolation, reviewer authorization) are structurally inapplicable: there is
no API, no per-organization row, and no tenant boundary to enforce,
because none of this touches the production database or its
authorization model at all.

**What would justify revisiting this**: a real multi-person review team
needing concurrent write access this file-based approach handles poorly
(two reviewers annotating simultaneously could both read-then-write and
lose each other's addition -- a real, documented limitation, not
overlooked). A future revision could move `annotation_store.py`'s two
functions (`save_annotation`/`save_resolution`) onto a real table without
changing anything above it (`ground_truth.py`, `evaluator_validation.py`,
`calibration.py`'s eligibility gate) -- the module boundary is deliberately
drawn there for exactly this reason.

### The ground-truth pipeline

```
AnnotationDecision (one reviewer, one case)      <- NEVER written by the LLM evaluator
        |
        v
resolve_ground_truth  ->  CaseGroundTruth (single_review / agreed_review /
        |                  resolved_disagreement / unresolved_disagreement)
        v
compute_inter_annotator_agreement  ->  AgreementReport (raw rate + Cohen's kappa)
        |
        v
validate_evaluator_against_ground_truth  ->  EvaluatorValidationReport
        |    (compares AnswerQualityResult, from a REAL benchmark run, against
        |     CaseGroundTruth -- deterministic Python, zero LLM calls)
        v
evaluator_reliability_eligibility  ->  CalibrationReport
        (setting_name="semantic_evaluator_reliability")
```

Direction is enforced by the module boundaries themselves, not just by
convention: `annotation_store.py` has no function that accepts an
`AnswerQualityResult` or anything evaluator-produced as input to a
SAVE call -- only `AnnotationDecision`/`ResolutionAnnotation`, which a
human (via the CLI) constructs. `evaluator_validation.py` is the only
module that reads BOTH an evaluator result and a ground truth, and it only
ever reads, never writes back to the annotation store.

### Annotation contract

`AnnotationDecision` (`app/evaluation/semantic/schemas.py`): `case_id`,
`dataset_version`, `annotation_schema_version` (`"annotation-v1"`),
`case_snapshot_hash` (see "Evidence versioning" below), `reviewer_id`
(pseudonymous -- a short handle like `"reviewer-a"`, never a real name),
`provenance` (`"synthetic_controlled_annotation"` or `"human_review"` --
see "Honest provenance" below), `annotated_at`, `observed_mode` (the SAME
`ObservedAnswerMode` vocabulary the automated evaluator uses --
`"substantive_answer"`/`"qualified_answer"`/`"no_answer"`),
`dimension_ratings` (optional, categorical: `"good"`/`"acceptable"`/
`"poor"` per rubric dimension -- never a numeric score a reviewer can't
reproduce), `rationale` (required, non-empty), `usable_for_calibration`
(a reviewer can flag a case unusable without deleting the record).

**Why `outcome_correctness` is never a separate hand-picked label.** A
reviewer records `observed_mode` only. `outcome_correctness` is DERIVED
via `ground_truth.derive_outcome_for_annotation`, which calls the exact
same `outcome.classify_outcome_correctness(case.expected_answer_mode,
observed_mode)` the automated evaluator's own outcome is computed
through. This holds a human and the evaluator to the identical decision
rule -- a human who disagrees with the evaluator's `outcome_correctness`
must do so by disagreeing about what mode the text exhibited, not by
silently applying a different mental model of what `"critical_failure"`
means.

### Evidence and case versioning (section 12)

Every annotatable case (`fixtures.load_annotatable_answer_quality_cases`)
has `fixed_answer` set -- the six Priority 9 contrast cases, plus three
new `repository_derived` cases built entirely from `scripts/
eval_confidence_dataset.json`'s OWN already-committed `evidence`/
`expected_answer` fields (`fixtures.load_repository_derived_annotatable_
cases`), never from live retrieval. This sidesteps retrieval drift
structurally, not just defensively: a fixed candidate answer against fixed
evidence cannot drift between benchmark runs, because nothing about it is
ever fetched live. `AnnotationDecision.case_snapshot_hash`
(`annotation_store.compute_case_snapshot_hash`, a SHA-256 of
`question`+sorted `evidence_texts`+the candidate answer) is still recorded
and still checked before every comparison, as a second, structural line of
defense: if a case's own definition ever changes without the annotation
being re-recorded, the stale annotation is excluded from that run's
comparison, never silently compared against different content.

**Why live-generated and `--repository-derived` cases are NOT
annotatable**: their candidate text changes every run (non-deterministic
generation) or their evidence can drift (live retrieval against
`test-org`, which changes as that organization's ingested data changes) --
an annotation of either would go stale almost immediately. A future
revision could support them by snapshotting the FULL (evidence, generated
answer) pair at benchmark-run time and annotating that specific pair
after the fact; not built here, since no current need exists for it and it
would add real complexity (matching section 24's "do not build unless
required" instruction).

### Independent review and disagreement (section 7)

Exactly the two-reviewer model section 7 describes: `resolve_ground_truth`
takes the chronologically first two annotations as "the independent
pair." Four states: `single_review` (one annotation, usable but lower
confidence), `agreed_review` (two independent annotations, same
`observed_mode`), `resolved_disagreement` (two annotations disagreed, a
`ResolutionAnnotation` was recorded -- the ORIGINAL two annotations remain
in the record, untouched; only the resolution's own label is used as the
final truth), `unresolved_disagreement` (two annotations disagreed, no
resolution recorded -- `final_observed_mode`/`final_outcome` are `None`,
and `evaluator_validation.py` excludes these cases from every statistic,
never silently treating either original annotation as truth).

A third or later annotation for the same case doesn't change this --
documented as a deliberate simplification in `ground_truth.py`'s own
module docstring, since this codebase's initial corpus never needs a
third independent reviewer and inventing a multi-rater statistic (Fleiss'
kappa) nobody asked for would be exactly the "sophisticated statistics
merely to look advanced" section 8 warns against.

### Inter-annotator agreement (section 8)

`ground_truth.compute_inter_annotator_agreement`: raw agreement rate over
every double-reviewed case, plus Cohen's kappa (`_cohens_kappa`, standard
two-rater multi-class formula) -- `None` when the expected-by-chance
agreement is 1.0 (kappa is mathematically undefined then, not silently
reported as a fake 1.0). Reuses `calibration.DEFAULT_MINIMUM_SAMPLE_SIZE`
(20) as the floor below which `status="insufficient_data"` and kappa is
never computed at all -- the same floor this package already uses
everywhere else a sample-size claim could be made, not a new number
invented for this one purpose.

### Evaluator validation (section 9) and severity (section 10)

`evaluator_validation.validate_evaluator_against_ground_truth`: plain,
deterministic Python -- no LLM call anywhere in this module. Compares
each case's `AnswerQualityResult` (from a real benchmark run) against its
`CaseGroundTruth`, computing answer-mode agreement, outcome agreement, a
full confusion matrix, per-class precision/recall/F1, and severe
disagreements.

**Severity model** (`_classify_severity`): not every disagreement is
equally dangerous. CRITICAL: human says `critical_failure` but the
evaluator called it `correct`/`partially_correct` (the evaluator would
normalize a hallucination), or human says `incorrect_refusal` but the
evaluator called it `correct` (the evaluator would rate a lazy refusal as
good). HIGH: human says `overconfident` but the evaluator called it
`correct` (the evaluator missed overstated certainty). Every other
mismatch (e.g. human `correct` vs. evaluator `partially_correct` --
section 10's own explicit "lower severity" example) is counted in the
confusion matrix and agreement rate but never flagged as severe -- a
deliberate asymmetry: an evaluator being MORE cautious than a human is a
real disagreement, but not the dangerous direction this benchmark most
exists to catch.

### Calibration eligibility (section 14)

`calibration.evaluator_reliability_eligibility` connects human ground
truth to this package's existing calibration architecture, answering a
DIFFERENT question than every other entry in this module: not "what
should threshold X be" but "is the semantic evaluator itself reliable
enough to be trusted for future calibration work at all." Reported as a
`CalibrationReport` (`setting_name="semantic_evaluator_reliability"`) so
it shares the same honest-status discipline as every other threshold.

Gating, most to least specific: `sample_size < floor` ->
`insufficient_data`; inter-annotator agreement itself not yet demonstrated
reliable -> `insufficient_agreement` (trusting the evaluator against
ground truth that hasn't itself been shown reproducible would be
circular); missing coverage of the two DANGEROUS outcome classes
(`critical_failure`, `incorrect_refusal`) among the compared cases ->
`insufficient_class_coverage` (a clean accuracy number that never
included a single hallucination example says nothing about whether the
evaluator catches one); any severe disagreement present -> stays
`provisional`, never `calibrated`, regardless of aggregate rate.

**This function can never currently return `"calibrated"`** -- even its
cleanest possible result (sample size met, agreement reliable, full class
coverage, zero severe disagreements) returns `"provisional"`, the same "a
single measurement pass is not enough evidence" discipline every other
entry in this module already applies. Reaching `"calibrated"` would need
repeated, independently-reviewed passes over time, which this single-run
architecture doesn't yet support -- a structural, not incidental, limit,
stated here rather than left to be discovered.

### Honest provenance: `synthetic_controlled_annotation` vs. `human_review`

Section 22's explicit requirement: an annotation created to validate the
MECHANISM (schema validation, agreement math, evaluator-comparison
plumbing) end to end must be labelled `"synthetic_controlled_annotation"`,
never presented as external human validation. `"human_review"` is
reserved for a person's genuine, independent judgment recorded through
`scripts/annotate_semantic_cases.py`; this package never assigns that
value itself.

**What this priority actually did**: authored 9 `synthetic_controlled_
annotation` records across the full annotatable corpus (one reviewer,
`"reviewer-1"`), plus a genuine independent second review
(`"reviewer-2"`) on 4 of those cases to exercise agreement/disagreement,
including one deliberate, defensible disagreement on
`annot-repo-python-version-conflict` (is a hedge that reports conflicting
values a `"qualified_answer"` or a `"no_answer"` in disguise?), resolved
by a third pseudonymous reviewer (`"reviewer-3-resolver"`). Every label
was determined by actually reading the case's fixed candidate text, not
by restating the case's own `expected_answer_mode` -- see
`scripts/annotate_semantic_cases.py`'s `list`/`show`/`agreement`/`status`
subcommands for the exact record. **This is mechanism validation, not
external human validation** -- 0 cases in this corpus have
`provenance="human_review"` as of this writing. A real reviewer (a person
who is not the same agent that built and is measuring the benchmark) can
record genuine `human_review` annotations with the exact same CLI tool;
none has yet.

### How to run it

```
python scripts/annotate_semantic_cases.py list
python scripts/annotate_semantic_cases.py show <case-id>
python scripts/annotate_semantic_cases.py annotate <case-id> --reviewer <id> \
    --observed-mode <substantive_answer|qualified_answer|no_answer> \
    --rationale "..." [--provenance human_review]
python scripts/annotate_semantic_cases.py resolve <case-id> --resolver <id> \
    --observed-mode <...> --rationale "..."
python scripts/annotate_semantic_cases.py status
python scripts/annotate_semantic_cases.py agreement
python scripts/run_semantic_evaluation.py   # now also prints Human Ground Truth
                                             # Coverage / Evaluator vs Human
                                             # Agreement sections
```

### Live results (what has actually been measured so far)

One bounded live run, 2026-08-26, real `gpt-4o-mini`, using the 9
`synthetic_controlled_annotation` records above:

- **Human Ground Truth Coverage**: 9/9 annotatable cases annotated, 4
  double-reviewed, 3 agreed, 0 unresolved disagreements (the one genuine
  disagreement was resolved), provenance `{synthetic_controlled: 6,
  repository_derived: 3}`.
- **Inter-Annotator Agreement**: n=4 double-reviewed cases -- below the
  20-case floor, `status="insufficient_data"`, raw agreement 0.75 (3/4).
  Cohen's kappa not computed (sample too small).
- **Evaluator vs Human Agreement**: n=9, `status="insufficient_data"` (below
  the floor), answer-mode agreement 0.89 (8/9), outcome agreement 0.89
  (8/9). Confusion matrix: 5 `correct->correct`, 1 `critical_failure->
  critical_failure`, 1 `incorrect_refusal->incorrect_refusal`, 1
  `overconfident->overconfident`, and one real, interesting disagreement:
  `correct->overconfident` on `annot-repo-python-version-conflict` -- the
  resolved human ground truth called the dataset's own hedge text
  `"qualified_answer"` (correct), but the LIVE evaluator classified the
  SAME text as `"substantive_answer"` this run (overconfident). Not a bug:
  a genuine, defensible edge case in what counts as "hedged enough," and
  exactly the kind of disagreement this priority's whole point is to
  surface rather than hide inside an aggregate rate. Not flagged severe
  (the direction is evaluator-more-cautious... actually evaluator-LESS-
  cautious-than-human here, but outside this severity model's two named
  dangerous patterns -- see "Severity model" above).
- **Calibration eligibility** (`semantic_evaluator_reliability`):
  `status="insufficient_data"`, n=9, below the 20-case floor. Every other
  threshold's own calibration status is unchanged by this priority.
- Cost: $0.0034 total (12 answer-quality + 3 investigation-A/B cases,
  including the 3 new repository-derived annotatable cases). No new LLM
  call was added for ground-truth comparison itself -- every human
  ground-truth computation in this run (`ground_truth.py`/
  `evaluator_validation.py`/`calibration.evaluator_reliability_
  eligibility`) is plain Python over already-computed results.

**What this does and does not demonstrate**: this proves the full
pipeline -- annotation, independent review, disagreement, resolution,
agreement statistics, evaluator-vs-human comparison, calibration
eligibility gating -- works correctly end to end against real evaluator
output, including catching a genuine (if minor) evaluator/human
disagreement. It does **not** demonstrate the evaluator is reliable in
general: n=9 is far below this package's own 20-case floor, all 9
annotations are `synthetic_controlled_annotation` (mechanism validation),
and only 4 cases were independently double-reviewed. See
`docs/PROJECT_STATUS.md`'s Phase 26 entry and this priority's Final
Report for the complete, honest accounting.

## Non-goals (explicitly out of scope for this priority)

Dynamic model routing, semantic caching, multimodal/OCR ingestion, a new
agent architecture, a new vector backend, autonomous threshold tuning in
production, automatic prompt optimization, new tenant/authorization
frameworks, human-feedback learning systems, a massive benchmark platform.
Retrieval Recall@K/Precision@K/MRR is also out of scope for the reason
given in "Metrics actually implemented" above.

**Priority 9 additionally did not**: redesign the evaluation framework
(only `answer_quality.py`/`outcome.py`/the case+result schemas changed);
replace Tier 1; move semantic evaluation into fast PR CI; invent
production data (the two real live runs are the only "production-like"
evidence, and are labelled as small, single-run, non-statistical
throughout); claim any threshold moved to `calibrated`; build a human-
review UI (see "Human-adjudication-ready output" -- structured report
fields only); add a new database (no persistence changes at all -- every
report is still a JSON file); add retrieval metrics; modify Answer Agent
behavior to make scores look better (the overconfidence bug reproduced in
"Results" is reported, not silently patched); tune prompts to the specific
contrast fixtures; or hide the one regression-triggering result this
priority's own live run produced.

**Priority 10 additionally did not**: build a new qualified-answer
generation capability (`AskResponse.answer_mode` stays a 2-value enum,
matching what production actually does, per "Exact vocabulary, and why
it's two values, not three" above); add a broad regex/phrase-list refusal
detector (rejected explicitly, see "Why phrase matching alone is not
authoritative"); add a second LLM call anywhere in the judge path (`judge
call count is unchanged: still exactly one per case`, measured); expose
`AnswerOutcome.reason` or any other internal detail on the public API
(section 4.2/11); change Tier 1 deterministic semantics (re-verified
unchanged: `scripts/run_evaluation.py` still reports 56 cases, `VERDICT:
CLEAN`); or claim the free-text-refusal-detection gap is fully solved --
"What is STILL genuinely limited, honestly" above is explicit that
historical/legacy text with no accompanying machine-readable outcome
remains inherently ambiguous.

**Priority 11 additionally did not**: build a database-backed annotation
system or a REST API (see "Why not database-backed" above); build any
UI beyond a CLI; claim any real `human_review` annotations exist (0 do;
every annotation recorded is honestly labelled `synthetic_controlled_
annotation`); claim `semantic_evaluator_reliability` or any other
threshold reached `calibrated` (all remain `insufficient_data`, correctly,
given n=9 against a 20-case floor); invent a multi-rater agreement
statistic for 3+ reviewers (documented two-reviewer simplification, see
"Independent review and disagreement" above); add any new LLM call for
ground-truth comparison (`evaluator_validation.py`/`ground_truth.py` are
plain Python, verified in "Live results" above); extend investigation A/B
with the same ground-truth machinery (no `fixed_hypotheses`-equivalent
mechanism exists yet to make investigation cases deterministically
annotatable -- a real, scoped gap, not attempted here); or support
annotating live-generated/`--repository-derived` cases (both can drift
between runs; only the fixed-answer annotatable corpus is supported, per
"Evidence and case versioning" above).
