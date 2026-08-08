"""Local console: read the graph a customer talks to, edit its prompts, see what each node
holds, watch what is actually reaching the outside world, and talk to it.

    PYTHONPATH=src python3 -m plumbing.dashboard.server

Binds to 127.0.0.1 only. No dependencies beyond the standard library.

**It shows the flow, not the five agents.** Those are still in `agents/` and still driven
by the testkit suite, but nothing a customer says reaches them any more — a console that
displayed them would be a console describing a system nobody is running.

Node tool lists are read-only here, deliberately. They live in `flow/flow.yaml` next to a
paragraph saying which failing run put each one there, and writing that file back from a
form would delete every one of those comments. What a node is allowed to do and why it is
allowed to do it are the same fact; a console that could edit one and not the other would
quietly separate them.

On API keys: this console reports only whether a key is configured. It never displays one
and never accepts one as input. Put the key in the project's .env file — a plaintext
credential has no business travelling through a web form.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from plumbing import config
from plumbing import prompt_history
from plumbing.llm import active_provider
from plumbing.paths import (
    CONFIG_DIR,
    FLOW_DIR,
    FLOW_RULES_DIR,
    FLOW_RUNS_DIR,
    PROMPT_HISTORY_DIR,
    load_dotenv,
)

HTML_PATH = Path(__file__).parent / "index.html"

# The flow lives above `src/`, so it is not on the path by virtue of this package.
sys.path.insert(0, str(FLOW_DIR.parent))


# ======================================================================
# Reads
# ======================================================================


def _graph() -> Any:
    from flow.runner.graph import load  # noqa: PLC0415
    from flow.sim import tools  # noqa: PLC0415

    return load(known_tools=tools.names())


def node_overview() -> dict[str, Any]:
    """Every node in the graph, in the order a conversation meets them.

    Walked breadth-first from the entry rather than listed in file order, because the
    question a reader has is "what happens next", and file order answers a different one.
    """
    from flow.runner.assemble import build  # noqa: PLC0415
    from flow.sim import tools as flow_tools  # noqa: PLC0415

    try:
        flow = _graph()
    except Exception as exc:  # noqa: BLE001 - a broken graph is the thing to report
        return {"error": f"{type(exc).__name__}: {exc}", "nodes": [], "entry": ""}

    order, queue = [], [flow.entry]
    while queue:
        name = queue.pop(0)
        if name in order:
            continue
        order.append(name)
        queue.extend(t for t in flow[name].exits if t not in order)

    rows = []
    for name in order:
        node = flow[name]
        prompt = build(node)
        schemas = json.dumps(flow_tools.schemas_for(node.tools))
        rows.append({
            "name": name,
            "goal": node.goal,
            "rules": list(node.rules),
            "tools": list(node.tools),
            "sets_status": node.sets_status,
            "next": node.next,
            "branch": dict(node.branch),
            "is_entry": name == flow.entry,
            "is_terminal": node.is_terminal,
            "prompt_chars": len(prompt),
            "schema_chars": len(schemas),
            # The number the whole rewrite is judged on. The five agents sent 42,968
            # characters on every single call; a node that grows back should not be able
            # to do it quietly.
            "total_chars": len(prompt) + len(schemas),
        })
    return {"entry": flow.entry, "nodes": rows, "error": ""}


def tools_overview() -> dict[str, Any]:
    """The flow's tools, which nodes hold each, and what would really happen if called.

    Four lights, and the one that matters is the third:

    - **local** — never leaves the process. The database is this deployment's own record.
    - **simulated** — it could reach a service and the switch says no.
    - **live** — the switch says yes and everything it needs is present.
    - **blocked** — the switch says yes and it *cannot* reach the service. This is the
      dangerous state and the reason there is a light at all: the agent will call it,
      be refused, and stop dead in the middle of somebody's booking.
    """
    from flow.live.world import GATES  # noqa: PLC0415
    from flow.sim import tools as flow_tools  # noqa: PLC0415
    from plumbing.integrations.gate import is_live, preflight  # noqa: PLC0415

    holders: dict[str, list[str]] = {}
    try:
        for name, node in _graph().nodes.items():
            for tool in node.tools:
                holders.setdefault(tool, []).append(name)
    except Exception:  # noqa: BLE001 - the tool list is still worth showing
        pass

    entries = []
    for name in sorted(flow_tools.names()):
        schema = flow_tools.schemas_for((name,))[0]["function"]
        parameters = schema.get("parameters", {})
        gate = GATES.get(name, "")

        if not gate:
            status, blocker = "local", ""
        elif not is_live(gate):
            status, blocker = "simulated", ""
        elif (blocker := preflight(gate)):
            status = "blocked"
        else:
            status = "live"

        entries.append({
            "name": name,
            "namespace": name.partition(".")[0],
            "description": schema.get("description", ""),
            "parameters": sorted((parameters.get("properties") or {}).keys()),
            "required": list(parameters.get("required") or []),
            # What the engine copies onto the ticket without asking the model to write it
            # down as well. Worth showing: it is the difference between a fact that
            # survives the step and one that goes with the messages.
            "remembers": list(flow_tools.remembers(name)),
            "held_by": holders.get(name, []),
            # Not always its own name — see flow/live/world.py:GATES. `technician.notify`
            # answers to `telegram.send`, and reading the tool name instead had the
            # console calling it simulated while it messaged a real technician.
            "gate": gate,
            "status": status,
            "blocker": blocker,
        })

    counts = {"live": 0, "blocked": 0, "simulated": 0, "local": 0, "unheld": 0}
    for entry in entries:
        counts[entry["status"]] += 1
        if not entry["held_by"]:
            counts["unheld"] += 1

    # One row per switch, which is what the toggles act on. Several tools can hang off
    # one, and turning `telegram.send` off has to visibly take both of its tools with it.
    services = []
    for gate in sorted(set(GATES.values())):
        blocker = preflight(gate)
        services.append({
            "gate": gate,
            "on": is_live(gate),
            "blocker": blocker,
            "tools": sorted(t for t, g in GATES.items() if g == gate),
        })

    return {"tools": entries, "counts": counts, "services": services}


def set_live(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn services on or off for this process. Nothing is written to disk.

    `all` is the one-click form the console offers; `tools` is the explicit list. Either
    way the master switch follows: an empty list means everything is simulated, because a
    master switch on with nothing behind it is a state that reads as armed and is not.
    """
    from flow.live.world import GATES  # noqa: PLC0415
    from plumbing.integrations.gate import set_switches  # noqa: PLC0415

    every = sorted(set(GATES.values()))
    if payload.get("all") is not None:
        wanted = every if payload["all"] else []
    else:
        wanted = [g for g in payload.get("tools") or [] if g in every]
        unknown = [g for g in payload.get("tools") or [] if g not in every]
        if unknown:
            raise ValueError(f"Not a switch: {unknown}. There are: {every}")

    set_switches(enabled=bool(wanted), tools=wanted)
    return {"saved": True, **tools_overview(), "switches": live_switches()}


