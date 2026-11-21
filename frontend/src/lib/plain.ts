import type { ConstraintRow } from "@/lib/api"

/** Plain-English rule sentence from typed fields (templates only — no model). */
export function plainRule(c: ConstraintRow): string {
  const v = c.value
  const unit = c.unit || ""
  if (c.constraint_type === "ramp_rate" && v != null) {
    return `Power may not change faster than ${v} ${unit}.`
  }
  if (c.constraint_type === "max_step" && v != null) {
    const win = c.window_s != null ? String(c.window_s) : "a few"
    return `Power may not jump more than ${v} MW within ${win} seconds.`
  }
  if (c.constraint_type === "fluctuation_band" && v != null) {
    const u = unit.toLowerCase()
    if (u === "pu" || u.includes("p.u")) {
      return `Shaft stress from repeating swings must stay under ${v} pu.`
    }
    const lo = c.freq_min_hz != null ? String(c.freq_min_hz) : "?"
    const hi = c.freq_max_hz != null ? String(c.freq_max_hz) : "?"
    return `Repeating power swings between ${lo} and ${hi} Hz must stay under ${v} MW.`
  }
  if (c.constraint_type === "dynamic_range" && v != null) {
    return `Power must stay within a ${v} MW range.`
  }
  return c.snippet ? `Other requirement: ${c.snippet.slice(0, 120)}` : "Other requirement on this page."
}

export function confirmBadge(c: ConstraintRow): { tone: "ok" | "warn" | "muted"; label: string } {
  if (!c.checkable) return { tone: "muted", label: "Cannot simulate" }
  if (c.human_confirmed) {
    return { tone: "ok", label: "Confirmed by a person, not by the Verifier" }
  }
  const conf = typeof c.confidence === "number" ? Math.round(c.confidence * 100) : null
  if (c.verified === true && !c.needs_human_review) {
    return {
      tone: "ok",
      label: conf != null ? `Confirmed on the page (${conf}%)` : "Confirmed on the page",
    }
  }
  return {
    tone: "warn",
    label: conf != null ? `Not sure, needs a human (${conf}%)` : "Not sure, needs a human",
  }
}
