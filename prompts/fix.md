You are addressing AI code review on pull request #__PR__ of TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. Work autonomously to completion.

Quote every complete URL passed to `gh api` (for example `gh api 'repos/OWNER/REPO/git/trees/main?recursive=1'`); zsh expands an unquoted `?` before `gh` receives it.

## Read the review
- Read the current published head, all relevant scoreboard comments, rubric threads, replies, and adjudications (follow pagination when needed):
  - `gh pr view __PR__ --repo TauCetiProject/TauCeti --json headRefOid`
  - `gh pr view __PR__ --repo TauCetiProject/TauCeti --json comments`
  - `gh api "/repos/TauCetiProject/TauCeti/pulls/__PR__/comments?per_page=100"` (the per-rubric review threads; each root carries a `<!--tauceti-rubric:NAME-->` marker, and the finding text + suggested fix). Keep the complete URL quoted; in zsh an unquoted `?` is a filename glob.
- Reconcile review rounds by the head and rubric revision they evaluated and the subsequent decisions/replies. Identify current blockers (⛔ or 🟡), applicable approvals (✅), unresolved earlier findings, and findings explicitly resolved or superseded. An absent or unevaluated rubric on a partial scoreboard is not approval and does not erase an unresolved finding.
- Read the PR commit history alongside the review history. For a recurring finding, inspect the prior attempted fix and the response to it; identify why it failed before choosing another edit. A new head alone is not evidence that the finding was resolved.
- Where review instructions conflict, identify the controlling decision and a coherent repair plan; do not alternate between incompatible suggested patches or invent a resolution that the review history does not support.
- For an add/remove reversal or incompatible requests across rubrics, link both exact threads and quote the conflicting requirements with their evaluated heads. Preserve any prior adjudication and state what new evidence would justify reversing it. If the published decisions cannot both be satisfied, contest that concrete contradiction and await adjudication instead of undoing the earlier repair silently.

## Do not regress what is already green
Preserve the applicable approvals you identified while clearing the unresolved blockers. A change that fixes one blocker but degrades an approved rubric will not converge. So:
- Make the SMALLEST change that clears each blocker; do not refactor or restructure beyond what the finding requires.
- Before pushing, re-read the approved rubrics (scope, reuse, generality, api-design, placement, naming, documentation, proof-quality, …) and confirm your change does not undermine any of them — e.g. don't add a less-general lemma (generality), a duplicate of Mathlib (reuse), an unexposed/ misplaced declaration (placement/api-design), or an undocumented public def (documentation).
- If a proposed fix conflicts with an applicable approval, first look for a coherent alternative within the authorized repair scope. Explain any remaining contradiction with exact review/source evidence; the conflict alone does not establish that the finding is wrong.

## Revise the next action from feedback
- Before editing or replying, briefly state the published head, the unresolved findings and relevant prior attempts/adjudications, and the next concrete action with its acceptance check. After a rejected contest or an attempt that left the same finding unresolved, explain how the next action addresses that feedback; do not repeat the unchanged argument or claim a previous attempt succeeded without evidence.
- Prefer a corrective code change within the authorized scope when the rejection identifies a valid defect. Repeat a contest only with materially new evidence or an explicit changed decision, and directly answer the reason for rejection. If a real scope or review-contract contradiction prevents a valid next action, report the precise decision needed and preserve the candidate. A no-change attempt by itself is not an attempt ceiling or a reason to abandon the target.
- For a repair that changes a mathematical representation or its operations, map its relevant fields to existing Mathlib/library representations before inventing replacements, and check the intended interpretation with a proportionate witness or preservation law capable of exposing the reported defect. Compilation and self-consistent operation laws alone do not establish that interpretation; a routine proof or naming fix does not require a new model or witness framework.
- Check completion against every current requested deliverable. Removing a documentation promise does not satisfy a separate API request to implement the promised theorem. Report such a request as unresolved unless you implement it or obtain an explicit adjudication that supersedes it.

