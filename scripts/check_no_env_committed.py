"""
Refuse to stage a real .env file.

.gitignore already covers this, but `git add -f` bypasses it. This hook is the
second lock: a leaked database password or SECRET_KEY in git history cannot be
removed by a later commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ALLOWED = {".env.example"}


def main(argv: list[str]) -> int:
    offenders = [
        name
        for name in argv
        if Path(name).name.startswith(".env") and Path(name).name not in ALLOWED
    ]
    if offenders:
        print("Refusing to commit environment files:", file=sys.stderr)
        for name in offenders:
            print(f"  {name}", file=sys.stderr)
        print(
            "\nSecrets must never enter git history. "
            "Unstage them with: git restore --staged <file>",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
