import { useEffect, useRef } from "react"

/** Six-mass turbine shaft (HP…EXC) that twists and reddens with torque. */
export function ShaftCanvas({
  controllerOn,
  stressBeforePu,
  stressAfterPu,
  twistsBefore,
  twistsAfter,
  timesLower,
  width = 720,
  height = 200,
}: {
  controllerOn: boolean
  stressBeforePu: number
  stressAfterPu: number
  twistsBefore?: number[]
  twistsAfter?: number[]
  timesLower?: number | null
  width?: number
  height?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const masses = ["HP", "IP", "LPA", "LPB", "GEN", "EXC"]

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

    const stress = controllerOn ? stressAfterPu : stressBeforePu
    const twists = controllerOn ? twistsAfter ?? [] : twistsBefore ?? []
    const mag = Math.min(1.8, Math.abs(stress) * 8)
    const redness = Math.min(1, mag / 1.2)

    const cy = h * 0.55
    const left = 56
    const right = w - 40
    const n = masses.length

    // Axle
    ctx.strokeStyle = `rgba(${Math.round(90 + redness * 140)},${Math.round(140 - redness * 80)},${Math.round(150 - redness * 100)},0.85)`
    ctx.lineWidth = 6
    ctx.beginPath()
    ctx.moveTo(left, cy)
    ctx.lineTo(right, cy)
    ctx.stroke()

    for (let i = 0; i < n; i++) {
      const t = i / (n - 1)
      const x = left + t * (right - left)
      const twist = twists[i] ?? 0
      const phase = Math.sin(twist * 40 + mag) * mag * 14
      const y = cy + phase
      const r = 14 + (i === 4 ? 6 : 0) // GEN slightly larger

      const rr = Math.round(70 + redness * 170)
      const g = Math.round(170 - redness * 130)
      const b = Math.round(160 - redness * 120)
      // Disc
      ctx.beginPath()
      ctx.ellipse(x, y, r * 0.55, r, 0, 0, Math.PI * 2)
      ctx.fillStyle = `rgb(${rr},${g},${b})`
      ctx.fill()
      ctx.strokeStyle = "rgba(255,255,255,0.25)"
      ctx.lineWidth = 1
      ctx.stroke()

      ctx.fillStyle = "rgba(220,230,240,0.85)"
      ctx.font = "11px 'Geist Mono Variable', monospace"
      ctx.textAlign = "center"
      ctx.fillText(masses[i]!, x, y + r + 16)
    }

    ctx.textAlign = "left"
    ctx.fillStyle = "rgba(200,210,220,0.7)"
    ctx.font = "12px 'Geist Mono Variable', monospace"
    ctx.fillText("visual scale, simulated", 14, 20)
    const stressTxt = `Shaft stress: ${stress.toFixed(3)} pu`
    ctx.fillStyle = redness > 0.55 ? "rgb(240,120,100)" : "rgb(120,210,160)"
    ctx.font = "16px 'Geist Sans Variable', sans-serif"
    ctx.fillText(stressTxt, 14, h - 28)
    if (timesLower != null && timesLower > 1.05) {
      ctx.fillStyle = "rgba(120,210,160,0.9)"
      ctx.font = "13px 'Geist Sans Variable', sans-serif"
      ctx.fillText(`${timesLower}× lower with the controller`, 14, h - 10)
    }
  }, [controllerOn, stressBeforePu, stressAfterPu, twistsBefore, twistsAfter, timesLower, width, height])

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      className="w-full max-w-full border border-border/50"
      aria-label="Turbine shaft stress, visual scale"
    />
  )
}

