import { useEffect, useRef, useState } from "react"

/** Animated left-to-right power chart. Axes in MW and seconds. */
export function PowerChart({
  t,
  without,
  withCtrl,
  gridSafeOn,
  width = 900,
  height = 220,
}: {
  t: number[]
  without: number[]
  withCtrl: number[]
  gridSafeOn: boolean
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [progress, setProgress] = useState(0)
  const animRef = useRef<number | null>(null)

  useEffect(() => {
    setProgress(0)
    if (animRef.current) cancelAnimationFrame(animRef.current)
    const start = performance.now()
    const dur = 2200
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur)
      setProgress(p)
      if (p < 1) animRef.current = requestAnimationFrame(tick)
    }
    animRef.current = requestAnimationFrame(tick)
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current)
    }
  }, [gridSafeOn, t, without, withCtrl])

  useEffect(() => {
    const canvas = ref.current
    if (!canvas || !t.length) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = "#0a0e14"
    ctx.fillRect(0, 0, w, h)

    const pad = { l: 56, r: 18, t: 28, b: 36 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b
    const yMax = Math.max(...without, ...withCtrl, 1) * 1.08
    const tMax = Math.max(...t, 1)
    const nShow = Math.max(2, Math.floor(t.length * progress))

    ctx.strokeStyle = "rgba(255,255,255,0.15)"
    ctx.strokeRect(pad.l, pad.t, plotW, plotH)
    ctx.fillStyle = "rgba(200,210,220,0.7)"
    ctx.font = "12px 'Geist Mono Variable', monospace"
    ctx.fillText("MW", 12, pad.t + 8)
    ctx.fillText("0", pad.l - 22, pad.t + plotH + 4)
    ctx.fillText(yMax.toFixed(0), pad.l - 36, pad.t + 10)
    ctx.fillText("0 s", pad.l, h - 12)
    ctx.fillText(`${tMax.toFixed(0)} s`, w - 48, h - 12)

    const draw = (ys: number[], color: string, widthPx: number, upTo: number) => {
      ctx.beginPath()
      ctx.strokeStyle = color
      ctx.lineWidth = widthPx
      for (let i = 0; i < upTo; i++) {
        const x = pad.l + (t[i]! / tMax) * plotW
        const y = pad.t + plotH - (ys[i]! / yMax) * plotH
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }

    if (gridSafeOn) {
      draw(without, "rgba(220,80,70,0.28)", 1.4, nShow)
      draw(withCtrl, "rgba(60,200,130,0.95)", 2.6, nShow)
    } else {
      draw(without, "rgba(220,80,70,0.95)", 2.6, nShow)
    }
  }, [t, without, withCtrl, gridSafeOn, progress, width, height])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full border border-border/50"
      aria-label="Power the data center pulls from the grid"
    />
  )
}
