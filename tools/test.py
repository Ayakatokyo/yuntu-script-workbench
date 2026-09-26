"""Run this repository's offline test suite."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    args = sys.argv[1:] or ["-q", "tests"]
    raise SystemExit(subprocess.call([sys.executable, "-B", "-m", "pytest", *args], cwd=ROOT, env=environment))
