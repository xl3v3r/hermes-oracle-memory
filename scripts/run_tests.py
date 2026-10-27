#!/usr/bin/env python3
"""Execute unit test suite for hermes-oracle-memory."""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path


def main() -> int:
    repo_dir = Path(__file__).resolve().parent.parent
    tests_dir = repo_dir / "tests"
    cmd = [sys.executable, "-m", "pytest", str(tests_dir), "-v"]
    print(f"Running hermes-oracle-memory test suite: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(repo_dir))
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
