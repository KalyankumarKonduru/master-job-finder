#!/usr/bin/env python3
"""Watch a resume get built: the job description on the left, the page on the right,
every pipeline step playing out as it happens.

tailor.py writes each posting's steps to applications/_live/<run>.jsonl; this serves
those files and a single page that plays them back. The watcher starts it on its own
(tailor.live_preview in config.yaml); run it by hand to replay past postings while
the watcher is off.

  python live_preview.py               # http://127.0.0.1:8765 and opens your browser
  python live_preview.py --port 9000 --no-open

It listens on 127.0.0.1 only: the runs hold your resume and the postings you chased.
"""
import argparse
import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "live_preview.html")
DEFAULT_PORT = 8765
TERMINAL = {"done": "done", "skipped": "skipped", "error": "error"}
STALLED_AFTER = 600          # seconds without a write before a running run is called stalled
log = logging.getLogger("live_preview")
_summaries = {}              # name -> (mtime, size, summary): runs are re-read only on change


# ---------------------------------------------------------------- run files
def run_path(live_dir: str, name: str):
    """The run file `name` inside live_dir, or None. Names never carry a path."""
    if not name or name != os.path.basename(name) or not name.endswith(".jsonl"):
        return None
    path = os.path.join(live_dir, name)
    return path if os.path.isfile(path) else None


def read_run(live_dir: str, name: str, start: int = 0):
    """The run's steps from line `start` on; None when the run doesn't exist. A line
    still being written is left for the next read."""
    path = run_path(live_dir, name)
    if path is None:
        return None
    events = []
    with open(path) as f:
        for i, line in enumerate(f):
            if i < start:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                break
    return events


def _summarise(path: str) -> dict:
    out = {"name": os.path.basename(path), "status": "running", "score": None,
           "company": "", "title": "", "started": None, "alert": None}
    for e in read_run(os.path.dirname(path), out["name"]) or []:
        step = e.get("step")
        if step == "job":
            out.update(company=e.get("company") or "", title=e.get("title") or "",
                       started=e.get("ts"))
        elif step == "score":
            out["score"] = e.get("score")
        elif step == "alert":
            out["alert"] = bool(e.get("sent"))
        if step in TERMINAL:
            out["status"] = TERMINAL[step]
    if out["status"] == "running" and time.time() - os.path.getmtime(path) > STALLED_AFTER:
        out["status"] = "stalled"
    return out


def list_runs(live_dir: str, limit: int = 300) -> list[dict]:
    """Newest first. Run names start with their timestamp, so name order is age order."""
    runs = []
    for path in sorted(glob.glob(os.path.join(live_dir, "*.jsonl")), reverse=True)[:limit]:
        st = os.stat(path)
        cached = _summaries.get(path)
        if cached and cached[:2] == (st.st_mtime, st.st_size) and cached[2]["status"] != "running":
            runs.append(cached[2])
            continue
        summary = _summarise(path)
        _summaries[path] = (st.st_mtime, st.st_size, summary)
        runs.append(summary)
    return runs


def resume_png(live_dir: str, name: str):
    """A Quick Look picture of the run's finished .docx (macOS), cached next to the runs."""
    events = read_run(live_dir, name) or []
    docx = next((e.get("docx") for e in events if e.get("step") == "done"), None)
    if not docx or not os.path.isfile(docx):
        return None
    cache = os.path.join(live_dir, "_png")
    os.makedirs(cache, exist_ok=True)
    png = os.path.join(cache, os.path.basename(docx) + ".png")
    target = os.path.join(cache, name[:-len(".jsonl")] + ".png")
    if os.path.isfile(target) and os.path.getmtime(target) >= os.path.getmtime(docx):
        return target
    try:
        subprocess.run(["qlmanage", "-t", "-s", "1700", "-o", cache, docx],
                       capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not render %s: %s", docx, e)
        return None
    if not os.path.isfile(png):
        return None
    os.replace(png, target)
    return target


# ---------------------------------------------------------------- http
def make_handler(live_dir: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # the watcher's log stays readable
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/api/runs":
                return self._send(200, {"runs": list_runs(live_dir)})
            if url.path == "/api/run":
                start = int(q.get("from", "0") or 0)
                events = read_run(live_dir, q.get("name", ""), start)
                if events is None:
                    return self._send(404, {"error": "no such run"})
                return self._send(200, {"events": events, "next": start + len(events)})
            if url.path == "/api/png":
                png = resume_png(live_dir, q.get("name", "")) if run_path(
                    live_dir, q.get("name", "")) else None
                if not png:
                    return self._send(404, {"error": "no rendered resume"})
                with open(png, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            name = parse_qs(url.query).get("name", [""])[0]
            if url.path == "/api/open" and run_path(live_dir, name):
                events = read_run(live_dir, name) or []
                docx = next((e.get("docx") for e in events if e.get("step") == "done"), None)
                if docx and docx.endswith(".docx") and os.path.isfile(docx):
                    subprocess.Popen(["open", docx])
                    return self._send(200, {"opened": os.path.basename(docx)})
            self._send(404, {"error": "no resume to open"})

    return Handler


def live_dir_for(cfg: dict) -> str:
    import tailor
    tailor.apply_config(cfg)
    return os.path.join(tailor.OUT_DIR, "_live")


def start_in_background(cfg: dict, port: int = None):
    """For the watcher: serve on a daemon thread. Returns the URL, or None when the port
    is taken (another watcher or preview already serves it)."""
    port = int(port or (cfg.get("tailor") or {}).get("live_preview_port", DEFAULT_PORT))
    live_dir = live_dir_for(cfg)
    os.makedirs(live_dir, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(live_dir))
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="live-preview").start()
    return f"http://127.0.0.1:{port}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(os.environ.get("JOBWATCH_CONFIG", os.path.join(HERE, "config.yaml"))))
    url = start_in_background(cfg, a.port)
    if not url:
        sys.exit(f"port {a.port or DEFAULT_PORT} is busy - the watcher may already serve the "
                 f"preview there; open it, or pass --port")
    print(f"live preview at {url}  (Ctrl+C to stop)")
    if not a.no_open:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
