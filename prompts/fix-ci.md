You are fixing FAILING CI on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The `build` check is red. Work autonomously to completion: make CI green without weakening the PR.

## Check that a contribution remains
Before running builds or repairing CI, refresh `origin/main` and read the complete
PR diff (`git diff origin/main...HEAD -- TauCeti/`), not just the latest commit.
If the diff is empty, whitespace-only, or no longer contains a coherent contribution
to the PR's target after review-driven removal or upstream changes, stop and report
that disposition for human attention. Do not create a CI-retrigger commit, push an
empty shell, or invent replacement scope. A failing check alone is not a reason to continue.

## Find out what's actually failing
- Quote every complete URL passed to `gh api` (for example `gh api 'repos/OWNER/REPO/git/trees/main?recursive=1'`); zsh expands an unquoted `?` before `gh` receives it.
- See which checks failed and read their logs:
  - `gh pr checks __PR__ --repo TauCetiProject/TauCeti`
  - `gh run view <run-id> --repo TauCetiProject/TauCeti --log-failed` (use the run id from the failing check)
- Match the failed run to its commit and compare the prior repair commits/checks when the same failure recurs. State why the previous change failed and what will differ this time; do not repeat an ineffective patch or re-trigger a deterministic failure.
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
  # On macOS, install GNU sed once if needed: `brew install gnu-sed`.
  if [ "$(uname -s)" = Darwin ]; then export PATH="$(brew --prefix bash)/bin:$(brew --prefix gnu-sed)/libexec/gnubin:$PATH"; fi
  bash scripts/lint-env.sh
  bash scripts/lint-style.sh
  ```
  If `lint-env` flags a declaration that is NOT in your diff, your branch is likely behind main (CI
  overlays your `TauCeti/` onto current main): merge `main` into the branch and re-check.

## Fix it on its merits
- Diagnose the real cause (a broken proof, a renamed/missing Mathlib lemma, a linter error, an axiom-audit failure, a flaky/transient infra error). Fix the underlying problem.
- If a review-requested `@[simp]` attribute causes `simpNF`, test the theorem with all candidate simp rules active. Preserve the useful theorem and remove or change the offending attribute as justified by the linter. Read the relevant review thread and report the exact lint evidence and why the requested attribute cannot stand, so the next review can adjudicate the request instead of restoring the same failing attribute. Do not weaken the theorem or suppress the linter.
- If the shim-expiry command fails, its annotations name exact Mathlib replacements and affected sources. Migrate only the superseded declarations/imports, preserve or re-home source-only API, and update `TauCeti/mathlib-shims.json` in the same source-only change. The checker derives each inherited source's declaration surface from the PR merge base and ratchets its probes until that surface is migrated, deleted, or re-homed under an entry preserving those probes, so never make the check green by merely deleting probes or changing an exact target to a speculative/landing sentinel.
- Only if a coherent target contribution remains, the failure is genuinely transient/infra (e.g. cache fetch timeout), the code and shim-expiry command are green locally, and the failed logs contain no actionable migration, do NOT hack the code — push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so in your report.
- Prefer the smallest correct fix. If a declaration is unsalvageable, it is better to remove it than to leave the PR red — but never gut the PR into vacuity; if almost nothing survives, stop and report that rather than pushing an empty shell.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` for project-specific declarations. When extending an existing Mathlib type, place its operations and associated API in that type's existing namespace (for example, root `ContMDiffMap`) so receiver dot notation works. Do not nest that namespace under `TauCeti` or add compatibility aliases solely to keep the old namespace. Preserve valid type-namespace placement during CI fixes, rebases, and toolchain bumps.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force the build green — that defeats the point.

