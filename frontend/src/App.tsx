import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { motion, AnimatePresence } from "motion/react"
import {
  Camera,
  FileUp,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Slider } from "@/components/ui/slider"
import { Stepper, STEPS } from "@/components/Stepper"
import { PowerCanvas, ShaftCanvas, SpectrumCanvas } from "@/components/ShaftCanvas"
import {
  attack,
  fetchAgentThreads,
  fetchHealth,
  loadReplaySession,
  selectPages,
  streamPost,
  thumbnailUrl,
  uploadCamera,
  uploadPdf,
  type ConstraintRow,
  type HealthResponse,
  type StepId,
} from "@/lib/api"

type StatusTone = "idle" | "streaming" | "error" | "ok"

function statusClass(ui?: string): string {
  if (!ui) return "text-muted-foreground"
  if (ui.startsWith("Verified")) return "text-emerald-400"
  if (ui === "Uncheckable") return "text-muted-foreground"
  return "text-amber-300"
}

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [step, setStep] = useState<StepId>("Input")
  const [completed, setCompleted] = useState<Set<StepId>>(new Set())
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sourceName, setSourceName] = useState("")
  const [pageCount, setPageCount] = useState(0)
  const [selectedPages, setSelectedPages] = useState<number[]>([])
  const [constraints, setConstraints] = useState<ConstraintRow[]>([])
  const [statusMsg, setStatusMsg] = useState("")
  const [tone, setTone] = useState<StatusTone>("idle")
  const [error, setError] = useState<string | null>(null)
  const [readerModel, setReaderModel] = useState("")
  const [verifierModel, setVerifierModel] = useState("")
  const [evolveLog, setEvolveLog] = useState<string[]>([])
  const [bestScore, setBestScore] = useState<number | null>(null)
  const [bestDiff, setBestDiff] = useState("")
  const [fallbackRung, setFallbackRung] = useState("")
  const [controllerCode, setControllerCode] = useState<string | null>(null)
  const [traces, setTraces] = useState<Record<string, unknown> | null>(null)
  const [certificate, setCertificate] = useState<Record<string, unknown> | null>(null)
  const [controllerOn, setControllerOn] = useState(true)
  const [panelOpen, setPanelOpen] = useState(false)
  const [threads, setThreads] = useState<Array<{ file: string; data: unknown }>>([])
  const [rhythm, setRhythm] = useState(0.5)
  const [depth, setDepth] = useState(0.45)
  const [clusterMw, setClusterMw] = useState(100)
  const [attackResult, setAttackResult] = useState<Record<string, unknown> | null>(null)
  const [attackBusy, setAttackBusy] = useState(false)
  const [attacksSurvived, setAttacksSurvived] = useState(0)
  const [attacksFailed, setAttacksFailed] = useState(0)
  const [cameraOn, setCameraOn] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const attackTimer = useRef<number | null>(null)

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null))
    fetchAgentThreads()
      .then((r) => setThreads(r.threads))
      .catch(() => setThreads([]))
  }, [])

  const markDone = useCallback((s: StepId) => {
    setCompleted((prev) => new Set([...prev, s]))
  }, [])

  const resetAll = useCallback(() => {
    abortRef.current?.abort()
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setCameraOn(false)
    setStep("Input")
    setCompleted(new Set())
    setSessionId(null)
    setSourceName("")
    setPageCount(0)
    setSelectedPages([])
    setConstraints([])
    setStatusMsg("")
    setTone("idle")
    setError(null)
    setEvolveLog([])
    setBestScore(null)
    setBestDiff("")
    setFallbackRung("")
    setControllerCode(null)
    setTraces(null)
    setCertificate(null)
    setAttackResult(null)
    setAttacksSurvived(0)
    setAttacksFailed(0)
  }, [])

  // Keyboard: Space / C / R / Escape
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
        void toggleCamera()
      } else if (e.key.toLowerCase() === "r") {
        e.preventDefault()
        void loadReplay()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  })

  const onUpload = async (file: File) => {
    setError(null)
    setTone("streaming")
    setStatusMsg("Uploading PDF…")
    try {
      const res = await uploadPdf(file)
      setSessionId(res.session_id)
      setSourceName(res.source_name)
      setPageCount(res.page_count)
      setSelectedPages([])
      setStatusMsg(`${res.page_count} pages · pick pages to read`)
      setTone("ok")
      markDone("Input")
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const toggleCamera = async () => {
    if (cameraOn) {
      streamRef.current?.getTracks().forEach((t) => t.stop())
      streamRef.current = null
      setCameraOn(false)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
        audio: false,
      })
      streamRef.current = stream
      setCameraOn(true)
      requestAnimationFrame(() => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream
          void videoRef.current.play()
        }
      })
    } catch (err) {
      setError(`Camera unavailable: ${err}`)
      setTone("error")
    }
  }

  const captureCamera = async () => {
    const video = videoRef.current
    if (!video) return
    const canvas = document.createElement("canvas")
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 720
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.drawImage(video, 0, 0)
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92))
    if (!blob) return
    setTone("streaming")
    setStatusMsg("Uploading camera snapshot…")
    try {
      const res = await uploadCamera(blob)
      setSessionId(res.session_id)
      setSourceName(res.source_name)
      setPageCount(1)
      setSelectedPages([1])
      setStatusMsg("Camera page ready · press Space to Read")
      setTone("ok")
      markDone("Input")
      streamRef.current?.getTracks().forEach((t) => t.stop())
      setCameraOn(false)
      setStep("Read")
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const togglePage = (p: number) => {
    setSelectedPages((prev) =>
      prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p].sort((a, b) => a - b),
    )
  }

  const runReadVerify = async () => {
    if (!sessionId || selectedPages.length === 0) {
      setError("Select at least one page first")
      setTone("error")
      return
    }
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setConstraints([])
    setError(null)
    setTone("streaming")
    setStep("Read")
    setStatusMsg("Reading selected pages…")
    try {
      await selectPages(sessionId, selectedPages)
      await streamPost(
        `/api/ui/session/${sessionId}/read-verify`,
        { pages: selectedPages },
        {
          status: (d) => {
            setStatusMsg(String(d.message ?? ""))
            if (d.phase === "verify") {
              markDone("Read")
              setStep("Verify")
            }
          },
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
          read_complete: (d) => {
            setReaderModel(String(d.reader_model ?? ""))
            markDone("Read")
            setStep("Verify")
            setStatusMsg(`Read ${d.count} constraints · verifying…`)
          },
          verify_complete: (d) => {
            setVerifierModel(String(d.verifier_model ?? ""))
            if (Array.isArray(d.constraints)) {
              setConstraints(d.constraints as ConstraintRow[])
            }
            markDone("Verify")
            setTone("ok")
            setStatusMsg("Verify complete")
          },
          error: (d) => {
            setError(String(d.message ?? "Read/verify failed"))
            setTone("error")
          },
          done: (d) => {
            if (d.ok) {
              setTone("ok")
              setStatusMsg("Ready to Evolve · Space")
            }
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
    setStep("Evolve")
    setTone("streaming")
    setEvolveLog([])
    setBestDiff("")
    setBestScore(null)
    setStatusMsg("Evolving controller (local loop)…")
    try {
      await streamPost(
        `/api/ui/session/${sessionId}/evolve`,
        { budget_s: 75, population_size: 6, seed: 101 },
        {
          status: (d) => setStatusMsg(String(d.message ?? "Evolving…")),
          evolve: (d) => {
            const ev = String(d.event ?? "")
            const gate = d.gate ? ` [${d.gate}]` : ""
            const line = `${ev}${gate} gen=${d.generation ?? "—"} ${d.strategy ?? ""} score=${d.score ?? d.best_score ?? "—"}`
            setEvolveLog((prev) => [...prev.slice(-40), line])
            if (typeof d.best_score === "number") setBestScore(d.best_score)
            if (typeof d.best_diff === "string" && d.best_diff) setBestDiff(d.best_diff)
            if (typeof d.diff === "string" && d.diff && d.passed) setBestDiff(d.diff)
          },
          evolve_complete: async (d) => {
            setFallbackRung(String(d.fallback_rung ?? d.outcome ?? ""))
            setControllerCode((d.best_code as string) || null)
            if (typeof d.best_diff === "string") setBestDiff(d.best_diff)
            if (typeof d.best_score === "number") setBestScore(d.best_score)
            markDone("Evolve")
            setTone(d.passed ? "ok" : "error")
            setStatusMsg(
              d.passed
                ? `Evolved · rung ${d.fallback_rung} · ${(d.elapsed_s as number)?.toFixed?.(1) ?? "?"}s`
                : `Refused / failed · ${d.fallback_rung}`,
            )
            // Fetch traces + certificate via state
            try {
              const st = await (await fetch(`/api/ui/session/${sessionId}/state`)).json()
              setCertificate(st.certificate ?? null)
              if (st.has_traces) {
                const tr = await (await fetch(`/api/ui/session/${sessionId}/traces`)).json()
                setTraces(tr)
              }
            } catch {
              /* ignore */
            }
            // Stay on Evolve so Result canvases (shaft/power/spectrum) are visible
          },
          error: (d) => {
            setError(String(d.message ?? "Evolve failed"))
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
    (r: number, d: number, c: number) => {
      if (!sessionId || !controllerCode) return
      if (attackTimer.current) window.clearTimeout(attackTimer.current)
      attackTimer.current = window.setTimeout(async () => {
        setAttackBusy(true)
        try {
          const res = await attack(sessionId, { rhythm: r, depth: d, cluster_mw: c })
          setAttackResult(res)
          if (res.passed) setAttacksSurvived((n) => n + 1)
          else setAttacksFailed((n) => n + 1)
          markDone("Attack")
        } catch (err) {
          setError(String(err))
        } finally {
          setAttackBusy(false)
        }
      }, 120)
    },
    [sessionId, controllerCode, markDone],
  )

  const loadReplay = async () => {
    setTone("streaming")
    setStatusMsg("Loading recorded replay…")
    try {
      const loaded = await loadReplaySession()
      setSessionId(loaded.session_id)
      setSourceName(loaded.source_name)
      setConstraints(loaded.constraints ?? [])
      setControllerCode(loaded.controller_code)
      setCertificate(loaded.certificate)
      setFallbackRung(String(loaded.fallback_rung ?? ""))
      if (loaded.has_traces) {
        const tr = await (await fetch(`/api/ui/session/${loaded.session_id}/traces`)).json()
        setTraces(tr)
      } else {
        setTraces(null)
      }
      setCompleted(new Set(STEPS))
      setStep("Certify")
      setTone("ok")
      setStatusMsg("Loaded recorded replay (R)")
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const primaryAction = async () => {
    if (step === "Input") {
      if (cameraOn) await captureCamera()
      else if (sessionId && selectedPages.length) {
        setStep("Read")
        await runReadVerify()
      } else fileRef.current?.click()
    } else if (step === "Read" || step === "Verify") {
      if (!constraints.length || tone !== "ok") await runReadVerify()
      else {
        setStep("Evolve")
        await runEvolve()
      }
    } else if (step === "Evolve") {
      if (!controllerCode) await runEvolve()
      else setStep("Attack")
    } else if (step === "Attack") {
      runAttack(rhythm, depth, clusterMw)
      markDone("Attack")
      setStep("Certify")
    } else if (step === "Certify") {
      markDone("Certify")
    }
  }

  const torqueNow = useMemo(() => {
    if (!traces) return 0.05
    const shaft = traces.shaft as Record<string, number> | undefined
    if (!shaft) return 0.05
    return controllerOn
      ? Number(shaft.max_torque_after_pu ?? 0.05)
      : Number(shaft.max_torque_before_pu ?? 0.2)
  }, [traces, controllerOn])

  const primaryLabel = (() => {
    if (step === "Input") {
      if (cameraOn) return "Capture page"
      if (sessionId && selectedPages.length) return `Read ${selectedPages.length} page(s)`
      return "Upload PDF"
    }
    if (step === "Read" || step === "Verify") {
      if (tone === "streaming") return "Working…"
      if (tone === "ok" && constraints.length) return "Evolve controller"
      return "Run Read + Verify"
    }
    if (step === "Evolve") return controllerCode ? "Break the grid" : "Start evolve"
    if (step === "Attack") return "Open certificate"
    return "Done"
  })()

  return (
    <div className="min-h-screen flex flex-col dark bg-background text-foreground">
      <div className="flex-1 flex flex-col max-w-[1400px] w-full mx-auto px-6 pt-5 pb-3 gap-4">
        <header className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <ShieldCheck className="h-8 w-8 text-emerald-400" />
            <div>
              <h1 className="text-2xl font-semibold tracking-tight leading-none">GridSafe</h1>
              <p className="text-sm text-muted-foreground mt-1">
                Rulebook → verified controller · local loop
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 font-mono text-sm">
            <Badge
              variant="outline"
              className={
                tone === "error"
                  ? "border-red-500/50 text-red-300"
                  : tone === "streaming"
                    ? "border-amber-500/50 text-amber-200"
                    : "border-emerald-500/40 text-emerald-300"
              }
            >
              {tone === "streaming" ? "STREAMING" : tone === "error" ? "ERROR" : "LIVE"}
            </Badge>
            <Button variant="ghost" size="sm" onClick={() => setPanelOpen((o) => !o)}>
              {panelOpen ? <PanelRightClose className="h-4 w-4" /> : <PanelRightOpen className="h-4 w-4" />}
              Thread
            </Button>
            <Button variant="ghost" size="sm" onClick={resetAll} title="Escape">
              <RotateCcw className="h-4 w-4" />
              Reset
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

        <div className="flex-1 grid grid-cols-1 lg:grid-cols-[1fr_auto] gap-4 min-h-0">
          <main className="min-h-[420px] border border-border/60 bg-card/30 p-5 relative overflow-hidden">
            <AnimatePresence mode="wait">
              <motion.div
                key={step}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.2 }}
                className="h-full flex flex-col gap-4"
              >
                {step === "Input" && (
                  <InputScreen
                    sourceName={sourceName}
                    pageCount={pageCount}
                    selectedPages={selectedPages}
                    sessionId={sessionId}
                    cameraOn={cameraOn}
                    videoRef={videoRef}
                    fileRef={fileRef}
                    onFile={(f) => void onUpload(f)}
                    onToggleCamera={() => void toggleCamera()}
                    onTogglePage={togglePage}
                    onQuickSelect={(pages) => setSelectedPages(pages)}
                  />
                )}

                {(step === "Read" || step === "Verify") && (
                  <ReadVerifyScreen
                    step={step}
                    sessionId={sessionId}
                    selectedPages={selectedPages}
                    constraints={constraints}
                    statusMsg={statusMsg}
                    readerModel={readerModel}
                    verifierModel={verifierModel}
                  />
                )}

                {step === "Evolve" && (
                  <EvolveScreen
                    log={evolveLog}
                    bestScore={bestScore}
                    bestDiff={bestDiff}
                    fallbackRung={fallbackRung}
                    streaming={tone === "streaming"}
                    traces={traces}
                    controllerOn={controllerOn}
                    setControllerOn={setControllerOn}
                    torqueNow={torqueNow}
                  />
                )}

                {step === "Attack" && (
                  <AttackScreen
                    rhythm={rhythm}
                    depth={depth}
                    clusterMw={clusterMw}
                    setRhythm={setRhythm}
                    setDepth={setDepth}
                    setClusterMw={setClusterMw}
                    onChange={(r, d, c) => runAttack(r, d, c)}
                    result={attackResult}
                    busy={attackBusy}
                    survived={attacksSurvived}
                    failed={attacksFailed}
                    traces={traces}
                    controllerOn={controllerOn}
                    setControllerOn={setControllerOn}
                    torqueNow={torqueNow}
                  />
                )}

                {step === "Certify" && (
                  <CertifyScreen
                    certificate={certificate}
                    constraints={constraints}
                    fallbackRung={fallbackRung}
                    attacksSurvived={attacksSurvived}
                    traces={traces}
                    controllerOn={controllerOn}
                    setControllerOn={setControllerOn}
                    torqueNow={torqueNow}
                    readerModel={readerModel}
                    verifierModel={verifierModel}
                  />
                )}
              </motion.div>
            </AnimatePresence>

            {error && (
              <div className="absolute bottom-4 left-5 right-5 border border-red-500/40 bg-red-950/80 text-red-100 px-4 py-3 text-sm font-mono">
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

          {panelOpen && (
            <aside className="w-full lg:w-80 border border-border/60 bg-card/40 p-4 overflow-auto max-h-[70vh]">
              <h2 className="font-mono text-xs uppercase tracking-widest text-muted-foreground mb-3">
                Agent thread
              </h2>
              {threads.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No threads in <code className="font-mono">eval/agent_threads/</code> yet (Phase 4).
                </p>
              ) : (
                <ul className="space-y-3 text-sm font-mono">
                  {threads.map((t) => (
                    <li key={t.file} className="border-b border-border/40 pb-2">
                      <div className="text-emerald-300/90">{t.file}</div>
                      <pre className="text-[11px] text-muted-foreground whitespace-pre-wrap max-h-40 overflow-auto">
                        {JSON.stringify(t.data, null, 2).slice(0, 800)}
                      </pre>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-4 pt-3 border-t border-border/40">
                <div className="text-xs text-muted-foreground mb-1">Evolve log</div>
                <pre className="text-[11px] font-mono text-muted-foreground whitespace-pre-wrap max-h-48 overflow-auto">
                  {evolveLog.join("\n") || "—"}
                </pre>
              </div>
            </aside>
          )}
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-border/50 pt-3">
          <div className="text-sm text-muted-foreground font-mono truncate">{statusMsg || "Space · C camera · R replay · Esc reset"}</div>
          <Button
            size="lg"
            className="min-w-[200px] h-12 text-base font-semibold bg-emerald-600 hover:bg-emerald-500 text-white"
            onClick={() => void primaryAction()}
            disabled={tone === "streaming" && step !== "Input"}
          >
            {tone === "streaming" && step !== "Input" ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4" />
            )}
            {primaryLabel}
          </Button>
        </div>
      </div>

      <footer className="border-t border-border/60 px-6 py-3 flex items-center justify-between text-sm text-muted-foreground font-mono">
        <span>Built today at the Gemini 4 NYC Hackathon</span>
        <span>
          Commits: {health?.git.commit_count ?? "—"} · First:{" "}
          {health?.git.first_commit_time?.slice(0, 19) ?? "—"}
        </span>
      </footer>
    </div>
  )
}

function InputScreen(props: {
  sourceName: string
  pageCount: number
  selectedPages: number[]
  sessionId: string | null
  cameraOn: boolean
  videoRef: React.RefObject<HTMLVideoElement | null>
  fileRef: React.RefObject<HTMLInputElement | null>
  onFile: (f: File) => void
  onToggleCamera: () => void
  onTogglePage: (p: number) => void
  onQuickSelect: (pages: number[]) => void
}) {
  const {
    sourceName,
    pageCount,
    selectedPages,
    sessionId,
    cameraOn,
    videoRef,
    fileRef,
    onFile,
    onToggleCamera,
    onTogglePage,
    onQuickSelect,
  } = props

  return (
    <div className="flex flex-col gap-5 h-full">
      <div>
        <h2 className="text-3xl font-semibold tracking-tight">Input</h2>
        <p className="text-muted-foreground mt-1 text-base">
          Drop a utility PDF or photograph a printed page. Then pick the pages to read.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <Button variant="secondary" onClick={() => fileRef.current?.click()}>
          <FileUp className="h-4 w-4" />
          Choose PDF
        </Button>
        <Button variant="secondary" onClick={onToggleCamera}>
          <Camera className="h-4 w-4" />
          {cameraOn ? "Close camera" : "Camera (C)"}
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onFile(f)
          }}
        />
      </div>

      {cameraOn && (
        <div className="relative border border-border/60 overflow-hidden max-w-xl">
          <video ref={videoRef} className="w-full aspect-video bg-black" muted playsInline />
          <Badge className="absolute top-2 left-2" variant="outline">
            live camera
          </Badge>
        </div>
      )}

      {sessionId && pageCount > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-mono text-sm text-muted-foreground">
              {sourceName} · {pageCount} pages · selected [{selectedPages.join(", ") || "none"}]
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onQuickSelect(Array.from({ length: Math.min(3, pageCount) }, (_, i) => i + 1))}
              >
                First 3
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onQuickSelect([])}
              >
                Clear
              </Button>
            </div>
          </div>
          <div className="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2 max-h-[340px] overflow-auto pr-1">
            {Array.from({ length: pageCount }, (_, i) => i + 1).map((p) => {
              const sel = selectedPages.includes(p)
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() => onTogglePage(p)}
                  className={`relative border overflow-hidden aspect-[3/4] ${
                    sel ? "border-emerald-400 ring-1 ring-emerald-400/40" : "border-border/50 opacity-80 hover:opacity-100"
                  }`}
                >
                  <img
                    src={thumbnailUrl(sessionId, p)}
                    alt={`Page ${p}`}
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                  <span className="absolute bottom-1 left-1 font-mono text-[11px] bg-black/70 px-1.5 py-0.5">
                    p.{p}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {!sessionId && !cameraOn && (
        <div
          className="flex-1 min-h-[220px] border border-dashed border-border/70 flex items-center justify-center text-muted-foreground"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault()
            const f = e.dataTransfer.files?.[0]
            if (f) onFile(f)
          }}
        >
          Drop PDF here
        </div>
      )}
    </div>
  )
}

function ReadVerifyScreen(props: {
  step: StepId
  sessionId: string | null
  selectedPages: number[]
  constraints: ConstraintRow[]
  statusMsg: string
  readerModel: string
  verifierModel: string
}) {
  const page = props.selectedPages[0]
  return (
    <div className="grid md:grid-cols-2 gap-5 h-full min-h-0">
      <div className="flex flex-col gap-2 min-h-0">
        <h2 className="text-3xl font-semibold">{props.step}</h2>
        <p className="text-muted-foreground text-base">
          Page image beside constraints. Verified with confidence, Needs human review, or Uncheckable.
        </p>
        {props.sessionId && page && (
          <img
            src={thumbnailUrl(props.sessionId, page, 120)}
            alt={`Page ${page}`}
            className="border border-border/50 max-h-[480px] object-contain bg-black/40"
          />
        )}
        <div className="font-mono text-xs text-muted-foreground">
          Reader: {props.readerModel || "—"} · Verifier: {props.verifierModel || "—"}
        </div>
      </div>
      <div className="overflow-auto max-h-[560px] space-y-2">
        {props.constraints.length === 0 && (
          <p className="text-muted-foreground">{props.statusMsg || "Waiting for constraints…"}</p>
        )}
        {props.constraints.map((c) => (
          <div key={c.id} className="border border-border/50 px-3 py-2.5 bg-black/20">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm text-foreground/90">
                {c.entity || "—"} · {c.constraint_type} · p.{c.page}
              </span>
              <span className={`font-mono text-sm ${statusClass(c.ui_status)}`}>
                {c.ui_status ?? "pending"}
              </span>
            </div>
            <div className="text-2xl font-semibold tabular-nums mt-1">
              {c.checkable && c.value != null ? (
                <>
                  {c.value} <span className="text-base text-muted-foreground">{c.unit}</span>
                </>
              ) : (
                <span className="text-base text-muted-foreground">uncheckable</span>
              )}
            </div>
            <p className="text-sm text-muted-foreground mt-1 line-clamp-2">{c.snippet}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

function EvolveScreen(props: {
  log: string[]
  bestScore: number | null
  bestDiff: string
  fallbackRung: string
  streaming: boolean
  traces: Record<string, unknown> | null
  controllerOn: boolean
  setControllerOn: (v: boolean) => void
  torqueNow: number
}) {
  const power = props.traces?.power as Record<string, number[]> | undefined
  const spectrum = props.traces?.spectrum as
    | {
        freqs_hz: number[]
        torque_before_pu: number[]
        torque_after_pu: number[]
        forbidden_bands: Array<{ freq_min_hz: number; freq_max_hz: number }>
      }
    | undefined

  return (
    <div className="flex flex-col gap-4 h-full overflow-auto">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h2 className="text-3xl font-semibold">Evolve</h2>
          <p className="text-muted-foreground">Streaming candidates · checker gate · best score · result traces</p>
        </div>
        <div className="text-right">
          <div className="text-xs uppercase tracking-widest text-muted-foreground font-mono">Best score</div>
          <div className="text-5xl font-semibold tabular-nums text-emerald-300">
            {props.bestScore != null ? props.bestScore.toFixed(1) : "—"}
          </div>
          {props.fallbackRung && (
            <div className="font-mono text-sm text-muted-foreground mt-1">rung: {props.fallbackRung}</div>
          )}
        </div>
      </div>
      <div className="grid md:grid-cols-2 gap-4 min-h-0">
        <pre className="font-mono text-xs border border-border/50 p-3 overflow-auto max-h-[220px] bg-black/30 text-muted-foreground">
          {props.log.join("\n") || (props.streaming ? "Waiting for candidates…" : "—")}
        </pre>
        <pre className="font-mono text-xs border border-border/50 p-3 overflow-auto max-h-[220px] bg-black/30 text-emerald-200/80 whitespace-pre-wrap">
          {props.bestDiff || "Diff of best controller appears here"}
        </pre>
      </div>
      {props.traces && !("error" in props.traces) && (
        <div className="space-y-3 border-t border-border/40 pt-3">
          <div className="flex items-center justify-between">
            <h3 className="text-xl font-semibold">Result · simulated</h3>
            <Button size="sm" variant="secondary" onClick={() => props.setControllerOn(!props.controllerOn)}>
              Controller {props.controllerOn ? "ON" : "OFF"}
            </Button>
          </div>
          <ShaftCanvas torquePu={props.torqueNow} controllerOn={props.controllerOn} height={120} />
          {power && (
            <PowerCanvas
              t={power.t_s ?? []}
              raw={power.p_raw_mw ?? []}
              before={power.p_grid_before_mw ?? []}
              after={power.p_grid_after_mw ?? []}
              height={120}
            />
          )}
          {spectrum && (
            <SpectrumCanvas
              freqs={spectrum.freqs_hz ?? []}
              before={spectrum.torque_before_pu ?? []}
              after={spectrum.torque_after_pu ?? []}
              bands={spectrum.forbidden_bands ?? []}
              height={140}
            />
          )}
        </div>
      )}
    </div>
  )
}

function AttackScreen(props: {
  rhythm: number
  depth: number
  clusterMw: number
  setRhythm: (n: number) => void
  setDepth: (n: number) => void
  setClusterMw: (n: number) => void
  onChange: (r: number, d: number, c: number) => void
  result: Record<string, unknown> | null
  busy: boolean
  survived: number
  failed: number
  traces: Record<string, unknown> | null
  controllerOn: boolean
  setControllerOn: (v: boolean) => void
  torqueNow: number
}) {
  const results = (props.result?.results as Array<Record<string, unknown>>) || []
  const power = props.traces?.power as Record<string, number[]> | undefined
  const spectrum = props.traces?.spectrum as
    | {
        freqs_hz: number[]
        torque_before_pu: number[]
        torque_after_pu: number[]
        forbidden_bands: Array<{ freq_min_hz: number; freq_max_hz: number }>
      }
    | undefined

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-3xl font-semibold">Attack</h2>
          <p className="text-muted-foreground">Break the grid — live simulated workloads (labeled)</p>
        </div>
        <div className="font-mono text-sm">
          survived <span className="text-emerald-300 text-2xl tabular-nums">{props.survived}</span>
          {" · "}
          broke <span className="text-red-300 text-2xl tabular-nums">{props.failed}</span>
          {props.busy && <Loader2 className="inline h-4 w-4 ml-2 animate-spin" />}
        </div>
      </div>

      <div className="grid md:grid-cols-3 gap-6">
        <SliderField
          label="Rhythm (period s)"
          value={props.rhythm}
          min={0.1}
          max={2}
          step={0.05}
          onValue={(v) => {
            props.setRhythm(v)
            props.onChange(v, props.depth, props.clusterMw)
          }}
        />
        <SliderField
          label="Depth"
          value={props.depth}
          min={0.05}
          max={1}
          step={0.05}
          onValue={(v) => {
            props.setDepth(v)
            props.onChange(props.rhythm, v, props.clusterMw)
          }}
        />
        <SliderField
          label="Cluster size (MW)"
          value={props.clusterMw}
          min={20}
          max={300}
          step={5}
          onValue={(v) => {
            props.setClusterMw(v)
            props.onChange(props.rhythm, props.depth, v)
          }}
        />
      </div>

      <div className="flex items-center gap-3">
        <Button
          size="sm"
          variant={props.controllerOn ? "default" : "secondary"}
          onClick={() => props.setControllerOn(!props.controllerOn)}
        >
          Controller {props.controllerOn ? "ON" : "OFF"}
        </Button>
        <span className="text-xs font-mono text-muted-foreground">visual scale · simulated</span>
      </div>

      <ShaftCanvas torquePu={props.torqueNow} controllerOn={props.controllerOn} />

      {power && (
        <PowerCanvas
          t={power.t_s ?? []}
          raw={power.p_raw_mw ?? []}
          before={power.p_grid_before_mw ?? []}
          after={power.p_grid_after_mw ?? []}
        />
      )}
      {spectrum && (
        <SpectrumCanvas
          freqs={spectrum.freqs_hz ?? []}
          before={spectrum.torque_before_pu ?? []}
          after={spectrum.torque_after_pu ?? []}
          bands={spectrum.forbidden_bands ?? []}
        />
      )}

      <div className="grid sm:grid-cols-2 gap-2 max-h-48 overflow-auto">
        {results.map((r) => (
          <div key={String(r.constraint_id)} className="border border-border/40 px-3 py-2 font-mono text-sm">
            <div className="flex justify-between">
              <span>{String(r.constraint_id)}</span>
              <span className={r.passed ? "text-emerald-400" : "text-red-400"}>
                {String(r.status)}
              </span>
            </div>
            <div className="text-muted-foreground text-xs mt-1">
              margin {r.margin_pct != null ? `${Number(r.margin_pct).toFixed(1)}%` : "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function CertifyScreen(props: {
  certificate: Record<string, unknown> | null
  constraints: ConstraintRow[]
  fallbackRung: string
  attacksSurvived: number
  traces: Record<string, unknown> | null
  controllerOn: boolean
  setControllerOn: (v: boolean) => void
  torqueNow: number
  readerModel: string
  verifierModel: string
}) {
  const rules =
    (props.certificate?.rules as Array<Record<string, unknown>>) ||
    props.constraints.filter((c) => c.checkable).map((c) => ({ ...c }))
  const models = (props.certificate?.models as Record<string, { model: string; job: string }>) || {
    reader: { model: props.readerModel || "gemini-4-neon", job: "Document → typed constraints" },
    verifier: {
      model: props.verifierModel || "gemini-decision-1",
      job: "Calibrated yes/no against page image",
    },
    designer: { model: "gemini-4-hydrogen", job: "Parallel controller mutations" },
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-3xl font-semibold">Certify</h2>
          <p className="text-muted-foreground">
            Each rule with page citation, entity, assumptions, margin, and model jobs
          </p>
        </div>
        <div className="text-right font-mono text-sm">
          <div className="text-xs uppercase tracking-widest text-muted-foreground">Outcome</div>
          <div className="text-4xl text-emerald-300">{props.fallbackRung || "—"}</div>
          <div className="text-muted-foreground mt-1">attacks survived: {props.attacksSurvived}</div>
        </div>
      </div>

      <ShaftCanvas torquePu={props.torqueNow} controllerOn={props.controllerOn} height={120} />

      <div className="grid md:grid-cols-3 gap-3">
        {Object.entries(models).map(([k, v]) => (
          <div key={k} className="border border-border/50 px-3 py-2">
            <div className="text-xs uppercase tracking-widest text-muted-foreground font-mono">{k}</div>
            <div className="font-mono text-emerald-300 text-sm mt-1">{v.model}</div>
            <div className="text-sm text-muted-foreground mt-1">{v.job}</div>
          </div>
        ))}
      </div>

      <div className="space-y-2 max-h-[360px] overflow-auto">
        {rules.map((r) => (
          <div key={String(r.id)} className="border border-border/50 px-3 py-3 bg-black/20">
            <div className="flex justify-between gap-2 font-mono text-sm">
              <span>
                {String(r.entity || "—")} · {String(r.constraint_type)} · p.{String(r.page)}
              </span>
              <span
                className={
                  r.status === "pass" || String(r.ui_status || "").startsWith("Verified")
                    ? "text-emerald-400"
                    : "text-amber-300"
                }
              >
                {r.margin_pct != null
                  ? `margin ${Number(r.margin_pct).toFixed(1)}%`
                  : String(r.ui_status || r.status || "")}
              </span>
            </div>
            <div className="text-xl font-semibold mt-1 tabular-nums">
              {r.value != null ? `${r.value} ${r.unit ?? ""}` : "—"}
            </div>
            <p className="text-sm text-muted-foreground mt-1">{String(r.snippet || "")}</p>
            {Array.isArray(r.assumptions) && r.assumptions.length > 0 && (
              <ul className="mt-2 text-xs font-mono text-amber-200/80 space-y-0.5">
                {(r.assumptions as string[]).map((a) => (
                  <li key={a}>assumption: {a}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
      <p className="text-xs font-mono text-muted-foreground">certificate from local loop run · simulated margins</p>
    </div>
  )
}

function SliderField(props: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onValue: (v: number) => void
}) {
  return (
    <div>
      <div className="flex justify-between font-mono text-sm mb-2">
        <span className="text-muted-foreground">{props.label}</span>
        <span className="text-2xl tabular-nums text-foreground">{props.value}</span>
      </div>
      <Slider
        value={[props.value]}
        min={props.min}
        max={props.max}
        step={props.step}
        onValueChange={(v) => {
          const n = Array.isArray(v) ? Number(v[0]) : Number(v)
          if (Number.isFinite(n)) props.onValue(n)
        }}
      />
    </div>
  )
}

export default App
