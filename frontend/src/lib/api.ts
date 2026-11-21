export interface HealthResponse {
  status: string
  mode: string
  api_key_configured: boolean
  git: { commit_count: number; first_commit_time: string | null }
}

export interface ConstraintRow {
  id: string
  constraint_type: string
  value: number | null
  unit: string
  page: number
  snippet: string
  checkable: boolean
  confidence?: number | null
  entity?: string
  verified?: boolean | null
  needs_human_review?: boolean
  assumptions?: string[]
  ui_status?: string
  verify_status?: string
  verify_reason?: string
  window_s?: number | null
  freq_min_hz?: number | null
  freq_max_hz?: number | null
  source_section?: string
  notes?: string
}

export interface UploadResponse {
  session_id: string
  source_name: string
  page_count: number
  rated_mw: number
  thumbnail_url_template?: string
  thumbnail_url?: string
}

async function parseJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? JSON.stringify(body)
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail))
  }
  return res.json() as Promise<T>
}

export async function fetchHealth(): Promise<HealthResponse> {
  return parseJson(await fetch("/api/ui/health"))
}

export async function uploadPdf(file: File, ratedMw = 100): Promise<UploadResponse> {
  const fd = new FormData()
  fd.append("file", file)
  fd.append("rated_mw", String(ratedMw))
  return parseJson(await fetch("/api/ui/upload", { method: "POST", body: fd }))
}

export async function uploadCamera(blob: Blob, ratedMw = 100): Promise<UploadResponse> {
  const fd = new FormData()
  fd.append("file", blob, "camera.jpg")
  fd.append("rated_mw", String(ratedMw))
  return parseJson(await fetch("/api/ui/camera", { method: "POST", body: fd }))
}

export function thumbnailUrl(sessionId: string, page: number, dpi = 72): string {
  return `/api/ui/session/${sessionId}/thumbnail/${page}?dpi=${dpi}`
}

export async function selectPages(sessionId: string, pages: number[]): Promise<void> {
  await parseJson(
    await fetch(`/api/ui/session/${sessionId}/select-pages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages }),
    }),
  )
}

export async function streamPost(
  url: string,
  body: unknown,
  handlers: Record<string, (data: Record<string, unknown>) => void>,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body ?? {}),
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`Stream failed: ${res.status} ${res.statusText}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let eventName = "message"

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split("\n")
    buffer = parts.pop() ?? ""
    for (const line of parts) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim()
      } else if (line.startsWith("data:")) {
        const raw = line.slice(5).trim()
        try {
          const data = JSON.parse(raw) as Record<string, unknown>
          handlers[eventName]?.(data)
          handlers["*"]?.(data)
        } catch {
          /* skip */
        }
        eventName = "message"
      } else if (line.trim() === "") {
        eventName = "message"
      }
    }
  }
}

export async function attack(
  sessionId: string,
  params: { pulse_rate: number; depth: number; cluster_mw: number },
) {
  return parseJson<Record<string, unknown>>(
    await fetch(`/api/ui/session/${sessionId}/attack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }),
  )
}

export async function fetchAgentThreads() {
  return parseJson<{ threads: Array<{ file: string; data: unknown }> }>(
    await fetch("/api/ui/agent-threads"),
  )
}

export async function loadReplaySession() {
  return parseJson<{
    session_id: string
    label: string
    source_name: string
    has_controller: boolean
    has_traces: boolean
    constraints: ConstraintRow[]
    controller_code: string | null
    certificate: Record<string, unknown> | null
    evolve_result: Record<string, unknown> | null
    fallback_rung?: string
  }>(await fetch("/api/ui/replay/load", { method: "POST" }))
}