## Verify before pushing (ALL of these MUST pass — they are exactly what the `build` check runs)
Use the supported local PR lint invocations below when selecting lint scope or handling macOS
argument limits; every required check must still finish successfully before publication.
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
# On macOS, install GNU sed once if needed: `brew install gnu-sed`.
if [ "$(uname -s)" = Darwin ]; then export PATH="$(brew --prefix bash)/bin:$(brew --prefix gnu-sed)/libexec/gnubin:$PATH"; fi
bash scripts/lint-env.sh
bash scripts/lint-style.sh
```
Iterate until every one is green. A green `lake build` alone is NOT enough — the `build` check also
fails on an axiom-audit, module-system, or lint-env violation (e.g. a missing docstring). Never push red.

Launch each verification command once in the foreground, with a generous timeout. If the tool
yields a session id, poll that same session until it finishes; do not launch a duplicate build or
audit. If the session handle is lost, inspect the process and its result before proceeding, and do
not restart the command until the prior process is known to have ended. Require a successful
terminal result for every required check before publishing. A local candidate commit for
exact lint-scope calculation is allowed under the instructions below.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Unless a stop condition above applies, do not yield, stop, or end your turn until you have committed and pushed (below). If publication is blocked, preserve the local candidate and report the exact blocker.

A lint driver error is a failed check, not a pass. On macOS use GNU Bash and GNU sed for these scripts. Require terminal exit zero from every check; if a tool returns a session id, poll that same invocation until it ends.

### Local PR lint scope and macOS argument limits

Use the repository's actual lint policy, not a blanket exemption for unrelated failures.
For an existing ordinary PR, inspect its current labels and branch before linting and again
before publishing. If it is labelled `full-lint`, is a `lint-repair/*` branch, or the policy
cannot be verified, run full environment lint with `LINT_ONLY_MODULES` unset. Otherwise the
repository supports changed-module environment lint. Read its `scripts/lint-scope.sh` and
`scripts/lint_scope_files.py`; use that same local file-discovery helper against the complete
candidate and its merge base with the current base branch. An unpublished candidate cannot
be compared through GitHub's compare API: use exact local Git objects. A local candidate
commit before validation is permitted; it must include all intended source edits and new
files, must remain unpublished until verification passes, and must be revalidated if changed.

Write the helper's NUL-delimited changed-file output to the round scratch directory, convert
existing canonical `TauCeti/.../*.lean` paths to module names exactly as `lint-scope.sh` does,
and verify that the list covers the complete proposed PR diff, not just this repair's last
commit. Run `LINT_ONLY_MODULES=<absolute-module-list> bash scripts/lint-env.sh`. Do not hand-pick
only passing declarations, change the baseline/allowlist, suppress a linter, or relabel a PR
to narrow scope. The existing script still performs its whole-library documentation and
nolint audits. Report the selected scope and terminal result, along with any earlier full-lint
failure. Passing scoped lint does not establish that the whole library passes full lint.

If `lint-style.sh` fails on macOS with `Argument list too long`, that invocation failed.
Complete the SAME checks without dropping files: use the unmodified `source-modules.sh`
discovery function to produce the complete validated file/module lists; run the unmodified
`HeaderStyle.lean` on every file in bounded batches (at most 1000 paths per call, smaller if
needed); require every batch to exit zero. Generate the same temporary `TauCetiLint.All`
import root as `lint-style.sh`, containing `import TauCeti` plus every discovered module,
check its import count, and run `lake exe lint-style TauCetiLint.All TauCeti` with that
root on `LEAN_SRC_PATH`. Run any additional checks present in the current style driver too.
Preserve its package filter and every diagnostic. Require terminal exit zero, record the
complete file count, and remove only your own temporary driver directory. Never edit the
repository's lint scripts or claim the failed one-command invocation passed.

These supported invocation changes do not replace the full build, axiom or module-system
audits. Preserve their successful exact-candidate results; repeat checks when source or
inputs changed, not merely because a later lint driver failed.

## Submit
Repeat the remaining-contribution check above with the proposed repair included
(inspect staged, unstaged, and new files as well as the committed PR diff). If the
repair removes the last coherent target contribution, stop without committing or pushing.

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
