import { useEffect, useRef } from "react"

/** Canvas 2D shaft that twists/reddens with torque. Labeled visual scale. */
export function ShaftCanvas({
  torquePu,
  controllerOn,
  width = 640,
  height = 160,
}: {
  torquePu: number
  controllerOn: boolean
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return

    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)

    // Atmosphere
    const bg = ctx.createLinearGradient(0, 0, w, h)
    bg.addColorStop(0, "#0c1018")
    bg.addColorStop(1, "#121820")
    ctx.fillStyle = bg
    ctx.fillRect(0, 0, w, h)

    const mag = Math.min(1.5, Math.abs(torquePu))
    const calm = controllerOn ? 0.35 : 1.0
    const twist = mag * calm * 0.55
    const redness = Math.min(1, mag * calm)

    const cx = w * 0.5
    const cy = h * 0.55
    const len = w * 0.72
    const nSeg = 28

    // Masses along shaft
    for (let i = 0; i <= nSeg; i++) {
      const t = i / nSeg
      const x = cx - len / 2 + t * len
      const phase = Math.sin(t * Math.PI * 3 + twist * 4) * twist * 18
      const y = cy + phase
      const r = 7 + Math.sin(t * Math.PI) * 4

      const g = Math.round(180 - redness * 140)
      const b = Math.round(160 - redness * 140)
      const rr = Math.round(80 + redness * 160)
      ctx.beginPath()
      ctx.arc(x, y, r, 0, Math.PI * 2)
      ctx.fillStyle = `rgb(${rr},${g},${b})`
      ctx.fill()

      if (i > 0) {
        const t0 = (i - 1) / nSeg
        const x0 = cx - len / 2 + t0 * len
        const y0 = cy + Math.sin(t0 * Math.PI * 3 + twist * 4) * twist * 18
        ctx.beginPath()
        ctx.moveTo(x0, y0)
        ctx.lineTo(x, y)
        ctx.strokeStyle = `rgba(${rr},${g},${b},0.55)`
        ctx.lineWidth = 3
        ctx.stroke()
      }
    }

    ctx.fillStyle = "rgba(200,210,220,0.55)"
    ctx.font = "12px 'Geist Mono Variable', monospace"
    ctx.fillText("visual scale · simulated shaft torque", 16, 22)
    ctx.fillText(
      `τ=${torquePu.toFixed(3)} pu · ${controllerOn ? "controller ON" : "uncontrolled"}`,
      16,
      h - 14,
    )
  }, [torquePu, controllerOn, width, height])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full max-w-full rounded-sm border border-border/50 bg-[#0c1018]"
      aria-label="Visual scale shaft torque"
    />
  )
}

export function SpectrumCanvas({
  freqs,
  before,
  after,
  bands,
  width = 640,
  height = 180,
}: {
  freqs: number[]
  before: number[]
  after: number[]
  bands: Array<{ freq_min_hz: number; freq_max_hz: number }>
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = "#0c1018"
    ctx.fillRect(0, 0, w, h)

    const pad = { l: 40, r: 12, t: 28, b: 28 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b
    const fMax = Math.max(...freqs, 1)
    const yMax = Math.max(...before, ...after, 1e-6) * 1.15

    // Forbidden bands
    for (const b of bands) {
      const x0 = pad.l + (b.freq_min_hz / fMax) * plotW
      const x1 = pad.l + (b.freq_max_hz / fMax) * plotW
      ctx.fillStyle = "rgba(220, 60, 50, 0.18)"
      ctx.fillRect(x0, pad.t, Math.max(2, x1 - x0), plotH)
    }

    const draw = (ys: number[], color: string) => {
      ctx.beginPath()
      ctx.strokeStyle = color
      ctx.lineWidth = 1.5
      for (let i = 0; i < freqs.length; i++) {
        const x = pad.l + (freqs[i]! / fMax) * plotW
        const y = pad.t + plotH - (ys[i]! / yMax) * plotH
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
    draw(before, "rgba(220,80,70,0.85)")
    draw(after, "rgba(60,200,130,0.9)")

    ctx.fillStyle = "rgba(200,210,220,0.55)"
    ctx.font = "12px 'Geist Mono Variable', monospace"
    ctx.fillText("spectrum · forbidden bands shaded · simulated", 16, 18)
    ctx.fillText("0 Hz", pad.l, h - 8)
    ctx.fillText(`${fMax.toFixed(0)} Hz`, w - 48, h - 8)
  }, [freqs, before, after, bands, width, height])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full max-w-full rounded-sm border border-border/50"
      aria-label="Torque spectrum with forbidden bands"
    />
  )
}

export function PowerCanvas({
  t,
  raw,
  before,
  after,
  width = 640,
  height = 160,
}: {
  t: number[]
  raw: number[]
  before: number[]
  after: number[]
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const w = canvas.width
    const h = canvas.height
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = "#0c1018"
    ctx.fillRect(0, 0, w, h)

    const pad = { l: 12, r: 12, t: 28, b: 16 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b
    const yMax = Math.max(...raw, ...before, ...after, 1) * 1.05
    const tMax = Math.max(...t, 1)

    const draw = (ys: number[], color: string, widthPx = 1.5) => {
      ctx.beginPath()
      ctx.strokeStyle = color
      ctx.lineWidth = widthPx
      for (let i = 0; i < t.length; i++) {
        const x = pad.l + (t[i]! / tMax) * plotW
        const y = pad.t + plotH - (ys[i]! / yMax) * plotH
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
    draw(raw, "rgba(160,170,190,0.45)", 1)
    draw(before, "rgba(220,80,70,0.8)")
    draw(after, "rgba(60,200,130,0.95)", 2)

    ctx.fillStyle = "rgba(200,210,220,0.55)"
    ctx.font = "12px 'Geist Mono Variable', monospace"
    ctx.fillText("grid power before / after · simulated", 16, 18)
  }, [t, raw, before, after, width, height])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full max-w-full rounded-sm border border-border/50"
      aria-label="Power before and after"
    />
  )
}
