#!/usr/bin/env python3
"""Exercise concurrent publications, claim binding, body races and retained receipts."""

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tauceti_worker import agents

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    fake = root / "bin"
    fake.mkdir()
    for name, source in {
        "git": "print('abc123')",
        "claim": "import os,sys; sys.exit(0 if sys.argv[2] == os.environ['HELD'] else 1)",
        "gh": """import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2] == ['pr','create']:
    Path(os.environ['ORIGINAL']).write_text('overwritten by another round')
    body=Path(args[args.index('--body-file')+1]).read_text()
    Path(os.environ['RESULT']).write_text(body)
    print('https://github.com/org/repo/pull/42')
else:
    print(json.dumps({'body':Path(os.environ['RESULT']).read_text(), 'headRefOid':os.environ.get('REMOTE_HEAD','abc123')}))
""",
    }.items():
        p = fake / name
        p.write_text(f"#!{sys.executable}\n" + source + "\n")
        p.chmod(0o755)

    def publish(slug, wrong=False, bad_head=False, mismatched_key=False):
        directory = root / slug
        directory.mkdir()
        body = (
            "This PR adds "
            + slug
            + ".\n<!--tauceti-target:v1 "
            + json.dumps({"focus": "Topology", "id": slug})
            + "-->\n"
        )
        original = directory / "body.md"
        original.write_text(body)
        receipt = directory / "receipt"
        result = directory / "result"
        key = "author/Topology/" + slug
        env = {
            **os.environ,
            "PATH": str(fake) + os.pathsep + os.environ["PATH"],
            "TAUCETI_REQUIRE_TARGET_MARKER": "1",
            "TAUCETI_AUTHOR_CLAIM_KEY": key if not mismatched_key else "author/Topology/other",
            "TAUCETI_CLAIM_SH": str(fake / "claim"),
            "HELD": key if not wrong else "author/Topology/other",
            "ORIGINAL": str(original),
            "RESULT": str(result),
            "TAUCETI_PR_RECEIPT_FILE": str(receipt),
            "REMOTE_HEAD": "wrong" if bad_head else "abc123",
            "TAUCETI_RESPECT_CLAIMS": "true",
        }
        env.pop("TAUCETI_TARGET_MARKER", None)
        proc = subprocess.run(
            [str(ROOT / "scripts/gh-safe-pr-create"), "--body-file", str(original), "--head", slug],
            env=env,
            capture_output=True,
            text=True,
        )
        if wrong or mismatched_key:
            assert proc.returncode != 0 and not result.exists(), proc
        else:
            assert proc.returncode == int(bad_head), proc.stderr
            assert result.read_text() == body
            assert original.read_text() != body
            assert receipt.read_text() == "42\n"
            audit = json.loads(Path(str(receipt) + ".jsonl").read_text())
            assert audit["verified"] == (not bad_head)
            assert audit["claim"] == key
        return proc.returncode

    with concurrent.futures.ThreadPoolExecutor() as pool:
        assert list(pool.map(publish, ["one", "two"])) == [0, 0]
    publish("foreign", wrong=True)
    publish("wrong-body", mismatched_key=True)
    publish("mismatch", bad_head=True)
    launches = []
    with patch.object(agents, "run_agent_proc", side_effect=lambda argv, **kw: launches.append((argv, kw)) or 0):
        for _ in range(2):
            agents.run_agent_host(root, "Task", "codex", root / "logs")
    a, b = [x[1]["env"]["TMPDIR"] for x in launches]
    assert a != b and Path(a).is_dir() and Path(b).is_dir()
    assert a in launches[0][0][-1] and b in launches[1][0][-1]
print("publication isolation: concurrent bodies, mutation, claim refusal, readback and scratch passed")
