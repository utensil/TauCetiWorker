#!/usr/bin/env python3
"""The Lake artifact pool: opt-in, partitioned by toolchain, and never a shared writable store.

`LAKE_CACHE_DIR` stays per-worker because Lake writes it throughout a build, so unlike the elan
toolchain directory it cannot be shared outright. It is instead pooled the way Mathlib's `.ltar`
cache is — a hardlink farm that only ever gains COMPLETE files — but with two extra constraints the
`.ltar` pool does not need:

  * the pool is an explicit opt-in (`TAUCETI_LAKE_POOL`), since there is no operator directory to
    adopt and a writable store must not be pooled by accident;
  * it is partitioned by the recorded canonical-main toolchain generation, because
    `clean_lake_cache_after_toolchain_bump` deliberately clears this worker's owned store on a bump,
    and a flat pool would hand the dropped generation straight back.

And with a narrower notion of "complete": only the content-addressed `artifacts/` subtree is
poolable, and only files Lake has sealed read-only. `outputs/`/`revisions/` are indexes Lake rewrites
in place — sharing an inode with a mutable file is how a link corrupts every holder at once.

These assertions pin the switch, the partitioning, the exchange itself, and the two ways a file is
refused: writable (not proven sealed, even if a bulk download completed) and outside `artifacts/`
(mutable).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import hashlib
import json
import stat
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import build_caches, checkout_recovery

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
            symlink = root / "artifacts" / "linked.olean"
            symlink.symlink_to(sealed)
            check("a read-only artifact counts as sealed", build_caches.lake_sealed(sealed))
            check("a writable artifact does not", not build_caches.lake_sealed(unsealed))
            check("a symlink does not", not build_caches.lake_sealed(symlink))
            check("a directory does not", not build_caches.lake_sealed(root / "artifacts"))

            # --- the exchange ----------------------------------------------------------------------
            pool = Path(tmp) / "pool"
            config = types.SimpleNamespace(
                checkout=Path(tmp) / "checkout", state=Path(tmp) / "state", data_home=Path(tmp) / "home"
            )
            config.checkout.mkdir(parents=True)
            (config.checkout / "lean-toolchain").write_text("leanprover/lean4:old-pr-pin\n")
            canonical_pin = b"leanprover/lean4:v4.35.0-rc3\n"
            generation = hashlib.sha256(canonical_pin).hexdigest()

            env.pop("TAUCETI_LAKE_POOL", None)
            env["LAKE_CACHE_DIR"] = str(root)
            tc.agents.sync_lake_pool(config)
            check("no pool root means the store is left alone", not pool.exists())

            env["TAUCETI_LAKE_POOL"] = str(pool)
            tc.agents.sync_lake_pool(config)
            check("no canonical-main marker means no pool is created", not pool.exists())
            marker = config.state / "cache" / "lake-cache-toolchain.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"sha256": generation, "toolchain": canonical_pin.decode().strip()}))
            messages = []
            original_log = tc.agents.log
            tc.agents.log = lambda message: messages.append(str(message))
            try:
                tc.agents.sync_lake_pool(config)
            finally:
                tc.agents.log = original_log
            partition = pool / generation
            pr_generation = hashlib.sha256((config.checkout / "lean-toolchain").read_bytes()).hexdigest()
            check("the pool is created under the recorded canonical generation", partition.is_dir())
            check("the current PR pin does not select a generation", not (pool / pr_generation).exists())
            check("a sealed artifact is promoted", (partition / "artifacts" / "sealed.olean").exists())
            check("an unsealed artifact is not promoted", not (partition / "artifacts" / "sealing.olean").exists())
            check("a symlink is not promoted", not (partition / "artifacts" / "linked.olean").exists())
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
                "a private cache inside the configured pool root exchanges nothing",
                sorted(p.name for p in partition.iterdir()) == ["artifacts"],
            )

            empty = types.SimpleNamespace(
                checkout=Path(tmp) / "no-pin", state=Path(tmp) / "no-marker-state", data_home=Path(tmp) / "home"
            )
            empty.checkout.mkdir()
            env.pop("LAKE_CACHE_DIR", None)
            empty_pool = Path(tmp) / "no-marker-pool"
            env["TAUCETI_LAKE_POOL"] = str(empty_pool)
            tc.agents.sync_lake_pool(empty)
            check("a state root with no canonical marker is left unpooled", not empty_pool.exists())

        # --- overlapping roots ---------------------------------------------------------------------
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            generation = "a" * 64
            config = types.SimpleNamespace(checkout=base / "checkout", state=base / "state", data_home=base / "home")
            marker = config.state / "cache" / "lake-cache-toolchain.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"sha256": generation, "toolchain": "canonical"}))

            private = store(base / "private")
            nested_pool = private / "pool"
            env["LAKE_CACHE_DIR"] = str(private)
            env["TAUCETI_LAKE_POOL"] = str(nested_pool)
            tc.agents.sync_lake_pool(config)
            check("a pool root below the private cache is rejected before creation", not nested_pool.exists())

            real_private = store(base / "real-private")
            private_alias = base / "private-alias"
            private_alias.symlink_to(real_private, target_is_directory=True)
            aliased_nested_pool = real_private / "aliased-pool"
            env["LAKE_CACHE_DIR"] = str(private_alias)
            env["TAUCETI_LAKE_POOL"] = str(aliased_nested_pool)
            tc.agents.sync_lake_pool(config)
            check("resolved private aliases cannot hide a nested pool", not aliased_nested_pool.exists())

            real_pool = base / "real-pool"
            real_pool.mkdir()
            pool_alias = base / "pool-alias"
            pool_alias.symlink_to(real_pool, target_is_directory=True)
            nested_private = store(real_pool / "worker-cache")
            env["LAKE_CACHE_DIR"] = str(nested_private)
            env["TAUCETI_LAKE_POOL"] = str(pool_alias)
            tc.agents.sync_lake_pool(config)
            check("resolved pool aliases cannot hide a nested private cache", not (real_pool / generation).exists())

            # Path.resolve() preserves spelling case. Model the case-insensitive filesystem identity
            # of an existing private root while keeping the test portable to case-sensitive hosts.
            identity_pool = base / "REAL-PRIVATE" / "identity-pool"
            original_samefile = tc.agents.os.path.samefile

            def case_insensitive_samefile(left, right):
                pair = {Path(left), Path(right)}
                if pair == {real_private.resolve(), identity_pool.parent}:
                    return True
                return original_samefile(left, right)

            env["LAKE_CACHE_DIR"] = str(real_private)
            env["TAUCETI_LAKE_POOL"] = str(identity_pool)
            with (
                patch.object(tc.agents.sys, "platform", "linux"),
                patch.object(tc.agents.os.path, "samefile", side_effect=case_insensitive_samefile),
            ):
                tc.agents.sync_lake_pool(config)
            check("filesystem-identical root ancestors cannot hide overlap", not identity_pool.exists())

            # Neither path exists, so there is no identity to compare and no symlink to resolve.
            # Darwin conservatively rejects a case-varied lexical nesting before creating either root.
            cold_private = base / "Cold-Cache"
            cold_pool = base / "cold-cache" / "pool"
            env["LAKE_CACHE_DIR"] = str(cold_private)
            env["TAUCETI_LAKE_POOL"] = str(cold_pool)
            with patch.object(tc.agents.sys, "platform", "darwin"):
                tc.agents.sync_lake_pool(config)
            check(
                "case-varied cold Darwin roots are rejected before creation",
                not cold_private.exists() and not cold_pool.exists(),
            )

        # --- fresh checkout, canonical bump, then exchange -----------------------------------------
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            checkout = base / "checkout"
            (checkout / ".git").mkdir(parents=True)
            toolchain = checkout / "lean-toolchain"
            toolchain.write_text("leanprover/lean4:prior-pr-pin\n")
            config = types.SimpleNamespace(checkout=checkout, state=base / "state", data_home=base / "home")
            private = store(config.data_home / ".cache" / "lake")
            pool = base / "pool"
            env.pop("LAKE_CACHE_DIR", None)
            env["TAUCETI_LAKE_POOL"] = str(pool)

            canonical_pins = [b"leanprover/lean4:canonical-one\n"]

            def git_run(argv, *args, **kwargs):
                if "checkout" in argv:
                    toolchain.write_bytes(canonical_pins[0])
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(tc.agents, "sync_mathlib_pool"),
                patch.object(tc.agents.subprocess, "run", side_effect=git_run),
                patch.object(checkout_recovery, "preserve_before_checkout"),
            ):
                check("fresh host preparation succeeds", tc.agents.prepare_checkout(config))
                first_generation = hashlib.sha256(canonical_pins[0]).hexdigest()
                first_partition = pool / first_generation
                check("fresh preparation records canonical main before pooling", first_partition.is_dir())
                check("fresh preparation preserves and promotes the established cache", private.exists())
                check(
                    "fresh preparation promotes sealed artifacts", (first_partition / "artifacts/sealed.olean").exists()
                )

                old_only = private / "artifacts/old-only.olean"
                old_only.write_bytes(b"old generation")
                old_only.chmod(0o444)
                canonical_pins[0] = b"leanprover/lean4:canonical-two\n"
                second_generation = hashlib.sha256(canonical_pins[0]).hexdigest()
                second_artifacts = pool / second_generation / "artifacts"
                second_artifacts.mkdir(parents=True)
                new_only = second_artifacts / "new-only.olean"
                new_only.write_bytes(b"new generation")
                new_only.chmod(0o444)

                check("host preparation after a canonical bump succeeds", tc.agents.prepare_checkout(config))
                check(
                    "the bumped private store is hydrated from the new generation",
                    (private / "artifacts/new-only.olean").exists(),
                )
                check(
                    "old private artifacts are not promoted into the new generation",
                    not (second_artifacts / "old-only.olean").exists(),
                )
                check("the old generation is retained rather than implicitly pruned", first_partition.exists())
                recorded = json.loads((config.state / "cache/lake-cache-toolchain.json").read_text())
                check("the bump marker advances before synchronization", recorded.get("sha256") == second_generation)

                canonical_pins[0] = b"leanprover/lean4:canonical-three\n"
                with (
                    patch.object(tc.agents, "_write_json_atomic", side_effect=OSError("marker unavailable")),
                    patch.object(tc.agents, "sync_lake_pool") as sync_after_failure,
                ):
                    check("opt-in checkout refuses a marker-write failure", not tc.agents.prepare_checkout(config))
                check("a stale marker never reaches pooling", not sync_after_failure.called)
                recorded = json.loads((config.state / "cache/lake-cache-toolchain.json").read_text())
                check("a failed marker update remains retryable", recorded.get("sha256") == second_generation)

                env.pop("TAUCETI_LAKE_POOL")
                with patch.object(tc.agents, "clean_lake_cache_after_toolchain_bump", return_value=False):
                    check("non-opt-in checkout behavior is unchanged", tc.agents.prepare_checkout(config))
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