def prompt_files() -> list[dict[str, Any]]:
    """Every editable prompt file: what is always true, plus one per rule.

    `owner` is which nodes assemble that file into their prompt. A rule read by four nodes
    is a rule four conversations change at once, and that is worth seeing before editing.
    """
    holders: dict[str, list[str]] = {}
    try:
        flow = _graph()
        for name, node in flow.nodes.items():
            for rule in node.rules:
                holders.setdefault(rule, []).append(name)
    except Exception:  # noqa: BLE001
        pass

    files = [{"file": "always.md", "kind": "shared", "owner": "every node"}]
    for path in sorted(FLOW_RULES_DIR.glob("*.md")):
        used_by = holders.get(path.stem, [])
        files.append({
            "file": f"rules/{path.name}",
            "kind": "rule",
            "owner": ", ".join(used_by) if used_by else "nothing reads this",
        })
    return files


def last_run() -> dict[str, Any]:
    """How the graph did the last time the scenario suite ran against it.

    Read straight off the newest report in flow/runs/ rather than tracked live. A harness
    run is minutes of work in another process; a console that only knew about runs it had
    watched would show nothing almost all of the time.
    """
    reports = sorted(FLOW_RUNS_DIR.glob("*.json")) if FLOW_RUNS_DIR.exists() else []
    if not reports:
        return {"when": "", "scenarios": [], "passed": 0, "total": 0}

    newest = reports[-1]
    try:
        results = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"when": "", "scenarios": [], "passed": 0, "total": 0}

    by_id: dict[str, dict[str, Any]] = {}
    for row in results:
        entry = by_id.setdefault(row["id"], {"id": row["id"], "won": 0, "runs": 0,
                                             "problems": [], "nodes": [], "seconds": 0.0})
        entry["runs"] += 1
        entry["won"] += 1 if row["passed"] else 0
        entry["seconds"] = max(entry["seconds"], row.get("seconds", 0))
        entry["nodes"] = entry["nodes"] or row.get("nodes", [])
        if not row["passed"]:
            entry["problems"] = row.get("problems", [])[:3]

    return {
        "when": newest.stem,
        "file": newest.name,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "scenarios": sorted(by_id.values(), key=lambda s: (s["won"] / s["runs"], s["id"])),
    }


