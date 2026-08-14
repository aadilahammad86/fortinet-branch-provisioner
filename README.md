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
