"""Reader: extract typed interconnection constraints from PDFs, page images, and camera frames.

Uses `gemini-4-neon` (from `config/models.yaml`) via the Interactions API with
strict JSON Schema structured outputs (`response_format`), native PDF document
slicing (`processing.page_options`), and deterministic conflict detection.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

from reader.pdf_utils import get_pdf_page_count
from rules.schema import (
    CHECKABLE_CONSTRAINT_TYPES,
    Constraint,
    ConstraintType,
    canonicalize_entity,
)
from rules.units import UnitConversionError, normalize_constraint_limit, parse_window_range_s

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
MODELS_CONFIG_PATH = REPO_ROOT / "config" / "models.yaml"


class ExtractedConstraintItem(BaseModel):
    """Strict structured output item returned by Gemini 4 Neon."""

    constraint_type: Literal["ramp_rate", "max_step", "fluctuation_band", "dynamic_range", "other"] = Field(
        description=(
            "Category of large-load power-variation / interconnection requirement: "
            "'ramp_rate' (MW/s, MW/min, %/min, kW/s rate of change), "
            "'max_step' (maximum instantaneous or short-window MW/kW/% step change), "
            "'fluctuation_band' (periodic power oscillation / flicker / subsynchronous frequency-band limit or shaft torque pu limit), "
            "'dynamic_range' (maximum total MW/% operating swing or peak-to-peak excursion), "
            "or 'other' (any qualitative, procedural, ride-through, harmonic, reactive power, or non-numeric requirement)."
        )
    )
    value: float | None = Field(
        default=None,
        description="Explicit numeric limit stated on the page. Must be null if qualitative or no numeric value is given.",
    )
    unit: str = Field(
        default="",
        description=(
            "Exact numeric unit stated on the page, e.g. 'MW/min', 'MW/s', 'kW/s', '%/min', "
            "'% of rated load', 'MW', 'kW', 'pu'. Empty string if value is null."
        ),
    )
    page: int = Field(
        ge=1,
        description="1-indexed PDF page number where this limit appears (1 for a single page image or camera photo).",
    )
    snippet: str = Field(
        description="Exact verbatim quote (1-2 sentences or table cell) from the source page containing the requirement."
    )
    checkable: bool = Field(
        description=(
            "True ONLY if constraint_type is one of ramp_rate, max_step, fluctuation_band, dynamic_range "
            "AND value is an explicit finite number with a compatible power/ramp/torque unit. "
            "False for qualitative, procedural, voltage/frequency ride-through, harmonic THD, or ambiguous rules."
        )
    )
    window_s: float | None = Field(
        default=None,
        description=(
            "Optional averaging or step measurement window in seconds if explicitly stated "
            "(e.g. 60 for 1-minute window, 5 for 5-second interval). If a window range is stated "
            "(e.g. '4 to 6 second interval'), set window_s to the larger value (6.0) and note the range."
        ),
    )
    freq_min_hz: float | None = Field(
        default=None,
        description="Lower frequency bound in Hz for fluctuation_band constraints if explicitly stated.",
    )
    freq_max_hz: float | None = Field(
        default=None,
        description="Upper frequency bound in Hz for fluctuation_band constraints if explicitly stated.",
    )
    aggregation: Literal["peak_bin", "adjacent_pair_sum", "band_sum"] | None = Field(
        default=None,
        description=(
            "For fluctuation_band only, when the document states how bins are combined: "
            "'adjacent_pair_sum' if 'sum of any two adjacent' bins; "
            "'band_sum' if 'sum of all bins'; "
            "'peak_bin' if a single-bin / peak limit. Null if unspecified."
        ),
    )
    fft_window_s: float | None = Field(
        default=None,
        description="FFT analysis window in seconds if explicitly stated (e.g. 10 s FFT window).",
    )
    averaging_window_s: float | None = Field(
        default=None,
        description="Rolling averaging window in seconds for P0-pk,avg style limits if explicitly stated.",
    )
    entity: str = Field(
        default="",
        description=(
            "Explicit utility, ISO/RTO, or standards body this constraint belongs to "
            "(e.g. 'CAISO', 'ERCOT', 'LIPA', 'Southern Company', 'IESO', 'MISO', 'SPP', 'ATC', 'AESO'). "
            "In multi-utility comparative tables or industry-practice summaries, ALWAYS name the specific utility."
        ),
    )
    source_section: str = Field(
        default="",
        description="Section header or table title (e.g. 'Section 4.2', 'Table 4-2') if explicitly stated.",
    )
    notes: str = Field(
        default="",
        description="Brief factual context (e.g. applies to loads >= 75 MW, normal vs contingency, or window range). Never invent numbers.",
    )


class ExtractionSchema(BaseModel):
    """Top-level structured output payload from Gemini 4 Neon."""

    constraints: list[ExtractedConstraintItem] = Field(
        default_factory=list,
        description="All interconnection power-variation / dynamic-load constraints found. Empty list if none exist.",
    )
    document_summary: str = Field(
        default="",
        description="One-sentence factual summary of what this slice/image covers.",
    )
    unreadable: bool = Field(
        default=False,
        description="True ONLY if the image/page is too blurry, dark, cropped, or degraded to read reliably.",
    )


@dataclass
class ExtractionOutput:
    """Result of running the Reader on a document or image."""

    source_name: str
    model: str
    constraints: list[Constraint]
    total_pages: int
    unreadable: bool = False
    summary: str = ""
    raw_responses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "model": self.model,
            "total_pages": self.total_pages,
            "unreadable": self.unreadable,
            "summary": self.summary,
            "constraints": [c.to_dict() for c in self.constraints],
            "raw_responses": self.raw_responses,
        }


READER_SYSTEM_INSTRUCTION = """You are GridSafe's utility interconnection rulebook extractor.
Your ONLY job is to extract explicit technical requirements governing large-load power behavior
(ramp rates, maximum step changes, periodic fluctuation/oscillation bands, dynamic operating range,
and other notable interconnection obligations) from utility documents, scanned pages, or camera photos.

