"""Extraction & verification metrics against human-labeled eval/gold/*.json files.

Reports precision, recall, hallucination rate, and abstention rate.
If no human-labeled gold files exist, explicitly reports 'no gold labels' and
NEVER fabricates gold entries.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rules.schema import Constraint
from rules.units import UnitConversionError, normalize_constraint_limit

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO_ROOT / "eval" / "gold"
CACHE_DIR = REPO_ROOT / "eval" / "cache" / "extractions"


@dataclass
class DocExtractionMetrics:
    doc_id: str
    split: str
    gold_count: int
    predicted_count: int
    verified_count: int
    abstained_count: int
    rejected_count: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    hallucination_rate: float | None
    abstention_rate: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionEvalSummary:
    status: str  # "ok" | "no gold labels"
    gold_files_found: int
    precision: float | None = None
    recall: float | None = None
    hallucination_rate: float | None = None
    abstention_rate: float | None = None
    total_tp: int = 0
    total_fp: int = 0
    total_fn: int = 0
    total_abstained: int = 0
    total_predicted: int = 0
    per_doc: list[DocExtractionMetrics] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "gold_files_found": self.gold_files_found,
            "precision": self.precision,
            "recall": self.recall,
            "hallucination_rate": self.hallucination_rate,
            "abstention_rate": self.abstention_rate,
            "total_tp": self.total_tp,
            "total_fp": self.total_fp,
            "total_fn": self.total_fn,
            "total_abstained": self.total_abstained,
            "total_predicted": self.total_predicted,
            "message": self.message,
            "per_doc": [d.to_dict() for d in self.per_doc],
        }


def load_human_gold_files(gold_dir: Path = GOLD_DIR) -> list[dict[str, Any]]:
    """Load only genuine human-labeled gold files (excludes TEMPLATE.json and non-human files)."""
    if not gold_dir.exists():
        return []
    docs: list[dict[str, Any]] = []
    for path in sorted(gold_dir.glob("*.json")):
        if path.name.upper() == "TEMPLATE.JSON":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("labeled_by") != "human":
            continue
        data["_path"] = str(path)
        docs.append(data)
    return docs


def _constraints_match(
    pred: Constraint,
    gold: Constraint,
    rated_mw: float,
    rel_tol: float = 0.05,
    page_tol: int = 0,
) -> bool:
    """Match a predicted constraint against a gold constraint on type, page, and normalized value."""
    if pred.constraint_type != gold.constraint_type:
        return False
    if abs(pred.page - gold.page) > page_tol:
        return False

    if not gold.checkable or gold.value is None:
        # Qualitative match by snippet token overlap
        g_words = {w for w in gold.snippet.lower().split() if len(w) > 3}
        p_words = {w for w in pred.snippet.lower().split() if len(w) > 3}
        if not g_words:
            return not pred.checkable
        overlap = len(g_words & p_words) / len(g_words)
        return overlap >= 0.4

    if not pred.checkable or pred.value is None:
        return False

    try:
        p_val, p_unit = normalize_constraint_limit(pred, rated_mw=rated_mw)
        g_val, g_unit = normalize_constraint_limit(gold, rated_mw=rated_mw)
    except UnitConversionError:
        return False

    if p_unit != g_unit:
        return False
    denom = max(abs(g_val), 1e-9)
    return abs(p_val - g_val) / denom <= rel_tol


def score_document(
    doc_id: str,
    split: str,
    predicted: list[Constraint],
    gold: list[Constraint],
    rated_mw: float = 100.0,
    checkable_only: bool = True,
) -> DocExtractionMetrics:
    """Compute precision, recall, hallucination rate, and abstention rate for one document."""
    pred_list = [c for c in predicted if (c.checkable or not checkable_only)]
    gold_list = [c for c in gold if (c.checkable or not checkable_only)]

    abstained = [c for c in pred_list if c.needs_human_review and c.verified is None]
    rejected = [c for c in pred_list if c.verified is False]
    # Active (accepted/verified) predictions are those NOT abstained and NOT rejected by Verifier
    active = [c for c in pred_list if (c.verified is True) or (c.verified is None and not c.needs_human_review)]

    matched_gold: set[int] = set()
    tp = 0
    fp = 0
    for p in active:
        found = False
        for g_idx, g in enumerate(gold_list):
            if g_idx in matched_gold:
                continue
            if _constraints_match(p, g, rated_mw=rated_mw):
                matched_gold.add(g_idx)
                tp += 1
                found = True
                break
        if not found:
            fp += 1

    fn = len(gold_list) - len(matched_gold)
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else (1.0 if len(gold_list) == 0 else None)
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else (1.0 if fp == 0 else 0.0)
    hallucination_rate = (fp / (tp + fp)) if (tp + fp) > 0 else 0.0
    abstention_rate = (len(abstained) / len(pred_list)) if len(pred_list) > 0 else 0.0

    return DocExtractionMetrics(
        doc_id=doc_id,
        split=split,
        gold_count=len(gold_list),
        predicted_count=len(pred_list),
        verified_count=sum(1 for c in pred_list if c.verified is True),
        abstained_count=len(abstained),
        rejected_count=len(rejected),
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        hallucination_rate=hallucination_rate,
        abstention_rate=abstention_rate,
    )


def evaluate_extraction(
    gold_dir: Path = GOLD_DIR,
    predictions_by_doc: dict[str, list[Constraint]] | None = None,
    cache_dir: Path = CACHE_DIR,
    split_filter: str | None = None,
    checkable_only: bool = True,
) -> ExtractionEvalSummary:
    """Evaluate extraction & verification against human-labeled eval/gold/*.json."""
    gold_docs = load_human_gold_files(gold_dir)
    if split_filter is not None:
        gold_docs = [d for d in gold_docs if d.get("split") == split_filter]

    if not gold_docs:
        return ExtractionEvalSummary(
            status="no gold labels",
            gold_files_found=0,
            message="no gold labels (run scripts/label.py to create human-labeled eval/gold/*.json files)",
        )

    per_doc: list[DocExtractionMetrics] = []
    tot_tp = tot_fp = tot_fn = tot_abs = tot_pred = 0

    for gdoc in gold_docs:
        doc_id = str(gdoc["doc_id"])
        split = str(gdoc.get("split", "dev"))
        rated_mw = float(gdoc.get("rated_mw", 100.0))
        gold_constraints = [Constraint.from_dict(x) for x in gdoc.get("constraints", [])]

        preds: list[Constraint] = []
        if predictions_by_doc is not None and doc_id in predictions_by_doc:
            preds = predictions_by_doc[doc_id]
        else:
            cache_file = cache_dir / f"{doc_id}.json"
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                preds = [Constraint.from_dict(x) for x in cached.get("constraints", [])]

        m = score_document(
            doc_id=doc_id,
            split=split,
            predicted=preds,
            gold=gold_constraints,
            rated_mw=rated_mw,
            checkable_only=checkable_only,
        )
        per_doc.append(m)
        tot_tp += m.tp
        tot_fp += m.fp
        tot_fn += m.fn
        tot_abs += m.abstained_count
        tot_pred += m.predicted_count

    precision = (tot_tp / (tot_tp + tot_fp)) if (tot_tp + tot_fp) > 0 else None
    recall = (tot_tp / (tot_tp + tot_fn)) if (tot_tp + tot_fn) > 0 else None
    hallucination_rate = (tot_fp / (tot_tp + tot_fp)) if (tot_tp + tot_fp) > 0 else 0.0
    abstention_rate = (tot_abs / tot_pred) if tot_pred > 0 else 0.0

    return ExtractionEvalSummary(
        status="ok",
        gold_files_found=len(gold_docs),
        precision=precision,
        recall=recall,
        hallucination_rate=hallucination_rate,
        abstention_rate=abstention_rate,
        total_tp=tot_tp,
        total_fp=tot_fp,
        total_fn=tot_fn,
        total_abstained=tot_abs,
        total_predicted=tot_pred,
        per_doc=per_doc,
        message=f"Evaluated {len(gold_docs)} human-labeled gold file(s).",
    )
