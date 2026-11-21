"""Verifier: calibrated yes/no verification via Gemini Decisions API (`gemini-decision-1`).

Batches ONE Decisions API call per cited page, asking a separate `noul` (yes/no)
question per constraint on that page plus a page-legibility check in the same
zero-output-token forward pass. Below threshold (or on an unreadable page),
abstains and marks `needs_human_review=True`.

Fallback (only if Decisions API is unavailable at runtime): uses `gemini-4-hydrogen`
with a strict yes/no JSON schema and explicitly labels `fallback_used=True`.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from repo_paths import find_repo_root
from typing import Any, Callable

import yaml
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

from reader.pdf_utils import render_pdf_page_png
from rules.schema import Constraint

REPO_ROOT = find_repo_root()
load_dotenv(REPO_ROOT / ".env")
MODELS_CONFIG_PATH = REPO_ROOT / "config" / "models.yaml"


@dataclass
class ConstraintVerification:
    """Calibrated verification outcome for a single constraint."""

    constraint_id: str
    page: int
    p_yes: float  # Calibrated P(yes) in [0.01, 0.99]
    confidence: float  # max(p_yes, 1 - p_yes)
    status: str  # "verified" | "abstained" | "rejected"
    verified: bool | None
    needs_human_review: bool
    page_readable_p: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageVerificationSummary:
    """Summary of one batched Decisions API call for a single page."""

    page: int
    question_count: int
    page_readable_p: float
    verifier_model: str
    fallback_used: bool
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """Full verification results across all pages of a document."""

    verifier_model: str
    fallback_used: bool
    threshold: float
    readability_threshold: float
    verified_count: int
    abstained_count: int
    rejected_count: int
    uncheckable_count: int
    items: list[ConstraintVerification] = field(default_factory=list)
    pages: list[PageVerificationSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_model": self.verifier_model,
            "fallback_used": self.fallback_used,
            "threshold": self.threshold,
            "readability_threshold": self.readability_threshold,
            "verified_count": self.verified_count,
            "abstained_count": self.abstained_count,
            "rejected_count": self.rejected_count,
            "uncheckable_count": self.uncheckable_count,
            "items": [i.to_dict() for i in self.items],
            "pages": [p.to_dict() for p in self.pages],
        }


class _FallbackDecisionItem(BaseModel):
    key: str
    answer_yes: bool
    confidence: float = Field(ge=0.5, le=0.99)


class _FallbackBatchSchema(BaseModel):
    answers: list[_FallbackDecisionItem]


def load_verifier_config() -> dict[str, Any]:
    if MODELS_CONFIG_PATH.exists():
        with MODELS_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = cfg.get("verifier", {})
        return {
            "model": str(v.get("model", "gemini-decision-1")),
            "confidence_threshold": float(v.get("confidence_threshold", 0.75)),
            "readability_threshold": float(v.get("readability_threshold", 0.60)),
            "fallback_model": str(v.get("fallback_model", "gemini-4-hydrogen")),
        }
    return {
        "model": "gemini-decision-1",
        "confidence_threshold": 0.75,
        "readability_threshold": 0.60,
        "fallback_model": "gemini-4-hydrogen",
    }


def _build_question_for_constraint(c: Constraint) -> dict[str, Any]:
    if c.checkable and c.value is not None:
        band_txt = ""
        if c.freq_min_hz is not None or c.freq_max_hz is not None:
            band_txt = f" in frequency band [{c.freq_min_hz}, {c.freq_max_hz}] Hz"
        win_txt = f" over a {c.window_s:g}s window" if c.window_s is not None else ""
        sec_txt = f" (section/utility: {c.source_section})" if c.source_section else ""
        instructions = (
            f"Does this page image clearly and explicitly state a {c.constraint_type} limit of "
            f"{c.value:g} {c.unit}{win_txt}{band_txt}{sec_txt}, matching the quoted excerpt: "
            f"\"{c.snippet}\"?"
        )
        criteria = {
            "true_description": (
                "The page image is legible and explicitly contains this exact numeric value, unit, "
                "and technical requirement matching the excerpt."
            ),
            "false_description": (
                "The numeric value or unit is absent, different, misread, hallucinated, applies to "
                "a different quantity (such as a load-size applicability threshold), or the text is unreadable."
            ),
        }
    else:
        instructions = (
            f"Does this page image clearly and explicitly state the following requirement or obligation: "
            f"\"{c.snippet}\"?"
        )
        criteria = {
            "true_description": "The page image is legible and explicitly contains this requirement matching the excerpt.",
            "false_description": "The requirement is absent, contradicted, misquoted, or the page image is unreadable.",
        }
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": criteria,
    }


def _build_page_readability_question() -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": (
            "Is the text and any numbers on this page image sharp and legible enough to read "
            "technical specifications reliably without guessing?"
        ),
        "criteria": {
            "true_description": "The text and numbers are clear enough to read accurately.",
            "false_description": "The image is severely blurred, heavily pixelated, blank, overexposed, or cut off so text/numbers cannot be reliably read.",
        },
    }


def _call_fallback_hydrogen(
    client: genai.Client,
    fallback_model: str,
    image_b64: str,
    mime_type: str,
    questions: dict[str, dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    """Labeled fallback if Decisions API is unavailable."""
    lines = ["Answer each yes/no verification question strictly based on the attached page image:"]
    for key, q in questions.items():
        lines.append(f"- {key}: {q['instructions']}")
    interaction = client.interactions.create(
        model=fallback_model,
        input=[
            {"type": "image", "data": image_b64, "mime_type": mime_type, "resolution": "high"},
            {"type": "text", "text": "\n".join(lines)},
        ],
        generation_config={"thinking_level": "low"},
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": _FallbackBatchSchema.model_json_schema(),
        },
    )
    parsed = _FallbackBatchSchema.model_validate_json(interaction.output_text)
    out: dict[str, tuple[float, float]] = {}
    for ans in parsed.answers:
        conf = min(max(float(ans.confidence), 0.50), 0.99)
        p_yes = conf if ans.answer_yes else (1.0 - conf)
        out[ans.key] = (p_yes, conf)
    return out


def verify_page_constraints(
    page_number: int,
    page_image_bytes: bytes,
    constraints: list[Constraint],
    *,
    mime_type: str = "image/png",
    threshold: float | None = None,
    readability_threshold: float | None = None,
    resolution: str = "high",
    client: genai.Client | None = None,
    allow_fallback: bool = True,
) -> tuple[list[ConstraintVerification], PageVerificationSummary]:
    """Verify all constraints citing a single page in ONE batched Decisions API call."""
    cfg = load_verifier_config()
    thresh = float(threshold if threshold is not None else cfg["confidence_threshold"])
    read_thresh = float(
        readability_threshold if readability_threshold is not None else cfg["readability_threshold"]
    )
    model_id = cfg["model"]
    fallback_model = cfg["fallback_model"]

    api_client = client if client is not None else genai.Client(http_options={"api_version": "v1beta"})
    image_b64 = base64.b64encode(page_image_bytes).decode("utf-8")

    questions: dict[str, dict[str, Any]] = {"page_readable": _build_page_readability_question()}
    key_map: dict[str, Constraint] = {}
    for idx, c in enumerate(constraints):
        qkey = f"c_{idx}"
        questions[qkey] = _build_question_for_constraint(c)
        key_map[qkey] = c

    fallback_used = False
    used_model = model_id
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw_answers: dict[str, tuple[float, float]] = {}

    try:
        resp = api_client.decisions.create(
            model=model_id,
            input=[
                {
                    "type": "image",
                    "data": image_b64,
                    "mime_type": mime_type,
                    "resolution": resolution,
                }
            ],
            questions=questions,
        )
        for qkey in questions:
            ans = resp.answers[qkey]
            raw_answers[qkey] = (float(ans.noul), float(ans.confidence))
        if getattr(resp, "usage", None) is not None:
            input_tokens = getattr(resp.usage, "total_input_tokens", None)
            output_tokens = getattr(resp.usage, "total_output_tokens", 0)
    except Exception:
        if not allow_fallback:
            raise
        fallback_used = True
        used_model = f"{fallback_model} (fallback)"
        raw_answers = _call_fallback_hydrogen(api_client, fallback_model, image_b64, mime_type, questions)

    page_readable_p, _ = raw_answers.get("page_readable", (0.5, 0.5))
    page_is_readable = page_readable_p >= read_thresh

    verifications: list[ConstraintVerification] = []
    for idx, c in enumerate(constraints):
        qkey = f"c_{idx}"
        p_yes, conf = raw_answers.get(qkey, (0.5, 0.5))

        if not page_is_readable:
            status = "abstained"
            verified_flag: bool | None = None
            needs_review = True
            reason = (
                f"Abstained: page {page_number} readability score {page_readable_p:.2f} "
                f"< {read_thresh:.2f} (Needs human review)."
            )
        elif conf < thresh:
            status = "abstained"
            verified_flag = None
            needs_review = True
            reason = (
                f"Abstained: Verifier confidence {conf:.2f} (P(yes)={p_yes:.2f}) "
                f"< threshold {thresh:.2f} (Needs human review)."
            )
        elif p_yes >= thresh:
            status = "verified"
            verified_flag = True
            needs_review = False
            reason = f"Verified on p.{page_number} (P(yes)={p_yes:.2f}, confidence={conf:.2f})."
        else:
            # High-confidence NO (1 - p_yes >= thresh)
            status = "rejected"
            verified_flag = False
            needs_review = True
            reason = (
                f"Rejected by Verifier on p.{page_number} (P(yes)={p_yes:.2f}, "
                f"confidence={conf:.2f} that claim is unsupported)."
            )

        # Mutate constraint verification fields in-place so downstream checker sees exact state
        c.confidence = round(conf, 4)
        c.verified = verified_flag
        c.needs_human_review = needs_review
        if fallback_used and "[Verifier fallback:" not in c.notes:
            c.notes = f"{c.notes} [Verifier fallback: {fallback_model}]".strip()

        verifications.append(
            ConstraintVerification(
                constraint_id=c.id,
                page=page_number,
                p_yes=round(p_yes, 4),
                confidence=round(conf, 4),
                status=status,
                verified=verified_flag,
                needs_human_review=needs_review,
                page_readable_p=round(page_readable_p, 4),
                reason=reason,
            )
        )

    page_summary = PageVerificationSummary(
        page=page_number,
        question_count=len(constraints),
        page_readable_p=round(page_readable_p, 4),
        verifier_model=used_model,
        fallback_used=fallback_used,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return verifications, page_summary


def verify_constraints(
    constraints: list[Constraint],
    *,
    pdf_path: str | Path | None = None,
    page_images: dict[int, bytes] | None = None,
    page_renderer: Callable[[int], bytes] | None = None,
    mime_type: str = "image/png",
    threshold: float | None = None,
    readability_threshold: float | None = None,
    dpi: int = 150,
    resolution: str = "high",
    client: genai.Client | None = None,
    allow_fallback: bool = True,
) -> VerificationReport:
    """Verify a list of constraints by batching ONE Decisions API call per cited page."""
    cfg = load_verifier_config()
    thresh = float(threshold if threshold is not None else cfg["confidence_threshold"])
    read_thresh = float(
        readability_threshold if readability_threshold is not None else cfg["readability_threshold"]
    )

    if not constraints:
        # Even with zero constraints, if a single page image was provided (e.g. unreadable camera frame),
        # optionally check readability so caller knows if the photo was unreadable vs genuinely empty.
        pages: list[PageVerificationSummary] = []
        if page_images:
            api_client = client if client is not None else genai.Client(http_options={"api_version": "v1beta"})
            for pg, img_bytes in sorted(page_images.items()):
                _, summary = verify_page_constraints(
                    page_number=pg,
                    page_image_bytes=img_bytes,
                    constraints=[],
                    mime_type=mime_type,
                    threshold=thresh,
                    readability_threshold=read_thresh,
                    resolution=resolution,
                    client=api_client,
                    allow_fallback=allow_fallback,
                )
                pages.append(summary)
        return VerificationReport(
            verifier_model=pages[0].verifier_model if pages else cfg["model"],
            fallback_used=any(p.fallback_used for p in pages),
            threshold=thresh,
            readability_threshold=read_thresh,
            verified_count=0,
            abstained_count=0,
            rejected_count=0,
            uncheckable_count=0,
            items=[],
            pages=pages,
        )

    api_client = client if client is not None else genai.Client(http_options={"api_version": "v1beta"})

    by_page: dict[int, list[Constraint]] = defaultdict(list)
    for c in constraints:
        by_page[c.page].append(c)

    all_items: list[ConstraintVerification] = []
    page_summaries: list[PageVerificationSummary] = []

    for page_num in sorted(by_page.keys()):
        page_constraints = by_page[page_num]
        if page_images is not None and page_num in page_images:
            img_bytes = page_images[page_num]
        elif page_renderer is not None:
            img_bytes = page_renderer(page_num)
        elif pdf_path is not None:
            img_bytes = render_pdf_page_png(pdf_path, page_num, dpi=dpi)
        else:
            raise ValueError(
                f"No image source available for page {page_num}; supply pdf_path, page_images, or page_renderer."
            )

        items, summary = verify_page_constraints(
            page_number=page_num,
            page_image_bytes=img_bytes,
            constraints=page_constraints,
            mime_type=mime_type,
            threshold=thresh,
            readability_threshold=read_thresh,
            resolution=resolution,
            client=api_client,
            allow_fallback=allow_fallback,
        )
        all_items.extend(items)
        page_summaries.append(summary)

    verified_count = sum(1 for i in all_items if i.status == "verified")
    abstained_count = sum(1 for i in all_items if i.status == "abstained")
    rejected_count = sum(1 for i in all_items if i.status == "rejected")
    uncheckable_count = sum(1 for c in constraints if not c.checkable)
    fallback_used = any(p.fallback_used for p in page_summaries)
    used_model = page_summaries[0].verifier_model if page_summaries else cfg["model"]

    return VerificationReport(
        verifier_model=used_model,
        fallback_used=fallback_used,
        threshold=thresh,
        readability_threshold=read_thresh,
        verified_count=verified_count,
        abstained_count=abstained_count,
        rejected_count=rejected_count,
        uncheckable_count=uncheckable_count,
        items=all_items,
        pages=page_summaries,
    )
