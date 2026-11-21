# GridSafe API Notes (Verified 2026-11-21)

Verified against live Google AI Studio documentation (`https://aistudio.google.com/llms.txt` and linked `.md` pages) and live-probed with our API key in `.env` using `google-genai==3.6.1`.

## 1. SDK & API Version
- **Canonical Python SDK**: `google-genai>=3.2.0` (installed: `3.6.1`). Never use legacy `google-generativeai`.
- **API Version**:
  - Standard Interactions API works with default `genai.Client()`, and **v1beta is required** for Decisions API (`client.decisions.create`), Managed Agents (`client.agents.*`, `antigravity-preview-09-2026`), and Hooks.
  - Standardize on `client = genai.Client(http_options={"api_version": "v1beta"})`.
- **Auth**: Reads `GEMINI_API_KEY` from environment (loaded via `python-dotenv` from `.env`, which is gitignored).

## 2. Verified Model IDs & Live Probe Status (2026-11-21)
| Job | Model / Agent ID | API | Live Probe | Context / Output | Thinking Levels (Default) |
| --- | --- | --- | --- | --- | --- |
| Reader | `gemini-4-neon` | Interactions API | **OK** | 1M / 64K (1M long decode) | `low`, `medium`, `high` *(default)*, `xhigh` |
| Verifier | `gemini-decision-1` | Decisions API (`v1beta`) | **OK** (`noul=0.99`, `confidence=0.99`) | 262,144 tokens / 0 output tokens | n/a (zero output tokens) |
| Designer | `gemini-4-hydrogen` | Interactions API | **OK** | 1M / 64K (1M long decode) | `low`, `medium`, `high` *(default)*, `xhigh` |
| Red-team / Hardener | `gemini-4-carbon` | Interactions API + Antigravity `agent_config` | **OK** | 1M / 64K (1M long decode) | `low`, `medium` *(default)*, `high` |
| Older comparison | `gemini-3.8-flash` | Interactions API | **OK** | 1M | `low`, `medium` *(default)*, `high` |
| Managed base agent | `antigravity-preview-09-2026` | Interactions API + `client.agents` (`v1beta`) | **OK** (`agents.list()` = 0) | Auto-compaction at ~135K | Controlled by harness |

> Note: `minimal` thinking level is **not** supported on Gemini 4 models or `gemini-3.8-flash`. `xhigh` is supported on `gemini-4-neon` and `gemini-4-hydrogen`, **not** on `gemini-4-carbon`.

## 3. Interactions API Call Shape
```python
from google import genai
client = genai.Client(http_options={"api_version": "v1beta"})

interaction = client.interactions.create(
    model="gemini-4-hydrogen",
    input="...",  # string | Content | list[Content]
    system_instruction="...",  # optional, interaction-scoped
    generation_config={
        "thinking_level": "medium",  # low | medium | high | xhigh (model-dependent)
        "thinking_summaries": "auto",  # optional
    },
)
print(interaction.output_text)
```
- **Server-side state**: pass `previous_interaction_id=prev.id`. Note that `tools`, `system_instruction`, and `generation_config` are **interaction-scoped** and must be re-specified on subsequent turns.
- **Steps**: `interaction.steps` contains chronological `thought`, `function_call`, `function_result`, `model_output` steps.

## 4. Structured Output (Interactions API)
Uses `response_format` with JSON Schema (Pydantic `.model_json_schema()`):
```python
interaction = client.interactions.create(
    model="gemini-4-neon",
    input=[...],
    response_format={
        "type": "text",
        "mime_type": "application/json",
        "schema": MyPydanticModel.model_json_schema(),
    },
)
parsed = MyPydanticModel.model_validate_json(interaction.output_text)
```
- **Important**: `antigravity-preview-09-2026` does **not** support `response_format` (structured outputs) or `temperature`/`top_p`/`top_k`/`stop_sequences`/`max_output_tokens`.

## 5. PDF, Image Input & Document Slicing
### Inline PDF or Uploaded PDF (Files API)
```python
# Inline base64 (up to 50 MB for PDFs)
{"type": "document", "data": base64_str, "mime_type": "application/pdf"}

# Or Files API URI (recommended for large / multi-turn PDFs)
uploaded = client.files.upload(file="rulebook.pdf")
{"type": "document", "uri": uploaded.uri, "mime_type": "application/pdf"}
```

