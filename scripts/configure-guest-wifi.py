#!/usr/bin/env python3
"""One-off: configure the Guest WiFi network (interface + DHCP).

Thin CLI wrapper around fortigate.branch.

Guest WiFi is deliberately internet-only: configure-internet-access.py adds its
`-> wan1` NAT policy and nothing else, so the FortiGate's default deny keeps
guests off the office LAN, Staff WiFi and (later) the HO VPN. Do not add a
guest -> inside policy.

Usage:  python configure-guest-wifi.py [--ip 192.168.2.1] [--clients 25]
                                       [--port internal3] [--first 2]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fortigate import FortiGate, FortiGateError, load_env      # noqa: E402
from fortigate import branch                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.2.1")
    ap.add_argument("--clients", type=int, default=25)
    ap.add_argument("--first", type=int, default=2)
    ap.add_argument("--port", default="internal3")
    args = ap.parse_args()

    cfg = load_env()
    fg = FortiGate(cfg["FGT_HOST"], cfg["FGT_USER"], cfg["FGT_PASSWORD"])
    log = lambda m: print(f"  {m}")                            # noqa: E731

    start, end = branch.compute_range(args.ip, args.clients, args.first)
    print(f"Configuring Guest WiFi on {args.port} -> {args.ip} (DHCP {start}-{end})")
    branch.set_lan_interface(fg, args.port, args.ip, branch.GUEST_ALIAS,
                             branch.NETMASK, log)
    branch.set_dhcp_server(fg, args.port, args.ip, start, end, branch.NETMASK, log)


if __name__ == "__main__":
    try:
        main()
    except FortiGateError as e:
        raise SystemExit(f"[!] {e}")
