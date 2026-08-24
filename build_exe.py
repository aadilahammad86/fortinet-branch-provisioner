#!/usr/bin/env python3
"""Build FortiGate Branch Provisioner.exe with PyInstaller.

    python build_exe.py            # one-folder build (recommended)
    python build_exe.py --onefile  # single .exe, higher antivirus false-positive rate

WHY ONE-FOLDER IS THE DEFAULT
A PyInstaller --onefile executable unpacks itself to a temp directory at every
launch, which is behaviour Windows Defender and SmartScreen treat as suspicious.
The one-folder build is flagged far less often. Zip the folder to distribute it.

Either way the build is unsigned, so Windows SmartScreen shows a
"Windows protected your PC" prompt on first run -- the operator clicks
"More info" then "Run anyway". A code-signing certificate removes that.

Output: dist/FortiGate Branch Provisioner/  (or dist/*.exe with --onefile)
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "FortiGate Branch Provisioner"


def main():
    onefile = "--onefile" in sys.argv

    # Keep the two variants side by side so building one does not delete the
    # other: onedir -> dist/<NAME>/ , onefile -> dist/portable/<NAME>.exe
    distpath = ROOT / "dist" / "portable" if onefile else ROOT / "dist"
    workpath = ROOT / "build" / ("portable" if onefile else "onedir")

    # --out DIR builds somewhere else entirely. Useful when a copy of the app
    # is running from the usual output folder: Windows locks the .exe, and the
    # clean-up below would fail rather than quietly building a stale binary.
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        distpath = Path(out).resolve()
        workpath = distpath / "_work"
        print(f"  building into {distpath}")
    for p in (distpath if onefile else distpath / NAME, workpath):
        if not p.exists():
            continue
        try:
            shutil.rmtree(p)
            print(f"  cleaned {p}")
        except OSError as e:
            # Windows keeps a lock for a moment after the app closes, and a
            # still-running copy locks it indefinitely. Say so plainly rather
            # than dying with a bare WinError deep in a build log.
            raise SystemExit(
                f"[!] Cannot clean {p}\n"
                f"    {e}\n\n"
                f"    The application is probably still running, or was closed "
                f"a second ago.\n"
                f"    Close every copy of '{NAME}' and run this again.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if onefile else "--onedir",
        "--windowed",                       # no console window behind the GUI
        "--name", NAME,
        "--distpath", str(distpath),
        "--workpath", str(workpath),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT),               # so `import fortigate` resolves
        "--hidden-import", "fortigate",
        "--hidden-import", "fortigate.client",
        "--hidden-import", "fortigate.branch",
        "--hidden-import", "fortigate.utm",
        "--hidden-import", "fortigate.templates",
        "--hidden-import", "fortigate.ddns",
        "--hidden-import", "fortigate.vpn",
        "--hidden-import", "fortigate.appctrl",
        "--hidden-import", "fortigate.connections",
        # docs are opened by the "User guide" button
        "--add-data", f"{ROOT / 'docs' / 'gui-user-guide.md'};docs",
        # nothing here needs these; excluding them keeps the build small
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
        "--exclude-module", "pytest",
        str(ROOT / "branch_gui.py"),
    ]

    print("  running PyInstaller...")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"[!] PyInstaller failed with exit code {r.returncode}")

    out = distpath / (f"{NAME}.exe" if onefile else NAME)
    print(f"\n[ok] build complete: {out}")
    if out.is_dir():
        exe = out / f"{NAME}.exe"
        total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        print(f"     launch: {exe}")
        print(f"     folder size: {total / 1_048_576:.1f} MB "
              f"({sum(1 for _ in out.rglob('*') if _.is_file())} files)")
        print(f"     NOT portable on its own -- the .exe needs the _internal")
        print(f"     folder beside it. Zip the whole '{NAME}' folder to share it.")
    else:
        print(f"     size: {out.stat().st_size / 1_048_576:.1f} MB")
        print(f"     fully portable -- this single file is the whole application.")
        print(f"     Config backups are written next to wherever the .exe is run.")


if __name__ == "__main__":
    main()
