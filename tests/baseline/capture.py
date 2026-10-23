#!/usr/bin/env python3
"""Capture the UI baseline: DOM landmarks + screenshots, from a fake archive.

    .venv/bin/python3 tests/baseline/capture.py            # rebuild, capture, verify
    .venv/bin/python3 tests/baseline/capture.py --reuse    # keep the demo tree

What it does, in order:

1. Refuses to run if the target is the live service (port 8765).
2. Builds a throwaway demo instance under ``HWA_BASELINE_ROOT`` (default
   ``~/.hermes/cache/scratch/hwa-baseline``): temp HOME, XDG dirs, INSTA_* paths,
   the skill's ``demo-instaloader-stub.py`` copied first on PATH, three demo
   personas with generated photos plus three portrait clips.
3. Prints the isolation proof read from the *real* state file (read-only): its
   job count and whether any demo persona appears in it.
4. Drives the app in headless Chrome over CDP, tab by tab, at 1440x900 and
   390x844, and writes ``dom.txt`` (sorted) plus four screenshots.
5. Re-reads each PNG's header for its true dimensions and re-checks that no
   real handle reached the output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cdp          # noqa: E402
import demo_env     # noqa: E402

PNG_BUDGET = 300_000        # bytes; the shipped docs/*.png sit at 36-276 KB
LIVE_PORT = 8765
REAL_STATE = Path.home() / ".config/hardlywatching/state.json"


# --------------------------------------------------------------- scopes
# (tab label, viewport width, height, view key, screenshot name or None)
SCOPES = [
    ("Download", 1440, 900, "download", "download-1440x900.png"),
    ("Download", 390, 844, "download", None),
    ("Download > TikTok", 1440, 900, "download_tiktok", None),
    ("Download > TikTok", 390, 844, "download_tiktok", None),
    ("Gallery", 1440, 900, "gallery", "gallery-1440x900.png"),
    ("Gallery", 390, 844, "gallery", None),
    ("Gallery > Bookmarks", 1440, 900, "gallery_bookmarks", None),
    ("Gallery > Bookmarks", 390, 844, "gallery_bookmarks", None),
    ("Shorts", 1440, 900, "shorts", "shorts-1440x900.png"),
    ("Shorts", 390, 844, "shorts", "shorts-390x844.png"),
]

# The harvest runs with every collapsed section open, so a field hidden behind
# a <summary> is still counted. Screenshots are taken from an untouched reload.
OPEN_FOR_HARVEST = """
(() => {
  for (const id of ["settingsBox", "excludedCard"]) {
    const d = document.getElementById(id);
    if (d) d.open = true;
  }
  const s = document.getElementById("sessBtn");
  if (s && !document.getElementById("sessPanel").offsetParent) s.click();
  return true;
})()
"""

HARVEST_JS = r"""
(() => {
  const DOC = __DOC__;
  const PREFIX = __PREFIX__;
  const rows = [];
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const text = (el) => clean(el.innerText);
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const st = el.ownerDocument.defaultView.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    if (parseFloat(st.opacity || "1") === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const hid = el.closest("[hidden]");
    if (hid && hid !== el) return false;
    return true;
  };
  const classes = (el) => clean(typeof el.className === "string" ? el.className : "")
      .split(" ").filter((c) => c && !/^(on|active|small|primary|hidden)$/.test(c));
  const selector = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id) return tag + "#" + el.id;
    const d = el.dataset || {};
    for (const k of ["k", "gview", "gden", "act", "vtag"]) {
      if (d[k] !== undefined && d[k] !== "") return tag + '[data-' + k + '="' + d[k] + '"]';
    }
    const cls = classes(el);
    if (cls.length) return tag + "." + cls[0];
    const title = clean(el.getAttribute("title"));
    if (title) return tag + '[title="' + title + '"]';
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return tag + '[aria-label="' + aria + '"]';
    if (tag === "input") return "input[type=" + (el.type || "text") + "]";
    return tag;
  };
  const labelText = (el) => {
    const c = el.cloneNode(true);
    for (const small of c.querySelectorAll("small")) small.remove();
    return clean(c.textContent);
  };
  const label = (el) => {
    const tag = el.tagName.toLowerCase();
    const aria = clean(el.getAttribute("aria-label"));
    if (tag === "input" || tag === "select" || tag === "textarea") {
      if (aria) return aria;
      const ph = clean(el.getAttribute("placeholder"));
      if (ph) return ph;
      if (el.id) {
        const l = DOC.querySelector('label[for="' + el.id + '"]');
        if (l) return text(l);
      }
      const wrap = el.closest("label");
      if (wrap) return labelText(wrap);
      const num = el.closest(".num");
      if (num) {
        const span = num.querySelector("span");
        if (span) return labelText(span);
      }
      return clean(el.getAttribute("title")) || "(unlabelled " + tag + ")";
    }
    const t = text(el);
    if (t) return t;
    if (aria) return aria;
    return clean(el.getAttribute("title")) || clean(el.getAttribute("role"));
  };

  const INTERACTIVE = "button, a[href], input:not([type=hidden]), select, textarea, " +
                      "summary, [role=button], [role=tab], [role=slider]";
  for (const el of DOC.querySelectorAll(INTERACTIVE)) {
    if (!visible(el)) continue;
    rows.push(["control", PREFIX + selector(el), label(el)]);
  }

  const LANDMARK = "header, h1, h2, h3, .tabs, .subtabs, .pane, .chip, #allBar, " +
                   "#gSearchWrap, #dropZone, .shorts-head, .profile-grid, " +
                   "#sessPanel, .bar, [role=group][aria-label], #settingsBox, " +
                   "#excludedCard, #topbar, #rail, #bottombar, #meta, #count, #goback";
  const seen = new Set();
  for (const el of DOC.querySelectorAll(LANDMARK)) {
    if (!visible(el) || seen.has(el)) continue;
    seen.add(el);
    const cls0 = classes(el)[0];
    const name = el.id ? "#" + el.id : (cls0 ? "." + cls0 : el.tagName.toLowerCase());
    const t = text(el);
    rows.push(["landmark", PREFIX + "landmark:" + name,
               t.length && t.length <= 60 ? t : "(container)"]);
  }
  return JSON.stringify(rows);
})()
"""


def guard(port: int) -> None:
    if port == LIVE_PORT or port < 8899:
        raise SystemExit("refusing port %d: that is the live service or below the "
                         "sandbox floor (>= 8899)" % port)


# --------------------------------------------------------------- real state
def real_state() -> dict | None:
    """The live state, read-only, only for the isolation proof. Never written."""
    try:
        return json.loads(REAL_STATE.read_text())
    except (OSError, ValueError):
        return None


def real_identifiers(state: dict | None) -> set[str]:
    if not state:
        return set()
    names = {str(j.get("username")) for j in state.get("jobs", []) if j.get("username")}
    names |= {str(n) for n in (state.get("removed") or []) if n}
    for root in [state.get("output_root")] + list(state.get("roots") or []):
        if not root:
            continue
        try:
            names |= {p.name for p in Path(root).iterdir() if p.is_dir()}
        except OSError:
            pass
    # Things a screenshot could carry that are not account names: the machine's
    # user, its hostname and the home directory. Deliberately NOT product words
    # like "instaloader" -- README.md may say that, and a hit there is noise.
    names |= {os.environ.get("USER", ""), os.uname().nodename, str(Path.home())}
    return {n for n in names if n and len(n) > 3}


def isolation_proof(before: dict | None, after: dict | None) -> bool:
    personas = [p[0] for p in demo_env.PERSONAS] + [q[0] for q in demo_env.QUEUED]
    print("-- isolation proof (read-only on the live state) --")
    if before is None:
        print("   no live state on this machine; nothing to collide with")
        return True
    b = {j.get("username") for j in before.get("jobs", []) if j.get("username")}
    a = {j.get("username") for j in (after or {}).get("jobs", []) if j.get("username")}
    hit = sorted(set(personas) & (b | a))
    print("   real jobs before: %d  after: %d  (same set: %s)"
          % (len(b), len(a), b == a))
    print("   demo personas in the real state: %d %s" % (len(hit), hit or ""))
    print("   real handles in the demo tree: %d"
          % len(b & set(personas)))
    return b == a and not hit


# --------------------------------------------------------------- browser
def ev(ws, sid: str, expr: str):
    r = ws.call("Runtime.evaluate", session_id=sid, expression=expr, returnByValue=True)
    if "exceptionDetails" in r:
        raise SystemExit("JS error: %s\n%s" % (r["exceptionDetails"].get("text"), expr[:400]))
    return r.get("result", {}).get("value")


def wait_until(ws, sid: str, expr: str, timeout: float = 20.0, what: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if ev(ws, sid, expr):
                return
        except SystemExit:
            pass
        time.sleep(0.15)
    raise SystemExit("timed out waiting for %s: %s" % (what or expr, expr))


def set_viewport(ws, sid: str, w: int, h: int) -> None:
    """Apply the viewport and prove it took: a reload drops the override.

    ``mobile=False`` even at 390 wide: with mobile emulation on, Chrome rescales
    a page that carries ``width=device-width`` and innerWidth came back 427 for a
    390 request. The layout viewport, and therefore every CSS media query the UI
    has, is the same either way -- and the PNG then really is 390x844.
    """
    want = [w, h]
    got = None
    for _ in range(6):
        ws.call("Emulation.setDeviceMetricsOverride", session_id=sid, width=w, height=h,
                deviceScaleFactor=1, mobile=False)
        got = json.loads(ev(ws, sid, "JSON.stringify([innerWidth, innerHeight])"))
        if got == want:
            return
    raise SystemExit("viewport will not stick: wanted %s got %s" % (want, got))


def navigate(ws, sid: str, port: int) -> None:
    if port == LIVE_PORT:
        raise SystemExit("refusing to navigate to the live service")
    # Through about:blank first: navigating a page to the URL it already shows
    # is a no-op, which would carry the previous scope's open tab and panels
    # into this one.
    ws.call("Page.navigate", session_id=sid, url="about:blank")
    wait_until(ws, sid, "document.readyState === 'complete'", 10, "blank page")
    ws.call("Page.navigate", session_id=sid, url="http://127.0.0.1:%d/" % port)
    wait_until(ws, sid, "document.readyState === 'complete'", 20, "page load")
    wait_until(ws, sid, "document.querySelectorAll('.tab').length === 3", 20, "tab strip")


def prepare(ws, sid: str, port: int, view: str, w: int, h: int) -> None:
    set_viewport(ws, sid, w, h)
    navigate(ws, sid, port)
    if view == "download":
        wait_until(ws, sid, "document.body.innerText.includes('demo.coral.reef')", 20,
                   "finished queue cards")
    elif view == "download_tiktok":
        wait_until(ws, sid, "document.body.innerText.includes('demo.coral.reef')", 20,
                   "queue cards")
        ev(ws, sid, "document.getElementById('subTt').click()")
        wait_until(ws, sid, "document.getElementById('ttPane').offsetParent !== null", 10,
                   "TikTok pane")
    elif view in ("gallery", "gallery_bookmarks"):
        wait_until(ws, sid, "document.body.innerText.includes('demo.coral.reef')", 20, "queue")
        ev(ws, sid, "document.getElementById('tabGallery').click()")
        wait_until(ws, sid, "document.body.innerText.includes('3 profiles')", 20, "profile list")
        if view == "gallery_bookmarks":
            ev(ws, sid, """(() => {
              const b = [...document.querySelectorAll('#allBar button')]
                .find(x => x.textContent.includes('Bookmarks'));
              if (!b) return false; b.click(); return true; })()""")
            wait_until(ws, sid, "document.body.innerText.includes('Studio refs')", 20,
                       "bookmark categories")
    elif view == "shorts":
        wait_until(ws, sid, "document.body.innerText.includes('demo.coral.reef')", 20, "queue")
        ev(ws, sid, "document.getElementById('tabShorts').click()")
        wait_until(ws, sid, """(() => { const f = document.getElementById('shortsFrame');
            const d = f && f.contentDocument;
            if (!d || !d.body) return false;
            return d.querySelectorAll('#feed > *').length > 0 ||
                   d.body.innerText.includes('@demo.'); })()""",
                   45, "shorts feed")
        time.sleep(0.8)
    else:
        raise SystemExit("unknown view %r" % view)
    time.sleep(0.6)          # one poll tick, so nothing re-renders mid-harvest


def harvest(ws, sid: str, tab: str, view: str, w: int, h: int) -> list[tuple[str, str, str]]:
    prepare(ws, sid, demo_env.PORT, view, w, h)
    if view.startswith("download"):
        ev(ws, sid, OPEN_FOR_HARVEST)
        time.sleep(0.5)
    rows = json.loads(ev(ws, sid, HARVEST_JS.replace("__DOC__", "document")
                                 .replace("__PREFIX__", "''")))
    if view == "shorts":
        frame_rows = json.loads(ev(ws, sid, HARVEST_JS
                                   .replace("__DOC__", "document.getElementById('shortsFrame').contentDocument")
                                   .replace("__PREFIX__", "'frame:'")))
        rows += frame_rows
    return [(tab, sel, lab) for _kind, sel, lab in rows]


def screenshot(ws, sid: str, port: int, view: str, w: int, h: int, dest: Path,
               ids: set[str]) -> tuple[int, int, int]:
    prepare(ws, sid, port, view, w, h)
    href = ev(ws, sid, "location.href")
    if str(LIVE_PORT) in href or ":8765" in href:
        raise SystemExit("refusing to photograph %s" % href)
    if "127.0.0.1:%d" % port not in href:
        raise SystemExit("wrong page: %s" % href)
    set_viewport(ws, sid, w, h)

    body = ev(ws, sid, "document.body.innerText")
    frame_text = ev(ws, sid, """(() => { const f = document.getElementById('shortsFrame');
        const d = f && f.contentDocument; return d && d.body ? d.body.innerText : ''; })()""") or ""
    for ident in ids:
        for where, blob in (("body", body), ("frame", frame_text)):
            if ident and ident in blob:
                raise SystemExit("LEAK: %r found in %s before shooting %s"
                                 % (ident, where, dest.name))

    data = ws.call("Page.captureScreenshot", session_id=sid, format="png",
                   captureBeyondViewport=False)["data"]
    import base64
    raw = base64.b64decode(data)
    dest.write_bytes(raw)
    size = shrink(dest)
    pw, ph = png_size(dest)
    return pw, ph, size


def png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("%s is not a PNG" % path)
    return struct.unpack(">II", head[16:24])


def shrink(path: Path) -> int:
    """Keep each PNG under the budget the docs images already meet."""
    if path.stat().st_size <= PNG_BUDGET:
        return path.stat().st_size
    from PIL import Image
    with Image.open(path) as im:
        im.convert("RGB").quantize(colors=192, method=Image.Quantize.MEDIANCUT).save(
            path, "PNG", optimize=True)
    return path.stat().st_size


# --------------------------------------------------------------- output text
def sanitise(text: str, root: Path) -> str:
    """No real path in a committed file: the demo tree becomes <demo-root>."""
    text = text.replace(str(root), "<demo-root>")
    text = text.replace(str(Path.home()), "<home>")
    if len(text) > 72:
        text = text[:69].rstrip() + "…"
    return text


def normalise(text: str) -> str:
    """Neutralise the numbers that move on their own between two runs."""
    text = re.sub(r"\d+(?:\.\d+)?\s?(?:KB|MB|GB|TB)\b", "<size>", text)
    text = re.sub(r"\d+(?:\.\d+)?%", "<pct>", text)
    text = re.sub(r"shown \d+×", "shown <n>×", text)
    return text


# --------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HWA_BASELINE_PORT", "8899")))
    ap.add_argument("--root", type=Path, default=demo_env.DEMO_ROOT)
    ap.add_argument("--out", type=Path, default=HERE)
    ap.add_argument("--reuse", action="store_true",
                    help="reuse the demo tree instead of rebuilding it")
    args = ap.parse_args()

    demo_env.DEMO_ROOT = args.root
    demo_env.PORT = args.port
    guard(args.port)

    before = real_state()
    ids = real_identifiers(before)
    print("-- building the demo instance under %s" % args.root)
    if not args.reuse:
        demo_env.build()
    proc = demo_env.start()
    port = demo_env.PORT
    guard(port)
    print("-- demo server on http://127.0.0.1:%d (live service untouched)" % port)
    rows: list[tuple[str, str, str]] = []
    shots: list[tuple[str, int, int, int]] = []
    chrome = ws = None
    try:
        demo_env.seed_ui_data(port)
        chrome, ws = cdp.launch(args.root / "chrome")
        for tab, w, h, view, shot in SCOPES:
            sid, tid = cdp.new_page(ws)
            try:
                rows += harvest(ws, sid, "%s @%dx%d" % (tab, w, h), view, w, h)
                print("   harvested %-20s %4dx%-4d" % (tab, w, h))
            finally:
                cdp.close_page(ws, tid)
        for tab, w, h, view, shot in SCOPES:
            if not shot:
                continue
            dest = args.out / shot
            sid, tid = cdp.new_page(ws)
            try:
                pw, ph, size = screenshot(ws, sid, port, view, w, h, dest, ids)
            finally:
                cdp.close_page(ws, tid)
            shots.append((shot, pw, ph, size))
            print("   shot %-24s %dx%d  %6.1f KB" % (shot, pw, ph, size / 1024))
            if (pw, ph) != (w, h):
                raise SystemExit("%s is %dx%d, wanted %dx%d" % (shot, pw, ph, w, h))
    finally:
        if chrome:
            cdp.kill(chrome)
        demo_env.stop(proc)

    # ---- dom.txt, aggregated and sorted
    # Labels are the page's own words, with three deliberate rewrites so two
    # runs on the same machine produce byte-identical files: the demo root is
    # replaced by <demo-root> (a real path may not reach git), and the disk
    # figures plus the shorts "shown N×" counter are environmental.
    counts: dict[tuple[str, str, str], int] = {}
    for tab, sel, lab in rows:
        key = (tab, sel, normalise(sanitise(lab, args.root)))
        counts[key] = counts.get(key, 0) + 1
    lines = [
        "# HardlyWatching UI baseline - DOM landmarks, captured from a FAKE archive.",
        "# format: tab @viewport | selector-or-role | visible label | count",
        "# regenerate: tests/baseline/capture.py (see README.md); do not hand-edit.",
        "# counts are per viewport: a control that only exists on one is a finding.",
        "",
    ]
    for (tab, sel, lab) in sorted(counts):
        lines.append("%s | %s | %s | %d" % (tab, sel, lab.replace("|", "\\|"), counts[(tab, sel, lab)]))
    out = args.out / "dom.txt"
    out.write_text("\n".join(lines) + "\n")
    print("-- wrote %s (%d rows, %d landmarks)"
          % (out, len(counts), sum(1 for r in counts if "landmark:" in r[1])))

    # ---- verification
    print("-- screenshots on disk")
    for name, pw, ph, size in shots:
        print("   %-24s %dx%d  %6.1f KB" % (name, pw, ph, size / 1024))
    ok = isolation_proof(before, real_state())
    print("-- leak check: committed text artefacts vs real identifiers")
    text_files = [p for p in args.out.rglob("*") if p.is_file() and p.suffix in (".txt", ".md", ".log")]
    hits = 0
    for p in text_files:
        blob = p.read_text(errors="replace")
        found = sorted(i for i in ids if i in blob)
        hits += len(found)
        print("   %-28s %s" % (p.name, "CLEAN" if not found else "FOUND %s" % found))
    return 0 if ok and hits == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
