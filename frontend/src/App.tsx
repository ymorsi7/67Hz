import { useCallback, useEffect, useRef, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import {
  Camera,
  FileUp,
  Loader2,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Slider } from "@/components/ui/slider"
import { Stepper, STEPS, type StepId } from "@/components/Stepper"
import { PowerCanvas, ShaftCanvas, SpectrumCanvas } from "@/components/ShaftCanvas"
import { confirmBadge, plainRule } from "@/lib/plain"
import {
  attack,
  fetchHealth,
  loadReplaySession,
  selectPages,
  streamPost,
  thumbnailUrl,
  uploadCamera,
  uploadPdf,
  type ConstraintRow,
  type HealthResponse,
} from "@/lib/api"

type Tone = "idle" | "busy" | "error" | "ok"

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [step, setStep] = useState<StepId>("Rulebook")
  const [completed, setCompleted] = useState<Set<StepId>>(new Set())
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sourceName, setSourceName] = useState("")
  const [pageCount, setPageCount] = useState(0)
  const [selectedPages, setSelectedPages] = useState<number[]>([])
  const [constraints, setConstraints] = useState<ConstraintRow[]>([])
  const [statusMsg, setStatusMsg] = useState("")
  const [tone, setTone] = useState<Tone>("idle")
  const [error, setError] = useState<string | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [certOpen, setCertOpen] = useState(false)
  const [showUncheckable, setShowUncheckable] = useState(false)
  const [cameraPhase, setCameraPhase] = useState<"off" | "live" | "preview">("off")
  const [cameraError, setCameraError] = useState<string | null>(null)
  const [captureBlob, setCaptureBlob] = useState<Blob | null>(null)
  const [captureUrl, setCaptureUrl] = useState<string | null>(null)

  const [headline, setHeadline] = useState("")
  const [evolveStatus, setEvolveStatus] = useState("")
  const [metrics, setMetrics] = useState<{
    extra: number | null
    slowed: number | null
    wear: number | null
  }>({ extra: null, slowed: null, wear: null })
  const [bestDiff, setBestDiff] = useState("")
  const [rawLog, setRawLog] = useState<string[]>([])
  const [controllerCode, setControllerCode] = useState<string | null>(null)
  const [traces, setTraces] = useState<Record<string, unknown> | null>(null)
  const [certificate, setCertificate] = useState<Record<string, unknown> | null>(null)
  const [controllerOn, setControllerOn] = useState(true)

  const [pulseRate, setPulseRate] = useState(2)
  const [depthPct, setDepthPct] = useState(45)
  const [clusterMw, setClusterMw] = useState(100)
  const [attackResult, setAttackResult] = useState<Record<string, unknown> | null>(null)
  const [checking, setChecking] = useState(false)
  const [survived, setSurvived] = useState(0)
  const [attempts, setAttempts] = useState(0)

  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const attackTimer = useRef<number | null>(null)
  const attackStart = useRef<number>(0)

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null))
  }, [])

  const markDone = useCallback((s: StepId) => {
    setCompleted((prev) => new Set([...prev, s]))
  }, [])

  const stopCamera = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setCameraPhase("off")
  }

  const resetAll = useCallback(() => {
    abortRef.current?.abort()
    stopCamera()
    if (captureUrl) URL.revokeObjectURL(captureUrl)
    setStep("Rulebook")
    setCompleted(new Set())
    setSessionId(null)
    setSourceName("")
    setPageCount(0)
    setSelectedPages([])
    setConstraints([])
    setStatusMsg("")
    setTone("idle")
    setError(null)
    setDetailsOpen(false)
    setCertOpen(false)
    setHeadline("")
    setEvolveStatus("")
    setMetrics({ extra: null, slowed: null, wear: null })
    setBestDiff("")
    setRawLog([])
    setControllerCode(null)
    setTraces(null)
    setCertificate(null)
    setAttackResult(null)
    setSurvived(0)
    setAttempts(0)
    setCameraError(null)
    setCaptureBlob(null)
    setCaptureUrl(null)
    setCameraPhase("off")
  }, [captureUrl])

  const openCamera = async () => {
    setCameraError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
        audio: false,
      })
      streamRef.current = stream
      setCameraPhase("live")
      requestAnimationFrame(() => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream
          void videoRef.current.play()
        }
      })
    } catch {
      setCameraError(
        "Camera permission was denied or no camera is available. Allow camera access in the browser, or upload a PDF instead.",
      )
      setCameraPhase("off")
    }
  }

  const captureStill = () => {
    const video = videoRef.current
    if (!video) return
    const canvas = document.createElement("canvas")
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 720
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    // Not mirrored — text reads correctly
    ctx.drawImage(video, 0, 0)
    canvas.toBlob(
      (blob) => {
        if (!blob) return
        if (captureUrl) URL.revokeObjectURL(captureUrl)
        setCaptureBlob(blob)
        setCaptureUrl(URL.createObjectURL(blob))
        setCameraPhase("preview")
        streamRef.current?.getTracks().forEach((t) => t.stop())
        streamRef.current = null
      },
      "image/jpeg",
      0.92,
    )
  }

  const usePhoto = async () => {
    if (!captureBlob) return
    setTone("busy")
    setStatusMsg("Uploading photo…")
    try {
      const res = await uploadCamera(captureBlob)
      setSessionId(res.session_id)
      setSourceName(res.source_name)
      setPageCount(1)
      setSelectedPages([1])
      markDone("Rulebook")
      setTone("ok")
      setStatusMsg("Photo ready")
      setStep("Rules")
      // Auto-run read+verify for single camera page
      await runReadVerify(res.session_id, [1])
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const onUpload = async (file: File) => {
    setError(null)
    setTone("busy")
    setStatusMsg("Uploading PDF…")
    stopCamera()
    try {
      const res = await uploadPdf(file)
      setSessionId(res.session_id)
      setSourceName(res.source_name)
      setPageCount(res.page_count)
      setSelectedPages([])
      setStatusMsg("Pick the pages to read")
      setTone("ok")
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const runReadVerify = async (sid?: string, pages?: number[]) => {
    const id = sid ?? sessionId
    const pgs = pages ?? selectedPages
    if (!id || pgs.length === 0) {
      setError("Pick at least one page first")
      setTone("error")
      return
    }
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setConstraints([])
    setError(null)
    setTone("busy")
    setStep("Rules")
    setStatusMsg("Reading the pages…")
    try {
      await selectPages(id, pgs)
      await streamPost(
        `/api/ui/session/${id}/read-verify`,
        { pages: pgs },
        {
          status: (d) => setStatusMsg(String(d.message ?? "")),
          constraint: (d) => {
            const row = d as unknown as ConstraintRow
            setConstraints((prev) => {
              const idx = prev.findIndex((c) => c.id === row.id)
              if (idx >= 0) {
                const next = [...prev]
                next[idx] = { ...next[idx], ...row }
                return next
              }
              return [...prev, row]
            })
          },
          verify_complete: (d) => {
            if (Array.isArray(d.constraints)) setConstraints(d.constraints as ConstraintRow[])
            markDone("Rulebook")
            markDone("Rules")
            setTone("ok")
            setStatusMsg("Rules ready — next: design a controller")
          },
          error: (d) => {
            setError(String(d.message ?? "Read failed"))
            setTone("error")
          },
        },
        ac.signal,
      )
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError(String(err))
        setTone("error")
      }
    }
  }

  const runEvolve = async () => {
    if (!sessionId) return
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setStep("Controller")
    setTone("busy")
    setBestDiff("")
    setRawLog([])
    setHeadline("")
    setEvolveStatus("Starting…")
    setStatusMsg("Designing a controller…")
    let localTried = 0
    let localPassed = 0
    let bestOh: number | null = null
    try {
      await streamPost(
        `/api/ui/session/${sessionId}/evolve`,
        { budget_s: 75, population_size: 6, seed: 101 },
        {
          evolve: (d) => {
            const ev = String(d.event ?? "")
            setRawLog((prev) => [...prev.slice(-60), JSON.stringify(d)])
            if (ev === "candidate_done") {
              localTried += 1
              if (d.passed) localPassed += 1
              if (typeof d.overhead_pct === "number" && d.passed) {
                if (bestOh == null || d.overhead_pct < bestOh) bestOh = d.overhead_pct
              }
              setEvolveStatus(
                `Gemini has written ${localTried} controllers. ${localPassed} follow the rules.` +
                  (bestOh != null ? ` Best one uses ${bestOh.toFixed(1)}% extra energy.` : ""),
              )
              if (typeof d.diff === "string" && d.diff) setBestDiff(d.diff)
            }
            if (typeof d.best_diff === "string" && d.best_diff) setBestDiff(d.best_diff)
          },
          evolve_complete: async (d) => {
            setHeadline(String(d.headline ?? ""))
            setMetrics({
              extra: typeof d.extra_energy_pct === "number" ? d.extra_energy_pct : null,
              slowed: typeof d.training_slowed_pct === "number" ? d.training_slowed_pct : null,
              wear: typeof d.battery_wear === "number" ? d.battery_wear : null,
            })
            setControllerCode((d.best_code as string) || null)
            if (typeof d.best_diff === "string") setBestDiff(d.best_diff)
            markDone("Controller")
            setTone(d.passed ? "ok" : "error")
            setEvolveStatus("")
            setStatusMsg(d.passed ? "Controller ready — try to break it" : "No safe controller found")
            try {
              const st = await (await fetch(`/api/ui/session/${sessionId}/state`)).json()
              setCertificate(st.certificate ?? null)
              if (st.has_traces) {
                const tr = await (await fetch(`/api/ui/session/${sessionId}/traces`)).json()
                setTraces(tr)
                const m = tr.metrics as Record<string, number> | undefined
                if (m) {
                  setMetrics({
                    extra: m.extra_energy_pct ?? null,
                    slowed: m.training_slowed_pct ?? null,
                    wear: m.battery_wear ?? null,
                  })
                }
              }
            } catch {
              /* ignore */
            }
          },
          error: (d) => {
            setError(String(d.message ?? "Design failed"))
            setTone("error")
          },
        },
        ac.signal,
      )
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError(String(err))
        setTone("error")
      }
    }
  }

  const runAttack = useCallback(
    (rate: number, depth: number, cluster: number, onRelease: boolean) => {
      if (!sessionId || !controllerCode) return
      if (attackTimer.current) window.clearTimeout(attackTimer.current)
      const delay = onRelease ? 0 : 180
      attackTimer.current = window.setTimeout(async () => {
        attackStart.current = performance.now()
        setChecking(true)
        try {
          const res = await attack(sessionId, {
            pulse_rate: rate,
            depth: depth / 100,
            cluster_mw: cluster,
          })
          const ms = performance.now() - attackStart.current
          setAttackResult(res)
          setAttempts((n) => n + 1)
          if (res.held || res.passed) setSurvived((n) => n + 1)
          markDone("Break")
          if (ms > 300 && !onRelease) {
            // Next time prefer release-only — already using debounce
          }
        } catch (err) {
          setError(String(err))
        } finally {
          setChecking(false)
        }
      }, delay)
    },
    [sessionId, controllerCode, markDone],
  )

  const fixAttack = async () => {
    if (!sessionId) return
    setTone("busy")
    setStatusMsg("Fixing with your attack included…")
    setStep("Controller")
    try {
      await streamPost(
        `/api/ui/session/${sessionId}/fix-attack`,
        { pulse_rate: pulseRate, depth: depthPct / 100, cluster_mw: clusterMw, budget_s: 60 },
        {
          status: (d) => setEvolveStatus(String(d.message ?? "")),
          evolve: (d) => setRawLog((prev) => [...prev.slice(-40), JSON.stringify(d)]),
          fix_complete: async (d) => {
            setHeadline(String(d.headline ?? ""))
            setControllerCode((d.best_code as string) || null)
            setTone(d.passed ? "ok" : "error")
            try {
              const tr = await (await fetch(`/api/ui/session/${sessionId}/traces`)).json()
              setTraces(tr)
              const st = await (await fetch(`/api/ui/session/${sessionId}/state`)).json()
              setCertificate(st.certificate ?? null)
            } catch {
              /* ignore */
            }
            setStatusMsg("Fixed — try breaking it again")
            setStep("Break")
            setAttackResult(null)
          },
          error: (d) => {
            setError(String(d.message ?? "Fix failed"))
            setTone("error")
          },
        },
      )
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const loadReplay = async () => {
    setTone("busy")
    try {
      const loaded = await loadReplaySession()
      setSessionId(loaded.session_id)
      setSourceName(loaded.source_name)
      setConstraints(loaded.constraints ?? [])
      setControllerCode(loaded.controller_code)
      setCertificate(loaded.certificate)
      setHeadline(String(loaded.certificate?.headline ?? loaded.fallback_rung ?? ""))
      if (loaded.has_traces) {
        const tr = await (await fetch(`/api/ui/session/${loaded.session_id}/traces`)).json()
        setTraces(tr)
      }
      setCompleted(new Set(STEPS))
      setStep("Controller")
      setTone("ok")
      setStatusMsg("Loaded a recorded run")
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const primaryAction = async () => {
    if (step === "Rulebook") {
      if (cameraPhase === "live") captureStill()
      else if (cameraPhase === "preview") await usePhoto()
      else if (sessionId && selectedPages.length) await runReadVerify()
      else fileRef.current?.click()
    } else if (step === "Rules") {
      if (tone === "busy") return
      if (!constraints.length) await runReadVerify()
      else await runEvolve()
    } else if (step === "Controller") {
      if (!controllerCode) await runEvolve()
      else {
        setStep("Break")
        runAttack(pulseRate, depthPct, clusterMw, true)
      }
    } else if (step === "Break") {
      resetAll()
    }
  }

  const primaryLabel = (() => {
    if (step === "Rulebook") {
      if (cameraPhase === "live") return "Capture"
      if (cameraPhase === "preview") return "Use this photo"
      if (sessionId && selectedPages.length) return "Read these pages"
      return "Upload a PDF"
    }
    if (step === "Rules") {
      if (tone === "busy") return "Reading…"
      if (constraints.some((c) => c.checkable && c.verified)) return "Design a controller"
      return "Read these pages"
    }
    if (step === "Controller") {
      if (tone === "busy") return "Designing…"
      if (controllerCode) return "Try to break it"
      return "Design a controller"
    }
    return "Start over"
  })()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === "INPUT" || tag === "TEXTAREA") return
      if (e.key === "Escape") {
        e.preventDefault()
        resetAll()
      } else if (e.key === " " || e.code === "Space") {
        e.preventDefault()
        void primaryAction()
      } else if (e.key.toLowerCase() === "c") {
        e.preventDefault()
        if (cameraPhase === "off") void openCamera()
        else stopCamera()
      } else if (e.key.toLowerCase() === "r") {
        e.preventDefault()
        void loadReplay()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  })

  const checkable = constraints.filter((c) => c.checkable)
  const uncheckable = constraints.filter((c) => !c.checkable)
  const activePage = selectedPages[0] ?? 1

  const shaft = traces?.shaft as Record<string, unknown> | undefined
  const power = traces?.power as Record<string, number[]> | undefined
  const spectrum = traces?.spectrum as
    | {
        freqs_hz: number[]
        without_mw?: number[]
        with_mw?: number[]
        limit_bands?: Array<{ freq_min_hz: number; freq_max_hz: number; limit_mw: number }>
      }
    | undefined

  const subtitle: Record<StepId, string> = {
    Rulebook: "Drop in a utility PDF or photograph a printed page.",
    Rules: "GridSafe reads the pages and confirms each rule it can simulate.",
    Controller: "GridSafe designs a controller that follows the confirmed rules.",
    Break: "Change how the AI cluster behaves and see if the rules still hold.",
  }

  const downloadCert = () => {
    if (!certificate) return
    const blob = new Blob([JSON.stringify(certificate, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = "gridsafe-certificate.json"
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="min-h-screen flex flex-col dark bg-background text-foreground">
      <div className="flex-1 flex flex-col max-w-[1200px] w-full mx-auto px-5 pt-5 pb-2 gap-4">
        <header className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <ShieldCheck className="h-8 w-8 text-emerald-400" />
            <div>
              <h1 className="text-2xl font-semibold tracking-tight leading-none">GridSafe</h1>
              <p className="text-sm text-muted-foreground mt-1">Rulebook → controller that follows it</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {(step === "Controller" || step === "Break") && (
              <Button variant="secondary" size="sm" onClick={() => setCertOpen(true)}>
                <ScrollText className="h-4 w-4" />
                Certificate
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={resetAll}>
              <RotateCcw className="h-4 w-4" />
              Start over
            </Button>
          </div>
        </header>

        <Stepper
          current={step}
          completed={completed}
          onJump={(s) => {
            if (completed.has(s) || STEPS.indexOf(s) <= STEPS.indexOf(step)) setStep(s)
          }}
        />

        <main className="flex-1 border border-border/60 bg-card/25 p-5 relative min-h-[360px] overflow-auto">
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="h-full"
            >
              {step === "Rulebook" && (
                <div className="space-y-5">
                  <div>
                    <h2 className="text-3xl font-semibold tracking-tight">Rulebook</h2>
                    <p className="text-muted-foreground text-base mt-1">{subtitle.Rulebook}</p>
                  </div>
                  <p className="text-xl max-w-3xl leading-snug">
                    Give GridSafe a utility&apos;s rulebook. It builds a controller that follows it, and proves it.
                  </p>

                  {cameraError && (
                    <div className="border border-amber-500/40 bg-amber-950/40 text-amber-100 px-4 py-3 text-sm">
                      {cameraError}
                    </div>
                  )}

                  {cameraPhase === "off" && !sessionId && (
                    <div className="grid sm:grid-cols-2 gap-4 max-w-3xl mt-2">
                      <button
                        type="button"
                        onClick={() => fileRef.current?.click()}
                        className="min-h-[120px] border-2 border-emerald-500/40 bg-emerald-500/5 hover:bg-emerald-500/10 px-6 py-6 text-left"
                      >
                        <FileUp className="h-8 w-8 text-emerald-400 mb-2" />
                        <div className="text-2xl font-semibold">Upload a PDF</div>
                        <div className="text-muted-foreground mt-1 text-base">Utility interconnection document</div>
                      </button>
                      <button
                        type="button"
                        onClick={() => void openCamera()}
                        className="min-h-[120px] border-2 border-emerald-500/40 bg-emerald-500/5 hover:bg-emerald-500/10 px-6 py-6 text-left"
                      >
                        <Camera className="h-8 w-8 text-emerald-400 mb-2" />
                        <div className="text-2xl font-semibold">Use camera</div>
                        <div className="text-muted-foreground mt-1 text-base">Printed or handwritten page</div>
                      </button>
                    </div>
                  )}

                  <input
                    ref={fileRef}
                    type="file"
                    accept="application/pdf"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0]
                      if (f) void onUpload(f)
                    }}
                  />

                  {cameraPhase === "live" && (
                    <div className="space-y-3 max-w-xl">
                      <p className="text-base">Hold a printed or handwritten rule up to the camera</p>
                      <video
                        ref={videoRef}
                        className="w-full aspect-video bg-black border border-border/60"
                        style={{ transform: "none" }}
                        muted
                        playsInline
                      />
                      <Button size="lg" className="h-12 text-base" onClick={captureStill}>
                        Capture
                      </Button>
                    </div>
                  )}

                  {cameraPhase === "preview" && captureUrl && (
                    <div className="space-y-3 max-w-xl">
                      <img src={captureUrl} alt="Captured page" className="w-full border border-border/60" />
                      <div className="flex gap-3">
                        <Button size="lg" onClick={() => void usePhoto()}>
                          Use this photo
                        </Button>
                        <Button
                          size="lg"
                          variant="secondary"
                          onClick={() => {
                            setCaptureBlob(null)
                            if (captureUrl) URL.revokeObjectURL(captureUrl)
                            setCaptureUrl(null)
                            void openCamera()
                          }}
                        >
                          Retake
                        </Button>
                      </div>
                    </div>
                  )}

                  {sessionId && pageCount > 0 && cameraPhase === "off" && (
                    <div className="space-y-3">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <h3 className="text-xl font-semibold">Pick the pages to read</h3>
                          <p className="text-sm text-muted-foreground font-mono">
                            {sourceName} · {selectedPages.length} selected
                          </p>
                        </div>
                        <Button
                          size="lg"
                          disabled={!selectedPages.length || tone === "busy"}
                          onClick={() => void runReadVerify()}
                        >
                          Read these pages
                        </Button>
                      </div>
                      <div className="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2 max-h-[320px] overflow-auto">
                        {Array.from({ length: pageCount }, (_, i) => i + 1).map((p) => {
                          const sel = selectedPages.includes(p)
                          return (
                            <button
                              key={p}
                              type="button"
                              onClick={() =>
                                setSelectedPages((prev) =>
                                  prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p].sort((a, b) => a - b),
                                )
                              }
                              className={`relative border aspect-[3/4] overflow-hidden ${
                                sel ? "border-emerald-400 ring-1 ring-emerald-400/40" : "border-border/50"
                              }`}
                            >
                              <img
                                src={thumbnailUrl(sessionId, p)}
                                alt={`Page ${p}`}
                                className="w-full h-full object-cover"
                                loading="lazy"
                              />
                              <span className="absolute bottom-1 left-1 font-mono text-[11px] bg-black/70 px-1">
                                p.{p}
                              </span>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {step === "Rules" && (
                <div className="space-y-4">
                  <div>
                    <h2 className="text-3xl font-semibold">Rules</h2>
                    <p className="text-muted-foreground mt-1">{subtitle.Rules}</p>
                  </div>
                  <div className="grid md:grid-cols-2 gap-5">
                    <div>
                      {sessionId && (
                        <img
                          src={thumbnailUrl(sessionId, activePage, 120)}
                          alt={`Page ${activePage}`}
                          className="border border-border/50 max-h-[520px] object-contain bg-black/40 w-full"
                        />
                      )}
                    </div>
                    <div className="space-y-2 overflow-auto max-h-[560px]">
                      {tone === "busy" && !checkable.length && (
                        <p className="text-muted-foreground flex items-center gap-2">
                          <Loader2 className="h-4 w-4 animate-spin" /> {statusMsg || "Reading…"}
                        </p>
                      )}
                      {checkable.map((c) => {
                        const badge = confirmBadge(c)
                        return (
                          <div key={c.id} className="border border-border/50 px-3 py-3 bg-black/20">
                            <p className="text-lg leading-snug">{plainRule(c)}</p>
                            <p className="text-xs text-muted-foreground mt-2">
                              {c.entity || "Utility"} · page {c.page}
                              {c.snippet ? ` · “${c.snippet.slice(0, 100)}${c.snippet.length > 100 ? "…" : ""}”` : ""}
                            </p>
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              <Badge
                                variant="outline"
                                className={
                                  badge.tone === "ok"
                                    ? "border-emerald-500/50 text-emerald-300"
                                    : "border-amber-500/50 text-amber-200"
                                }
                              >
                                {badge.label}
                              </Badge>
                              {badge.tone === "warn" && (
                                <span className="text-xs text-amber-200/90">
                                  GridSafe will not build on a rule it cannot confirm.
                                </span>
                              )}
                            </div>
                          </div>
                        )
                      })}
                      {uncheckable.length > 0 && (
                        <button
                          type="button"
                          className="text-sm text-muted-foreground underline-offset-2 hover:underline"
                          onClick={() => setShowUncheckable((v) => !v)}
                        >
                          {uncheckable.length} other rules on these pages cannot be simulated (
                          {showUncheckable ? "hide" : "show"})
                        </button>
                      )}
                      {showUncheckable &&
                        uncheckable.map((c) => (
                          <div key={c.id} className="text-sm text-muted-foreground border-l border-border/40 pl-3">
                            p.{c.page}: {c.snippet?.slice(0, 140) || "qualitative rule"}
                          </div>
                        ))}
                    </div>
                  </div>
                </div>
              )}

              {step === "Controller" && (
                <div className="space-y-4 overflow-auto max-h-[70vh]">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <h2 className="text-3xl font-semibold">Controller</h2>
                      <p className="text-muted-foreground mt-1">{subtitle.Controller}</p>
                    </div>
                    {controllerCode && (
                      <label className="flex items-center gap-3 font-mono text-sm border border-border/50 px-3 py-2">
                        <span>Controller {controllerOn ? "ON" : "OFF"}</span>
                        <input
                          type="checkbox"
                          checked={controllerOn}
                          onChange={(e) => setControllerOn(e.target.checked)}
                          className="size-5"
                        />
                      </label>
                    )}
                  </div>

                  {tone === "busy" && (
                    <p className="text-lg text-amber-100 flex items-center gap-2">
                      <Loader2 className="h-5 w-5 animate-spin" />
                      {evolveStatus || "Designing…"}
                    </p>
                  )}

                  {headline && <p className="text-2xl font-semibold leading-snug max-w-3xl">{headline}</p>}

                  {(metrics.extra != null || metrics.slowed != null) && (
                    <div className="grid sm:grid-cols-3 gap-3">
                      <Metric
                        label="Extra energy used"
                        value={metrics.extra}
                        unit="%"
                        hint="Lower is better"
                      />
                      <Metric
                        label="Training slowed by"
                        value={metrics.slowed}
                        unit="%"
                        hint="Lower is better"
                      />
                      <Metric label="Battery wear" value={metrics.wear} unit="" hint="Lower is better" />
                    </div>
                  )}

                  {traces && !("error" in traces) && (
                    <div className="space-y-3">
                      <ShaftCanvas
                        controllerOn={controllerOn}
                        stressBeforePu={Number(shaft?.stress_before_pu ?? shaft?.max_torque_before_pu ?? 0)}
                        stressAfterPu={Number(shaft?.stress_after_pu ?? shaft?.max_torque_after_pu ?? 0)}
                        twistsBefore={(shaft?.peak_twist_before as number[]) ?? []}
                        twistsAfter={(shaft?.peak_twist_after as number[]) ?? []}
                        timesLower={(shaft?.times_lower as number) ?? null}
                      />
                      {power && (
                        <PowerCanvas
                          t={power.t_s ?? []}
                          without={power.without_mw ?? power.p_grid_before_mw ?? []}
                          withCtrl={power.with_mw ?? power.p_grid_after_mw ?? []}
                          controllerOn={controllerOn}
                        />
                      )}
                      {spectrum && (
                        <SpectrumCanvas
                          freqs={spectrum.freqs_hz ?? []}
                          without={spectrum.without_mw ?? []}
                          withCtrl={spectrum.with_mw ?? []}
                          limitBands={spectrum.limit_bands ?? []}
                          controllerOn={controllerOn}
                        />
                      )}
                    </div>
                  )}

                  <details className="text-sm" open={detailsOpen} onToggle={(e) => setDetailsOpen((e.target as HTMLDetailsElement).open)}>
                    <summary className="cursor-pointer text-muted-foreground">Details</summary>
                    <pre className="mt-2 font-mono text-[11px] text-muted-foreground whitespace-pre-wrap max-h-40 overflow-auto border border-border/40 p-2">
                      {bestDiff || rawLog.slice(-8).join("\n") || "—"}
                    </pre>
                  </details>
                </div>
              )}

              {step === "Break" && (
                <div className="space-y-4">
                  <div>
                    <h2 className="text-3xl font-semibold">Break it</h2>
                    <p className="text-muted-foreground mt-1">{subtitle.Break}</p>
                  </div>
                  <p className="text-xl max-w-3xl">
                    Try to break it. Change how the AI cluster behaves and see if the controller still follows the
                    rules.
                  </p>

                  <div className="grid md:grid-cols-3 gap-5">
                    <SliderField
                      label="Pulse rate (training steps per second)"
                      value={pulseRate}
                      min={0.5}
                      max={10}
                      step={0.1}
                      display={`${pulseRate.toFixed(1)} /s`}
                      onDrag={(v) => {
                        setPulseRate(v)
                        runAttack(v, depthPct, clusterMw, false)
                      }}
                      onRelease={(v) => runAttack(v, depthPct, clusterMw, true)}
                    />
                    <SliderField
                      label="Pulse depth (% of full power)"
                      value={depthPct}
                      min={5}
                      max={100}
                      step={1}
                      display={`${depthPct}%`}
                      onDrag={(v) => {
                        setDepthPct(v)
                        runAttack(pulseRate, v, clusterMw, false)
                      }}
                      onRelease={(v) => runAttack(pulseRate, v, clusterMw, true)}
                    />
                    <SliderField
                      label="Cluster size (MW)"
                      value={clusterMw}
                      min={20}
                      max={300}
                      step={5}
                      display={`${clusterMw} MW`}
                      onDrag={(v) => {
                        setClusterMw(v)
                        runAttack(pulseRate, depthPct, v, false)
                      }}
                      onRelease={(v) => runAttack(pulseRate, depthPct, v, true)}
                    />
                  </div>

                  <div
                    className={`border px-4 py-4 text-xl font-semibold ${
                      attackResult?.held || attackResult?.passed
                        ? "border-emerald-500/50 bg-emerald-950/40 text-emerald-200"
                        : attackResult
                          ? "border-red-500/50 bg-red-950/40 text-red-200"
                          : "border-border/50 text-muted-foreground"
                    }`}
                  >
                    {checking ? (
                      <span className="inline-flex items-center gap-2">
                        <Loader2 className="h-5 w-5 animate-spin" /> Checking…
                      </span>
                    ) : (
                      String(attackResult?.banner ?? "Move a slider to attack")
                    )}
                  </div>

                  <p className="font-mono text-sm text-muted-foreground">
                    Attacks survived: {survived} of {attempts || 0}
                  </p>

                  <div className="space-y-2">
                    {((attackResult?.results as Array<Record<string, unknown>>) || []).map((r, i) => {
                      const used = r.used_pct_of_limit as number | null | undefined
                      const cannot = r.cannot_check_reason as string | undefined
                      const pct = typeof used === "number" ? Math.min(160, Math.max(0, used)) : 0
                      return (
                        <div key={i} className="border border-border/40 px-3 py-2">
                          <div className="text-sm mb-1">{String(r.plain)}</div>
                          {cannot ? (
                            <p className="text-xs text-amber-200/90">{cannot}</p>
                          ) : (
                            <>
                              <div className="h-2 bg-muted/40 relative">
                                <div
                                  className={`h-full ${pct > 100 ? "bg-red-500" : "bg-emerald-500"}`}
                                  style={{ width: `${Math.min(100, pct)}%` }}
                                />
                              </div>
                              <div className="text-xs font-mono text-muted-foreground mt-1">
                                {used != null ? `uses ${used}% of the limit` : "—"}
                              </div>
                            </>
                          )}
                        </div>
                      )
                    })}
                  </div>

                  {attackResult && !(attackResult.held || attackResult.passed) && (
                    <Button size="lg" onClick={() => void fixAttack()}>
                      Fix it
                    </Button>
                  )}
                </div>
              )}
            </motion.div>
          </AnimatePresence>

          {error && (
            <div className="absolute bottom-4 left-5 right-5 border border-red-500/40 bg-red-950/85 text-red-100 px-4 py-3 text-sm">
              {error}
              <div className="mt-2 flex gap-2">
                <Button size="sm" variant="secondary" onClick={() => void primaryAction()}>
                  Retry
                </Button>
                <Button size="sm" variant="ghost" onClick={() => void loadReplay()}>
                  Replay (recorded)
                </Button>
              </div>
            </div>
          )}
        </main>

        <div className="flex items-center justify-between gap-3">
          <div className="text-xs font-mono text-muted-foreground">
            Space next · C camera · R replay · Esc start over
          </div>
          <Button
            size="lg"
            className="min-w-[200px] h-12 text-base font-semibold bg-emerald-600 hover:bg-emerald-500 text-white"
            onClick={() => void primaryAction()}
            disabled={tone === "busy" && step !== "Rulebook"}
          >
            {tone === "busy" && step !== "Rulebook" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {primaryLabel}
          </Button>
        </div>
      </div>

      <footer className="border-t border-border/60 px-5 py-3 flex items-center justify-between text-sm text-muted-foreground font-mono">
        <span>Built today at the Gemini 4 NYC Hackathon</span>
        <span>
          Commits: {health?.git.commit_count ?? "—"} · First:{" "}
          {health?.git.first_commit_time?.slice(0, 19) ?? "—"}
        </span>
      </footer>

      {certOpen && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-end sm:items-center justify-center p-4">
          <div className="bg-background border border-border w-full max-w-2xl max-h-[85vh] overflow-auto p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-2xl font-semibold">Certificate</h3>
              <Button variant="ghost" size="sm" onClick={() => setCertOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            {headline && <p className="text-lg mb-4">{headline}</p>}
            <div className="space-y-3">
              {((certificate?.rules as Array<Record<string, unknown>>) || checkable.map((c) => ({
                plain: plainRule(c),
                entity: c.entity,
                page: c.page,
                used_pct_of_limit: null,
                how_measured: "",
                simplified_measurement: false,
              }))).map((r, i) => (
                <div key={i} className="border border-border/50 px-3 py-3">
                  <p className="text-base">{String(r.plain)}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {String(r.entity || "Utility")} · page {String(r.page)}
                    {r.used_pct_of_limit != null ? ` · uses ${r.used_pct_of_limit}% of the limit` : ""}
                  </p>
                  {r.how_measured ? (
                    <p className="text-sm mt-2 text-muted-foreground">
                      <span className="text-foreground/80">How it was measured: </span>
                      {String(r.how_measured)}
                    </p>
                  ) : null}
                  {r.simplified_measurement ? (
                    <p className="text-sm mt-1 text-amber-200">Simplified measurement</p>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="flex gap-3 mt-5">
              <Button onClick={downloadCert}>Download certificate (JSON)</Button>
              <Button variant="secondary" onClick={resetAll}>
                Start over
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function Metric({
  label,
  value,
  unit,
  hint,
}: {
  label: string
  value: number | null
  unit: string
  hint: string
}) {
  return (
    <div className="border border-border/50 px-3 py-3">
      <div className="text-xs uppercase tracking-widest text-muted-foreground font-mono">{label}</div>
      <div className="text-4xl font-semibold tabular-nums mt-1">
        {value != null ? `${value.toFixed(1)}${unit}` : "—"}
      </div>
      <div className="text-xs text-muted-foreground mt-1">{hint}</div>
    </div>
  )
}

function SliderField(props: {
  label: string
  value: number
  min: number
  max: number
  step: number
  display: string
  onDrag: (v: number) => void
  onRelease: (v: number) => void
}) {
  return (
    <div>
      <div className="flex justify-between gap-2 mb-2">
        <span className="text-sm text-muted-foreground">{props.label}</span>
        <span className="text-xl tabular-nums font-semibold">{props.display}</span>
      </div>
      <Slider
        value={[props.value]}
        min={props.min}
        max={props.max}
        step={props.step}
        onValueChange={(v) => {
          const n = Array.isArray(v) ? Number(v[0]) : Number(v)
          if (Number.isFinite(n)) props.onDrag(n)
        }}
        onValueCommitted={(v) => {
          const n = Array.isArray(v) ? Number(v[0]) : Number(v)
          if (Number.isFinite(n)) props.onRelease(n)
        }}
      />
    </div>
  )
}

export default App
