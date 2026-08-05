"""Local console: see which agents are working or on standby, edit prompts, manage the
tool catalogue and each agent's tool grants, and adjust model settings.

    PYTHONPATH=src python3 -m plumbing.dashboard.server

Binds to 127.0.0.1 only. No dependencies beyond the standard library.

On API keys: this console reports only whether a key is configured. It never displays one
and never accepts one as input. Put the key in the project's .env file — a plaintext
credential has no business travelling through a web form.
"""

from __future__ import annotations

import argparse
import json
import os
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from plumbing import agent_registry, config, livestatus
from plumbing import prompt_history
from plumbing.paths import AGENTS_DIR, CONFIG_DIR, PROMPT_HISTORY_DIR, load_dotenv
from plumbing.tools import catalog as tool_catalog
from plumbing.tools import resolve

HTML_PATH = Path(__file__).parent / "index.html"


# ======================================================================
# Reads
# ======================================================================


def agent_overview() -> list[dict[str, Any]]:
    config.reload_all()
    cfg = config.agents_config()
    live = livestatus.read()
    active = live.get("active_agent", "")
    stats = live.get("agents", {})

    rows = []
    for name, spec in cfg["agents"].items():
        prompt_file = spec["prompt"]
        prompt_path = AGENTS_DIR / prompt_file
        try:
            tools = resolve(spec.get("tools", []))
            tool_names = [t.name for t in tools]
            tool_error = ""
        except ValueError as exc:
            tool_names = []
            tool_error = str(exc)

        entry = stats.get(name, {})
        if active == name and live.get("running"):
            status = "working"
        elif entry.get("turns") or entry.get("tool_calls"):
            status = "done"
        else:
            status = "standby"

        rows.append(
            {
                "name": name,
                "description": spec["description"],
                "is_stub": bool(spec.get("stub")),
                "is_entry": name == cfg.get("entry_agent"),
                "prompt_file": prompt_file,
                "prompt_chars": prompt_path.read_text(encoding="utf-8").__len__()
                if prompt_path.exists()
                else 0,
                "assembled_chars": len(agent_registry.build_system_prompt(name, cfg)),
                "shared": spec.get("shared", []),
                "patterns": spec.get("tools", []),
                "tools": tool_names,
                "tool_error": tool_error,
                "handoff_to": spec.get("handoff_to", []),
                "status": status,
                "turns": entry.get("turns", 0),
                "tool_calls": entry.get("tool_calls", 0),
            }
        )
    return rows


def tools_overview() -> dict[str, Any]:
    """The full tool catalogue plus which agents currently hold each tool."""
    config.reload_all()
    cfg = config.agents_config()
    entries = tool_catalog()

    granted: dict[str, list[str]] = {}
    for agent_name, spec in cfg["agents"].items():
        try:
            for item in resolve(spec.get("tools", [])):
                granted.setdefault(item.name, []).append(agent_name)
        except ValueError:
            continue

    for entry in entries:
        entry["granted_to"] = granted.get(entry["name"], [])

    counts = {"live": 0, "mocked": 0, "planned": 0}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    return {"tools": entries, "counts": counts}


def prompt_files() -> list[dict[str, Any]]:
    """Every editable prompt file: shared fragments plus each agent's own."""
    files = []
    for path in sorted((AGENTS_DIR / "_shared").glob("*.md")):
        files.append({"file": f"_shared/{path.name}", "kind": "shared", "owner": "all agents"})
    cfg = config.agents_config()
    for name, spec in cfg["agents"].items():
        files.append({"file": spec["prompt"], "kind": "agent", "owner": name})
    return files


def llm_settings() -> dict[str, Any]:
    load_dotenv()
    cfg = config.llm_config()
    key_env = cfg["provider"].get("api_key_env", "DEEPSEEK_API_KEY")
    key = os.environ.get(key_env, "")
    return {
        "provider": cfg["provider"],
        "roles": cfg["roles"],
        "limits": cfg.get("limits", {}),
        "doctor_backend": cfg.get("doctor_backend", {}),
        "api_key_env": key_env,
        # Presence and length only — never the value
        "api_key_configured": bool(key),
        "api_key_length": len(key),
    }


def full_state() -> dict[str, Any]:
    return {
        "agents": agent_overview(),
        "live": livestatus.read(),
        "prompt_files": prompt_files(),
        "llm": llm_settings(),
        "prompt_changes": prompt_history.summary(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# ======================================================================
# Writes
# ======================================================================


def save_prompt(rel_file: str, content: str) -> dict[str, Any]:
    allowed = {f["file"] for f in prompt_files()}
    if rel_file not in allowed:
        raise ValueError(f"Editing '{rel_file}' is not permitted. Editable: {sorted(allowed)}")
    if not content.strip():
        raise ValueError("Content cannot be empty")

    path = AGENTS_DIR / rel_file
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old == content:
        return {"saved": False, "message": "No change"}

    # Same history mechanism doctor uses, so manual edits are equally revertible
    PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = rel_file.replace("/", "_").replace(".md", "")
    history = PROMPT_HISTORY_DIR / f"{stamp}-{slug}-manual.md"
    history.write_text(
        f"# Prompt change record (manual console edit)\n\n"
        f"- Time: {datetime.now().isoformat()}\n"
        f"- File: agents/{rel_file}\n\n"
        f"## Before\n\n```markdown\n{old}\n```\n\n"
        f"## After\n\n```markdown\n{content}\n```\n",
        encoding="utf-8",
    )
    path.write_text(content, encoding="utf-8")
    config.reload_all()
    return {"saved": True, "history": history.name, "chars": len(content)}


def save_agent_tools(agent: str, tools: list[str]) -> dict[str, Any]:
    """Replace an agent's tool allow-list and write config/agents.yaml."""
    cfg = config.agents_config()
    if agent not in cfg["agents"]:
        raise ValueError(f"Unknown agent '{agent}'")
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")

    cleaned = [str(t).strip() for t in tools if str(t).strip()]
    if not cleaned:
        raise ValueError("An agent needs at least one tool")

    # Every entry must resolve, so a bad edit cannot silently break the next run.
    try:
        resolved = resolve(cleaned)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    cfg["agents"][agent]["tools"] = cleaned
    _write_yaml_preserving_header(CONFIG_DIR / "agents.yaml", cfg)
    config.reload_all()
    return {"saved": True, "agent": agent, "patterns": cleaned, "resolved": len(resolved)}


def save_llm(payload: dict[str, Any]) -> dict[str, Any]:
    """Only base_url, timeout, retries, per-role model settings and run limits."""
    cfg = config.llm_config()

    provider = payload.get("provider", {})
    for key in ("base_url", "timeout_seconds", "max_retries"):
        if key in provider:
            cfg["provider"][key] = provider[key]
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
                    {"file": rel, "content": (AGENTS_DIR / rel).read_text(encoding="utf-8")}
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
                name = parse_qs(route.query).get("agent", [""])[0]
                cfg = config.agents_config()
                if name not in cfg["agents"]:
                    self._json({"error": f"Unknown agent '{name}'"}, 400)
                    return
                self._json(
                    {"agent": name, "content": agent_registry.build_system_prompt(name, cfg)}
                )
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
            elif route.path == "/api/agent-tools":
                self._json(save_agent_tools(payload["agent"], payload["tools"]))
            elif route.path == "/api/llm":
                self._json(save_llm(payload))
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