Strict rules:
1. ZERO FABRICATION: Extract ONLY limits explicitly written on the page(s). If a page contains no
   numeric power-variation or interconnection limits (e.g., title page, table of contents, executive
   summary prose, bibliography, or unrelated text), return an empty `constraints` list `[]`.
2. EXACT CITATIONS: Always report the exact 1-indexed PDF page number and a verbatim `snippet` quote.
3. CHECKABLE VS UNCHECKABLE:
   - Set `checkable=true` ONLY when `constraint_type` is `ramp_rate`, `max_step`, `fluctuation_band`,
     or `dynamic_range` AND there is an explicit numeric `value` and unambiguous unit (`MW/s`, `MW/min`,
     `kW/s`, `kW/min`, `%/min`, `%/s`, `MW`, `kW`, `% of rated load`, or `pu` torque).
   - Keep qualitative rules, ride-through curves, harmonic/flicker IEEE 519 references, power factor
     requirements, or rules without a clear numeric MW/ramp/fluctuation value with `constraint_type="other"`
     and `checkable=false`. Do NOT force-fit voltage/frequency ride-through (e.g. 59.4 Hz, 0.9 pu voltage)
     or threshold definitions (e.g. "large load >= 75 MW") into `ramp_rate`/`max_step`/`dynamic_range`!
4. THRESHOLDS ARE NOT LIMITS: Applicability thresholds like "applies to facilities >= 75 MW" or
   "projects above 50 MW" define WHICH loads are regulated, NOT a dynamic_range or max_step limit.
   Do not emit applicability size thresholds as `max_step` or `dynamic_range`.
5. UNREADABLE INPUTS: If an image/photo is severely blurred, heavily cropped, or illegible such that
   numbers cannot be read reliably, set `unreadable=true` and do NOT guess numbers.
6. EXPLICIT ENTITY ATTRIBUTION: Always populate `entity` with the specific utility/ISO/RTO
   (e.g. CAISO, ERCOT, LIPA, Southern Company, IESO, MISO, SPP, ATC, AESO) that owns or proposes
   the requirement, especially in multi-utility comparison tables or industry-practice sections.
7. WINDOW RANGES: If a step or ramp limit specifies a window range (e.g. "4 to 6 second interval"
   or "4–6 s"), set `window_s` to the larger value (e.g. 6.0) and record the stated range in `notes`.
