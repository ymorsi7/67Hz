"""Phase 2 tests: Reader, Verifier (batched per page), degradation, conflicts, abstention, offline smoke."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.degrade import degrade_image, generate_degraded_suite, png_bytes_to_rgb_array
from eval.extraction import evaluate_extraction, score_document
from reader.extract import detect_conflicts, extract_from_image_bytes
from reader.pdf_utils import render_text_to_png
from reader.verify import verify_constraints, verify_page_constraints
from rules.schema import Constraint

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "eval" / "cache"


class _FakeInteractions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="fake_int_1", output_text=json.dumps(self.payload))


class _FakeDecisions:
    def __init__(self, answers_by_call: list[dict[str, tuple[float, float]]]) -> None:
        self.answers_by_call = answers_by_call
        self.calls: list[dict] = []

    def create(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        mapping = self.answers_by_call[min(idx, len(self.answers_by_call) - 1)]
        answers = {}
        for qkey in kwargs["questions"]:
            noul, conf = mapping.get(qkey, (0.50, 0.50))
            answers[qkey] = SimpleNamespace(type="noul", noul=noul, confidence=conf)
        usage = SimpleNamespace(total_input_tokens=1200, total_output_tokens=0)
        return SimpleNamespace(answers=answers, usage=usage)


class _FakeClient:
    def __init__(self, interaction_payload: dict | None = None, decision_answers: list[dict] | None = None) -> None:
        self.interactions = _FakeInteractions(interaction_payload or {"constraints": [], "document_summary": "", "unreadable": False})
        self.decisions = _FakeDecisions(decision_answers or [{}])


def test_page_with_no_numeric_limits_yields_zero_constraints() -> None:
    """A page with no numeric interconnection limits produces zero constraints."""
    png = render_text_to_png(
        "Section 1. Introduction\nThis stakeholder process welcomes public comments from market participants."
    )
    fake_client = _FakeClient(
        interaction_payload={
            "constraints": [],
            "document_summary": "Introductory prose with no numeric power limits.",
            "unreadable": False,
        }
    )
    out = extract_from_image_bytes(png, source_name="no_limits.png", client=fake_client)
    assert out.constraints == []
    assert out.unreadable is False


def test_conflicting_limits_flagged_and_adjacent_bands_not_flagged() -> None:
    """Conflicting limits of the same type/band are flagged; adjacent frequency bands are not."""
    constraints = [
        Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min"),
        Constraint("ramp_rate", 10.0, "MW/min", page=14, snippet="AESO: 10 MW/min"),
        Constraint(
            "fluctuation_band",
            10.0,
            "MW",
            page=23,
            snippet="LIPA 0.1-5 Hz < 10 MW",
            freq_min_hz=0.1,
            freq_max_hz=5.0,
        ),
        Constraint(
            "fluctuation_band",
            3.5,
            "MW",
            page=23,
            snippet="LIPA 5-55 Hz < 3.5 MW",
            freq_min_hz=5.0,
            freq_max_hz=55.0,
        ),
    ]
    for idx, c in enumerate(constraints):
        c.id = f"{c.constraint_type}_{idx}"

    detect_conflicts(constraints, rated_mw=100.0)
    assert constraints[0].conflict_flag is True
    assert constraints[1].conflict_flag is True
    assert "CONFLICT" in constraints[0].notes
    # Adjacent LIPA bands [0.1, 5.0] and [5.0, 55.0] do NOT overlap and must NOT be flagged
    assert constraints[2].conflict_flag is False
    assert constraints[3].conflict_flag is False


def test_verifier_batches_one_call_per_page_and_abstains_below_threshold() -> None:
    """Verifier makes exactly ONE Decisions API call per cited page and abstains below threshold."""
    c_p1_a = Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min", id="r0")
    c_p1_b = Constraint("max_step", 10.0, "MW", page=1, snippet="10 MW", id="s1")
    c_p2_a = Constraint("dynamic_range", 25.0, "MW", page=2, snippet="25 MW", id="d2")

    fake_client = _FakeClient(
        decision_answers=[
            # Page 1 call: c_0 verified (0.95), c_1 low confidence (0.68 -> abstain)
            {"page_readable": (0.94, 0.94), "c_0": (0.95, 0.95), "c_1": (0.68, 0.68)},
            # Page 2 call: c_0 high-confidence NO (0.04 -> rejected)
            {"page_readable": (0.92, 0.92), "c_0": (0.04, 0.96)},
        ]
    )
    dummy_png = render_text_to_png("Dummy page")
    report = verify_constraints(
        [c_p1_a, c_p1_b, c_p2_a],
        page_images={1: dummy_png, 2: dummy_png},
        threshold=0.85,
        readability_threshold=0.60,
        client=fake_client,
        allow_fallback=False,
    )

    # Exactly 2 Decisions API calls for 3 constraints across 2 pages!
    assert len(fake_client.decisions.calls) == 2
    assert len(fake_client.decisions.calls[0]["questions"]) == 3  # page_readable + 2 constraints
    assert len(fake_client.decisions.calls[1]["questions"]) == 2  # page_readable + 1 constraint

    assert report.verified_count == 1
    assert report.abstained_count == 1
    assert report.rejected_count == 1
    assert c_p1_a.verified is True and c_p1_a.needs_human_review is False
    assert c_p1_b.verified is None and c_p1_b.needs_human_review is True
    assert c_p2_a.verified is False and c_p2_a.needs_human_review is True


def test_unreadable_photo_abstains() -> None:
    """An unreadable/heavily blurred photo must abstain and mark Needs human review."""
    clean_png = render_text_to_png("Maximum ramp rate shall not exceed 20 MW/min.")
    sev4_png, spec = degrade_image(clean_png, severity=4, seed=42)
    assert spec.severity == 4

    c = Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min", id="r0")
    fake_client = _FakeClient(
        decision_answers=[
            # Even if c_0 had middling/high p_yes, unreadable page (0.13 < 0.60) forces abstention!
            {"page_readable": (0.1314, 0.8686), "c_0": (0.87, 0.87)},
        ]
    )
    items, summary = verify_page_constraints(
        1, sev4_png, [c], threshold=0.85, readability_threshold=0.60, client=fake_client, allow_fallback=False
    )
    assert summary.page_readable_p < 0.60
    assert items[0].status == "abstained"
    assert c.verified is None
    assert c.needs_human_review is True


def test_degrade_pipeline_deterministic_four_severities() -> None:
    clean_png = render_text_to_png("Ramp rate <= 10 MW/min.\nMax step <= 5 MW.")
    suite_a = generate_degraded_suite(clean_png, seed=7)
    suite_b = generate_degraded_suite(clean_png, seed=7)
    assert set(suite_a.keys()) == {1, 2, 3, 4}
    for sev in (1, 2, 3, 4):
        png_a, spec_a = suite_a[sev]
        png_b, _ = suite_b[sev]
        assert png_a == png_b  # deterministic
        arr = png_bytes_to_rgb_array(png_a)
        assert arr.ndim == 3 and arr.shape[2] == 3 and arr.shape[0] > 32
        assert spec_a.severity == sev


def test_offline_smoke_on_recorded_api_responses() -> None:
    """Offline smoke test verifying recorded real Neon + Decisions API runs with zero network calls."""
    calib_path = CACHE_DIR / "dev_calibration.json"
    caiso_path = CACHE_DIR / "extractions" / "Large-Load-Technical-Requirements-Straw-Proposal-Jun-15-2026.json"
    esig_path = CACHE_DIR / "extractions" / "LL-Performance-Requirements-Framework-and-Methodology_v2.json"
    arxiv_path = CACHE_DIR / "extractions" / "2601.12686v1.json"

    assert calib_path.exists(), "Missing recorded dev_calibration.json"
    assert caiso_path.exists() and esig_path.exists() and arxiv_path.exists()

    calib = json.loads(calib_path.read_text(encoding="utf-8"))
    # Recorded real CAISO p.1 cover page -> 0 constraints
    assert calib["no_limits_page"]["constraints_found"] == 0
    # Recorded real negative controls -> true verified, all 3 hallucinations rejected
    ctrl_by_id = {x["constraint_id"]: x for x in calib["hallucination_controls"]}
    assert ctrl_by_id["true_20mw_min"]["status"] == "verified"
    assert ctrl_by_id["halluc_wrong_value_50mw_min"]["status"] == "rejected"
    assert ctrl_by_id["halluc_wrong_unit_20mw_s"]["status"] == "rejected"
    assert ctrl_by_id["halluc_invented_max_step"]["status"] == "rejected"
    # Recorded real Severity 4 unreadable photo -> abstained
    sev4 = next(x for x in calib["degradation_sweep"] if x["severity"] == 4)
    assert sev4["reader_unreadable"] is True
    assert sev4["page_readable_p"] < 0.60
    assert sev4["verifications"][-1]["status"] == "abstained"

    # Recorded real PDF extractions deserialize cleanly into Constraint objects
    for p in (caiso_path, esig_path, arxiv_path):
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["reader_model"] == "gemini-4-neon"
        assert rec["verifier_model"] == "gemini-decision-1"
        assert rec["verifier_fallback_used"] is False
        constraints = [Constraint.from_dict(c) for c in rec["constraints"]]
        assert len(constraints) == len(rec["constraints"])


def test_extraction_eval_reports_no_gold_labels_and_scores_fixture(tmp_path: Path) -> None:
    """Reports 'no gold labels' when no human gold files exist; scores correctly on temp fixture."""
    # Empty / template-only gold dir -> must report 'no gold labels'
    (tmp_path / "TEMPLATE.json").write_text('{"labeled_by": "human", "doc_id": "t", "constraints": []}')
    empty_res = evaluate_extraction(gold_dir=tmp_path)
    assert empty_res.status == "no gold labels"
    assert empty_res.gold_files_found == 0

    # Real repo eval/gold/ currently has no human gold files yet -> must also report 'no gold labels'
    repo_res = evaluate_extraction()
    if repo_res.gold_files_found == 0:
        assert repo_res.status == "no gold labels"

    # Exact scoring math on an in-memory unit fixture (never written to repo eval/gold/)
    gold = [
        Constraint("ramp_rate", 20.0, "MW/min", page=4, snippet="20 MW/min"),
        Constraint("max_step", 10.0, "MW", page=5, snippet="10 MW"),
    ]
    preds = [
        Constraint("ramp_rate", 20_000.0, "kW/min", page=4, snippet="20 MW/min", verified=True, needs_human_review=False),
        Constraint("dynamic_range", 30.0, "MW", page=6, snippet="halluc", verified=True, needs_human_review=False),
        Constraint("max_step", 10.0, "MW", page=5, snippet="blurry", verified=None, needs_human_review=True),
    ]
    m = score_document("unit_fixture", "dev", preds, gold, rated_mw=100.0)
    assert m.tp == 1
    assert m.fp == 1
    assert m.fn == 1
    assert m.precision == pytest.approx(0.5)
    assert m.recall == pytest.approx(0.5)
    assert m.hallucination_rate == pytest.approx(0.5)
    assert m.abstention_rate == pytest.approx(1.0 / 3.0)
