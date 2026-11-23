#!/usr/bin/env python3
"""Conservative safety lint for user-facing shell snippets in Markdown files.

It is a *gate*, not a shell interpreter or proof of safety. It intentionally
flags some borderline constructs for manual review. It never executes code.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class Finding:
    filename: str
    lineNumber: int
    rule: str
    text: str


# Disallow changes to the calling shell's error handling, or termination.
# The guarded Markdown may be pasted directly into an interactive terminal.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shell-options", re.compile(r"\bset\s+(?:[-+][a-zA-Z]|\+o\s|--\s|-[a-zA-Z]*\b|\-o\s)", re.I)),
    ("shell-exit", re.compile(r"(?:^|[;|&({]\s*|\bthen\s+|\bdo\s+)exit(?:\s|;|$)", re.I)),
    ("shell-exec", re.compile(r"(?:^|[;|&({]\s*|\bthen\s+|\bdo\s+)exec(?:\s|;|$)", re.I)),
    ("shell-trap", re.compile(r"(?:^|[;|&({]\s*|\bthen\s+|\bdo\s+)trap(?:\s|;|$)", re.I)),
    ("source-current-shell", re.compile(r"(?:^|[;|&({]\s*|\bthen\s+|\bdo\s+)(?:source\s+|\.\s+\S)", re.I)),
    ("shell-with-errexit", re.compile(r"\b(?:bash|sh|zsh)\s+-[a-zA-Z]*e(?:[a-zA-Z]*\b|\s)", re.I)),
    ("recursive-delete", re.compile(r"\brm\s+(?:(?:-[a-zA-Z]*[rf][a-zA-Z]*\s+)+|--recursive\b|--force\b)", re.I)),
    ("broad-process-kill", re.compile(r"\b(?:killall\s+|pkill\s+-(?:[a-zA-Z]*f|[a-zA-Z]*x[a-zA-Z]*f)\b)", re.I)),
    ("force-git-discard", re.compile(r"\bgit\s+(?:reset\s+--hard\b|clean\s+-[a-zA-Z]*f\b|checkout\s+--\s+\.)", re.I)),
    ("pipe-to-shell", re.compile(r"\|\s*(?:(?:sudo\s+)?(?:bash|sh|zsh))\b", re.I)),
)


def scanMarkdown(markdown: str, filename: str = "<input>") -> list[Finding]:
    findings: list[Finding] = []
    active = False
    fenceCharacter = ""
    fenceLength = 0
    for lineNumber, rawLine in enumerate(markdown.splitlines(), 1):
        fence = re.match(r"^\s*(`{3,}|~{3,})([^`]*)$", rawLine)
        if fence:
            marker, languageText = fence.groups()
            if active and marker[0] == fenceCharacter and len(marker) >= fenceLength:
                active = False
                fenceCharacter = ""
                continue
            if not active:
                language = languageText.strip().split(maxsplit=1)[0].lower() if languageText.strip() else ""
                active = language in {"bash", "sh", "shell", "zsh", "console", "terminal"}
                fenceCharacter = marker[0]
                fenceLength = len(marker)
                continue
        if not active:
            continue
        command = re.sub(r"^\s*(?:\$\s+|[^\s@]+@[^\s$]+\$\s+)", "", rawLine).strip()
        if not command or command.startswith("#"):
            continue
        for ruleName, pattern in RULES:
            if pattern.search(command):
                findings.append(Finding(filename, lineNumber, ruleName, rawLine.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint shell snippets for interactive-terminal safety; never executes commands")
    parser.add_argument("files", nargs="+", type=Path, help="Markdown files with user-facing shell command blocks")
    arguments = parser.parse_args(argv)
    allFindings: list[Finding] = []
    for path in arguments.files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"ERROR: cannot read {path}: {error}", file=sys.stderr)
            return 2
        allFindings.extend(scanMarkdown(text, str(path)))
    for finding in allFindings:
        print(f"{finding.filename}:{finding.lineNumber}: {finding.rule}: {finding.text}")
    if allFindings:
        print(f"BLOCKED: {len(allFindings)} potentially unsafe shell construct(s). Review and remove before delivery.")
        return 1
    print("PASS: no listed shell hazards in fenced shell snippets (manual safety review still required).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