### Document Slicing & Content Layers
Supported on Gemini 3 & Gemini 4 models via `processing` + `resolution` on `document` blocks:
```python
{
    "type": "document",
    "uri": uploaded.uri,
    "mime_type": "application/pdf",
    "resolution": "high",  # "low" (~280 tok/pg), "medium" (~560, default), "high" (~1120)
    "processing": {
        "type": "static",
        "page_options": {
            "start_page": 1,   # 1-indexed inclusive
            "end_page": 10,    # 1-indexed inclusive, clamped to last page
            "content_layers": ["page_image"],  # "page_image", "page_text", or both
        },
    },
}
```
- Tokens are billed only for the sliced pages; total page count is automatically provided to the model.

### Image Input
```python
{
    "type": "image",
    "data": base64_str,  # or "uri": uploaded.uri
    "mime_type": "image/png",  # png, jpeg, webp, heic, heif, gif, bmp, tiff
    "resolution": "high",  # optional: "low" | "medium" | "high" | "ultra_high"
}
```

## 6. Decisions API (`gemini-decision-1`)
Evaluates 1 to 1,000 independent typed questions (`noul`, `choice`, `score`) against a shared multimodal input in **one forward pass** with **zero output tokens**.
```python
result = client.decisions.create(
    model="gemini-decision-1",
    input=[
        {"type": "image", "data": page_png_b64, "mime_type": "image/png", "resolution": "high"},
    ],
    questions={
        "c_0": {
            "type": "noul",
            "instructions": "Does this page state a maximum ramp rate limit of 10 MW/min?",
            "criteria": {
                "true_description": "The page explicitly states this numeric limit and unit.",
                "false_description": "The limit is missing, different, unreadable, or unsupported.",
            },
        },
    },
)
ans = result.answers["c_0"]
# ans.noul in [0.01, 0.99] (calibrated P(yes)); ans.confidence = max(noul, 1 - noul)
```
- **Key details**:
  - `noul` probabilities are clamped to `[0.01, 0.99]` — never set threshold `> 0.99`.
  - Question keys must match `^[A-Za-z0-9_-]{1,64}$`; the model **never sees question keys**, only `instructions`.
  - `instructions` are text-only; shared `input` accepts text, image, audio, video, PDF (no `gs://`, no YouTube, no context caching).
  - Batching all constraints citing the same page into a single `decisions.create` call runs in one forward pass at flat latency!
  - Live probe confirmed `gemini-decision-1` is **available** on our key (fallback to Hydrogen not needed unless runtime error).

## 7. Managed Agents (`antigravity-preview-09-2026`)
### Creating a Saved Managed Agent (`client.agents.create`)
```python
agent = client.agents.create(
    id="gridsafe-engineer",  # cannot start with reserved prefixes (gemini-, google-, antigravity-, etc.)
    base_agent="antigravity-preview-09-2026",
    agent_config={
        "type": "antigravity",
        "model": "gemini-4-hydrogen",  # locked at creation time; cannot override per interaction
    },
    system_instruction="...",
    tools=[{"type": "code_execution"}, ...],  # defaults: code_execution, google_search, url_context
    base_environment={
        "type": "remote",
        "sources": [
            {
                "type": "repository",
                "source": "https://github.com/ymorsi7/67Hz",
                "target": "/workspace/repo",  # CANNOT be "/" (root mount disallowed)
            },
            {
                "type": "inline",
                "target": ".agents/AGENTS.md",
                "content": "...",
            },
        ],
    },
)
```
- **Model per agent**: `agent_config.model` supports `gemini-4-hydrogen` (default), `gemini-4-carbon`, `gemini-4-neon`, `gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`.
- **Important difference from spec**: For a named managed agent created with `agents.create`, `model` **cannot be overridden at interaction time**. Therefore, the background hardening run on `gemini-4-carbon` must either use a second saved agent (e.g., `gridsafe-hardener` with `model: "gemini-4-carbon"`) or invoke `antigravity-preview-09-2026` directly with `agent_config={"type": "antigravity", "model": "gemini-4-carbon"}`.
- **Important multimodal limitation**: `antigravity-preview-09-2026` only accepts `text` and inline base64 `image` in `input` — **not** `document` (PDF). For PDFs, upload the file into the sandbox (via `environments` file upload or inline/repo) or pass rendered page images.
- **No root mount**: `target` in `environment.sources` cannot be `/`; mount the repo at `/workspace/repo` and mount `.agents/AGENTS.md`, `.agents/skills/<name>/SKILL.md`, and `.agents/hooks.json` at `.agents/...` (auto-discovered by runtime).
- **Pre-installed sandbox software**: Ubuntu, Python 3.12 (`numpy`, `pandas`, `requests`, `google-genai`, `beautifulsoup4`, `pyyaml`), Node 22, `git`, `ripgrep`, `jq`. Note that `scipy`, `pymupdf`, and `pytest` are **not** pre-installed; either fork a warm base environment with them installed or install via `pip` / keep checker pure-Python+numpy.