def assembled_prompt(name: str) -> dict[str, Any]:
    """Exactly what one node sends, and the tool schemas that go up beside it.

    Both, because the prompt alone is half the bill and half the story: a node whose
    wording is short and whose five tools carry two thousand characters of schema is a
    node that costs what a long prompt costs.
    """
    from flow.runner.assemble import build  # noqa: PLC0415
    from flow.sim import tools as flow_tools  # noqa: PLC0415

    flow = _graph()
    if name not in flow.nodes:
        raise ValueError(f"Unknown node '{name}'. There are: {sorted(flow.nodes)}")

    node = flow[name]
    schemas = flow_tools.schemas_for(node.tools, outcomes=node.choices or ("done",))
    return {
        "node": name,
        "content": build(node),
        "schemas": json.dumps(schemas, indent=2),
    }


def live_switches() -> dict[str, Any]:
    """What is actually reaching the outside world from this process, and on whose say-so.

    A console that said a tool was live while the process believed otherwise would be
    worse than no console: somebody reads it, believes the technician is being notified,
    and stops checking.
    """
    from plumbing.integrations.gate import live_status  # noqa: PLC0415

    return live_status()


def llm_settings() -> dict[str, Any]:
    load_dotenv()
    cfg = config.llm_config()
    provider = active_provider(cfg)
    key_env = provider.get("api_key_env", "DEEPSEEK_API_KEY")
    key = os.environ.get(key_env, "")
    return {
        "provider": provider,
        "active_provider": cfg.get("active"),
        "available_providers": sorted(cfg.get("providers") or {}),
        # A role only names a model when it should differ from the active provider's, so
        # the raw config has no model on most of them. Fill in the effective one — the
        # console was rendering blank rows after that became true.
        "roles": {
            name: {
                **spec,
                "model": spec.get("model") or provider.get("model"),
                "model_inherited": "model" not in spec,
            }
            for name, spec in cfg["roles"].items()
        },
        "limits": cfg.get("limits", {}),
        "doctor_backend": cfg.get("doctor_backend", {}),
        "api_key_env": key_env,
        # Presence and length only — never the value
        "api_key_configured": bool(key),
        "api_key_length": len(key),
    }


