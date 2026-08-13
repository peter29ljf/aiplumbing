"""A second builder backend, for when Claude Code is not the thing writing the files.

`claude.py` hands the work to Claude Code, which brings its own file tools and its own
confinement flags. This brings neither: it is a tool loop over any OpenAI-compatible
endpoint, which is what makes the generator usable on a provider the CLI cannot reach —
Bailian's DeepSeek, in practice, which is also what the generated agents are tested
against.

**The confinement is code here, and that is the whole difference.** Claude Code is kept
inside one directory by `--add-dir`, enforced by something else. Nothing enforces anything
for this loop, so every path a tool is given is resolved and checked against the project
root before it is opened. A rule the model could talk itself out of is not a boundary.

The reply shape matches `claude.Reply` exactly, so `session.py` does not know or care
which backend ran, and the console's event stream is the same either way.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from bat.builder.claude import Reply, Spend, record

# How many tool calls one turn may make before it is stopped. A build writes twenty-odd
# files and reads a dozen; sixty is generous. Without it, a loop that has decided to grep
# the whole tree runs until the bill says otherwise.
MAX_STEPS = 60
# A build writes twenty-odd files and reads a dozen before it is finished; the first real
# run stopped at sixty with sixteen files written and the scenarios still to come.
BUILD_STEPS = 200

SYSTEM = """You are building files inside one project directory. You have file tools and
one command. Work until the job is done, then say what you did in a few sentences.

Rules that are enforced rather than requested:

- Every path you give a tool is resolved against the project root and refused if it lands
  outside. Relative paths are simplest and always work.
- `run_harness` is the only command you can run.