### Custom Functions (Function Calling on Antigravity)
- Pass `{"type": "function", "name": "...", "description": "...", "parameters": {...}}` in `tools`.
- When `interaction.status == "requires_action"`, inspect `interaction.steps` for `step.type == "function_call"` without a matching `function_result` (`call_id`).
- Return result in next turn with `previous_interaction_id=interaction.id`, `environment=interaction.environment_id`, and `input=[{"type": "function_result", "name": fc.name, "call_id": fc.id, "result": {...}}]`. Stateful mode only.

### Hooks (`.agents/hooks.json`)
- Auto-discovered at `.agents/hooks.json` or `/.agents/hooks.json`.
- Events: `pre_tool_execution` (can return `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}`) and `post_tool_execution` (logging/formatting only).
- Matchers (RE2 regex) intercept sandbox built-in tools: `code_execution`, `view_file`, `write_to_file`, `replace_file_content`, `list_dir`, `delete_file`. (Do **not** fire on custom `function` or `mcp_server` tools.)
- Command hook receives JSON on `stdin`: `{"tool_call": {"name": ..., "args": ...}, "environment_id": ...}`.
- **Fail-open behavior**: script crash, timeout, or non-deny output is treated as `allow`. Also agent can edit writable `.agents/hooks.json` unless protected; enforce checker gate both in `pre_tool_execution` hook AND in the `certify` skill + backend verification.

### Token Budget & Background Streaming
- **Budget**: `agent_config={"type": "antigravity", "model": "...", "max_total_tokens": 200000}`. Stops with `status == "incomplete"` when hit; resumable with `previous_interaction_id` + `environment=interaction.environment_id`.
- **Background execution**: `background=True` (requires `store=True`, the default). Poll via `client.interactions.get(id=interaction.id)` or stream via:
```python
stream = client.interactions.get(id=interaction.id, stream=True, last_event_id=last_event_id)
for event in stream:
    if event.event_id:
        last_event_id = event.event_id
    # event.event_type: "step.delta", "interaction.completed", etc.
```
- Cancel with `client.interactions.cancel(id=interaction.id)`.
- Download sandbox outputs via Environments API (`environments.get_environment_files` / file download).

## 8. shadcn/ui + Vite Setup Notes
- Tailwind v4 via `@tailwindcss/vite` and `@import "tailwindcss";` in `src/index.css`.
- Path alias `@/* -> ./src/*` in `tsconfig.json`, `tsconfig.app.json`, and `vite.config.ts`.
- `shadcn@4.23.0` (`init --defaults --yes`) installs `@base-ui/react`, `tw-animate-css`, `class-variance-authority`, `lucide-react`, `@fontsource-variable/geist`.
- Workaround noted: `recast` inside `shadcn` CLI fails under `tslib@2.9.1`; pinning `tslib@2.8.1` as a devDependency resolves `npx shadcn@latest add ...`.
- TypeScript 7.0 (`~7.0.2`) removes `baseUrl`; keep `"paths": { "@/*": ["./src/*"] }` without `baseUrl` in `tsconfig.json` / `tsconfig.app.json`.
