"""GridSafe managed-agent backbone (Phase 4).

Two saved agents communicate via the Interactions API through a host-side
`message_agent` bridge. No API keys are placed in sandboxes; the backend
re-checks every controller and every claimed counterexample.
"""

from agents.bridge import AgentBridge
from agents.client import get_genai_client, load_managed_config
from agents.thread_log import AgentThreadLogger

__all__ = [
    "AgentBridge",
    "AgentThreadLogger",
    "get_genai_client",
    "load_managed_config",
]
