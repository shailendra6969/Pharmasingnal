"""Agent investigation tools interface (spec §23–26, Phase 5)."""
from app.tools.label_tool import search_labels
from app.tools.signal_tool import get_signal_scores

__all__ = ["get_signal_scores", "search_labels"]
