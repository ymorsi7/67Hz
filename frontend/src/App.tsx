import { useEffect, useState } from "react"
import { motion } from "motion/react"
import { Activity, ShieldCheck, Radio } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

interface HealthResponse {
  status: string
  mode: string
  api_key_configured: boolean
  git: {
    commit_count: number
    first_commit_time: string | null
  }
}

const STEPS = ["Input", "Read", "Verify", "Evolve", "Attack", "Certify"] as const

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [sseSteps, setSseSteps] = useState<string[]>([])
  const [streaming, setStreaming] = useState(false)

  useEffect(() => {
    fetch("/api/health")
      .then((res) => res.json())
      .then((data: HealthResponse) => setHealth(data))
      .catch(() => setHealth(null))
  }, [])

  const testStream = () => {
    setSseSteps([])
    setStreaming(true)
    const source = new EventSource("/api/stream/ping")
    source.addEventListener("step", (ev) => {
      const parsed = JSON.parse((ev as MessageEvent).data) as { name: string }
      setSseSteps((prev) => [...prev, parsed.name])
    })
    source.addEventListener("done", () => {
      setStreaming(false)
      source.close()
    })
    source.onerror = () => {
      setStreaming(false)
      source.close()
    }
  }

  return (
    <div className="min-h-screen flex flex-col justify-between p-8 max-w-6xl mx-auto">
      <header className="flex items-center justify-between border-b border-border pb-4">
        <div className="flex items-center gap-3">
          <ShieldCheck className="h-7 w-7 text-emerald-400" />
          <span className="text-2xl font-semibold tracking-tight">GridSafe</span>
          <Badge variant="outline" className="font-mono text-xs">
            STEP 0 SCAFFOLD
          </Badge>
        </div>
        <div className="flex items-center gap-3 font-mono text-sm">
          <span className="inline-flex items-center gap-2 text-emerald-400">
            <Radio className="h-4 w-4" />
            {health ? `API ${health.status.toUpperCase()}` : "CONNECTING"}
          </span>
        </div>
      </header>

      <main className="my-10 space-y-6">
        <div className="grid grid-cols-6 gap-2">
          {STEPS.map((step, i) => (
            <motion.div
              key={step}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.04 }}
              className={`rounded-lg border px-4 py-3 font-mono text-sm ${
                sseSteps.includes(step)
                  ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-300"
                  : "border-border bg-card text-muted-foreground"
              }`}
            >
              {i + 1}. {step}
            </motion.div>
          ))}
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-xl">
              <Activity className="h-5 w-5 text-emerald-400" />
              Agent Flow Ready
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-muted-foreground text-base">
              FastAPI backend, SSE streaming, Vite + React + TypeScript + Tailwind + shadcn/ui +
              Motion + Geist Sans/Mono are wired and verified.
            </p>
            <Button onClick={testStream} disabled={streaming}>
              {streaming ? "Streaming SSE..." : "Test SSE Stream"}
            </Button>
          </CardContent>
        </Card>
      </main>

      <footer className="border-t border-border pt-4 flex items-center justify-between text-sm text-muted-foreground font-mono">
        <span>Built today at the Gemini 4 NYC Hackathon</span>
        <span>
          Commits: {health?.git.commit_count ?? "—"} · First:{" "}
          {health?.git.first_commit_time ?? "—"}
        </span>
      </footer>
    </div>
  )
}

export default App
