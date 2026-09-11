# Observing long validation commands

Host integrations can opt a synchronous build or validation command into the
existing round activity channel:

```sh
python -m tauceti_worker.observed_process --idle-seconds 5400 --max-seconds 14400 -- lake build
```

The helper forwards combined stdout/stderr and renews round progress when it sees
a fresh nonempty output line, before the command finishes. It records process
identities and monotonic progress times, never command text or output. Repeated
lines (including ANSI-decorated repeats) do not renew activity. Distinct output
shows activity, not correctness; callers must still check the exit status.

Both deadlines are required, finite and positive. Silence expires the idle
deadline, while even continuously changing output cannot extend the absolute
deadline. Deadline expiry returns 124; normal exit status is preserved and a
signal exit uses the shell convention. The helper discovers owned descendants,
including sampled separate sessions, and verifies their cleanup before returning.
A failed process snapshot cannot certify cleanup. As with the native supervisor,
a descendant that escapes and becomes orphaned between snapshots cannot be
identified by ancestry alone.

This is an opt-in subprocess helper, not another worker or scheduler. The native
supervisor retains the round lock, claim-loss handling and checkout admission.
The helper uses the existing round token and status lock and refuses stale-token
launches and renewals. It also works outside a worker round with local deadlines
and cleanup. Project adapters choose validation commands and cache preparation;
do not wrap log followers, polling loops or provider retry chatter as validation.

Existing JSON event observation and ordinary worker behavior are unchanged.
