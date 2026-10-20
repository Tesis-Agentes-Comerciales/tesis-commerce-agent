"""Checkout locations. The evaluation package runs from an editable workspace install only."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVALUATION = ROOT / "evaluation"
DATA = EVALUATION / "data"
RESULTS = EVALUATION / "results"
# Experimental outputs stay out of Git, under this worktree's .local directory.
LOCAL = ROOT / ".local"
