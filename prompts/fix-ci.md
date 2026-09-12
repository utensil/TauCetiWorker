You are fixing FAILING CI on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The `build` check is red. Work autonomously to completion: make CI green without weakening the PR.

## Find out what's actually failing
- Quote every complete URL passed to `gh api` (for example `gh api 'repos/OWNER/REPO/git/trees/main?recursive=1'`); zsh expands an unquoted `?` before `gh` receives it.
- See which checks failed and read their logs:
  - `gh pr checks __PR__ --repo TauCetiProject/TauCeti`
  - `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed` (use the run id from the failing check)
- Reproduce locally — this is the source of truth, not the log alone. The single `build` check bundles
  the sandboxed build, the audits, and the lint, so run the WHOLE suite, not just `lake build`:
  ```
  set -e
  lake exe cache get
  git fetch -q origin main
  shim_args=(--fail-on-available); base_shims="$(mktemp)"; base_root="$(mktemp -d)"; have_base=0
  base_ref="$(git merge-base origin/main HEAD)"
  if git show "$base_ref":TauCeti/mathlib-shims.json > "$base_shims" 2>/dev/null; then git archive "$base_ref" TauCeti | tar -x -C "$base_root"; shim_args+=(--base-manifest "$base_shims" --base-root "$base_root"); have_base=1; fi
  if [ "$have_base" = 1 ] && git diff --quiet "$base_ref" -- lake-manifest.json lean-toolchain; then shim_args+=(--only-new); fi
  if [ -f scripts/check-expired-mathlib-shims.py ]; then python3 scripts/check-expired-mathlib-shims.py "${shim_args[@]}"; fi
  rm -f "$base_shims"; rm -rf "$base_root"
  lake build --iofail
  lake exe axioms
  lake exe module-system
  bash scripts/lint-env.sh
  bash scripts/lint-style.sh
  ```
  If `lint-env` flags a declaration that is NOT in your diff, your branch is likely behind main (CI
  overlays your `TauCeti/` onto current main): merge `main` into the branch and re-check.

## Fix it on its merits
- Diagnose the real cause (a broken proof, a renamed/missing Mathlib lemma, a linter error, an axiom-audit failure, a flaky/transient infra error). Fix the underlying problem.
- If the shim-expiry command fails, its annotations name exact Mathlib replacements and affected sources. Migrate only the superseded declarations/imports, preserve or re-home source-only API, and update `TauCeti/mathlib-shims.json` in the same source-only change. The checker derives each inherited source's declaration surface from the PR merge base and ratchets its probes until that surface is migrated, deleted, or re-homed under an entry preserving those probes, so never make the check green by merely deleting probes or changing an exact target to a speculative/landing sentinel.
- If the failure is genuinely transient/infra (e.g. cache fetch timeout), the code and shim-expiry command are green locally, and the failed logs contain no actionable migration, do NOT hack the code — push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so in your report.
- Prefer the smallest correct fix. If a declaration is unsalvageable, it is better to remove it than to leave the PR red — but never gut the PR into vacuity; if almost nothing survives, stop and report that rather than pushing an empty shell.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` for project-specific declarations. When extending an existing Mathlib type, place its operations and associated API in that type's existing namespace (for example, root `ContMDiffMap`) so receiver dot notation works. Do not nest that namespace under `TauCeti` or add compatibility aliases solely to keep the old namespace. Preserve valid type-namespace placement during CI fixes, rebases, and toolchain bumps.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force the build green — that defeats the point.

## Verify before pushing (ALL of these MUST pass — they are exactly what the `build` check runs)
```
set -e
lake exe cache get
git fetch -q origin main
shim_args=(--fail-on-available); base_shims="$(mktemp)"; base_root="$(mktemp -d)"; have_base=0
base_ref="$(git merge-base origin/main HEAD)"
if git show "$base_ref":TauCeti/mathlib-shims.json > "$base_shims" 2>/dev/null; then git archive "$base_ref" TauCeti | tar -x -C "$base_root"; shim_args+=(--base-manifest "$base_shims" --base-root "$base_root"); have_base=1; fi
if [ "$have_base" = 1 ] && git diff --quiet "$base_ref" -- lake-manifest.json lean-toolchain; then shim_args+=(--only-new); fi
if [ -f scripts/check-expired-mathlib-shims.py ]; then python3 scripts/check-expired-mathlib-shims.py "${shim_args[@]}"; fi
rm -f "$base_shims"; rm -rf "$base_root"
lake build --iofail
lake exe axioms
lake exe module-system
bash scripts/lint-env.sh
bash scripts/lint-style.sh
```
Iterate until every one is green. A green `lake build` alone is NOT enough — the `build` check also
fails on an axiom-audit, module-system, or lint-env violation (e.g. a missing docstring). Never push red.

Launch each verification command once in the foreground, with a generous timeout. If the tool
yields a session id, poll that same session until it finishes; do not launch a duplicate build or
audit. If the session handle is lost, inspect the process and its result before proceeding, and do
not restart the command until the prior process is known to have ended. Require a successful
terminal result for every verification command before committing or pushing.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). If publication is blocked, preserve the local candidate and report the exact blocker.

A lint driver error is a failed check, not a pass. On macOS use GNU Bash and GNU sed for these scripts. Require terminal exit zero from every check; if a tool returns a session id, poll that same invocation until it ends.

## Submit
Keep the Worker-provided push destination and expected head unchanged; do not override them with `origin`. On failure, inspect Git's actual diagnostic: permission and transport errors are not evidence of a concurrent push.

- Commit the fix with an informative conventional subject (`<type>: <subject>`, imperative present) and a substantive body. Use real line breaks; do not add an AI co-author trailer or literal `\\n` escapes.
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, publication is blocked — STOP and say so in your report (the next round re-syncs); do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: what was failing, the root cause, what you changed (or that you only re-triggered transient CI), and the exact shim-expiry / `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
