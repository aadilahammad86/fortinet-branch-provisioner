#!/usr/bin/env python3
"""One-off: ensure the inside networks have internet (NAT) policies via wan1.

Thin CLI wrapper around fortigate.branch.ensure_policies.

Branch design is one ISP per site (wan1), so all inside networks -- LAN
(internal), Staff WiFi (internal2) and Guest WiFi (internal3) -- exit through
wan1 with NAT. Idempotent: existing policies are skipped.

Guest gets its wan1 policy and NOTHING else. The FortiGate's default deny is
what isolates it from the LAN and Staff WiFi.

Usage:  python configure-internet-access.py [--ports internal internal2 internal3]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", nargs="+",
                    default=["internal", "internal2", "internal3"])
    args = ap.parse_args()

    cfg = load_env()
    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    branch.ensure_policies(fg, args.ports, lambda m: print(f"  {m}"))

    print("\n--- current policies ---")
    for p in fg.results("/api/v2/cmdb/firewall/policy"):
        si = ",".join(x["name"] for x in p.get("srcintf", []))
        di = ",".join(x["name"] for x in p.get("dstintf", []))
        print(f"  #{p.get('policyid')} {p.get('name') or '(unnamed)':<22} "
              f"{si:>10} -> {di:<6} act={p.get('action')} nat={p.get('nat')} "
              f"utm={p.get('utm-status')}")


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
