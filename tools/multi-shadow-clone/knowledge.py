#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from multi_shadow_clone.bootstrap import knowledge_run

if __name__ == "__main__":
    raise SystemExit(knowledge_run())
