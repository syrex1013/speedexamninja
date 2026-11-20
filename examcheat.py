#!/usr/bin/env python3
"""examcheat - read SpeedExam questions, ask Z.ai GLM, show the answer on a HUD
that screen-recording proctoring cannot capture.

Run:  python3 examcheat.py   (or: python3 main.py to launch Chrome first)

The script only reads the active question and displays the model's pick. It never
clicks, never navigates: you click the answer and Next yourself.
Everything else (login, captcha, registration, Next navigation) is done by the human.

Browser: attaches to an existing Chrome on http://127.0.0.1:9222. If no such
endpoint answers, it starts Chrome on ~/.examcheat-chrome-profile with
--remote-debugging-port=9222 so you can sign in and open the exam there.

Needs: playwright, pyobjc (AppKit), Google Chrome.
"""
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSPanel,
    NSTextField,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSTimer
from playwright.sync_api import sync_playwright
from rich.console import Console

def load_env(path):
    """Tiny .env loader (stdlib only): KEY=VALUE lines, # comments, optional quotes."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(Path(__file__).with_name(".env"))

ZAI_KEY = os.environ.get("ZAI_KEY", "")
ZAI_URL = os.environ.get("ZAI_URL", "https://api.z.ai/api/paas/v4/chat/completions")
ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-5.3-flash")
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", "~/.examcheat-chrome-profile")).expanduser()
CHROME_BIN = os.environ.get(
    "CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1.0"))
PANEL_W = int(os.environ.get("PANEL_W", "340"))
PANEL_H = int(os.environ.get("PANEL_H", "64"))

console = Console()
NSStatusWindowLevel = 25
NSWindowCollectionBehaviorCanJoinAllSpaces = 1
NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8

READ_JS = """() => {
  const act = document.querySelector('#workarea div.div.active');
  if (!act) return null;
  const rows = [...act.querySelectorAll('.answerbox tr')].map(tr => ({
    letter: (tr.querySelector('.ansno b')?.innerText || tr.querySelector('.ansno')?.innerText || '')
      .trim().replace(/[.\\s]/g, '').toUpperCase(),
    text: (tr.querySelector('.anstext')?.innerText || '').trim(),
    type: tr.querySelector('input')?.type || ''
  })).filter(r => r.letter);
  const imgs = [...act.querySelectorAll('img')]
    .filter(i => i.src && !/images\\/294\\.GIF|images\\/244\\.gif/.test(i.src)).length;
  return {
    qid: act.getAttribute('data-qid'),
    q: act.querySelector('.questiontext')?.innerText.trim() || '',
    rows, imgs
  };
}"""



class Overlay:
    """Borderless non-activating panel with NSWindowSharingNone: stays on screen,
    never blurs the browser, and is excluded from other processes' screen capture."""

    def __init__(self):
        self.q = queue.Queue()
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(20, 40, PANEL_W, PANEL_H),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.panel.setSharingType_(NSWindowSharingNone)
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.blackColor())
        self.panel.setAlphaValue_(0.92)
        self.field = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 12, PANEL_W - 24, 40))
        self.field.setEditable_(False)
        self.field.setBezeled_(False)
        self.field.setDrawsBackground_(False)
        self.field.setTextColor_(NSColor.systemGreenColor())
        self.field.setFont_(NSFont.boldSystemFontOfSize_(15))
        self.panel.contentView().addSubview_(self.field)

    def show(self, text, conf=1.0):
        self.q.put((text, conf))

    def _drain(self, _timer):
        while True:
            try:
                text, conf = self.q.get_nowait()
            except queue.Empty:
                return
            self.field.setTextColor_(
                NSColor.systemGreenColor() if conf >= 0.7 else NSColor.systemOrangeColor()
            )
            self.field.setStringValue_(text)
            self.panel.orderFrontRegardless()

    def start(self):
        self.timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.1, True, self._drain
        )
        self.panel.orderFrontRegardless()

    def stop(self):
        self.panel.orderOut_(None)


def ask_ai(question, rows, image_b64=None):
    """Return (letters, confidence). Empty letters on failure."""
    system = (
        "You answer multiple-choice exam questions. Reply ONLY minified JSON: "
        '{"answer":"<letter>","confidence":<0-1>}. '
        'If several options are correct, "answer" is an array of letters.'
    )
    body_text = "Q: " + question + "\n" + "\n".join(
        f"{r['letter']} {r['text']}" for r in rows
    )
    if image_b64:
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
            {"type": "text", "text": body_text},
        ]
    else:
        content = body_text
    payload = {
        "model": ZAI_MODEL,
        "thinking": {"type": "enabled"},
        "max_tokens": 700,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    data = json.dumps(payload).encode()
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                ZAI_URL,
                data=data,
                headers={
                    "Authorization": "Bearer " + ZAI_KEY,
                    "Content-Type": "application/json",
                },
            )
            res = json.loads(urllib.request.urlopen(req, timeout=90).read())
            raw = res["choices"][0]["message"].get("content") or ""
            try:
                obj = json.loads(raw)
                ans, conf = obj.get("answer", ""), float(obj.get("confidence") or 0)
            except (json.JSONDecodeError, ValueError, TypeError):
                ans, conf = (re.search(r"[A-Za-z]", raw) or [""])[0], 0.5
            letters = [ans] if isinstance(ans, str) else list(ans)
            letters = [str(x).strip().upper()[:1] for x in letters if str(x).strip()]
            return letters, conf
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            console.log(f"[red]ai error ({attempt}/2): {exc}[/]")
            if attempt == 1:
                time.sleep(2)
    return [], 0.0


