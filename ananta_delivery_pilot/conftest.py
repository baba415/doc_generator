"""Pytest conftest — add repo root to sys.path so all packages are importable."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
