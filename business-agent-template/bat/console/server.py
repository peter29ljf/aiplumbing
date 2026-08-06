"""The console: one page, a build running behind it, and everything it did visible.

Written on `http.server` so that starting it installs nothing. That is not frugality for
its own sake — the thing this console is for is watching an unattended process that takes
minutes, and a tool you have to set up before you can watch anything is a tool you use
once.

A build runs on its own thread and drops events into a queue per project; the browser
holds an SSE connection and drains it. Server-sent events rather than websockets because
everything here flows one way: the page sends a message now and then over an ordinary
POST, and watches for the next twenty minutes.

    python3 -m bat.console            # http://127.0.0.1:8770
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from bat.builder import claude, session
from bat.runtime import project as projects
from bat.runtime import registry
from bat.runtime.assemble import report as prompt_report
from bat.runtime.graph import BrokenFlow, load

STATIC = Path(__file__).resolve().parent / "static"
HOST, PORT = "127.0.0.1", 8770


@dataclass
class Live:
    """One project's running build: the thread, the pause switch, and the watchers."""

    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    watchers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            for watcher in list(self.watchers):
                try:
                    watcher.put_nowait(event)
                except queue.Full:
                    # A browser tab that stopped reading must not wedge the build. The
                    # state is on disk; a reconnect gets the truth from there.
                    pass

    def watch(self) -> queue.Queue:
        watcher: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.watchers.append(watcher)
        return watcher

    def unwatch(self, watcher: queue.Queue) -> None:
        with self.lock:
            if watcher in self.watchers:
                self.watchers.remove(watcher)

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


LIVE: dict[str, Live] = {}
LIVE_LOCK = threading.Lock()


def live(name: str) -> Live:
    with LIVE_LOCK:
        return LIVE.setdefault(name, Live())


# ----------------------------------------------------------------------
# what the page asks for
# ----------------------------------------------------------------------


def list_projects() -> dict[str, Any]:
    rows = []
    for project in projects.every():
        build = session.load(project.name)
        spend, calls = claude.total(project.dir / "spend.jsonl")
        rows.append({
            "name": project.name, "phase": build.phase, "waiting": build.waiting,
            "note": build.note, "usd": round(spend.usd, 4), "calls": calls,
            "nodes": _node_count(project),
        })
    # Projects that exist only as a half-finished build have no flow.yaml yet, so
    # `projects.every()` cannot see them. They are the ones most worth showing.
    known = {row["name"] for row in rows}
    if projects.PROJECTS.exists():
        for directory in sorted(projects.PROJECTS.iterdir()):
            if directory.is_dir() and directory.name not in known \
                    and (directory / "build.json").exists():
                build = session.load(directory.name)
                spend, calls = claude.total(directory / "spend.jsonl")
                rows.append({"name": directory.name, "phase": build.phase,
                             "waiting": build.waiting, "note": build.note,
                             "usd": round(spend.usd, 4), "calls": calls, "nodes": 0})
    return {"projects": rows}


def _node_count(project: projects.Project) -> int:
    try:
        return len(load(project, known_tools=registry.load_tools(project)).nodes)
    except (BrokenFlow, OSError):
        return 0


def build_state(name: str) -> dict[str, Any]:
    build = session.load(name)
    spend, calls = claude.total(session.directory(name) / "spend.jsonl")
    return {
        **build.as_dict(),
        "busy": live(name).busy,
        "spend": {**spend.as_dict(), "calls": calls},
        "plan": _read(session.directory(name) / "PLAN.md"),
    }


