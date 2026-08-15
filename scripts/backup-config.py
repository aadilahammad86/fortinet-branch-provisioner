#!/usr/bin/env python3
"""Download a full backup of the FortiGate configuration.

Saves the complete running config to ../configs/<serial>-<timestamp>.conf.
This file contains secrets (password hashes, keys) so configs/ is git-ignored.
Use it as a restore point before a factory reset or any big change.

Connection settings come from ../.env.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402


def main():
    cfg = load_env()
    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    out, st, size, lines = fg.backup(ROOT / "configs")
    print(f"[ok] Backup saved: {out}")
    print(f"     device={st['hostname']} serial={st['serial']} firmware={st['version']}")
    print(f"     size={size:,} bytes, {lines:,} lines")
    head = out.read_text(errors="replace").splitlines()[:1]
    # sanity check: a real FortiGate config starts with a config-version header
    print(f"     first line: {head[0] if head else '(empty)'}")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
