#!/usr/bin/env python3
"""Local operator entry point; no dependency downloads."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from multi_shadow_clone.bootstrap import operator_run

if __name__ == "__main__":
    raise SystemExit(operator_run())