def full_state() -> dict[str, Any]:
    graph = node_overview()
    return {
        "graph": graph,
        "switches": live_switches(),
        "run": last_run(),
        "prompt_files": prompt_files(),
        "llm": llm_settings(),
        "prompt_changes": prompt_history.summary(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# ======================================================================
# Writes
# ======================================================================


def save_prompt(rel_file: str, content: str) -> dict[str, Any]:
    """Write a rule back, and keep a snapshot of what it said before.

    The saved file is read straight into the next conversation's prompt — `assemble`
    caches per process, so the cache is cleared here rather than on the next restart.
    """
    allowed = {f["file"] for f in prompt_files()}
    if rel_file not in allowed:
        raise ValueError(f"Editing '{rel_file}' is not permitted. Editable: {sorted(allowed)}")
    if not content.strip():
        raise ValueError("Content cannot be empty")

    path = FLOW_DIR / rel_file
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old == content:
        return {"saved": False, "message": "No change"}

    # Same history mechanism doctor uses, so manual edits are equally revertible.
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = rel_file.replace("/", "_").replace(".md", "")
    history = PROMPT_HISTORY_DIR / f"{stamp}-flow_{slug}-manual.md"
    history.write_text(
        f"# Prompt change record (manual console edit)\n\n"
        f"- Time: {datetime.now().isoformat()}\n"
        f"- File: flow/{rel_file}\n\n"
        f"## Before\n\n```markdown\n{old}\n```\n\n"
        f"## After\n\n```markdown\n{content}\n```\n",
        encoding="utf-8",
    )
    path.write_text(content, encoding="utf-8")

    from flow.runner import assemble  # noqa: PLC0415

    assemble._read.cache_clear()
    config.reload_all()
    return {"saved": True, "history": history.name, "chars": len(content)}


def save_llm(payload: dict[str, Any]) -> dict[str, Any]:
    """Only base_url, timeout, retries, per-role model settings and run limits."""
    cfg = config.llm_config()

    # Writes land on whichever provider is active, so editing the console while
    # `active: deepseek` cannot silently retune the qwen block.
    target = active_provider(cfg)
    provider = payload.get("provider", {})
    for key in ("base_url", "timeout_seconds", "max_retries"):
        if key in provider:
            target[key] = provider[key]
    if "api_key" in payload or "api_key" in provider:
        raise ValueError(
            "This console does not accept API keys. Edit the project's .env file directly."
        )

    for role, spec in (payload.get("roles") or {}).items():
        if role not in cfg["roles"]:
            raise ValueError(f"Unknown role '{role}'")
        for key in ("model", "temperature", "max_tokens"):
            if key in spec:
                cfg["roles"][role][key] = spec[key]

    for key, value in (payload.get("limits") or {}).items():
        if key in cfg.get("limits", {}):
            cfg["limits"][key] = int(value)

    doctor = payload.get("doctor_backend") or {}
    for key in ("kind", "model", "timeout_seconds"):
        if key in doctor:
            cfg.setdefault("doctor_backend", {})[key] = doctor[key]

    _write_yaml_preserving_header(CONFIG_DIR / "llm.yaml", cfg)
    config.reload_all()
    return {"saved": True}


def _write_yaml_preserving_header(path: Path, data: dict[str, Any]) -> None:
    """Keep the leading comment block, rewrite the body.

    Inline comments deeper in the file are lost — an accepted trade for being able to
    edit config from the console without a round-tripping YAML parser.
    """
    header_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break
    header = "\n".join(header_lines).rstrip()
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    path.write_text(f"{header}\n\n{body}", encoding="utf-8")


# ======================================================================
# Talking to the agent
# ======================================================================
#
# The same FlowConversation production uses, over the same graph and the same live world,
# so what is said here is what a customer would be told. What differs is only the switches:
# this process has no PLUMBING_LIVE_* set, so every adapter answers "not live" and nothing
# leaves the machine. Booking from this console writes a row and puts no entry in anybody's
# real diary.
#
# It does write to the console's own database, on purpose. A conversation that persisted
# nothing would not be exercising the half of production most likely to be wrong.
#
# Every model call is timed. "Is the agent slow?" deserves a breakdown rather than a
# verdict — the answer decides whether to change the model, the prompt, or the tool list.

CONSOLE_DB = "data/console.db"

_chat_lock = threading.Lock()
_chat: dict[str, Any] = {"conversation": None, "llm": None}


def _conversation(phone: str = "") -> Any:
    from plumbing.live.flow_conversation import FlowConversation  # noqa: PLC0415
    from plumbing.llm import LLM  # noqa: PLC0415
    from plumbing.store import SqliteStore  # noqa: PLC0415

    if _chat["conversation"] is None:
        llm = LLM()
        _chat["llm"] = llm
        _chat["conversation"] = FlowConversation(
            store=SqliteStore(CONSOLE_DB), llm=llm, channel="chat",
            phone=phone, session_id="console",
        )
    return _chat["conversation"]


def chat_reset(phone: str = "") -> dict[str, Any]:
    """A number here means the conversation starts the way a real one does.

    Production takes it on the form, so the agent is told it up front and spends its first
    turn looking the customer up, reading the diary and pricing the call-out. Without one
    the agent just asks for it, which is a much shorter turn — and timing that instead is
    how you measure the wrong thing and conclude the agent is fast.
    """
    with _chat_lock:
        _chat["conversation"] = None
        _chat["llm"] = None
        _chat["turn"] = None
        _chat["phone"] = (phone or "").strip()
    return {"ok": True, "phone": _chat.get("phone", "")}


def chat_start(text: str) -> dict[str, Any]:
    """Take the message and answer later, so the page can watch it being worked on.

    It used to block until the reply came, which is why the console could only ever show
    three dots and a clock: with no request in flight there was nothing to ask. A turn is
    twenty to forty seconds of real work — a lookup, the diary, the pricing — and a
    console whose whole job is showing what the agent does should show that rather than
    hide it behind a spinner.

    Deliberately the same shape as the widget's `/chat/message` + `/chat/poll`, and it
    reuses the same wording table, so what a developer watches here and what a customer
    reads are the same thing.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Say something first.")

    with _chat_lock:
        running = _chat.get("turn")
        if running is not None and not running["done"]:
            raise ValueError("Still working on the last message — one moment.")
        turn: dict[str, Any] = {"done": False, "began": time.monotonic(), "doing": "",
                                "node": "", "calls": [], "reply": None, "error": None}
        _chat["turn"] = turn

    threading.Thread(target=_run_console_turn, args=(text, turn), daemon=True).start()
    return {"status": "working"}


def _run_console_turn(text: str, turn: dict[str, Any]) -> None:
    from plumbing.live.server import _doing  # noqa: PLC0415
    from plumbing.llm import LLM  # noqa: PLC0415

    original = LLM.chat
    try:
        with _chat_lock:
            conversation = _conversation(_chat.get("phone", ""))

        def watch(tool: str) -> None:
            # The last tool that had something worth saying. A batch usually ends on
            # bookkeeping, and letting that blank the line means the only thing anybody
            # ever sees is the generic one — the same trap the widget fell into.
            if (phrase := _doing(tool)):
                turn["doing"] = phrase

        def timed(self, role, messages, tools=None, tool_choice=None, response_format=None):
            started = time.monotonic()
            message = original(self, role, messages, tools=tools, tool_choice=tool_choice,
                               response_format=response_format)
            turn["calls"].append({
                "seconds": round(time.monotonic() - started, 1),
                "tools": [c.function.name
                          for c in (getattr(message, "tool_calls", None) or [])],
            })
            turn["node"] = conversation.node
            return message

        conversation.progress = watch
        LLM.chat = timed
        turn["reply"] = conversation.say(text)
        turn["node"] = conversation.node
        turn["tags"] = dict(conversation.talk.tags)
        turn["ticket"] = conversation.ticket_id
        turn["closed"] = conversation.closed
    except Exception as exc:  # noqa: BLE001 - it belongs on the page, not in a traceback
        turn["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        LLM.chat = original
        turn["done"] = True


def chat_poll() -> dict[str, Any]:
    """Is it done? And if not, what is it doing and how long has it been at it."""
    turn = _chat.get("turn")
    if turn is None:
        return {"status": "idle"}

    elapsed = round(time.monotonic() - turn["began"], 1)
    if not turn["done"]:
        return {
            "status": "working",
            "seconds": elapsed,
            "doing": turn["doing"] or "Thinking",
            "agent": turn["node"],
            "calls": list(turn["calls"]),
        }

    with _chat_lock:
        _chat["turn"] = None            # handed over once, then forgotten

    if turn["error"]:
        return {"status": "error", "error": turn["error"], "seconds": elapsed}
    return {
        "status": "ready",
        "reply": turn["reply"],
        "seconds": elapsed,
        "calls": turn["calls"],
        "agent": turn["node"],
        "tags": turn.get("tags") or {},
        "ticket": turn.get("ticket", ""),
        "closed": turn.get("closed", False),
    }


def chat_say(text: str) -> dict[str, Any]:
    """One turn, start to finish, in one call. Kept for scripts; the page uses the pair
    above so it can watch the work rather than wait on it."""
    from plumbing.llm import LLM  # noqa: PLC0415

    text = (text or "").strip()
    if not text:
        raise ValueError("Say something first.")

    with _chat_lock:
        conversation = _conversation(_chat.get("phone", ""))
        calls: list[dict[str, Any]] = []
        original = LLM.chat

        def timed(self, role, messages, tools=None, tool_choice=None, response_format=None):
            started = time.monotonic()
            message = original(self, role, messages, tools=tools, tool_choice=tool_choice,
                               response_format=response_format)
            calls.append({
                "seconds": round(time.monotonic() - started, 1),
                "tools": [c.function.name
                          for c in (getattr(message, "tool_calls", None) or [])],
            })
            return message

        LLM.chat = timed
        began = time.monotonic()
        try:
            reply = conversation.say(text)
        finally:
            LLM.chat = original

        return {
            "reply": reply,
            "seconds": round(time.monotonic() - began, 1),
            "calls": calls,
            # Which step of the graph is holding the conversation now, and what it has
            # concluded. Between them they answer the question the console exists for:
            # when an answer is wrong, which node's prompt to go and read.
            "agent": conversation.node,
            "ticket": conversation.ticket_id,
            "tags": dict(conversation.talk.tags),
            "closed": conversation.closed,
        }


# ======================================================================
# HTTP
# ======================================================================


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # silence access logs
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path in ("/", "/index.html"):
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif route.path == "/api/state":
                self._json(full_state())
            elif route.path == "/api/tools":
                self._json(tools_overview())
            elif route.path == "/api/prompt":
                rel = parse_qs(route.query).get("file", [""])[0]
                allowed = {f["file"] for f in prompt_files()}
                if rel not in allowed:
                    self._json({"error": f"Reading '{rel}' is not permitted"}, 400)
                    return
                self._json(
                    {"file": rel, "content": (FLOW_DIR / rel).read_text(encoding="utf-8")}
                )
            elif route.path == "/api/prompt-changes":
                self._json(prompt_history.records())
            elif route.path == "/api/prompt-change":
                rec_id = parse_qs(route.query).get("id", [""])[0]
                found = prompt_history.get(rec_id)
                if found is None:
                    self._json({"error": "no such record"}, 404)
                else:
                    self._json(found)
            elif route.path == "/api/assembled":
                name = parse_qs(route.query).get("node", [""])[0]
                self._json(assembled_prompt(name))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")

            if route.path == "/api/prompt":
                self._json(save_prompt(payload["file"], payload["content"]))
            elif route.path == "/api/live":
                self._json(set_live(payload))
            elif route.path == "/api/llm":
                self._json(save_llm(payload))
            elif route.path == "/api/chat":
                self._json(chat_start(payload.get("text", "")))
            elif route.path == "/api/chat/poll":
                self._json(chat_poll())
            elif route.path == "/api/chat/reset":
                self._json(chat_reset(payload.get("phone", "")))
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plumbing agent console")
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Console running at {url}")
    print("Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
