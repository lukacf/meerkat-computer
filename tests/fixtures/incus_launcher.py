#!/usr/bin/env python3
"""Execute per-test fixture data without making its writable inode executable."""

import os
import sys
from pathlib import Path

# Keep the invoked symlink's directory, where the test's private state lives.
body = Path(sys.argv[0]).parent / "incus-fixture"
os.execv(sys.executable, [sys.executable, str(body), *sys.argv[1:]])
