#!/usr/bin/env python3
"""Run the checkout without package installation or user-site dependencies."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from kagebunshin.bootstrap import run

if __name__ == "__main__":
    raise SystemExit(run())