"""


def load_reader_config() -> tuple[str, str]:
    """Return (model_id, thinking_level) for Reader from config/models.yaml."""
    if MODELS_CONFIG_PATH.exists():
        with MODELS_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        reader_cfg = cfg.get("reader", {})
        return (
            str(reader_cfg.get("model", "gemini-4-neon")),
            str(reader_cfg.get("thinking_level", "high")),
        )
    return "gemini-4-neon", "high"


def _get_client(client: genai.Client | None = None) -> genai.Client:
    if client is not None:
        return client
    return genai.Client(http_options={"api_version": "v1beta"})


def _normalize_extracted_item(
    item: ExtractedConstraintItem,
    idx: int,
    rated_mw: float,
    page_offset: int = 0,
    max_page: int | None = None,
    doc_context: str = "",
) -> Constraint:
    """Convert raw Pydantic extraction item into a validated GridSafe Constraint."""
    page = item.page
    # If Gemini returned a chunk-relative page index (1..chunk_len) when page_offset > 0, keep exact PDF page
    if page_offset > 0 and page <= page_offset:
        page = page_offset + page
    if max_page is not None:
        page = min(max(1, page), max_page)

    ctype = item.constraint_type
    checkable = bool(item.checkable)
    notes = item.notes.strip()
    assumptions: list[str] = []

    # Enforce strict checkability via deterministic unit normalizer
    if ctype not in CHECKABLE_CONSTRAINT_TYPES or item.value is None or not item.unit.strip():
        checkable = False
    else:
        probe = Constraint(
            constraint_type=ctype,
            value=item.value,
            unit=item.unit.strip(),
            page=page,
            snippet=item.snippet.strip(),
            checkable=True,
        )
        try:
            normalize_constraint_limit(probe, rated_mw=rated_mw)
        except UnitConversionError as exc:
            checkable = False
            notes = f"{notes} [Marked uncheckable: {exc}]".strip()

    # Clean invalid frequency bounds if model returned inverted bounds
    f_min = item.freq_min_hz
    f_max = item.freq_max_hz
    if f_min is not None and f_min < 0:
        f_min = None
    if f_max is not None and f_max < 0:
        f_max = None
    if f_min is not None and f_max is not None and f_max < f_min:
        f_min, f_max = f_max, f_min

    win_s = item.window_s if (item.window_s is not None and item.window_s >= 0) else None
    # Handle window ranges like "4 to 6 second interval" -> use larger value and note the range
    if checkable and ctype in (
        ConstraintType.MAX_STEP.value,
        ConstraintType.RAMP_RATE.value,
        ConstraintType.DYNAMIC_RANGE.value,
    ):
        win_range = parse_window_range_s(f"{item.snippet} {notes}")
        if win_range is not None:
            lo_s, hi_s = win_range
            if win_s is None or (lo_s - 1e-9 <= win_s < hi_s - 1e-9):
                win_s = hi_s
            range_msg = f"Window range {lo_s:g}-{hi_s:g} s stated in source; using upper bound {hi_s:g} s."
            assumptions.append(range_msg)
            if range_msg not in notes:
                notes = f"{notes} ({range_msg})".strip()

    entity = canonicalize_entity(
        entity=item.entity,
        source_section=item.source_section,
        snippet=item.snippet,
        doc_context=doc_context,
    )

    aggregation = getattr(item, "aggregation", None) or "peak_bin"
    fft_win = getattr(item, "fft_window_s", None)
    avg_win = getattr(item, "averaging_window_s", None)
    return Constraint(
        id=f"{ctype}_{idx}",
        constraint_type=ctype,
        value=item.value,
        unit=item.unit.strip(),
        page=page,
        snippet=item.snippet.strip(),
        checkable=checkable,
        confidence=None,
        entity=entity,
        window_s=win_s,
        freq_min_hz=f_min,
        freq_max_hz=f_max,
        aggregation=str(aggregation),
        fft_window_s=float(fft_win) if fft_win is not None and float(fft_win) > 0 else None,
        averaging_window_s=float(avg_win) if avg_win is not None and float(avg_win) > 0 else None,
        source_section=item.source_section.strip(),
        notes=notes,
        assumptions=assumptions,
    )


def _deduplicate_constraints(
    constraints: list[Constraint],
    rated_mw: float = 100.0,
) -> list[Constraint]:
    """Deduplicate identical constraints including limits restated across multiple pages.

    For checkable numeric constraints of the same entity, type, canonical unit,
    window, and frequency band whose normalized values match within 1%, merge
    duplicates into a single entry (keeping the earliest page and noting any
    additional pages where the same limit was restated).
    """
    unique: list[Constraint] = []
    checkable_index: dict[tuple[Any, ...], int] = {}
    seen_other: set[tuple[Any, ...]] = set()

    for c in constraints:
        ent_key = c.entity.lower().strip()
        if c.checkable and c.value is not None:
            try:
                norm_val, canon_unit = normalize_constraint_limit(c, rated_mw=rated_mw)
                val_bucket = round(norm_val, 4)
            except UnitConversionError:
                canon_unit = c.unit.lower().strip()
                val_bucket = round(float(c.value), 4)

            ckey = (
                ent_key,
                c.constraint_type,
                val_bucket,
                canon_unit,
                round(float(c.window_s), 6) if c.window_s is not None else None,
                round(float(c.freq_min_hz), 6) if c.freq_min_hz is not None else None,
                round(float(c.freq_max_hz), 6) if c.freq_max_hz is not None else None,
            )
            if ckey in checkable_index:
                existing = unique[checkable_index[ckey]]
                if c.page != existing.page:
                    also = f"Also stated on p.{c.page}."
                    if also not in existing.notes:
                        existing.notes = f"{existing.notes} {also}".strip()
                if (c.confidence or 0.0) > (existing.confidence or 0.0):
                    existing.confidence = c.confidence
                    if c.verified is not None:
                        existing.verified = c.verified
                continue
            checkable_index[ckey] = len(unique)
            unique.append(c)
        else:
            okey = (
                ent_key,
                c.constraint_type,
                c.value,
                c.unit.lower().strip(),
                c.source_section.lower().strip(),
                c.snippet[:80].lower().strip(),
            )
            if okey not in seen_other:
                seen_other.add(okey)
                unique.append(c)

    for idx, c in enumerate(unique):
        c.id = f"{c.constraint_type}_{idx}"
    return unique


def detect_conflicts(
    constraints: list[Constraint],
    rated_mw: float = 100.0,
    rel_tol: float = 0.02,
    group_by_entity: bool = True,
    group_by_section: bool = False,
) -> list[Constraint]:
    """Flag checkable constraints of the same entity/type/band with conflicting numeric limits.

    Conflicts are detected ONLY within the same entity (`group_by_entity=True` by
    default). Limits attributed to different utilities (e.g. ERCOT vs AESO in a
    comparative table) are never flagged as conflicts against each other.
    """
    checkable_indices: list[tuple[int, float, str, str]] = []
    for idx, c in enumerate(constraints):
        c.conflict_flag = False
        if not c.checkable or c.value is None:
            continue
        try:
            norm_val, canon_unit = normalize_constraint_limit(c, rated_mw=rated_mw)
        except UnitConversionError:
            continue
        ent = canonicalize_entity(c.entity, c.source_section, c.snippet)
        checkable_indices.append((idx, norm_val, canon_unit, ent.lower()))

    def _bands_overlap(a: Constraint, b: Constraint) -> bool:
        if a.constraint_type != ConstraintType.FLUCTUATION_BAND.value:
            return True
        a_lo = 0.0 if a.freq_min_hz is None else a.freq_min_hz
        a_hi = float("inf") if a.freq_max_hz is None else a.freq_max_hz
        b_lo = 0.0 if b.freq_min_hz is None else b.freq_min_hz
        b_hi = float("inf") if b.freq_max_hz is None else b.freq_max_hz
        if a_lo == a_hi and b_lo == b_hi:
            return abs(a_lo - b_lo) <= 1e-9
        return max(a_lo, b_lo) < min(a_hi, b_hi) - 1e-9

    for i_pos in range(len(checkable_indices)):
        idx_a, val_a, unit_a, ent_a = checkable_indices[i_pos]
        ca = constraints[idx_a]
        for j_pos in range(i_pos + 1, len(checkable_indices)):
            idx_b, val_b, unit_b, ent_b = checkable_indices[j_pos]
            cb = constraints[idx_b]

            if ca.constraint_type != cb.constraint_type or unit_a != unit_b:
                continue
            if ca.window_s != cb.window_s:
                continue
            if not _bands_overlap(ca, cb):
                continue
            if group_by_entity and (ent_a or ent_b) and ent_a != ent_b:
                continue
            if group_by_section and ca.source_section.lower() != cb.source_section.lower():
                continue

            denom = max(abs(val_a), abs(val_b), 1e-9)
            if abs(val_a - val_b) / denom > rel_tol:
                ca.conflict_flag = True
                cb.conflict_flag = True
                msg_a = f"Conflicts with {cb.id} (p.{cb.page}: {cb.value} {cb.unit} = {val_b:.4g} {unit_b})"
                msg_b = f"Conflicts with {ca.id} (p.{ca.page}: {ca.value} {ca.unit} = {val_a:.4g} {unit_a})"
                if msg_a not in ca.notes:
                    ca.notes = f"{ca.notes} [CONFLICT: {msg_a}]".strip()
                if msg_b not in cb.notes:
                    cb.notes = f"{cb.notes} [CONFLICT: {msg_b}]".strip()

    return constraints


def extract_from_image_bytes(
    image_bytes: bytes,
    mime_type: str = "image/png",
    *,
    source_name: str = "camera_frame",
    page_number: int = 1,
    rated_mw: float = 100.0,
    model: str | None = None,
    thinking_level: str | None = None,
    resolution: str = "high",
    client: genai.Client | None = None,
) -> ExtractionOutput:
    """Extract constraints from a single page image or camera photo using Gemini 4 Neon."""
    default_model, default_thinking = load_reader_config()
    model_id = model or default_model
    t_level = thinking_level or default_thinking
    api_client = _get_client(client)

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        f"Analyze this single page image (page {page_number}) from a utility interconnection rulebook "
        f"or handwritten rule sheet. Extract all explicit large-load power-variation constraints "
        f"(ramp_rate, max_step, fluctuation_band, dynamic_range) and keep qualitative/non-numeric "
        f"interconnection obligations with checkable=false. Use page={page_number} for every item. "
        f"If the page has no limits, return an empty list. If the image is unreadable, set unreadable=true."
    )

    interaction = api_client.interactions.create(
        model=model_id,
        system_instruction=READER_SYSTEM_INSTRUCTION,
        input=[
            {
                "type": "image",
                "data": b64,
                "mime_type": mime_type,
                "resolution": resolution,
            },
            {"type": "text", "text": prompt},
        ],
        generation_config={"thinking_level": t_level},
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": ExtractionSchema.model_json_schema(),
        },
    )

    parsed = ExtractionSchema.model_validate_json(interaction.output_text)
    constraints = [
        _normalize_extracted_item(
            item, idx, rated_mw=rated_mw, page_offset=0, max_page=page_number, doc_context=source_name
        )
        for idx, item in enumerate(parsed.constraints)
    ]
    for c in constraints:
        c.page = page_number
    constraints = _deduplicate_constraints(constraints, rated_mw=rated_mw)
    detect_conflicts(constraints, rated_mw=rated_mw, group_by_entity=True)

    return ExtractionOutput(
        source_name=source_name,
        model=model_id,
        constraints=constraints,
        total_pages=1,
        unreadable=parsed.unreadable,
        summary=parsed.document_summary,
        raw_responses=[{"page": page_number, "interaction_id": interaction.id, "parsed": parsed.model_dump()}],
    )


def extract_from_pdf(
    pdf_path: str | Path,
    *,
    rated_mw: float = 100.0,
    chunk_pages: int = 12,
    resolution: str = "medium",
    model: str | None = None,
    thinking_level: str | None = None,
    client: genai.Client | None = None,
    max_pages: int | None = None,
    group_conflicts_by_section: bool = False,
) -> ExtractionOutput:
    """Extract constraints from a PDF using native Gemini document slicing (`processing.page_options`)."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    default_model, default_thinking = load_reader_config()
    model_id = model or default_model
    t_level = thinking_level or default_thinking
    api_client = _get_client(client)

    total_pages = get_pdf_page_count(path)
    effective_pages = min(total_pages, max_pages) if max_pages is not None else total_pages

    pdf_bytes = path.read_bytes()
    use_files_api = len(pdf_bytes) > 15 * 1024 * 1024
    uploaded_file = None
    pdf_b64 = None
    if use_files_api:
        uploaded_file = api_client.files.upload(file=str(path))
    else:
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    all_constraints: list[Constraint] = []
    raw_responses: list[dict[str, Any]] = []
    summaries: list[str] = []
    any_unreadable = False

    try:
        for start_page in range(1, effective_pages + 1, chunk_pages):
            end_page = min(start_page + chunk_pages - 1, effective_pages)

            doc_block: dict[str, Any] = {
                "type": "document",
                "mime_type": "application/pdf",
                "resolution": resolution,
                "processing": {
                    "type": "static",
                    "page_options": {
                        "start_page": start_page,
                        "end_page": end_page,
                        "content_layers": ["page_image", "page_text"],
                    },
                },
            }
            if uploaded_file is not None:
                doc_block["uri"] = uploaded_file.uri
            else:
                doc_block["data"] = pdf_b64

            prompt = (
                f"You are inspecting pages {start_page} through {end_page} (1-indexed PDF page numbers) "
                f"of `{path.name}` (total {total_pages} pages).\n"
                f"Extract every explicit large-load power-variation or interconnection requirement in this page range.\n"
                f"IMPORTANT: Report the ACTUAL 1-indexed PDF page number (between {start_page} and {end_page}), "
                f"NOT a chunk-relative page number.\n"
                f"Keep qualitative/non-numeric or voltage/frequency ride-through obligations as constraint_type='other' "
                f"with checkable=false. Set checkable=true ONLY for explicit numeric ramp_rate, max_step, "
                f"fluctuation_band, or dynamic_range limits with valid units. Return empty list if none appear."
            )

            interaction = api_client.interactions.create(
                model=model_id,
                system_instruction=READER_SYSTEM_INSTRUCTION,
                input=[doc_block, {"type": "text", "text": prompt}],
                generation_config={"thinking_level": t_level},
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": ExtractionSchema.model_json_schema(),
                },
            )

            parsed = ExtractionSchema.model_validate_json(interaction.output_text)
            if parsed.document_summary:
                summaries.append(f"pp.{start_page}-{end_page}: {parsed.document_summary}")
            if parsed.unreadable:
                any_unreadable = True

            raw_responses.append(
                {
                    "start_page": start_page,
                    "end_page": end_page,
                    "interaction_id": interaction.id,
                    "parsed": parsed.model_dump(),
                }
            )

            for item in parsed.constraints:
                # If model returned chunk-relative page (e.g., 1..chunk_pages when start_page > 1), map cleanly
                raw_page = item.page
                if start_page > 1 and raw_page < start_page:
                    mapped_page = start_page + raw_page - 1
                else:
                    mapped_page = raw_page
                mapped_page = min(max(start_page, mapped_page), end_page)

                item_copy = item.model_copy(update={"page": mapped_page})
                c = _normalize_extracted_item(
                    item_copy,
                    idx=len(all_constraints),
                    rated_mw=rated_mw,
                    page_offset=0,
                    max_page=total_pages,
                    doc_context=path.name,
                )
                all_constraints.append(c)
    finally:
        if uploaded_file is not None:
            try:
                api_client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

    deduped = _deduplicate_constraints(all_constraints, rated_mw=rated_mw)
    detect_conflicts(
        deduped,
        rated_mw=rated_mw,
        group_by_entity=True,
        group_by_section=group_conflicts_by_section,
    )

    return ExtractionOutput(
        source_name=path.name,
        model=model_id,
        constraints=deduped,
        total_pages=total_pages,
        unreadable=any_unreadable,
        summary=" | ".join(summaries),
        raw_responses=raw_responses,
    )


