"""
Read / write web/.env while preserving comments and key order.
"""
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def read_env():
    """Return dict {key: value} of all lines with `KEY=value`."""
    out = {}
    if not ENV_FILE.exists():
        return out
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(updates):
    """
    Rewrite .env in-place:
      - existing keys are updated (order preserved)
      - new keys appended at the end
      - comments and blank lines preserved
    """
    if not ENV_FILE.exists():
        lines = []
    else:
        lines = ENV_FILE.read_text().splitlines()

    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            new_lines.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n")
    return True


def mask(secret):
    """Show only last 4 chars."""
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]