def flow_state(name: str) -> dict[str, Any]:
    """The graph, with what each node's prompt actually costs.

    The byte count is an acceptance figure, not decoration: a node that quietly grows back
    to the size this whole architecture exists to avoid is visible here and nowhere else.
    """
    try:
        project = projects.find(name)
    except projects.NoSuchProject as missing:
        return {"error": str(missing)}
    try:
        flow = load(project, known_tools=registry.load_tools(project))
    except BrokenFlow as broken:
        return {"error": str(broken)}

    sizes = {row["node"]: row for row in prompt_report(flow, registry.schemas_for)}
    return {
        "entry": flow.entry,
        "nodes": [
            {
                "name": node.name, "goal": node.goal, "rules": list(node.rules),
                "tools": list(node.tools), "sets_status": node.sets_status,
                "next": node.next, "branch": node.branch,
                "terminal": node.is_terminal,
                **{k: sizes.get(node.name, {}).get(k, 0)
                   for k in ("prompt", "schemas", "total")},
            }
            for node in flow.nodes.values()
        ],
    }


def dashboard(name: str) -> dict[str, Any]:
    """The last run, and what building this has cost so far.

    Two meters, kept apart. The builder's spend is Anthropic's and is what making the
    agent cost; the agent's is its own provider's and is what running it costs. Added
    together they would hide which one is worth attacking.
    """
    try:
        project = projects.find(name)
        runs_dir = project.runs_dir
    except projects.NoSuchProject:
        runs_dir = session.directory(name) / "runs"

    reports = sorted(runs_dir.glob("*.json")) if runs_dir.exists() else []
    latest = json.loads(reports[-1].read_text(encoding="utf-8")) if reports else []

    scenarios: dict[str, list[bool]] = {}
    faults: dict[str, int] = {}
    seconds: dict[str, list[float]] = {}
    agent = {"prompt": 0, "completion": 0, "cache_hit": 0, "calls": 0}

    for run in latest:
        scenarios.setdefault(run["id"], []).append(bool(run["passed"]))
        for verdict in run.get("verdicts") or []:
            faults[verdict["source"]] = faults.get(verdict["source"], 0) + 1
        for call in run.get("calls") or []:
            seconds.setdefault(call["node"], []).append(float(call["seconds"]))
        usage = run.get("usage") or {}
        agent["prompt"] += int(usage.get("prompt_tokens") or 0)
        agent["completion"] += int(usage.get("completion_tokens") or 0)
        agent["cache_hit"] += int(usage.get("cache_hit_tokens") or 0)
        agent["calls"] += int(usage.get("calls") or 0)

    spend, calls = claude.total(session.directory(name) / "spend.jsonl")
    won = sum(sum(1 for ok in runs if ok) for runs in scenarios.values())
    total_runs = sum(len(runs) for runs in scenarios.values())

    return {
        "report": reports[-1].name if reports else "",
        "reports": [p.name for p in reports[-20:]],
        "passed": won, "runs": total_runs,
        "rate": round(won / total_runs, 3) if total_runs else 0.0,
        "scenarios": [
            {"id": name_, "won": sum(1 for ok in runs if ok), "of": len(runs)}
            for name_, runs in sorted(scenarios.items())
        ],
        "faults": faults,
        "timings": sorted(
            ({"node": node, "worst": max(times), "calls": len(times),
              "mean": round(sum(times) / len(times), 1),
              "over": sum(1 for t in times if t > 20)}
             for node, times in seconds.items()),
            key=lambda row: -row["worst"],
        ),
        "builder": {**spend.as_dict(), "calls": calls},
        "agent": {
            **agent,
            "cache_hit_rate": round(agent["cache_hit"] / agent["prompt"], 3)
            if agent["prompt"] else 0.0,
        },
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ----------------------------------------------------------------------
# doing things
# ----------------------------------------------------------------------


def say(name: str, text: str) -> dict[str, Any]:
    """Start one builder turn on its own thread. Returns immediately; watch the stream."""
    running = live(name)
    if running.busy:
        return {"error": "already working — pause it first"}

    build = session.load(name)
    if build.phase == session.DONE:
        return {"error": "this one is finished"}

    running.stop.clear()

    def work() -> None:
        running.emit({"type": "bat.started", "phase": build.phase})
        try:
            reply = session.turn(build, text, on_event=running.emit, stop=running.stop)
            running.emit({"type": "bat.finished", "ok": reply.ok,
                          "error": reply.error, "usd": reply.spend.usd,
                          "text": reply.text})
        except Exception:  # noqa: BLE001 - a crashed thread must still report
            running.emit({"type": "bat.crashed", "trace": traceback.format_exc()[-3000:]})

    running.thread = threading.Thread(target=work, daemon=True)
    running.thread.start()
    return {"started": True, "phase": build.phase}


def approve(name: str) -> dict[str, Any]:
    build = session.load(name)
    if build.waiting != session.WAITING_FOR_APPROVAL:
        return {"error": f"not waiting for approval — it is {build.waiting or 'running'}"}
    build.waiting, build.note = "", ""
    session.save(build)
    return build_state(name)


def pause(name: str) -> dict[str, Any]:
    running = live(name)
    running.stop.set()
    build = session.load(name)
    build.waiting = session.PAUSED
    session.save(build)
    return {"paused": True}


def resume(name: str) -> dict[str, Any]:
    build = session.load(name)
    if build.waiting == session.PAUSED:
        build.waiting, build.note = "", ""
        session.save(build)
    live(name).stop.clear()
    return build_state(name)


def create(name: str) -> dict[str, Any]:
    clean = "".join(c for c in name.strip().lower().replace(" ", "_")
                    if c.isalnum() or c in "_-")
    if not clean:
        return {"error": "needs a name"}
    if (projects.PROJECTS / clean).exists():
        return {"error": f"`{clean}` already exists"}
    session.start(clean)
    return {"name": clean}


ROUTES_GET: dict[str, Callable[[str], dict[str, Any]]] = {
    "build": build_state,
    "flow": flow_state,
    "dashboard": dashboard,
}
ROUTES_POST: dict[str, Callable[[str], dict[str, Any]]] = {
    "approve": approve,
    "pause": pause,
    "resume": resume,
}


# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "bat-console"

    def log_message(self, *_: Any) -> None:
        """Quiet. The interesting log is the build stream, and one line per poll buries
        it."""

    # ---- plumbing ----------------------------------------------------
    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---- GET ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if len(parts) == 1 and parts[0] in ("app.js", "style.css"):
            kind = "text/css" if parts[0].endswith(".css") else "application/javascript"
            return self._file(parts[0], f"{kind}; charset=utf-8")

        if path == "/api/projects":
            return self._json(list_projects())

        if len(parts) == 4 and parts[0] == "api" and parts[3] == "events":
            return self._events(parts[2])

        if len(parts) == 3 and parts[0] == "api" and parts[1] in ROUTES_GET:
            return self._json(ROUTES_GET[parts[1]](parts[2]))

        self._json({"error": "no such thing"}, 404)

    def _file(self, name: str, kind: str) -> None:
        found = STATIC / name
        if not found.exists():
            return self._json({"error": f"{name} is missing"}, 404)
        self._send(200, found.read_bytes(), kind)

    def _events(self, name: str) -> None:
        """Hold the connection open and forward whatever the build says."""
        running = live(name)
        watcher = running.watch()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    event = watcher.get(timeout=15)
                except queue.Empty:
                    # A comment line. Keeps proxies and impatient browsers from deciding
                    # a quiet build is a dead connection.
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(
                    f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    .encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            running.unwatch(watcher)

    # ---- POST --------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()

        if parts == ["api", "projects"]:
            return self._json(create(str(body.get("name") or "")))

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "build" \
                and parts[3] == "say":
            return self._json(say(parts[2], str(body.get("text") or "")))

        if len(parts) == 3 and parts[0] == "api" and parts[1] in ROUTES_POST:
            return self._json(ROUTES_POST[parts[1]](parts[2]))

        self._json({"error": "no such thing"}, 404)


def serve(host: str = HOST, port: int = PORT) -> None:
    projects.PROJECTS.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"console on http://{host}:{port}  (projects in {projects.PROJECTS})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    serve()
