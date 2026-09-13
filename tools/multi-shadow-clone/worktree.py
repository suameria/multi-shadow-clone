#!/usr/bin/env python3
"""Thin checkout entrypoint for the owned GranSkypolis worktree adapter."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from multi_shadow_clone.bootstrap import worktree_run

if __name__ == "__main__":
    raise SystemExit(worktree_run())
