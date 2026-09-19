#!/usr/bin/env python3
"""Publish one frozen PR body and retain an ownership receipt even if readback fails."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout


def freeze_args(args, frozen):
    forwarded, bodies = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, sep, value = arg.partition("=")
        if flag in ("--body", "-b", "--body-file", "-F"):
            if not sep:
                i += 1
                if i >= len(args):
                    raise ValueError(f"missing value for {flag}")
                value = args[i]
            if flag in ("--body-file", "-F"):
                body = sys.stdin.buffer.read() if value == "-" else Path(value).read_bytes()
            else:
                body = value.encode()
            bodies.append(body)
        elif arg.startswith(("-b", "-F")) and len(arg) > 2:
            raise ValueError("use separate arguments for -b and -F")
        elif flag in (
            "--fill",
            "--fill-first",
            "--fill-verbose",
            "--template",
            "-T",
            "--editor",
            "-e",
            "--web",
            "-w",
            "--recover",
        ):
            raise ValueError(f"{flag} can replace the verified body; provide --body-file")
        else:
            forwarded.append(arg)
        i += 1
    if len(bodies) != 1:
        raise ValueError("provide exactly one --body or --body-file")
    body = bodies[0].decode("utf-8")
    frozen.write_bytes(bodies[0])
    frozen.chmod(0o400)
    return [*forwarded, "--body-file", str(frozen)], body


def claim_for(body):
    marker = os.environ.get("TAUCETI_TARGET_MARKER", "")
    required = os.environ.get("TAUCETI_REQUIRE_TARGET_MARKER") == "1"
    key = os.environ.get("TAUCETI_AUTHOR_CLAIM_KEY", "")
    matches = re.findall(r"<!--tauceti-target:v1\s+(\{[^\n]*?\})-->", body)
    if marker and marker not in body:
        raise ValueError("body is missing the required exact target marker")
    if required or marker or key or matches:
        if len(matches) != 1:
            raise ValueError("body must contain exactly one target marker")
        target = json.loads(matches[0])
        if not isinstance(target, dict) or any(
            not isinstance(target.get(k), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", target[k])
            for k in ("focus", "id")
        ):
            raise ValueError("target marker must name one roadmap and target slug")
        expected = f"author/{target['focus']}/{target['id']}"
        if key != expected:
            raise ValueError("body target does not match TAUCETI_AUTHOR_CLAIM_KEY")
        # Explicit ignore-claims remains an operator override; target agreement is still required.
        if os.environ.get("TAUCETI_RESPECT_CLAIMS", "true").lower().strip() not in ("0", "false", "no", "off"):
            claim = os.environ.get("TAUCETI_CLAIM_SH", str(Path(__file__).with_name("claim.sh")))
            run(claim, "holds", key)
    return key


def main():
    with tempfile.TemporaryDirectory(prefix="tauceti-publication-") as tmp:
        args, body = freeze_args(sys.argv[1:], Path(tmp) / "body.md")
        key = claim_for(body)
        head = run("git", "rev-parse", "HEAD").strip()
        output = run("gh", "pr", "create", *args)
        print(output, end="", flush=True)
        urls = re.findall(r"https://github\.com/[^\s/]+/[^\s/]+/pull/[1-9][0-9]*", output)
        if len(urls) != 1:
            raise ValueError("creation returned no unique PR URL; inspect GitHub before retrying")
        url = urls[0]
        receipt = os.environ.get("TAUCETI_PR_RECEIPT_FILE")
        # Record ownership BEFORE checking the result: failed verification must not orphan a PR.
        if receipt:
            with Path(receipt).open("a") as out:
                out.write(url.rsplit("/", 1)[1] + "\n")
        published = json.loads(run("gh", "pr", "view", url, "--json", "body,headRefOid"))
        verified = published.get("body") == body and published.get("headRefOid") == head
        if receipt:
            with Path(receipt + ".jsonl").open("a") as out:
                out.write(
                    json.dumps(
                        {
                            "url": url,
                            "head": head,
                            "claim": key,
                            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                            "verified": verified,
                        }
                    )
                    + "\n"
                )
        if not verified:
            raise ValueError(
                "created PR body/head differs from the frozen candidate; ownership recorded, inspect before retrying"
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"gh-safe-pr-create: {exc}", file=sys.stderr)
        sys.exit(1)