Say nothing until you have finished. Nobody is reading along; the answer is the files."""


class OutsideTheProject(Exception):
    """A path that resolved somewhere it has no business being."""


def _under(root: Path, target: Path) -> bool:
    root = root.resolve()
    return target == root or root in target.parents


def _inside(root: Path, given: str, *, readable: tuple[Path, ...] = ()) -> Path:
    """Resolve `given` under `root`, or refuse.

    `resolve()` before comparing, so `../../etc/passwd` and a symlink pointing out of the
    tree are both caught — a string check on `..` catches neither.

    `readable` widens this for reads only. The kit's tools and rule patterns live beside
    the project and are what a build copies its shapes from; the first real run tried to
    list them, was refused, and spent a dozen steps grepping for what it could have read
    in one. **Writes never take this argument** — the asymmetry is the point.
    """
    target = (root / given).resolve() if not Path(given).is_absolute() else Path(given).resolve()
    if _under(root, target) or any(_under(extra, target) for extra in readable):
        return target
    raise OutsideTheProject(
        f"'{given}' resolves to {target}, which is outside {root}. "
        f"Everything you need is inside the project; use a relative path."
    )


# ----------------------------------------------------------------------
# the tools
# ----------------------------------------------------------------------

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file. Use it before editing anything — an edit written "
                       "from memory of a file is an edit against a file that has changed.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative to the project root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a file, creating directories as needed. Replaces whatever "
                       "was there.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact stretch of text in a file. Fails if the old "
                       "text is not there or appears more than once — which is the point: "
                       "a replacement that silently did nothing is worse than an error.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "What is in the project, or in one directory of it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Optional; the whole project if omitted"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Find a string across the project's text files.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "run_harness",
        "description": "Run the project's scenarios. This is also how you check the graph "
                       "loads: a broken flow.yaml reports every problem at once.",
        "parameters": {"type": "object", "properties": {
            "only": {"type": "string", "description": "Optional scenario id fragment"},
            "repeat": {"type": "integer", "description": "Defaults to 1 while building"}},
            "required": []}}},
]

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".py", ".json", ".txt"}


def _do(root: Path, name: str, args: dict[str, Any], project_name: str,
        repo_root: Path, readable: tuple[Path, ...] = ()) -> str:
    if name == "read_file":
        found = _inside(root, args["path"], readable=readable)
        if not found.exists():
            return f"There is no {args['path']} yet."
        return found.read_text(encoding="utf-8")

    if name == "write_file":
        target = _inside(root, args["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["text"], encoding="utf-8")
        return f"Wrote {args['path']}, {len(args['text'])} characters."

    if name == "edit_file":
        target = _inside(root, args["path"])
        if not target.exists():
            return f"There is no {args['path']} to edit."
        body = target.read_text(encoding="utf-8")
        found = body.count(args["old"])
        if found == 0:
            return ("That text is not in the file. Read it again — it has probably "
                    "changed since you last looked.")
        if found > 1:
            return (f"That text appears {found} times, so replacing it would be a guess. "
                    f"Include more of the surrounding lines.")
        target.write_text(body.replace(args["old"], args["new"]), encoding="utf-8")
        return f"Edited {args['path']}."

    if name == "list_files":
        base = _inside(root, args.get("path") or ".", readable=readable)
        # Relative to the project when it is in the project, absolute when it is the kit —
        # `relative_to` raises on a path that is not underneath, and listing the kit is a
        # thing this is now allowed to do.
        under = _under(root, base)
        found = sorted(
            (p.relative_to(root).as_posix() if under else p.as_posix())
            for p in base.rglob("*") if p.is_file()
            and "__pycache__" not in p.parts and "/runs/" not in p.as_posix())
        return "\n".join(found) or "(nothing here yet)"

    if name == "search":
        hits = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8",
                                                         errors="replace").splitlines(), 1):
                if args["text"] in line:
                    hits.append(f"{path.relative_to(root)}:{number}: {line.strip()[:160]}")
        return "\n".join(hits[:60]) or "No matches."

    if name == "run_harness":
        argv = ["python3", "-m", "bat.runtime.harness", "--project", project_name,
                "--repeat", str(args.get("repeat") or 1)]
        if args.get("only"):
            argv.append(args["only"])
        done = subprocess.run(argv, cwd=str(repo_root), capture_output=True, text=True,
                              timeout=1800)
        return (done.stdout + done.stderr)[-12000:]

    return f"No tool called {name}."


# ----------------------------------------------------------------------
def run(prompt: str, *, project_dir: Path, settings: dict[str, Any],
        system: str = "", project_name: str = "", repo_root: Path | None = None,
        readable: tuple[Path, ...] = (), max_steps: int = MAX_STEPS,
        history: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        stop: threading.Event | None = None,
        ledger: Path | None = None, phase: str = "") -> tuple[Reply, list[dict[str, Any]]]:
    """One builder turn. Returns the reply and the messages, for the next turn to resume.

    There is no session id to resume from the way Claude Code has one, so continuity is
    the message list — kept by the caller and handed back in. That is the whole difference
    between the two backends from `session.py`'s point of view.
    """
    from bat.runtime.llm import LLM

    root = project_dir.resolve()
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    project_name = project_name or root.name
    llm = LLM(settings)

    # Built once, on the first turn, and never touched again. Everything stable lives
    # here so the provider sees a byte-identical prefix on every call; the turn's own
    # content is the only thing appended. Rebuilding this each turn — which is what
    # putting it in the user message amounted to — is why the loop ran at 49% hits.
    messages = list(history or [
        {"role": "system", "content": f"{SYSTEM}\n\n---\n\n{system}" if system
                                      else SYSTEM}
    ])
    messages.append({"role": "user", "content": prompt})

    reply = Reply()
    for _ in range(max_steps):
        if stop is not None and stop.is_set():
            reply.error = "stopped"
            if on_event is not None:
                on_event({"type": "bat.stopped", "reason": "asked to stop"})
            break

        message = llm.chat("builder", messages, tools=TOOLS)
        calls = list(getattr(message, "tool_calls", None) or [])
        said = (message.content or "").strip()

        payload: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if calls:
            payload["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls]
        messages.append(payload)

        if on_event is not None and said:
            on_event({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": said}]}})

        if not calls:
            reply.text = said
            break

        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as bad:
                answer = f"Those arguments are not JSON: {bad}"
            else:
                if on_event is not None:
                    on_event({"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": call.function.name, "input": args}]}})
                try:
                    answer = _do(root, call.function.name, args, project_name,
                                 repo_root, readable)
                except OutsideTheProject as refused:
                    answer = str(refused)
                except Exception as broke:      # noqa: BLE001 - a bad call is not fatal
                    answer = f"{type(broke).__name__}: {broke}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": answer})
    else:
        reply.error = f"gave up after {max_steps} tool calls without finishing"

    usage = llm.usage.as_dict() if hasattr(llm, "usage") else {}
    reply.spend = Spend(
        # No dollar figure: an OpenAI-compatible endpoint does not return one, and an
        # invented one is worse than none. The tokens are real and the provider's own
        # console does the arithmetic.
        usd=0.0,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read=int(usage.get("cache_hit_tokens") or 0),
    )
    reply.turns = int(usage.get("calls") or 0)
    if ledger is not None:
        record(reply, ledger, phase=phase, prompt=prompt)
    if on_event is not None:
        on_event({"type": "bat.finished", "ok": reply.ok, "error": reply.error,
                  "usd": 0.0, "text": reply.text})
    return reply, messages
