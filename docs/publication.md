# Publication checks and repair budgets

Host authoring binds the worker-selected destination, branch, expected remote
head, and optional `TAUCETI_PRE_PUSH_CHECK` executable in the selected worktree's
private Git directory before launching the agent. A mistaken environment override
such as `TAUCETI_PUSH_REMOTE=origin` cannot redirect that bound publication.

Set `TAUCETI_PRE_PUSH_CHECK` to an absolute executable path for a deployment that
requires project-specific validation. The command runs synchronously from the
candidate checkout after the candidate is committed. It must return zero only
after all required checks terminate successfully. Missing executables, nonzero
exits, a dirty candidate, or a changed HEAD block publication. The wrapper checks
the lease before validation and again before pushing the immutable checked commit.
The worker does not embed project validation commands or platform-specific tools.
The configured host check cannot silently fall back to Bubble, where it is not
mounted; Bubble authoring without a host check retains its existing environment
contract and sends successful push receipts through a dedicated writable inbox.

This guards cooperating agents against mistakes; it is not an adversarial sandbox
against an agent deliberately editing its private policy or bypassing sanctioned
wrappers. The check is not a replacement for independent remote CI and review.

The safe wrapper records the exact commit only after a successful push. The
round's GitHub readback must match that receipt to count as publication. Comment
growth, another worker's push, or an unavailable query cannot establish success.
An unadjudicated contest remains a pending review decision; it is not credited as
an accepted finding. Failed readback backs off and retains the local candidate.

Review repairs normally have three attempts per head and five across the PR's
heads. The PR total is derived from existing per-head counter files, so upgrading
does not grant new credits and moving the head does not reset the budget.
Publication does not refund attempts or establish review acceptance. The explicit
owned retry override remains available for operator-controlled recovery; leaving
it enabled removes the ceilings. CI repair retains its existing three-per-head,
five-per-PR limits. Exhaustion preserves the candidate and requests human attention.

Repair prompts require concrete links and evaluated heads for conflicting review
requests, retain prior adjudications, and distinguish an implemented API request
from merely deleting its documentation promise. Proposed simp rules must pass the
actual candidate environment lint; a failed lint driver is an unfinished check.
