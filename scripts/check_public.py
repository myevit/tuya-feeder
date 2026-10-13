"""Reject obvious sensitive material in tracked public text (not a full audit)."""

import re
import subprocess
from pathlib import Path

files = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
patterns = [
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}",
    r"op://",
    r"(?i)(?:local_key|client_secret|access_token|password)\s*[=:]\s*['\"][^'\"]{6,}['\"]",
    r"(?i)nyx|yoolax|/config/\.storage/core\.config_entries",
]
violations = []
for filename in filter(None, files):
    if filename == "scripts/check_public.py":
        continue
    path = Path(filename)
    if path.suffix == ".png":
        continue
    try:
        text = path.read_text()
    except UnicodeError:
        violations.append(f"{filename}: unexpected binary")
        continue
    if any(re.search(pattern, text) for pattern in patterns):
        violations.append(f"{filename}: sensitive-pattern match")
if violations:
    raise SystemExit("\n".join(violations))
print(
    f"Public-file pattern check passed ({len(list(filter(None, files)))} tracked files); manual provenance review also required."
)
