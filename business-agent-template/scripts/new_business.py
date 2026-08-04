#!/usr/bin/env python3
"""Scaffold a new business workflow agent project from this one.

Copies the engine verbatim, keeps the service-business kit, and blanks out everything
domain-specific into commented templates with TODO markers.

    python3 business-agent-template/scripts/new_business.py \\
        --name "Northshore Dental" \\
        --package dental \\
        --out ~/workspace/dental \\
        --delivery in_store

What you get is a project that imports cleanly and passes its engine tests on day one,
with every domain decision marked TODO and cross-referenced to CHECKLIST.md.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "template"

# Copied as-is (only the package name and domain examples in comments change).
ENGINE_FILES = [
    "src/{pkg}/__init__.py",
    "src/{pkg}/llm.py",
    "src/{pkg}/config.py",
    "src/{pkg}/paths.py",
    "src/{pkg}/agent.py",
    "src/{pkg}/agent_registry.py",
    "src/{pkg}/orchestrator.py",
    "src/{pkg}/livestatus.py",
    "src/{pkg}/tools/__init__.py",
    "src/{pkg}/tools/registry.py",
    "src/{pkg}/testkit/__init__.py",
    "src/{pkg}/testkit/scenario.py",
    "src/{pkg}/testkit/assertions.py",
    "src/{pkg}/testkit/judge.py",
    "src/{pkg}/testkit/doctor.py",
    "src/{pkg}/testkit/loop.py",
    "src/{pkg}/testkit/runner.py",
    "src/{pkg}/dashboard/__init__.py",
    "src/{pkg}/dashboard/server.py",
    "src/{pkg}/dashboard/index.html",
    "src/{pkg}/integrations/__init__.py",
    "src/{pkg}/integrations/gate.py",
    "src/{pkg}/integrations/twilio_sms.py",
    "src/{pkg}/integrations/gmail_email.py",
    "src/{pkg}/integrations/stripe_payments.py",
    "src/{pkg}/sim/__init__.py",
    "src/{pkg}/sim/customer.py",
    "src/{pkg}/sim/technician.py",
    "src/{pkg}/sim/supervisor.py",
    "scripts/check_llm.py",
    "pytest.ini",
    ".gitignore",
    ".env.example",
]

# The service-business kit: structure survives, vocabulary and rules do not.
KIT_FILES = [
    "src/{pkg}/world.py",
    "src/{pkg}/tools/info_tools.py",
    "src/{pkg}/tools/comms_tools.py",
    "src/{pkg}/tools/ops_tools.py",
    "src/{pkg}/tools/job_tools.py",
]

# Engine tests worth keeping — they test the engine, not the domain.
ENGINE_TEST_MARKERS = [
    "test_registry_is_safe_under_concurrent_first_use",
    "test_master_switch_is_off_by_default",
    "test_tool_marked_live_still_simulated_while_master_switch_off",
    "test_gate_opens_only_when_both_switches_set",
    "test_wire_names_are_openai_compatible",
    "test_bad_json_arguments_returned_as_error_not_crash",
    "test_whitelist_hides_unlisted_tools",
]

# Domain vocabulary that must not survive into a new project's comments and examples.
# Left in place, a reader of the new codebase is quietly misled about what it does.
DOMAIN_WORDS = [
    "plumbing", "Plumbing", "PLUMBING",
    "Fangxin", "technician", "Technician",
    "warranty", "Warranty", "plumber", "Red Seal",
    "burst pipe", "sewage", "drain cleaning", "strata",
]


def rename_package(text: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


def flag_domain_words(path: Path, text: str) -> list[str]:
    """Report domain vocabulary a human still needs to rewrite."""
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for word in DOMAIN_WORDS:
            if word in line:
                hits.append(f"{path}:{line_no}: {line.strip()[:90]}")
                break
    return hits


def copy_with_rename(rel: str, out_root: Path, old_pkg: str, new_pkg: str) -> Path | None:
    src = SOURCE_ROOT / rel.format(pkg=old_pkg)
    if not src.exists():
        return None
    dst = out_root / rel.format(pkg=new_pkg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix in (".py", ".html", ".ini", ".example", ".md", ".yaml") or src.name == ".gitignore":
        dst.write_text(rename_package(src.read_text(encoding="utf-8"), old_pkg, new_pkg),
                       encoding="utf-8")
    else:
        shutil.copy2(src, dst)
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new business workflow agent")
    parser.add_argument("--name", required=True, help='Business name, e.g. "Northshore Dental"')
    parser.add_argument("--package", required=True, help="Python package name, e.g. dental")
    parser.add_argument("--out", required=True, help="Destination directory")
    parser.add_argument(
        "--delivery",
        choices=["on_site", "in_store", "both"],
        default="on_site",
        help="How service is delivered: we travel, they come in, or both",
    )
    parser.add_argument("--source-package", default="plumbing", help="Package name to rename from")
    parser.add_argument("--force", action="store_true", help="Overwrite a non-empty destination")
    args = parser.parse_args()

    out_root = Path(args.out).expanduser().resolve()
    if out_root.exists() and any(out_root.iterdir()) and not args.force:
        print(f"[x] {out_root} exists and is not empty. Use --force to overwrite.")
        return 1
    out_root.mkdir(parents=True, exist_ok=True)

    old_pkg, new_pkg = args.source_package, args.package
    review: list[str] = []

    print(f"[i] {args.name} -> {out_root}")
    print(f"[i] package {old_pkg} -> {new_pkg}, delivery mode: {args.delivery}\n")

    print("=== engine (copied as-is) ===")
    for rel in ENGINE_FILES:
        dst = copy_with_rename(rel, out_root, old_pkg, new_pkg)
        if dst:
            print(f"  {rel.format(pkg=new_pkg)}")
            review.extend(flag_domain_words(dst.relative_to(out_root), dst.read_text(encoding="utf-8")))

    print("\n=== service kit (structure kept, vocabulary needs review) ===")
    for rel in KIT_FILES:
        dst = copy_with_rename(rel, out_root, old_pkg, new_pkg)
        if dst:
            hits = flag_domain_words(dst.relative_to(out_root), dst.read_text(encoding="utf-8"))
            print(f"  {rel.format(pkg=new_pkg)}  ({len(hits)} lines to review)")
            review.extend(hits)

    print("\n=== domain templates (fill these in) ===")
    for tmpl in sorted(TEMPLATE_ROOT.rglob("*")):
        if not tmpl.is_file():
            continue
        rel = tmpl.relative_to(TEMPLATE_ROOT)
        dst = out_root / str(rel).replace(".tmpl", "")
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = (
            tmpl.read_text(encoding="utf-8")
            .replace("{{BUSINESS_NAME}}", args.name)
            .replace("{{PACKAGE}}", new_pkg)
            .replace("{{DELIVERY}}", args.delivery)
        )
        dst.write_text(text, encoding="utf-8")
        print(f"  {dst.relative_to(out_root)}")

    (out_root / "docs").mkdir(parents=True, exist_ok=True)
    carried = []
    for extra in ("PLAYBOOK.md", "CHECKLIST.md", "ARCHITECTURE.md"):
        src = TEMPLATE_ROOT.parent / extra
        if src.exists():
            shutil.copy2(src, out_root / "docs" / extra)
            carried.append(f"docs/{extra}")
    if carried:
        print("  " + ", ".join(carried))

    for keep in ("runs", "prompt_history"):
        (out_root / keep).mkdir(exist_ok=True)
        (out_root / keep / ".gitkeep").touch()

    report = out_root / "TODO_DOMAIN_REVIEW.md"
    report.write_text(
        "# Lines still carrying the previous domain's vocabulary\n\n"
        "The engine is domain-free in logic, but comments and examples are not. Left as they\n"
        "are, someone reading this codebase is quietly misled about what it does.\n\n"
        f"{len(review)} lines to review:\n\n```\n" + "\n".join(review) + "\n```\n\n"
        "Work through `docs/CHECKLIST.md` first — most of these become obvious once the\n"
        "domain is actually defined.\n",
        encoding="utf-8",
    )

    print(f"\n[ok] scaffolded into {out_root}")
    print(f"[!] {len(review)} lines still mention the previous domain — see TODO_DOMAIN_REVIEW.md")
    print("\nNext:")
    print("  1. Work through docs/CHECKLIST.md, starting with config/business_rules.yaml")
    print("  2. cp .env.example .env  and fill in your key")
    print("  3. python3 -m pytest -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
