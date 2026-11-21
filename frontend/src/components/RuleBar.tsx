/** One rule bar: plain sentence, limit tick at 100%, measured fill. */

export type RuleBarRow = {
  plain: string
  used_pct_of_limit?: number | null
  cannot_check_reason?: string | null
  passed?: boolean | null
}

export function RuleBar({
  plain,
  usedPct,
  cannotCheck,
  mode,
}: {
  plain: string
  usedPct: number | null | undefined
  cannotCheck?: string | null
  /** off = uncontrolled (red past limit); on = controlled */
  mode: "off" | "on" | "attack"
}) {
  const used = typeof usedPct === "number" ? usedPct : null
  const over = used != null && used > 100
  const timesOver = used != null && used > 100 ? used / 100 : null
  // Bar scale: show up to 160% so overshoot is visible; tick at 100%
  const scaleMax = 160
  const fillPct = used != null ? Math.min(scaleMax, Math.max(0, used)) : 0
  const tickLeft = (100 / scaleMax) * 100

  let caption = "—"
  if (cannotCheck) caption = cannotCheck
  else if (used == null) caption = "No measurement yet"
  else if (mode === "off" || (mode === "attack" && over)) {
    caption =
      timesOver != null
        ? `${timesOver.toFixed(1).replace(/\.0$/, "")} times over the limit`
        : `uses ${used}% of the limit`
  } else {
    caption = `uses ${used}% of the limit`
  }

  const barColor =
    cannotCheck || used == null
      ? "bg-muted-foreground/40"
      : over
        ? "bg-red-500"
        : "bg-emerald-500"

  return (
    <div className="border border-border/40 px-3 py-2.5">
      <div className="text-sm sm:text-base mb-2 leading-snug">{plain}</div>
      {cannotCheck ? (
        <p className="text-xs text-amber-200/90">{cannotCheck}</p>
      ) : (
        <>
          <div className="h-3 bg-muted/40 relative overflow-visible">
            <div
              className={`h-full transition-[width] duration-500 ${barColor}`}
              style={{ width: `${(fillPct / scaleMax) * 100}%` }}
            />
            {/* Limit tick */}
            <div
              className="absolute top-[-3px] bottom-[-3px] w-0.5 bg-white/90"
              style={{ left: `${tickLeft}%` }}
              title="Limit"
            />
          </div>
          <div
            className={`text-xs font-mono mt-1.5 ${
              over ? "text-red-300" : "text-emerald-300/90"
            }`}
          >
            {caption}
          </div>
        </>
      )}
    </div>
  )
}
