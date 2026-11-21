/** Minimal markdown → React for RESULTS.md / FAILURES.md. No extra libraries. */
import type { ReactNode } from "react"

function renderInline(text: string): ReactNode[] {
  const parts: ReactNode[] = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith("**")) {
      parts.push(
        <strong key={i++} className="font-semibold text-foreground">
          {tok.slice(2, -2)}
        </strong>,
      )
    } else {
      parts.push(
        <code key={i++} className="font-mono text-[0.9em] bg-black/40 px-1 rounded">
          {tok.slice(1, -1)}
        </code>,
      )
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function parseTable(rows: string[]): ReactNode {
  const cells = rows.map((r) =>
    r
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim()),
  )
  if (cells.length < 2) return null
  const head = cells[0]!
  const body = cells.slice(2) // skip separator
  return (
    <div key={rows.join("\n")} className="overflow-x-auto my-3 border border-border/50">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="bg-black/40">
            {head.map((h, i) => (
              <th key={i} className="text-left px-3 py-2 font-semibold border-b border-border/40">
                {renderInline(h)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri} className="border-b border-border/30">
              {row.map((c, ci) => (
                <td key={ci} className="px-3 py-2 align-top font-mono text-[12px] tabular-nums">
                  {renderInline(c)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function MarkdownDoc({ markdown, emptyLabel }: { markdown: string | null; emptyLabel: string }) {
  if (!markdown) {
    return <p className="text-muted-foreground text-base">{emptyLabel}</p>
  }

  const lines = markdown.replace(/\r\n/g, "\n").split("\n")
  const nodes: ReactNode[] = []
  let i = 0
  let para: string[] = []
  let list: string[] = []
  let table: string[] = []

  const flushPara = () => {
    if (!para.length) return
    nodes.push(
      <p key={`p-${nodes.length}`} className="text-sm leading-relaxed my-2 text-foreground/90">
        {renderInline(para.join(" "))}
      </p>,
    )
    para = []
  }
  const flushList = () => {
    if (!list.length) return
    nodes.push(
      <ul key={`ul-${nodes.length}`} className="list-disc pl-5 my-2 space-y-1 text-sm">
        {list.map((item, idx) => (
          <li key={idx}>{renderInline(item)}</li>
        ))}
      </ul>,
    )
    list = []
  }
  const flushTable = () => {
    if (!table.length) return
    const t = parseTable(table)
    if (t) nodes.push(t)
    table = []
  }

  while (i < lines.length) {
    const line = lines[i]!
    if (line.trim().startsWith("|")) {
      flushPara()
      flushList()
      table.push(line)
      i++
      continue
    }
    if (table.length) flushTable()

    if (/^#{1,3}\s/.test(line)) {
      flushPara()
      flushList()
      const level = line.match(/^#+/)![0].length
      const text = line.replace(/^#+\s*/, "")
      const Tag = (level === 1 ? "h2" : level === 2 ? "h3" : "h4") as "h2" | "h3" | "h4"
      const cls =
        level === 1
          ? "text-2xl font-semibold mt-4 mb-2"
          : level === 2
            ? "text-xl font-semibold mt-4 mb-2"
            : "text-lg font-semibold mt-3 mb-1"
      nodes.push(
        <Tag key={`h-${nodes.length}`} className={cls}>
          {renderInline(text)}
        </Tag>,
      )
      i++
      continue
    }

    if (/^[-*]\s+/.test(line.trim())) {
      flushPara()
      list.push(line.trim().replace(/^[-*]\s+/, ""))
      i++
      continue
    }
    if (list.length && line.trim() === "") {
      flushList()
      i++
      continue
    }

    if (line.trim() === "") {
      flushPara()
      flushList()
      i++
      continue
    }

    if (/^---+$/.test(line.trim())) {
      flushPara()
      flushList()
      nodes.push(<hr key={`hr-${nodes.length}`} className="border-border/40 my-4" />)
      i++
      continue
    }

    para.push(line.trim())
    i++
  }
  flushTable()
  flushList()
  flushPara()

  return <div className="space-y-1">{nodes}</div>
}
