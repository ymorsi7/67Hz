---
name: read-rulebook
description: Extract typed interconnection constraints from a utility rulebook via the backend Neon Reader.
---

# Read Rulebook

Do **not** open PDFs or images yourself. Call the custom function `read_rulebook` with:
- `source_path` (repo-relative PDF path), and/or
- `page_images_b64` for camera/photo pages.

Expect structured constraints with `page`, `snippet`, `checkable`, `entity`, and units.
Keep uncheckable constraints; mark them `checkable=false`.
Next: call `verify_constraint` for every checkable numeric limit.
