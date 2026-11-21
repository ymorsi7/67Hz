import { useCallback, useEffect, useRef, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import {
  Camera,
  Check,
  Copy,
  FileUp,
  Loader2,
  RotateCcw,
  ScrollText,
  X,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Slider } from "@/components/ui/slider"
import { Stepper, STEPS, type StepId } from "@/components/Stepper"
import { SpectrumCanvas } from "@/components/ShaftCanvas"
import { PowerChart } from "@/components/PowerChart"
import { ShaftAnim } from "@/components/ShaftAnim"
import { RuleBar } from "@/components/RuleBar"
import { confirmBadge, plainRule } from "@/lib/plain"
import { MarkdownDoc } from "@/components/MarkdownDoc"
import {
  addImagePage,
  attack,
  confirmConstraint,
  fetchAgentTally,
  fetchDocsResults,
  fetchHealth,
  loadReplaySession,
  removeConstraint,
  selectPages,
  streamPost,
  thumbnailUrl,
  uploadCamera,
  uploadPdf,
  type ConstraintRow,
  type HealthResponse,
} from "@/lib/api"

type Tone = "idle" | "busy" | "error" | "ok"

function metricsFromTraces(tr: Record<string, unknown> | null | undefined) {
  const m = (tr?.metrics as Record<string, unknown> | undefined) ?? undefined
  if (!m) {
    return {
      extra: null as number | null,
      slowed: null as number | null,
      wear: null as number | null,
      spanLabel: null as string | null,
      from30min: false,
      coldSlowed: null as number | null,
    }
  }
  const cold = (m.cold_start as Record<string, unknown> | undefined) ?? undefined
  const span =
    typeof m.span_label === "string"
      ? m.span_label
      : m.from_30min
        ? "Over a 30-minute run"
        : typeof m.duration_s === "number"
          ? `Over a ${m.duration_s}s run`
          : null
  return {
    extra: typeof m.extra_energy_pct === "number" ? m.extra_energy_pct : null,
    slowed: typeof m.training_slowed_pct === "number" ? m.training_slowed_pct : null,
    wear: typeof m.battery_wear === "number" ? m.battery_wear : null,
    spanLabel: span,
    from30min: Boolean(m.from_30min),
    coldSlowed:
      typeof cold?.training_slowed_pct === "number" ? (cold.training_slowed_pct as number) : null,
  }
}

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [step, setStep] = useState<StepId>("Rulebook")
  const [completed, setCompleted] = useState<Set<StepId>>(new Set())
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sourceName, setSourceName] = useState("")
  const [ratedMw, setRatedMw] = useState(100)
  const [pageCount, setPageCount] = useState(0)
  const [selectedPages, setSelectedPages] = useState<number[]>([])
  const [constraints, setConstraints] = useState<ConstraintRow[]>([])
  const [statusMsg, setStatusMsg] = useState("")
  const [tone, setTone] = useState<Tone>("idle")
  const [error, setError] = useState<string | null>(null)
  const [certOpen, setCertOpen] = useState(false)
  const [showUncheckable, setShowUncheckable] = useState(false)
  const [cameraPhase, setCameraPhase] = useState<"off" | "live" | "preview">("off")
  const [cameraError, setCameraError] = useState<string | null>(null)
  const [captureBlob, setCaptureBlob] = useState<Blob | null>(null)
  const [captureUrl, setCaptureUrl] = useState<string | null>(null)

  const [headline, setHeadline] = useState("")
  const [verdict, setVerdict] = useState("")
  const [winnerLine, setWinnerLine] = useState("")
  const [authorLine, setAuthorLine] = useState("")
  const [evolveStatus, setEvolveStatus] = useState("")
  const [metrics, setMetrics] = useState<{
    extra: number | null
    slowed: number | null
    wear: number | null
    spanLabel: string | null
    from30min: boolean
    coldSlowed: number | null
  }>({ extra: null, slowed: null, wear: null, spanLabel: null, from30min: false, coldSlowed: null })
  const [resultsOpen, setResultsOpen] = useState(false)
  const [resultsMd, setResultsMd] = useState<string | null>(null)
  const [failuresMd, setFailuresMd] = useState<string | null>(null)
  const [resultsMsg, setResultsMsg] = useState<string | null>(null)
  const [failuresMsg, setFailuresMsg] = useState<string | null>(null)
  const [pickPdfPages, setPickPdfPages] = useState(false)
  const [noRulePhoto, setNoRulePhoto] = useState(false)
  const [bestDiff, setBestDiff] = useState("")
  const [rawLog, setRawLog] = useState<string[]>([])
  const [controllerCode, setControllerCode] = useState<string | null>(null)
  const [traces, setTraces] = useState<Record<string, unknown> | null>(null)
  const [certificate, setCertificate] = useState<Record<string, unknown> | null>(null)
  const [controllerOn, setControllerOn] = useState(false)
  const [codeOpen, setCodeOpen] = useState(false)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [codeCopied, setCodeCopied] = useState(false)

  const [pulseRate, setPulseRate] = useState(2)
  const [depthPct, setDepthPct] = useState(45)
  const [clusterMw, setClusterMw] = useState(100)
  const [attackResult, setAttackResult] = useState<Record<string, unknown> | null>(null)
  const [checking, setChecking] = useState(false)
  const [survived, setSurvived] = useState(0)
  const [attempts, setAttempts] = useState(0)

  const [agentMsgs, setAgentMsgs] = useState<
    Array<{
      who: string
      summary: string
      payload?: unknown
      stamp?: "confirmed" | "rejected" | null
    }>
  >([])
  const [agentVerdict, setAgentVerdict] = useState<string | null>(null)
  const [agentBusy, setAgentBusy] = useState(false)
  const [agentError, setAgentError] = useState<string | null>(null)
  const [agentJsonOpen, setAgentJsonOpen] = useState<Record<number, boolean>>({})
  const [agentTally, setAgentTally] = useState<{
    running: boolean
    running_since_utc: string | null
    rounds: number
    breaks_found: number
    fixes_confirmed: number
    message: string | null
  } | null>(null)
  const [addRuleBusy, setAddRuleBusy] = useState(false)
  const addFileRef = useRef<HTMLInputElement>(null)
  const addCamInputRef = useRef<HTMLInputElement>(null)

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
    setRatedMw(100)
    setPageCount(0)
    setSelectedPages([])
    setConstraints([])
    setStatusMsg("")
    setTone("idle")
    setError(null)
    setCertOpen(false)
    setHeadline("")
    setVerdict("")
    setWinnerLine("")
    setAuthorLine("")
    setEvolveStatus("")
    setMetrics({ extra: null, slowed: null, wear: null, spanLabel: null, from30min: false, coldSlowed: null })
    setResultsOpen(false)
    setPickPdfPages(false)
    setNoRulePhoto(false)
    setBestDiff("")
    setRawLog([])
    setControllerCode(null)
    setTraces(null)
    setCertificate(null)
    setControllerOn(false)
    setCodeOpen(false)
    setEvidenceOpen(false)
    setAttackResult(null)
    setSurvived(0)
    setAttempts(0)
    setCameraError(null)
    setCaptureBlob(null)
    setCaptureUrl(null)
    setCameraPhase("off")
    setAgentMsgs([])
    setAgentVerdict(null)
    setAgentError(null)
    setAgentBusy(false)
    setAgentTally(null)
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
      setRatedMw(res.rated_mw ?? 100)
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
      setRatedMw(res.rated_mw ?? 100)
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
            setVerdict(String(d.verdict ?? d.headline ?? ""))
            setWinnerLine(String(d.winner ?? ""))
            setAuthorLine(String(d.author_line ?? ""))
            setControllerOn(false)
            setMetrics({
              extra: typeof d.extra_energy_pct === "number" ? d.extra_energy_pct : null,
              slowed: typeof d.training_slowed_pct === "number" ? d.training_slowed_pct : null,
              wear: typeof d.battery_wear === "number" ? d.battery_wear : null,
              spanLabel: null,
              from30min: false,
              coldSlowed: null,
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
              if (st.certificate?.verdict) setVerdict(String(st.certificate.verdict))
              if (st.certificate?.winner) setWinnerLine(String(st.certificate.winner))
              if (st.certificate?.author_line) setAuthorLine(String(st.certificate.author_line))
              if (st.has_traces) {
                const tr = await (await fetch(`/api/ui/session/${sessionId}/traces`)).json()
                setTraces(tr)
                setMetrics(metricsFromTraces(tr))
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
            setVerdict(String(d.verdict ?? d.headline ?? ""))
            setWinnerLine(String(d.winner ?? ""))
            setAuthorLine(String(d.author_line ?? ""))
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
      {
        const outcome = String(
          loaded.certificate?.outcome ?? loaded.evolve_result?.outcome ?? loaded.fallback_rung ?? "",
        )
        const certRules = (loaded.certificate?.rules as unknown[] | undefined) ?? []
        const nRules =
          Number(loaded.certificate?.n_rules) ||
          certRules.length ||
          (Array.isArray(loaded.constraints)
            ? loaded.constraints.filter((c) => c.checkable).length
            : 0)
        // Always prefer structured fields; never show the old combined headline as the verdict
        if (loaded.certificate?.verdict) {
          setVerdict(String(loaded.certificate.verdict))
        } else if (outcome === "refused") {
          setVerdict("GridSafe refused: no controller met the rules.")
        } else if (
          outcome === "evolved_compliant" ||
          outcome === "fallback_b3" ||
          outcome === "fallback_conservative_floor"
        ) {
          setVerdict(`Follows all ${nRules || 1} rules`)
        } else {
          setVerdict(String(loaded.certificate?.headline ?? ""))
        }
        if (loaded.certificate?.winner) {
          setWinnerLine(String(loaded.certificate.winner))
        } else if (outcome === "evolved_compliant") {
          setWinnerLine("Winner: a Gemini-written controller.")
        } else if (outcome === "fallback_b3") {
          setWinnerLine("Winner: the tuned filter baseline (no AI).")
        } else if (outcome === "fallback_conservative_floor") {
          setWinnerLine("Fell back to the safe, expensive setting.")
        } else {
          setWinnerLine("")
        }
        if (loaded.certificate?.author_line) {
          setAuthorLine(String(loaded.certificate.author_line))
        } else if (outcome === "evolved_compliant") {
          setAuthorLine("Written by Gemini 4 Hydrogen, checked by GridSafe")
        } else if (outcome === "fallback_b3") {
          setAuthorLine("Tuned filter baseline (no AI), checked by GridSafe")
        } else if (outcome === "fallback_conservative_floor") {
          setAuthorLine("Safe expensive fallback (no AI), checked by GridSafe")
        } else {
          setAuthorLine("Checked by GridSafe")
        }
      }
      setControllerOn(false)
      if (loaded.has_traces) {
        const tr = await (await fetch(`/api/ui/session/${loaded.session_id}/traces`)).json()
        setTraces(tr)
        setMetrics(metricsFromTraces(tr))
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

  const runReadVerifyAppend = async (sid: string, pages: number[], fromCamera = false) => {
    setAddRuleBusy(true)
    setTone("busy")
    setStatusMsg("Reading new rule…")
    setNoRulePhoto(false)
    setPickPdfPages(false)
    let gotConstraint = false
    try {
      await selectPages(sid, pages)
      await streamPost(
        `/api/ui/session/${sid}/read-verify`,
        { pages, append: true },
        {
          status: (d) => setStatusMsg(String(d.message ?? "")),
          constraint: (d) => {
            const row = d as unknown as ConstraintRow
            gotConstraint = true
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
            if (Number(d.count ?? 0) === 0) gotConstraint = false
          },
          verify_complete: (d) => {
            if (Array.isArray(d.constraints)) setConstraints(d.constraints as ConstraintRow[])
            const appended = Array.isArray(d.constraints)
              ? (d.constraints as ConstraintRow[]).filter((c) => pages.includes(c.page))
              : []
            if (fromCamera && !gotConstraint && appended.length === 0) {
              setNoRulePhoto(true)
              setStatusMsg("No rule found in this photo")
            } else {
              setNoRulePhoto(false)
              setStatusMsg("Rules updated — design a controller against every confirmed rule")
            }
            setControllerCode(null)
            setTraces(null)
            setCertificate(null)
            setHeadline("")
            setVerdict("")
            setTone("ok")
            setStep("Rules")
          },
          error: (d) => {
            setError(String(d.message ?? "Read failed"))
            setTone("error")
          },
        },
      )
    } catch (err) {
      setError(String(err))
      setTone("error")
    } finally {
      setAddRuleBusy(false)
    }
  }

  const openResults = async () => {
    setResultsOpen(true)
    try {
      const docs = await fetchDocsResults()
      setResultsMd(docs.results.exists ? docs.results.markdown : null)
      setFailuresMd(docs.failures.exists ? docs.failures.markdown : null)
      setResultsMsg(docs.results.message)
      setFailuresMsg(docs.failures.message)
    } catch (err) {
      setResultsMd(null)
      setFailuresMd(null)
      setResultsMsg(String(err))
      setFailuresMsg(null)
    }
  }

  const onHumanConfirm = async (id: string) => {
    if (!sessionId) return
    try {
      const res = await confirmConstraint(sessionId, id)
      setConstraints(res.constraints)
      setControllerCode(null)
      setTraces(null)
      setCertificate(null)
      setHeadline("")
      setVerdict("")
      setStatusMsg("Rule confirmed by a person — design a controller against every confirmed rule")
    } catch (err) {
      setError(String(err))
    }
  }

  const onAddCameraRule = async (file: File) => {
    if (!sessionId) return
    try {
      const res = await addImagePage(sessionId, file)
      setPageCount(res.page_count)
      await runReadVerifyAppend(sessionId, [res.page], true)
    } catch (err) {
      setError(String(err))
      setTone("error")
    }
  }

  const onRemoveRule = async (id: string) => {
    if (!sessionId) return
    try {
      const res = await removeConstraint(sessionId, id)
      setConstraints(res.constraints)
      setControllerCode(null)
      setTraces(null)
      setCertificate(null)
      setHeadline("")
      setVerdict("")
    } catch (err) {
      setError(String(err))
    }
  }

  const refreshAgentTally = async () => {
    try {
      const t = await fetchAgentTally()
      setAgentTally(t)
      setAgentError(null)
    } catch {
      setAgentTally(null)
      setAgentError("Agents not connected")
    }
  }

  useEffect(() => {
    if (step !== "Break") return
    void refreshAgentTally()
    const id = window.setInterval(() => void refreshAgentTally(), 15000)
    return () => window.clearInterval(id)
  }, [step])

  const runAgentHarden = async () => {
    if (!controllerCode || !constraints.length) return
    setAgentBusy(true)
    setAgentMsgs([])
    setAgentVerdict(null)
    setAgentError(null)
    try {
      await streamPost("/api/agents/harden", {
        controller_code: controllerCode,
        constraints: constraints.filter((c) => c.checkable && c.verified),
        rated_mw: ratedMw,
      }, {
        status: (d) => {
          if (d.ok === false || (typeof d.message === "string" && /disable|unavail|error|off/i.test(String(d.message)))) {
            setAgentError(String(d.message || "Agents not connected"))
          }
        },
        agent_message: (d) => {
          const sender = String(d.sender || "")
          const who =
            sender.includes("engineer") || sender === "gridsafe-engineer"
              ? "Engineer agent (Gemini 4 Hydrogen)"
              : sender.includes("hardener") || sender === "gridsafe-hardener"
                ? "Hardener agent (Gemini 4 Carbon)"
                : sender || "Agent"
          setAgentMsgs((prev) => [
            ...prev,
            { who, summary: String(d.summary || ""), payload: d.payload, stamp: null },
          ])
        },
        backend_check: (d) => {
          let stamp: "confirmed" | "rejected" | null = null
          if (d.break_found === true || d.passed === true) stamp = "confirmed"
          else if (d.break_found === false || d.passed === false) stamp = "rejected"
          setAgentMsgs((prev) => [
            ...prev,
            {
              who: "GridSafe",
              summary: String(d.summary || "Backend check"),
              payload: d.detail ?? d,
              stamp,
            },
          ])
        },
        verdict: (d) => {
          setAgentVerdict(String(d.message || d.verdict || ""))
          if (d.verdict === "agents_disabled" || d.verdict === "unavailable") {
            setAgentError(String(d.message || "Agents not connected"))
          }
          if (typeof d.fixed_controller_code === "string" && d.fixed_controller_code) {
            setControllerCode(d.fixed_controller_code)
          }
        },
        error: (d) => {
          setAgentError(String(d.message || "Agents not connected"))
        },
      })
    } catch (err) {
      setAgentError("Agents not connected")
      console.warn(err)
    } finally {
      setAgentBusy(false)
      void refreshAgentTally()
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
  const ruleBars = (traces?.rule_bars as Array<Record<string, unknown>> | undefined) ?? []

  const stricterTypes = (() => {
    const groups = new Map<string, ConstraintRow[]>()
    for (const c of checkable) {
      const key = `${c.constraint_type}|${c.window_s ?? ""}|${c.freq_min_hz ?? ""}|${c.freq_max_hz ?? ""}`
      const list = groups.get(key) ?? []
      list.push(c)
      groups.set(key, list)
    }
    const ids = new Set<string>()
    for (const list of groups.values()) {
      if (list.length >= 2) list.forEach((c) => ids.add(c.id))
    }
    return ids
  })()

  const tallyLine = (() => {
    if (agentError && !agentTally) return null
    if (!agentTally) return null
    if (agentTally.message && !agentTally.running && agentTally.rounds === 0) {
      return null
    }
    const since = agentTally.running_since_utc
    let hhmm = "—"
    if (since) {
      try {
        const d = new Date(since)
        hhmm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
      } catch {
        hhmm = since.slice(11, 16)
      }
    }
    return `The hardener agent has been attacking since ${hhmm}: ${agentTally.rounds} rounds, ${agentTally.breaks_found} breaks found, ${agentTally.fixes_confirmed} fixes confirmed.`
  })()

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
            <img
              src="/favicon.png"
              alt=""
              width={40}
              height={40}
              className="h-10 w-10 rounded-[10px] object-cover shadow-[0_0_0_1px_rgba(255,255,255,0.06)]"
              decoding="async"
            />
            <div>
              <h1 className="text-2xl font-semibold tracking-tight leading-none">GridSafe</h1>
              <p className="text-sm text-muted-foreground mt-1">Rulebook → controller that follows it</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => void openResults()}>
              Results
            </Button>
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
                <div className="space-y-4">
                  <div>
                    <h2 className="text-3xl font-semibold tracking-tight">Rulebook</h2>
                    <p className="text-muted-foreground text-base mt-1">
                      {subtitle.Rulebook} Give GridSafe a utility&apos;s rulebook — it builds a controller that
                      follows it, and proves it.
                    </p>
                  </div>

                  {cameraError && (
                    <div className="border border-amber-500/40 bg-amber-950/40 text-amber-100 px-4 py-3 text-sm">
                      {cameraError}
                    </div>
                  )}

                  {cameraPhase === "off" && !sessionId && (
                    <div className="max-w-3xl mt-2 space-y-3">
                      <p className="text-lg text-muted-foreground">Choose one to start</p>
                      <div className="grid sm:grid-cols-[1fr_auto_1fr] gap-3 items-stretch">
                        <button
                          type="button"
                          onClick={() => fileRef.current?.click()}
                          className="min-h-[100px] border-2 border-emerald-500/40 bg-emerald-500/5 hover:bg-emerald-500/10 px-5 py-5 text-left"
                        >
                          <FileUp className="h-7 w-7 text-emerald-400 mb-2" />
                          <div className="text-xl font-semibold">Upload a PDF</div>
                          <div className="text-muted-foreground mt-1 text-sm">Utility interconnection document</div>
                        </button>
                        <div className="hidden sm:flex items-center justify-center text-muted-foreground text-lg font-medium px-1">
                          or
                        </div>
                        <button
                          type="button"
                          onClick={() => void openCamera()}
                          className="min-h-[100px] border-2 border-emerald-500/40 bg-emerald-500/5 hover:bg-emerald-500/10 px-5 py-5 text-left"
                        >
                          <Camera className="h-7 w-7 text-emerald-400 mb-2" />
                          <div className="text-xl font-semibold">Use camera</div>
                          <div className="text-muted-foreground mt-1 text-sm">Printed or handwritten page</div>
                        </button>
                      </div>
                      <p className="sm:hidden text-center text-muted-foreground">or</p>
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
                      <button
                        type="button"
                        className="text-sm text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
                        onClick={() => {
                          setSessionId(null)
                          setSourceName("")
                          setPageCount(0)
                          setSelectedPages([])
                        }}
                      >
                        ← Back
                      </button>
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
                      <div className="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2 max-h-[280px] overflow-auto">
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
                    <p className="text-sm text-muted-foreground mt-1">
                      The controller is built against every confirmed rule in this list.
                    </p>
                  </div>
                  <div className="grid md:grid-cols-2 gap-5">
                    <div>
                      {sessionId && (
                        <img
                          src={thumbnailUrl(sessionId, activePage, 120)}
                          alt={`Page ${activePage}`}
                          className="border border-border/50 max-h-[420px] object-contain bg-black/40 w-full"
                        />
                      )}
                    </div>
                    <div className="space-y-2 overflow-auto max-h-[480px]">
                      {tone === "busy" && !checkable.length && (
                        <p className="text-muted-foreground flex items-center gap-2">
                          <Loader2 className="h-4 w-4 animate-spin" /> {statusMsg || "Reading…"}
                        </p>
                      )}
                      {noRulePhoto && (
                        <div className="border border-amber-500/40 bg-amber-950/30 px-4 py-3 space-y-2">
                          <p className="text-base text-amber-100">No rule found in this photo</p>
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => {
                              setNoRulePhoto(false)
                              addCamInputRef.current?.click()
                            }}
                          >
                            Retake
                          </Button>
                        </div>
                      )}
                      {checkable.map((c) => {
                        const badge = confirmBadge(c)
                        const source =
                          c.source_label ||
                          (c.entity ? `${c.entity}, page ${c.page}` : `page ${c.page}`)
                        return (
                          <div key={c.id} className="border border-border/50 px-3 py-3 bg-black/20">
                            <div className="flex items-start justify-between gap-2">
                              <p className="text-lg leading-snug">{plainRule(c)}</p>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="shrink-0 text-muted-foreground"
                                onClick={() => void onRemoveRule(c.id)}
                              >
                                Remove
                              </Button>
                            </div>
                            <p className="text-xs text-muted-foreground mt-2">{source}</p>
                            {stricterTypes.has(c.id) && (
                              <p className="text-xs text-amber-200 mt-1">the stricter rule decides</p>
                            )}
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
                                <>
                                  <span className="text-xs text-amber-200/90">
                                    GridSafe will not build on a rule it cannot confirm.
                                  </span>
                                  <Button
                                    size="sm"
                                    variant="secondary"
                                    onClick={() => void onHumanConfirm(c.id)}
                                  >
                                    I checked the page: this rule is correct
                                  </Button>
                                </>
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
                      <div className="pt-2 flex flex-wrap gap-2">
                        <Button
                          variant="secondary"
                          disabled={!sessionId || addRuleBusy || tone === "busy"}
                          onClick={() => addCamInputRef.current?.click()}
                        >
                          {addRuleBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Camera className="h-4 w-4" />}
                          Add another rule
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={!sessionId || addRuleBusy || pageCount < 1}
                          onClick={() => setPickPdfPages((v) => !v)}
                        >
                          <FileUp className="h-4 w-4" />
                          Another PDF page
                        </Button>
                        <input
                          ref={addCamInputRef}
                          type="file"
                          accept="image/*"
                          capture="environment"
                          className="hidden"
                          onChange={(e) => {
                            const f = e.target.files?.[0]
                            if (f) void onAddCameraRule(f)
                            e.target.value = ""
                          }}
                        />
                        <input
                          ref={addFileRef}
                          type="file"
                          accept="image/*"
                          className="hidden"
                          onChange={(e) => {
                            const f = e.target.files?.[0]
                            if (!f || !sessionId) return
                            void onAddCameraRule(f)
                            e.target.value = ""
                          }}
                        />
                      </div>
                      {pickPdfPages && sessionId && (
                        <div className="space-y-2 pt-2">
                          <p className="text-sm text-muted-foreground">Pick a page to add</p>
                          <div className="grid grid-cols-4 sm:grid-cols-6 gap-2 max-h-[220px] overflow-auto">
                            {Array.from({ length: pageCount }, (_, i) => i + 1).map((p) => (
                              <button
                                key={p}
                                type="button"
                                disabled={addRuleBusy}
                                onClick={() => void runReadVerifyAppend(sessionId, [p])}
                                className="relative border border-border/50 aspect-[3/4] overflow-hidden hover:border-emerald-400"
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
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {step === "Controller" && (
                <div className="space-y-5 overflow-auto max-h-[72vh] pr-1">
                  {tone === "busy" && (
                    <p className="text-lg text-amber-100 flex items-center gap-2">
                      <Loader2 className="h-5 w-5 animate-spin" />
                      {evolveStatus || "Designing…"}
                    </p>
                  )}

                  {/* a) Verdict */}
                  {(verdict || headline) && (
                    <div className="space-y-2">
                      {String(verdict || headline).toLowerCase().includes("refuse") ? (
                        <p className="text-3xl sm:text-4xl font-semibold text-red-300 leading-tight">
                          {verdict || headline}
                        </p>
                      ) : (
                        <p className="text-3xl sm:text-4xl font-semibold text-emerald-300 leading-tight flex items-start gap-3">
                          <Check className="h-9 w-9 sm:h-10 sm:w-10 shrink-0 mt-1" strokeWidth={3} />
                          <span>{verdict || headline}</span>
                        </p>
                      )}
                      {winnerLine && (
                        <p className="text-xl text-foreground/90 leading-snug">{winnerLine}</p>
                      )}
                    </div>
                  )}

                  {/* b) Large GridSafe switch */}
                  {controllerCode && (
                    <button
                      type="button"
                      onClick={() => setControllerOn((v) => !v)}
                      className={`w-full max-w-md h-16 sm:h-20 border-2 flex items-center justify-between px-6 transition-colors ${
                        controllerOn
                          ? "border-emerald-400 bg-emerald-500/15 text-emerald-200"
                          : "border-red-400/70 bg-red-950/30 text-red-100"
                      }`}
                      aria-pressed={controllerOn}
                    >
                      <span className="text-2xl sm:text-3xl font-semibold tracking-tight">
                        GridSafe {controllerOn ? "ON" : "OFF"}
                      </span>
                      <span
                        className={`relative w-16 h-9 rounded-full transition-colors ${
                          controllerOn ? "bg-emerald-500" : "bg-red-500/80"
                        }`}
                      >
                        <span
                          className={`absolute top-1 size-7 rounded-full bg-white transition-transform ${
                            controllerOn ? "translate-x-8" : "translate-x-1"
                          }`}
                        />
                      </span>
                    </button>
                  )}

                  {/* c) One bar per rule */}
                  {ruleBars.length > 0 && (
                    <div className="space-y-2 max-w-3xl">
                      {ruleBars.map((r, i) => {
                        const used = controllerOn
                          ? (r.used_pct_on as number | null)
                          : (r.used_pct_off as number | null)
                        return (
                          <RuleBar
                            key={String(r.id ?? i)}
                            plain={String(r.plain)}
                            usedPct={used}
                            cannotCheck={
                              used == null
                                ? (r.cannot_check_reason as string | undefined)
                                : undefined
                            }
                            mode={controllerOn ? "on" : "off"}
                          />
                        )
                      })}
                    </div>
                  )}

                  {/* d) Power chart */}
                  {power && traces && !("error" in traces) && (
                    <div className="space-y-2 max-w-4xl">
                      <h3 className="text-xl font-semibold">Power the data center pulls from the grid</h3>
                      <PowerChart
                        t={power.t_s ?? []}
                        without={power.without_mw ?? power.p_grid_before_mw ?? []}
                        withCtrl={power.with_mw ?? power.p_grid_after_mw ?? []}
                        gridSafeOn={controllerOn}
                      />
                      <p className="text-sm text-muted-foreground max-w-2xl">
                        Every AI training step makes the power jump. The controller smooths it out without
                        stopping the training.
                      </p>
                    </div>
                  )}

                  {/* e) Shaft */}
                  {shaft && traces && !("error" in traces) && (
                    <div className="space-y-2 max-w-4xl">
                      <ShaftAnim
                        gridSafeOn={controllerOn}
                        stressBeforePu={Number(shaft?.stress_before_pu ?? shaft?.max_torque_before_pu ?? 0)}
                        stressAfterPu={Number(shaft?.stress_after_pu ?? shaft?.max_torque_after_pu ?? 0)}
                        twistsBefore={(shaft?.peak_twist_before as number[]) ?? []}
                        twistsAfter={(shaft?.peak_twist_after as number[]) ?? []}
                        timesLower={(shaft?.times_lower as number) ?? null}
                        height={110}
                      />
                      <p className="text-sm text-muted-foreground max-w-2xl">
                        Why utilities care: these jumps shake the spinning shafts inside power plant
                        generators.
                        {typeof shaft.times_lower === "number" && shaft.times_lower > 1
                          ? ` Stress is ${shaft.times_lower} times lower with GridSafe.`
                          : ""}{" "}
                        <span className="font-mono text-xs">(simulated, visual scale)</span>
                      </p>
                    </div>
                  )}

                  {/* f) Cost */}
                  {(metrics.extra != null || metrics.slowed != null) && (
                    <div className="space-y-1">
                      <p className="text-lg">
                        {metrics.from30min || metrics.spanLabel?.includes("30-minute")
                          ? "Over a 30-minute run"
                          : metrics.spanLabel || "Cost"}
                        :{" "}
                        {metrics.extra != null ? `${metrics.extra.toFixed(1)}%` : "—"} extra energy,
                        training {metrics.slowed != null ? `${metrics.slowed.toFixed(1)}%` : "—"} slower.
                      </p>
                      {metrics.coldSlowed != null && (
                        <p className="text-xs text-muted-foreground">
                          First 20 seconds after a cold start: {metrics.coldSlowed.toFixed(1)}% slower,
                          because the rule limits how fast power may rise.
                        </p>
                      )}
                    </div>
                  )}

                  {/* g) Buttons */}
                  {controllerCode && (
                    <div className="flex flex-wrap gap-3 items-center">
                      <Button variant="secondary" onClick={() => setCodeOpen(true)}>
                        View controller code
                      </Button>
                      <Button variant="secondary" onClick={() => setCertOpen(true)}>
                        <ScrollText className="h-4 w-4" />
                        Certificate
                      </Button>
                      <Button
                        size="lg"
                        className="bg-emerald-600 hover:bg-emerald-500 text-white h-12 text-base font-semibold"
                        onClick={() => {
                          setStep("Break")
                          runAttack(pulseRate, depthPct, clusterMw, true)
                        }}
                      >
                        Try to break it
                      </Button>
                    </div>
                  )}

                  {/* h) Evidence (closed by default) */}
                  {controllerCode && (
                    <details
                      className="text-sm"
                      open={evidenceOpen}
                      onToggle={(e) => setEvidenceOpen((e.target as HTMLDetailsElement).open)}
                    >
                      <summary className="cursor-pointer text-muted-foreground">Show the evidence</summary>
                      <div className="mt-3 space-y-3">
                        {spectrum && (
                          <SpectrumCanvas
                            freqs={spectrum.freqs_hz ?? []}
                            without={spectrum.without_mw ?? []}
                            withCtrl={spectrum.with_mw ?? []}
                            limitBands={spectrum.limit_bands ?? []}
                            controllerOn={controllerOn}
                          />
                        )}
                        <pre className="font-mono text-[11px] text-muted-foreground whitespace-pre-wrap max-h-40 overflow-auto border border-border/40 p-2">
                          {rawLog.slice(-12).join("\n") || "—"}
                        </pre>
                        {bestDiff && (
                          <pre className="font-mono text-[11px] text-muted-foreground whitespace-pre-wrap max-h-40 overflow-auto border border-border/40 p-2">
                            {bestDiff}
                          </pre>
                        )}
                      </div>
                    </details>
                  )}
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

                  <div className="space-y-2 max-w-3xl">
                    {((attackResult?.results as Array<Record<string, unknown>>) || []).map((r, i) => (
                      <RuleBar
                        key={i}
                        plain={String(r.plain)}
                        usedPct={r.used_pct_of_limit as number | null | undefined}
                        cannotCheck={r.cannot_check_reason as string | undefined}
                        mode="attack"
                      />
                    ))}
                  </div>

                  {attackResult && !(attackResult.held || attackResult.passed) && (
                    <Button size="lg" onClick={() => void fixAttack()}>
                      Fix it
                    </Button>
                  )}

                  <div className="border-t border-border/40 pt-4 space-y-3">
                    {tallyLine && (
                      <p className="text-sm text-muted-foreground font-mono">{tallyLine}</p>
                    )}
                    {agentError && (
                      <p className="text-sm text-amber-200">{agentError}</p>
                    )}
                    <Button
                      size="lg"
                      variant="secondary"
                      disabled={agentBusy || !controllerCode}
                      onClick={() => void runAgentHarden()}
                    >
                      {agentBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                      Let the AI agents attack it
                    </Button>
                    {agentMsgs.length > 0 && (
                      <div className="space-y-2 max-w-3xl">
                        {agentMsgs.map((m, i) => (
                          <div key={i} className="border border-border/40 px-3 py-2">
                            <div className="text-xs font-mono text-muted-foreground">{m.who}</div>
                            <p className="text-base mt-0.5">{m.summary}</p>
                            {m.stamp && (
                              <p
                                className={`text-xs mt-1 font-mono ${
                                  m.stamp === "confirmed" ? "text-emerald-300" : "text-red-300"
                                }`}
                              >
                                Checked by GridSafe: {m.stamp}
                              </p>
                            )}
                            {m.payload != null && (
                              <button
                                type="button"
                                className="text-xs text-muted-foreground underline mt-1"
                                onClick={() =>
                                  setAgentJsonOpen((prev) => ({ ...prev, [i]: !prev[i] }))
                                }
                              >
                                {agentJsonOpen[i] ? "Hide JSON" : "Show JSON"}
                              </button>
                            )}
                            {agentJsonOpen[i] && (
                              <pre className="mt-1 font-mono text-[10px] text-muted-foreground whitespace-pre-wrap max-h-32 overflow-auto">
                                {JSON.stringify(m.payload, null, 2)}
                              </pre>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {agentVerdict && (
                      <p className="text-lg font-semibold max-w-3xl">{agentVerdict}</p>
                    )}
                  </div>
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

      {resultsOpen && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-end sm:items-center justify-center p-4">
          <div className="bg-background border border-border w-full max-w-4xl max-h-[90vh] overflow-auto p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-2xl font-semibold">Results</h3>
              <Button variant="ghost" size="sm" onClick={() => setResultsOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="space-y-8">
              <section>
                <h4 className="text-lg font-semibold mb-2 font-mono text-muted-foreground">RESULTS.md</h4>
                <MarkdownDoc markdown={resultsMd} emptyLabel={resultsMsg || "not generated yet"} />
              </section>
              <section>
                <h4 className="text-lg font-semibold mb-2 font-mono text-muted-foreground">FAILURES.md</h4>
                <MarkdownDoc markdown={failuresMd} emptyLabel={failuresMsg || "not generated yet"} />
              </section>
            </div>
          </div>
        </div>
      )}

      {codeOpen && controllerCode && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-end sm:items-center justify-center p-4">
          <div className="bg-background border border-border w-full max-w-3xl max-h-[85vh] overflow-auto p-5">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-2xl font-semibold">Controller code</h3>
              <Button variant="ghost" size="sm" onClick={() => setCodeOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <p className="text-sm text-muted-foreground mb-3">
              {authorLine || "Checked by GridSafe"}
            </p>
            <div className="flex gap-2 mb-3">
              <Button
                size="sm"
                variant="secondary"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(controllerCode)
                    setCodeCopied(true)
                    window.setTimeout(() => setCodeCopied(false), 1500)
                  } catch {
                    /* ignore */
                  }
                }}
              >
                {codeCopied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                {codeCopied ? "Copied" : "Copy"}
              </Button>
            </div>
            <pre className="font-mono text-xs whitespace-pre-wrap border border-border/50 p-3 bg-black/40 max-h-[60vh] overflow-auto">
              {controllerCode}
            </pre>
          </div>
        </div>
      )}

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
                human_confirmed: c.human_confirmed,
                confirm_label: c.human_confirmed
                  ? "Confirmed by a person, not by the Verifier"
                  : null,
              }))).map((r, i) => (
                <div key={i} className="border border-border/50 px-3 py-3">
                  <p className="text-base">{String(r.plain)}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {String(r.entity || "Utility")} · page {String(r.page)}
                    {r.used_pct_of_limit != null ? ` · uses ${r.used_pct_of_limit}% of the limit` : ""}
                  </p>
                  {(r.confirm_label || r.human_confirmed) && (
                    <p className="text-sm mt-2 text-emerald-300">
                      {String(r.confirm_label || "Confirmed by a person, not by the Verifier")}
                    </p>
                  )}
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
