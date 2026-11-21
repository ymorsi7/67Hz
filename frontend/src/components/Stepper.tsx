import { motion } from "motion/react"

export type StepId = "Rulebook" | "Rules" | "Controller" | "Break"

export const STEPS: StepId[] = ["Rulebook", "Rules", "Controller", "Break"]

const LABELS: Record<StepId, string> = {
  Rulebook: "1 Rulebook",
  Rules: "2 Rules",
  Controller: "3 Controller",
  Break: "4 Break it",
}

export function Stepper({
  current,
  completed,
  onJump,
}: {
  current: StepId
  completed: Set<StepId>
  onJump?: (step: StepId) => void
}) {
  const currentIdx = STEPS.indexOf(current)

  return (
    <nav className="flex items-stretch gap-1 w-full" aria-label="Flow steps">
      {STEPS.map((step, i) => {
        const isCurrent = step === current
        const isDone = completed.has(step) || i < currentIdx
        return (
          <motion.button
            key={step}
            type="button"
            layout
            onClick={() => onJump?.(step)}
            className={`relative font-mono text-sm transition-colors border px-3 py-2.5 text-left ${
              isCurrent
                ? "flex-[1.4] border-emerald-500/70 bg-emerald-500/10 text-emerald-200"
                : isDone
                  ? "flex-1 border-border/60 bg-card/40 text-muted-foreground hover:text-foreground"
                  : "flex-1 border-border/40 bg-transparent text-muted-foreground/60"
            }`}
          >
            <span className={`block ${isCurrent ? "text-base font-semibold" : "text-sm"}`}>
              {LABELS[step]}
            </span>
          </motion.button>
        )
      })}
    </nav>
  )
}
