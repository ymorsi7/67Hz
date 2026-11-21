import { useEffect, useRef, useState } from "react"

/** Compact shaft that twists in sync with the power chart animation. */
export function ShaftAnim({
  gridSafeOn,
  stressBeforePu,
  stressAfterPu,
  twistsBefore,
  twistsAfter,
  timesLower,
  width = 900,
  height = 120,
}: {
  gridSafeOn: boolean
  stressBeforePu: number
  stressAfterPu: number
  twistsBefore?: number[]
  twistsAfter?: number[]
  timesLower?: number | null
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [progress, setProgress] = useState(0)
  const masses = ["HP", "IP", "LPA", "LPB", "GEN", "EXC"]

  useEffect(() => {
    setProgress(0)
    const start = performance.now()
    const dur = 2200
    let id = 0
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur)
      setProgress(p)
      if (p < 1) id = requestAnimationFrame(tick)
    }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [gridSafeOn, stressBeforePu, stressAfterPu])

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)
    const bg = ctx.createLinearGradient(0, 0, 0, h)
    bg.addColorStop(0, "#0a0e14")
    bg.addColorStop(1, "#121820")
    ctx.fillStyle = bg
    ctx.fillRect(0, 0, w, h)

    const stress = gridSafeOn ? stressAfterPu : stressBeforePu
    const twists = gridSafeOn ? twistsAfter ?? [] : twistsBefore ?? []
    // Pulse magnitude follows the animation — jumps early when OFF
    const pulse = gridSafeOn
      ? Math.min(1.2, Math.abs(stress) * 6) * (0.35 + 0.65 * progress)
      : Math.min(1.8, Math.abs(stress) * 8) * (0.5 + 0.5 * Math.sin(progress * Math.PI * 6) ** 2)
    const redness = Math.min(1, pulse / 1.2)

    const cy = h * 0.5
    const left = 48
    const right = w - 36
    const n = masses.length

    ctx.strokeStyle = `rgba(${Math.round(90 + redness * 140)},${Math.round(140 - redness * 80)},${Math.round(150 - redness * 100)},0.85)`
    ctx.lineWidth = 5
    ctx.beginPath()
    ctx.moveTo(left, cy)
    ctx.lineTo(right, cy)
    ctx.stroke()

    for (let i = 0; i < n; i++) {
      const x = left + (i / (n - 1)) * (right - left)
      const twist = twists[i] ?? 0
      const phase = Math.sin(twist * 40 + pulse + progress * 8) * pulse * 10
      const y = cy + phase
      const r = 11 + (i === 4 ? 4 : 0)
      const rr = Math.round(70 + redness * 170)
      const g = Math.round(170 - redness * 130)
      const b = Math.round(160 - redness * 120)
      ctx.beginPath()
      ctx.ellipse(x, y, r * 0.55, r, 0, 0, Math.PI * 2)
      ctx.fillStyle = `rgb(${rr},${g},${b})`
      ctx.fill()
      ctx.fillStyle = "rgba(220,230,240,0.75)"
      ctx.font = "10px 'Geist Mono Variable', monospace"
      ctx.textAlign = "center"
      ctx.fillText(masses[i]!, x, y + r + 12)
    }

    ctx.textAlign = "left"
    ctx.fillStyle = "rgba(200,210,220,0.55)"
    ctx.font = "11px 'Geist Mono Variable', monospace"
    ctx.fillText("simulated, visual scale", 12, 16)
  }, [
    gridSafeOn,
    stressBeforePu,
    stressAfterPu,
    twistsBefore,
    twistsAfter,
    timesLower,
    progress,
    width,
    height,
  ])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full border border-border/50"
      aria-label="Generator shaft stress, simulated visual scale"
    />
  )
}