export function PowerCanvas({
  t,
  without,
  withCtrl,
  controllerOn,
  width = 720,
  height = 180,
}: {
  t: number[]
  without: number[]
  withCtrl: number[]
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
    ctx.fillStyle = "#0a0e14"
    ctx.fillRect(0, 0, w, h)

    const pad = { l: 52, r: 16, t: 36, b: 32 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b
    const yMax = Math.max(...without, ...withCtrl, 1) * 1.08
    const tMax = Math.max(...t, 1)

    // axes
    ctx.strokeStyle = "rgba(255,255,255,0.15)"
    ctx.strokeRect(pad.l, pad.t, plotW, plotH)
    ctx.fillStyle = "rgba(200,210,220,0.65)"
    ctx.font = "11px 'Geist Mono Variable', monospace"
    ctx.fillText("Grid power (MW)", 8, 18)
    ctx.fillText("0", pad.l - 18, pad.t + plotH)
    ctx.fillText(yMax.toFixed(0), pad.l - 28, pad.t + 10)
    ctx.fillText("0 s", pad.l, h - 10)
    ctx.fillText(`${tMax.toFixed(0)} s`, w - 40, h - 10)

    const draw = (ys: number[], color: string, widthPx: number) => {
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
    draw(without, "rgba(220,80,70,0.55)", controllerOn ? 1.2 : 2.2)
    draw(withCtrl, "rgba(60,200,130,0.95)", controllerOn ? 2.4 : 1.2)

    // legend
    ctx.fillStyle = "rgb(220,80,70)"
    ctx.fillRect(pad.l + 8, 8, 12, 4)
    ctx.fillStyle = "rgba(200,210,220,0.8)"
    ctx.fillText("Without GridSafe", pad.l + 24, 14)
    ctx.fillStyle = "rgb(60,200,130)"
    ctx.fillRect(pad.l + 180, 8, 12, 4)
    ctx.fillStyle = "rgba(200,210,220,0.8)"
    ctx.fillText("With GridSafe", pad.l + 196, 14)
  }, [t, without, withCtrl, controllerOn, width, height])

  return (
    <canvas ref={ref} width={width} height={height} className="w-full border border-border/50" aria-label="Power over time" />
  )
}

export function SpectrumCanvas({
  freqs,
  without,
  withCtrl,
  limitBands,
  controllerOn,
  width = 720,
  height = 200,
}: {
  freqs: number[]
  without: number[]
  withCtrl: number[]
  limitBands: Array<{ freq_min_hz: number; freq_max_hz: number; limit_mw: number }>
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
    ctx.fillStyle = "#0a0e14"
    ctx.fillRect(0, 0, w, h)

    const pad = { l: 52, r: 16, t: 36, b: 32 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b
    const fMax = Math.max(...freqs, 1)
    const yMax = Math.max(...without, ...withCtrl, ...limitBands.map((b) => b.limit_mw), 1e-3) * 1.25

    ctx.strokeStyle = "rgba(255,255,255,0.15)"
    ctx.strokeRect(pad.l, pad.t, plotW, plotH)
    ctx.fillStyle = "rgba(200,210,220,0.65)"
    ctx.font = "11px 'Geist Mono Variable', monospace"
    ctx.fillText("Power swing amplitude (MW)", 8, 18)
    ctx.fillText("0 Hz", pad.l, h - 10)
    ctx.fillText(`${fMax.toFixed(0)} Hz`, w - 48, h - 10)

    // Limit lines + shade ABOVE the limit across the band
    for (const b of limitBands) {
      const x0 = pad.l + (b.freq_min_hz / fMax) * plotW
      const x1 = pad.l + (b.freq_max_hz / fMax) * plotW
      const yLim = pad.t + plotH - (b.limit_mw / yMax) * plotH
      ctx.fillStyle = "rgba(220, 60, 50, 0.22)"
      ctx.fillRect(x0, pad.t, Math.max(2, x1 - x0), Math.max(0, yLim - pad.t))
      ctx.strokeStyle = "rgba(240,100,90,0.9)"
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.moveTo(x0, yLim)
      ctx.lineTo(x1, yLim)
      ctx.stroke()
    }

    const draw = (ys: number[], color: string, widthPx: number) => {
      ctx.beginPath()
      ctx.strokeStyle = color
      ctx.lineWidth = widthPx
      for (let i = 0; i < freqs.length; i++) {
        const x = pad.l + (freqs[i]! / fMax) * plotW
        const y = pad.t + plotH - (ys[i]! / yMax) * plotH
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
    draw(without, "rgba(220,80,70,0.55)", controllerOn ? 1 : 2)
    draw(withCtrl, "rgba(60,200,130,0.95)", controllerOn ? 2 : 1)

    ctx.fillStyle = "rgba(200,210,220,0.75)"
    ctx.fillText("Limit = horizontal line · red = above the limit (not allowed)", pad.l + 8, 30)
  }, [freqs, without, withCtrl, limitBands, controllerOn, width, height])

  return (
    <canvas ref={ref} width={width} height={height} className="w-full border border-border/50" aria-label="Spectrum with rule limits" />
  )
}
