#!/usr/bin/env python3
"""The Lake artifact pool: opt-in, partitioned by toolchain, and never a shared writable store.

`LAKE_CACHE_DIR` stays per-worker because Lake writes it throughout a build, so unlike the elan
toolchain directory it cannot be shared outright. It is instead pooled the way Mathlib's `.ltar`
cache is — a hardlink farm that only ever gains COMPLETE files — but with two extra constraints the
`.ltar` pool does not need:

  * the pool is an explicit opt-in (`TAUCETI_LAKE_POOL`), since there is no operator directory to
    adopt and a writable store must not be pooled by accident;
  * it is partitioned by toolchain generation, because `clean_lake_cache_after_toolchain_bump`
    deliberately clears this worker's owned store on a bump, and a flat pool would hand the dropped
    generation straight back and keep every generation's bytes forever.

And with a narrower notion of "complete": only the content-addressed `artifacts/` subtree is
poolable, and only files Lake has sealed read-only. `outputs/`/`revisions/` are indexes Lake rewrites
in place — sharing an inode with a mutable file is how a link corrupts every holder at once.

These assertions pin the switch, the partitioning, the exchange itself, and the two ways a file is
refused: still writable (incomplete) and outside `artifacts/` (mutable).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import stat
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import build_caches

fails = 0
LOGIN_HOME = Path("/home/pretend-operator")
VARS = ("TAUCETI_LAKE_POOL", "LAKE_CACHE_DIR")


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def store(root: Path, *, writable=False) -> Path:
    """A minimal Lake store: one sealed artifact, one unsealed one, plus the mutable indexes."""
    root.mkdir(parents=True, exist_ok=True)
    artifacts = root / "artifacts"
    artifacts.mkdir()
    for name, body in (("sealed.olean", b"sealed"), ("sealing.olean", b"sealing")):
        f = artifacts / name
        f.write_bytes(body)
        f.chmod(0o666 if (writable and name == "sealing.olean") else 0o444)
    for index in ("outputs", "revisions"):
        d = root / index
        d.mkdir()
        (d / "current.json").write_text("{}")
    (root / "artifacts" / "half.olean.part").write_bytes(b"partial")
    return root


def main():
    env = tc.agents.os.environ
    saved = {k: env.get(k) for k in VARS}
    saved_platform = tc.agents.sys.platform
    orig_host_home = tc.agents._host_home
    tc.agents._host_home = lambda: LOGIN_HOME
    try:
        # --- the switch ----------------------------------------------------------------------------
        check("pooling is off unless a pool root is named", build_caches.lake_pool(LOGIN_HOME, env={}) is None)
        check(
            "an explicit pool root is partitioned by generation",
            build_caches.lake_pool(LOGIN_HOME, "abc", {"TAUCETI_LAKE_POOL": "/pool"}) == Path("/pool/abc"),
        )
        check(
            "two generations never share a pool directory",
            build_caches.lake_pool(LOGIN_HOME, "abc", {"TAUCETI_LAKE_POOL": "/pool"})
            != build_caches.lake_pool(LOGIN_HOME, "def", {"TAUCETI_LAKE_POOL": "/pool"}),
        )

        # --- what may be pooled, and what may not ---------------------------------------------------
        check("the artifacts subtree is poolable", not build_caches.lake_mutable(Path("artifacts/x.olean")))
        check("the rewritten outputs index is not", build_caches.lake_mutable(Path("outputs/x.json")))
        check("the rewritten revisions index is not", build_caches.lake_mutable(Path("revisions/x")))
        check("the store root itself is not", build_caches.lake_mutable(Path("artifacts")))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lake"
            store(root)
            sealed, unsealed = root / "artifacts" / "sealed.olean", root / "artifacts" / "sealing.olean"
            unsealed.chmod(0o644)
            check("a read-only artifact counts as sealed", build_caches.lake_sealed(sealed))
            check("a writable artifact does not", not build_caches.lake_sealed(unsealed))
            check("a directory does not", not build_caches.lake_sealed(root / "artifacts"))

            # --- the exchange ----------------------------------------------------------------------
            pool = Path(tmp) / "pool"
            config = types.SimpleNamespace(
                checkout=Path(tmp) / "checkout", state=Path(tmp) / "state", data_home=Path(tmp) / "home"
            )
            config.checkout.mkdir(parents=True)
            (config.checkout / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc3\n")
            generation, label = tc.agents.toolchain_generation(config)
            check("the generation is the pin's digest", bool(generation) and label == "leanprover/lean4:v4.35.0-rc3")

            env.pop("TAUCETI_LAKE_POOL", None)
            env["LAKE_CACHE_DIR"] = str(root)
            tc.agents.sync_lake_pool(config)
            check("no pool root means the store is left alone", not pool.exists())

            env["TAUCETI_LAKE_POOL"] = str(pool)
            messages = []
            original_log = tc.agents.log
            tc.agents.log = lambda message: messages.append(str(message))
            try:
                tc.agents.sync_lake_pool(config)
            finally:
                tc.agents.log = original_log
            partition = pool / generation
            check("the pool is created under the generation", partition.is_dir())
            check("a sealed artifact is promoted", (partition / "artifacts" / "sealed.olean").exists())
            check("an unsealed artifact is not promoted", not (partition / "artifacts" / "sealing.olean").exists())
            check("the mutable outputs index is never pooled", not (partition / "outputs").exists())
            check("the mutable revisions index is never pooled", not (partition / "revisions").exists())
            check("a half-written artifact is never pooled", not (partition / "artifacts" / "half.olean.part").exists())
            check("the exchange is reported", any("lake artifact pool" in m for m in messages))

            # A name the pool already holds is never redefined, so one worker cannot replace an
            # artifact another worker or the operator is already using.
            (partition / "artifacts" / "shared.olean").write_bytes(b"pool copy")
            (partition / "artifacts" / "shared.olean").chmod(0o444)
            (root / "artifacts" / "shared.olean").write_bytes(b"worker copy")
            (root / "artifacts" / "shared.olean").chmod(0o444)
            tc.agents.sync_lake_pool(config)
            check(
                "an existing pool name keeps the pool's bytes",
                (partition / "artifacts" / "shared.olean").read_bytes() == b"pool copy",
            )

            # Hydration must not loosen the seal: a writable worker copy would let a build regenerate
            # in place through the link and reach the pool (the hazard 4fcc749 detaches around).
            (root / "artifacts" / "sealed.olean").unlink()
            tc.agents.sync_lake_pool(config)
            hydrated = root / "artifacts" / "sealed.olean"
            check("a missing artifact is hydrated back", hydrated.exists())
            check("hydration keeps the pool's read-only seal", not hydrated.stat().st_mode & stat.S_IWUSR)
            check("the hydrated file is a link, not a copy", hydrated.stat().st_nlink > 1)
            check(
                "a repeated sync is a no-op",
                build_caches.sync_pool(root, partition, skip=build_caches.lake_mutable, accept=build_caches.lake_sealed)
                == (0, 0),
            )

            # --- refusals --------------------------------------------------------------------------
            check(
                "a cross-device pair links nothing",
                build_caches.link_into(
                    root, Path("/nonexistent-pool"), skip=build_caches.lake_mutable, accept=build_caches.lake_sealed
                )
                == (0, 0),
            )
            env["LAKE_CACHE_DIR"] = str(partition)
            tc.agents.sync_lake_pool(config)
            check(
                "a worker pointed straight at the pool exchanges nothing",
                sorted(p.name for p in partition.iterdir()) == ["artifacts"],
            )

            empty = types.SimpleNamespace(
                checkout=Path(tmp) / "no-pin", state=Path(tmp) / "state", data_home=Path(tmp) / "home"
            )
            empty.checkout.mkdir()
            env.pop("LAKE_CACHE_DIR", None)
            tc.agents.sync_lake_pool(empty)
            check("a checkout with no readable pin is left unpooled", tc.agents.toolchain_generation(empty) == ("", ""))
    finally:
        tc.agents._host_home = orig_host_home
        tc.agents.sys.platform = saved_platform
        for key, value in saved.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
