import { motion } from "motion/react"
import type { StepId } from "@/lib/api"

const STEPS: StepId[] = ["Input", "Read", "Verify", "Evolve", "Attack", "Certify"]

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
        const collapsed = !isCurrent && isDone

        return (
          <motion.button
            key={step}
            type="button"
            layout
            onClick={() => onJump?.(step)}
            className={`relative font-mono text-sm transition-colors border px-3 py-2.5 text-left ${
              isCurrent
                ? "flex-[2] border-emerald-500/70 bg-emerald-500/10 text-emerald-200"
                : isDone
                  ? "flex-1 border-border/60 bg-card/40 text-muted-foreground hover:text-foreground"
                  : "flex-1 border-border/40 bg-transparent text-muted-foreground/60"
            }`}
          >
            <span className="block text-[10px] uppercase tracking-widest opacity-60">
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className={`block ${collapsed ? "truncate" : "text-base font-semibold"}`}>
              {step}
            </span>
          </motion.button>
        )
      })}
    </nav>
  )
}

export { STEPS }
