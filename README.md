# Fortinet Project

Working project for managing and interacting with the FortiGate firewall.

## Device

| Field | Value |
|-------|-------|
| Type | FortiGate firewall |
| Admin URL | https://172.21.0.1 |
| Admin user | `admin` |
| Credentials | see `.env` (NOT committed — copy from `.env.example`) |

> The device uses a self-signed certificate, so tools need to skip cert
> verification (`curl -k`, etc.) when connecting.

## Layout

```
fortinet-project/
├── configs/    # exported / backed-up FortiGate configs
├── docs/       # notes, findings, diagrams
├── scripts/    # helper scripts (connectivity, backup, API calls)
└── .env        # secrets — git-ignored
```

## Provision a new branch (main workflow)

For every new FortiGate, connect to it and run the provisioner. It asks only
for what changes per branch (WiFi LAN gateway IP + client count) and applies
the whole standard template (interface, DHCP, NAT policies, wan1 PPPoE
plug-and-play). ISP PPPoE credentials are entered **on-site**, not here.

```
# interactive (operator answers prompts)
python scripts\provision-branch.py

# or non-interactive
python scripts\provision-branch.py --wifi-ip 192.168.2.1 --clients 25 --hostname FGT-BranchB --yes
```

Set the target device / admin login in `.env` (or pass `--host/--user/--password`).

## Getting started

1. Copy the secrets template and fill it in:
   ```
   copy .env.example .env
   ```
2. Run the connectivity check:
   ```
   powershell -File scripts\check-connection.ps1
   ```

## Security note

Do not commit real credentials. `.env` is git-ignored. Rotate the admin
password if it has ever been shared in plain text.
