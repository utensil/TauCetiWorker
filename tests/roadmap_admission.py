#!/usr/bin/env python3
"""Static regression guard for roadmap target admission requirements."""

from pathlib import Path


PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "roadmap.md"


def main() -> int:
    text = PROMPT.read_text()
    required = (
        "Admit the target before authoring.",
        "exact roadmap item and milestone",
        "declarations (or files and named declarations)",
        "one concrete consumer",
        "no identified consumer",
        "duplicate of an existing declaration",
        "Re-check `main` and the open sibling PRs immediately before editing",
    )
    missing = [phrase for phrase in required if phrase not in text]
    if missing:
        print("missing roadmap admission requirement(s): " + ", ".join(missing))
        return 1
    print("roadmap admission requirements present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