## Decide, per finding, on its merits
For each finding, judge whether it is actually correct:
- **If it is correct**, fix the code. Verify the fix empirically (does it build? does the claimed Mathlib lemma actually exist — `grep`/`#check`? does the suggested `@[simp]` lemma have a variable head, which the linter forbids?). Reviewers are sometimes confidently wrong; do not blindly comply.
- Before adding or restoring `@[simp]`, run the actual environment lint with all candidate rules active. A locally compiling theorem can still violate `simpNF`; a failed lint driver is an incomplete check, never a pass. When a requested attribute fails, keep the useful theorem without that attribute and contest the attribute request with the exact compiler/linter evidence.
- **If it is wrong**, do NOT comply. Reply on that rubric's thread explaining why, with evidence (a synth-check, a Mathlib citation, a build error). Post the reply to the thread root:
  `gh api -X POST "/repos/TauCetiProject/TauCeti/pulls/__PR__/comments/<ROOT_ID>/replies" -f body="..."`
  (A re-review reads these replies, so a well-evidenced contest can clear a wrong finding.) Before making a public claim about code on the PR, re-read the published head and verify the cited declarations/behavior at that exact head; identify any evidence from an unpublished local candidate explicitly.

## Discussion-only rounds
If the next action is only an evidence-backed reply or a report of a review-contract contradiction,
verify the current published head and inspect the relevant source, review history, and rubric
revision. Run a focused probe when needed to support a technical claim. An attribution or policy
dispute does not by itself require a full build, axiom audit, or linter run on unchanged source.
Do not create an empty commit or push just to complete such a round. If citing existing validation,
identify its exact head and source; report it as prior evidence, not a fresh check. Preserve and
identify any unpublished local changes separately. If you make source changes for publication,
the full verification and safe-push requirements below apply.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds every module under `TauCeti/`, so there is no need to touch it (if a reviewer claims your API is not reachable from the root, the glob already covers it). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Use `namespace TauCeti` for project-specific declarations. When extending an existing Mathlib type, place its operations and associated API in that type's existing namespace (for example, root `ContMDiffMap`) so receiver dot notation works. Do not nest that namespace under `TauCeti` or add compatibility aliases solely to keep the old namespace. Preserve valid type-namespace placement during CI fixes, rebases, and toolchain bumps.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If a finding means the PR's target is not on any roadmap, say so in your report and stop.
- Must stay green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force a change through — that is itself a reason to push back on the finding.

## Verify before pushing (all commands MUST pass)
Run this gate when publishing source changes. A discussion-only round follows the evidence checks
above and may finish without running this gate or the Submit section.
```
set -e
if [ "$(uname -s)" = Darwin ]; then export PATH="$(brew --prefix bash)/bin:$(brew --prefix gnu-sed)/libexec/gnubin:$PATH"; fi
lake exe cache get
lake build --iofail
lake exe axioms
lake exe module-system
bash scripts/lint-env.sh
bash scripts/lint-style.sh
```
Iterate until green. Never push red.

Launch each command once as the authoritative foreground invocation, with a generous timeout. If the
tool yields a session id, poll that same session until it finishes; do not start a duplicate command.
If the session handle is lost, inspect the process and its receipt/result before proceeding, and do
not restart the command until the prior process is known to have ended.

**Complete the active round synchronously.** Run these commands in the FOREGROUND and wait for each to finish; do not leave an unobserved background build. Commit and safely publish verified fixes when possible. If interrupted or blocked, preserve the local candidate and report the unfinished step and observed result; do not discard useful work or claim an unverified check or publication succeeded. Pushing updates the PR; local work may remain unpublished and must not be treated as disposable.

A lint driver error is a failed check, not a pass. On macOS use GNU Bash and GNU sed for these scripts. Require terminal exit zero from every check; if a tool returns a session id, poll that same invocation until it ends.

## Submit
Keep the Worker-provided push destination and expected head unchanged; do not override them with `origin`. On failure, inspect Git's actual diagnostic: permission and transport errors are not evidence of a concurrent push.

- Commit the fixes with an informative conventional subject (`<type>: <subject>`, imperative present) and a substantive body. Use real line breaks; do not add an AI co-author trailer or literal `\\n` escapes.
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push"
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, publication is blocked — STOP and say so in your report (the next round re-syncs and retries); do not work around it. A successful push updates the PR; a re-review runs separately.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which findings you fixed (and how you verified each), which you contested
(and the new evidence and response to any prior rejection), remaining findings, and the revised
next action or precise blocker. For source publication, include the exact `lake build` /
`lake exe axioms` results and the other required gate results. For a discussion-only round, state
that no source was published and report only checks actually run and explicitly identified prior
evidence; fresh full-build results are not required. Distinguish the published head from an
unpublished candidate. Do not claim green unless you saw it.
