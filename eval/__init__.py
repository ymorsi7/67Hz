"""GridSafe evaluation package."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
for _p in (_REPO / "src", _REPO):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
