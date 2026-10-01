#!/usr/bin/python3
"""Git-only credential response; never store the token in a remote URL."""
import sys
from pathlib import Path

if "Username" in " ".join(sys.argv[1:]):
    print("x-access-token")
elif "Password" in " ".join(sys.argv[1:]):
    print((Path.home() / "git_token.txt").read_text().strip())
else:
    raise SystemExit(1)