def find_page(context):
    for page in context.pages:
        url = page.url or ""
        if "candidate.speedexam.net" in url and "startexam.aspx" in url:
            return page
    return None


def exam_tab_exists():
    """CDP HTTP probe: True when an exam tab exists, even if Playwright lost it."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
            return any(
                t.get("type") == "page" and "startexam.aspx" in (t.get("url") or "")
                for t in json.load(r)
            )
    except Exception:
        return False


def run(overlay):
    pw = sync_playwright().start()
    browser = None
    launched = False

    def connect_once():
        for host in ("127.0.0.1", "[::1]"):
            try:
                b = pw.chromium.connect_over_cdp(f"http://{host}:{CDP_PORT}")
                if b.contexts:
                    return b
                b.close()
            except Exception:
                pass
        return None

    deadline = time.time() + 60
    while browser is None and time.time() < deadline:
        browser = connect_once()
        if browser is None:
            if not launched:
                console.log(f"[yellow]no CDP endpoint, launching Chrome on {PROFILE_DIR}[/]")
                subprocess.Popen(
                    [CHROME_BIN, f"--remote-debugging-port={CDP_PORT}",
                     f"--user-data-dir={PROFILE_DIR}"]
                )
                launched = True
            time.sleep(1)
    if browser is None:
        overlay.show("Chrome CDP unreachable")
        pw.stop()
        return

    context = browser.contexts[0]
    page = None
    last_hash = ""
    fails = {}
    console.log("[bold cyan]examcheat[/] waiting for exam page")

    while True:
        try:
            if page is None or page.is_closed():
                page = find_page(context)
                if page is None:
                    if exam_tab_exists():
                        # Tab exists but this CDP connection does not see it (it was
                        # opened after the connect): reconnect instead of stalling.
                        console.log("[yellow]exam tab exists but is invisible to CDP, reconnecting[/]")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = None
                        while browser is None:
                            browser = connect_once()
                            if browser is None:
                                time.sleep(1)
                            elif not find_page(browser.contexts[0]):
                                browser.close()
                                browser = None
                                time.sleep(1)
                        context = browser.contexts[0]
                        page = find_page(context)
                        if page is None:
                            time.sleep(1)
                            continue
                    else:
                        time.sleep(2)
                        continue
                console.log(f"[green]attached[/] {page.url}")

            try:
                q = page.evaluate(READ_JS)
            except Exception:
                page = None
                time.sleep(1)
                continue
            if not q:
                if "startexam.aspx" not in (page.url or ""):
                    page = None
                time.sleep(POLL_SECONDS)
                continue

            h = f"{q['qid']}|{q['q']}|{q['rows']}"
            if h == last_hash:
                time.sleep(POLL_SECONDS)
                continue

            if not q["rows"] or not any(r["type"] in ("radio", "checkbox") for r in q["rows"]):
                overlay.show(f"{q['qid']}: non-MCQ, skipped")
                last_hash = h
                time.sleep(POLL_SECONDS)
                continue

            overlay.show("analyzing…", 0.0)
            image_b64 = None
            if q["imgs"]:
                try:
                    image_b64 = base64.b64encode(
                        page.locator("#workarea div.div.active").screenshot()
                    ).decode()
                except Exception as exc:  # noqa: BLE001
                    console.log(f"[red]screenshot failed: {exc}[/]")

            letters, conf = ask_ai(q["q"], q["rows"], image_b64)
            row_letters = [r["letter"] for r in q["rows"]]
            if not letters:
                fails[q["qid"]] = fails.get(q["qid"], 0) + 1
                if fails[q["qid"]] >= 5:
                    overlay.show(f"AI FAIL {q['qid']}", 0.0)
                    console.log(f"[red]AI failed {q['qid']} 5x[/]")
                    last_hash = h
                else:
                    overlay.show(f"analyzing… (retry {fails[q['qid']]})", 0.0)
                time.sleep(3)
                continue
            fails.pop(q["qid"], None)
            if not any(letter in row_letters for letter in letters):
                idx = max(0, min(len(row_letters) - 1, ord(letters[0]) - 65))
                letters = [row_letters[idx]]

            # The page may have advanced while the model was thinking.
            try:
                cur = page.evaluate(
                    "() => document.querySelector('#workarea div.div.active')?.getAttribute('data-qid') || ''"
                )
            except Exception:
                page = None
                continue
            if cur != q["qid"]:
                time.sleep(POLL_SECONDS)
                continue

            text = next((r["text"] for r in q["rows"] if r["letter"] in letters), "")
            overlay.show(f"{'/'.join(letters)}. {text[:38]} ({conf:.2f})", conf)
            console.log(f"[green]answer[/] {q['qid']} {letters} conf={conf:.2f}")
            last_hash = h
            time.sleep(POLL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            console.log(f"[red]loop error: {exc}[/]")
            time.sleep(POLL_SECONDS)


def main():
    if not ZAI_KEY:
        raise SystemExit(
            "ZAI_KEY is not set. Copy .env.example to .env and fill in your key."
        )
    overlay = Overlay()
    threading.Thread(target=run, args=(overlay,), daemon=True).start()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    overlay.start()
    app.run()
    overlay.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
