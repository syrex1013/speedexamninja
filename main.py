#!/usr/bin/env python3
"""Launcher: start Chrome (persistent profile, CDP port 9222), open SpeedExam,
then run the auto-answer loop. You log in, register, and click Start Test;
examcheat.py handles the rest (reads questions, answers, HUD).

Run:  python3 main.py
"""
import subprocess
import time
import urllib.request

from rich.console import Console

from examcheat import CDP_PORT, CHROME_BIN, PROFILE_DIR, main

console = Console()


def cdp_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


def ensure_chrome():
    if cdp_up():
        console.log("[green]Chrome CDP already up[/] on port " + str(CDP_PORT))
        return
    console.log(f"[yellow]launching Chrome[/] on {PROFILE_DIR}")
    subprocess.Popen(
        [
            CHROME_BIN,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "https://candidate.speedexam.net/",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )
    for _ in range(30):
        time.sleep(1)
        if cdp_up():
            console.log("[green]Chrome CDP ready[/]")
            return
    raise SystemExit("Chrome CDP endpoint did not come up on port " + str(CDP_PORT))


if __name__ == "__main__":
    ensure_chrome()
    main()