def extract_constraints(
    source: str | Path | bytes,
    *,
    mime_type: str | None = None,
    source_name: str | None = None,
    rated_mw: float = 100.0,
    model: str | None = None,
    thinking_level: str | None = None,
    client: genai.Client | None = None,
) -> ExtractionOutput:
    """Unified entrypoint accepting a PDF path, image path, or raw image/PDF bytes."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        suffix = path.suffix.lower()
        if suffix == ".pdf" or mime_type == "application/pdf":
            return extract_from_pdf(
                path,
                rated_mw=rated_mw,
                model=model,
                thinking_level=thinking_level,
                client=client,
            )
        img_mime = mime_type or (
            "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/webp" if suffix == ".webp" else "image/png"
        )
        return extract_from_image_bytes(
            path.read_bytes(),
            mime_type=img_mime,
            source_name=source_name or path.name,
            rated_mw=rated_mw,
            model=model,
            thinking_level=thinking_level,
            client=client,
        )

    raw_bytes = bytes(source)
    if mime_type == "application/pdf" or raw_bytes[:5] == b"%PDF-":
        tmp_path = REPO_ROOT / "eval" / "tmp" / (source_name or "uploaded.pdf")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(raw_bytes)
        try:
            out = extract_from_pdf(
                tmp_path,
                rated_mw=rated_mw,
                model=model,
                thinking_level=thinking_level,
                client=client,
            )
            if source_name:
                out.source_name = source_name
            return out
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return extract_from_image_bytes(
        raw_bytes,
        mime_type=mime_type or "image/png",
        source_name=source_name or "camera_frame",
        rated_mw=rated_mw,
        model=model,
        thinking_level=thinking_level,
        client=client,
    )
