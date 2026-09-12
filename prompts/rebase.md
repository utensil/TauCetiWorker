You are resolving merge conflicts on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The PR has become un-mergeable: its branch conflicts with the current `main`. Bring it up to date with `main` and resolve the conflicts so it can merge again. Work autonomously to completion.

## Rebase onto current main
- Quote every complete URL passed to `gh api` (for example `gh api 'repos/OWNER/REPO/git/trees/main?recursive=1'`); zsh expands an unquoted `?` before `gh` receives it.
- Fetch and integrate the latest `main`:
  ```
  git fetch origin
  git merge origin/main      # (or: git rebase origin/main — either is fine; merge is simpler to resolve)
  ```
- Resolve every conflict on its merits:
  - **`TauCeti.lean` (the root module)** is intentionally empty — the lakefile's glob builds every module without it, and PRs do not edit it, so it should NOT appear among your conflicts. If it somehow does, do not hand-merge: take `main`'s version rather than reconstructing anything by hand.
  - **A source file under `TauCeti/`**: resolve so both the upstream change and your PR's intent are preserved. If `main` now provides something your PR duplicated, prefer the upstream version and drop the duplicate.
- Do NOT discard upstream work to "win" a conflict, and do NOT weaken or delete your PR's real content to dodge one. If a conflict is genuinely irreconcilable (your PR's target no longer makes sense because `main` subsumed it), stop and say so in your report rather than forcing a merge.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT hand-edit the root `TauCeti.lean` — it is auto-managed (see above). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` for project-specific declarations. When extending an existing Mathlib type, place its operations and associated API in that type's existing namespace (for example, root `ContMDiffMap`) so receiver dot notation works. Do not nest that namespace under `TauCeti` or add compatibility aliases solely to keep the old namespace. Preserve valid type-namespace placement during CI fixes, rebases, and toolchain bumps.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If your work needs one, say so in your report and stop.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter.

## Verify before pushing (all three MUST pass, after the merge/rebase)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red — a botched conflict resolution that builds red is worse than the conflict.

Run each command synchronously in a single foreground shell invocation with a generous timeout. Do
not use the interactive `write_stdin`/session-polling tool for a long-running cache or build command:
its transient process handle can disappear before the result is returned. If a tool call yields a
session id anyway, rerun the command with a longer foreground timeout rather than polling that id.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). If publication is blocked, preserve the local candidate and report the exact blocker.

## Submit
If an operator configured a pre-push check, the safe wrapper runs it synchronously against the committed candidate. Wait for its terminal result; do not launch a duplicate validation or bypass a failed check. A failed check or push preserves local work. Diagnose the exact error: permission and transport failures do not establish a concurrent branch update.

- Commit the merge/resolution (if `git merge` left a merge commit, keep its default message; otherwise use an informative conventional subject (`<type>: <subject>`) and a substantive body). Use real line breaks; do not add an AI co-author trailer or literal `\\n` escapes.
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  It compare-and-swaps the PR branch against the head you started from (so it works whether you merged or rebased, and never clobbers a concurrent push). Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, publication is blocked — STOP and say so in your report; do not work around it.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which files conflicted, how you resolved each, and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
