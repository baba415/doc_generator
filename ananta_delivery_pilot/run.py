from __future__ import annotations

import sys

from app.cli import main as legacy_main
from apps.cli import main as phase1_main
from apps.cli import phase1_commands


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in phase1_commands():
        phase1_main(sys.argv[1:])
    else:
        legacy_main()
