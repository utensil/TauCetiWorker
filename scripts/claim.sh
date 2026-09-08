#!/usr/bin/env bash
# claim.sh — optional, cooperative task de-contention for Tau Ceti agents.
#
# A claim is a custom git ref `refs/tauceti-claims/<key>` in the work repo, pointing at an orphan
# commit whose message is a JSON lease {owner, expires_at, ...}. Acquire/renew/takeover/release are
# all done with ONE atomic GitHub primitive — `git push --force-with-lease=<ref>:[<oid>]`:
#   * expected EMPTY  → create-only (succeeds iff the ref does not exist)
#   * expected <oid>  → succeeds iff the ref still points at <oid> (compare-and-swap)
# (Validated against real GitHub: a second create is rejected "stale info"; a CAS with the wrong
# old-oid is rejected; with the right old-oid it forces. So races have exactly one winner.)
#
# This is [COOP] in the coordination contract: honoring claims only avoids DUPLICATE work. It is
# NOT the safety mechanism — the branch-level `--force-with-lease` in git-safe-push is. A claim can
# expire (TTL) so a dead holder never blocks anyone; takeover of an expired claim is itself a CAS,
# so two reclaimers can't both win.
#
# Usage:
#   claim.sh acquire <key> [ttl_seconds]   # acquire, renew mine, or take over a valid expired lease
#   claim.sh renew   <key> [ttl_seconds]   # renew only mine; bounded same-owner CAS retry
#   claim.sh release <key>                 # release only mine; absent is already released
#   claim.sh holds   <key>                 # 0 only when mine and unexpired; typed failure below
#   claim.sh read    <key>                 # print the lease JSON (empty if unclaimed)
#   claim.sh list    [--full]              # list live claim refs (--full fetches each lease)
#   claim.sh gc                            # CAS-delete expired claims
#
# Env: CLAIM_REPO (default TauCetiProject/TauCeti), TAUCETI_WORKER_ID (default host-pid),
#      CLAIM_TTL (default 1500), CLAIM_GITDIR_BASE (per-repo scratch parent),
#      CLAIM_GITDIR (explicit scratch object store override).
# Exit status: 0 success; 1 other owner; 2 transport/command error (unknown);
# 3 absent; 4 malformed lease; 5 expired; 6 CAS race; 64 invalid invocation.
# Bound the whole operation, including git's network descendants. This supervisor also forwards
# shutdown to its own child group; it never signals the caller's group.
if [[ "${_TAUCETI_CLAIM_BOUNDED:-}" != 1 ]]; then
    exec python3 - "$0" "$@" <<'PY_BOUND'
import os
import signal
import subprocess
import sys

child = subprocess.Popen(
    ["bash", *sys.argv[1:]],
    env={**os.environ, "_TAUCETI_CLAIM_BOUNDED": "1"},
    start_new_session=True,
)

def stop(*_):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()
    raise SystemExit(2)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    raise SystemExit(child.wait(timeout=30))
except subprocess.TimeoutExpired:
    print("claim: command-timeout (ownership unknown)", file=sys.stderr)
    stop()
PY_BOUND
fi
set -uo pipefail

REPO="${CLAIM_REPO:-TauCetiProject/TauCeti}"
URL="https://github.com/$REPO"
WID="${TAUCETI_WORKER_ID:-$(hostname)-$$}"
DEFAULT_TTL="${CLAIM_TTL:-1500}"
GITDIR="${CLAIM_GITDIR:-${CLAIM_GITDIR_BASE:-$HOME/.cache/tauceti-claims}/${REPO//\//__}.git}"
NS="refs/tauceti-claims"
export GIT_AUTHOR_NAME="tauceti-claim" GIT_AUTHOR_EMAIL="claim@tauceti.invalid"
export GIT_COMMITTER_NAME="tauceti-claim" GIT_COMMITTER_EMAIL="claim@tauceti.invalid"

now() { date +%s; }
ref_of() { printf '%s/%s' "$NS" "$1"; }
fail() { echo "claim: $2" >&2; return "$1"; }
ensure_repo() {
    if [[ ! -d "$GITDIR" ]]; then
        mkdir -p "$(dirname "$GITDIR")" && git init -q --bare "$GITDIR" || return 2
    fi
    git -C "$GITDIR" remote get-url origin >/dev/null 2>&1 \
        || git -C "$GITDIR" remote add origin "$URL" || return 2
    git -C "$GITDIR" remote set-url origin "$URL" || return 2
}
g() { git -C "$GITDIR" "$@"; }
empty_tree() { g hash-object -t tree -w /dev/null; }

remote_oid() {
    local out
    out=$(g ls-remote origin "$1" 2>/dev/null) || { fail 2 "remote-query-failed (ownership unknown)"; return 2; }
    [[ -n "$out" ]] || { fail 3 "absent"; return 3; }
    awk 'NR==1{print $1}' <<<"$out"
}
lease_json() {
    local oid="$1" js
    if ! g cat-file -e "$oid" 2>/dev/null; then
        g fetch -q --no-tags origin "$2" 2>/dev/null \
            || { fail 2 "lease-fetch-failed (ownership unknown)"; return 2; }
    fi
    js=$(g cat-file commit "$oid" 2>/dev/null | sed '1,/^$/d') \
        || { fail 2 "lease-object-unavailable (ownership unknown)"; return 2; }
    jq -se --arg key "${2#"$NS"/}" '
        length == 1 and (.[0] |
        type == "object" and .schema == "tauceti-claim/v1" and .resource == $key and
        (.owner | type == "string" and length > 0) and
        (.expires_at | type == "number" and . >= 0 and . < 9007199254740991 and floor == .))
    ' <<<"$js" >/dev/null 2>&1 || { fail 4 "malformed-lease"; return 4; }
    printf '%s\n' "$js"
}
# -F - is essential: without it commit-tree silently creates an empty lease message.
build_oid() { printf '%s' "$1" | g commit-tree "$(empty_tree)" -F -; }
payload() {
    local n; n=$(now)
    jq -nc --arg s "tauceti-claim/v1" --arg o "$WID" --arg h "$(hostname)" \
        --argjson pid "$$" --argjson aq "$n" --argjson ex "$2" --arg res "$1" \
        --arg observed "${CLAIM_OBSERVED_OID:-}" \
        '{schema:$s, owner:$o, host:$h, pid:$pid, acquired_at:$aq, expires_at:$ex,
          resource:$res, observed_branch_oid:($observed | if . == "" then null else . end)}'
}
push_cas() {
    local out
    out=$(g push --force-with-lease="$1:$2" origin "$3:$1" 2>&1) && return 0
    if [[ "$out" == *"stale info"* || "$out" == *"[rejected]"* ]]; then
        fail 6 "cas-race"; return 6
    fi
    fail 2 "push-failed (ownership unknown)"
}
push_delete() { push_cas "$1" "$2" ""; }

cmd_acquire() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner exp n rc oid
    ref=$(ref_of "$key"); n=$(now); ensure_repo || return 2
    cur=$(remote_oid "$ref"); rc=$?
    [[ "$rc" == 0 || "$rc" == 3 ]] || return "$rc"
    if [[ "$rc" == 0 ]]; then
        js=$(lease_json "$cur" "$ref") || return $?
        owner=$(jq -r '.owner' <<<"$js"); exp=$(jq -r '.expires_at' <<<"$js")
        if [[ "$owner" != "$WID" && "$exp" -gt "$n" ]]; then
            fail 1 "other-owner"; return 1
        fi
    fi
    oid=$(build_oid "$(payload "$key" "$((n+ttl))")") || return 2
    push_cas "$ref" "$cur" "$oid"
}
cmd_renew() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner n oid rc attempt
    ref=$(ref_of "$key"); ensure_repo || return 2
    # One bounded retry tolerates another renewal by this same owner. Reread and revalidate
    # ownership each time; a race never authorizes an unconditional write.
    for attempt in 1 2; do
        n=$(now)
        cur=$(remote_oid "$ref") || return $?
        js=$(lease_json "$cur" "$ref") || return $?
        owner=$(jq -r '.owner' <<<"$js")
        [[ "$owner" == "$WID" ]] || { fail 1 "other-owner"; return 1; }
        oid=$(build_oid "$(payload "$key" "$((n+ttl))")") || return 2
        push_cas "$ref" "$cur" "$oid"; rc=$?
        [[ "$rc" == 6 ]] || return "$rc"
    done
    return 6
}
cmd_release() {
    local key="$1" ref cur js owner rc
    ref=$(ref_of "$key"); ensure_repo || return 2
    cur=$(remote_oid "$ref"); rc=$?
    [[ "$rc" != 3 ]] || return 0
    [[ "$rc" == 0 ]] || return "$rc"
    js=$(lease_json "$cur" "$ref") || return $?
    owner=$(jq -r '.owner' <<<"$js")
    [[ "$owner" == "$WID" ]] || { fail 1 "other-owner (left untouched)"; return 1; }
    push_delete "$ref" "$cur"
}
cmd_holds() {
    local key="$1" ref cur js owner exp n
    ref=$(ref_of "$key"); n=$(now); ensure_repo || return 2
    cur=$(remote_oid "$ref") || return $?
    js=$(lease_json "$cur" "$ref") || return $?
    owner=$(jq -r '.owner' <<<"$js"); exp=$(jq -r '.expires_at' <<<"$js")
    [[ "$owner" == "$WID" ]] || { fail 1 "other-owner"; return 1; }
    [[ "$exp" -gt "$n" ]] || { fail 5 "expired"; return 5; }
}
cmd_read() {
    local ref cur rc; ref=$(ref_of "$1"); ensure_repo || return 2
    cur=$(remote_oid "$ref"); rc=$?
    [[ "$rc" != 3 ]] || return 0
    [[ "$rc" == 0 ]] || return "$rc"
    lease_json "$cur" "$ref"
}
cmd_list() {
    local refs oid ref js
    ensure_repo || return 2
    refs=$(g ls-remote origin "$NS/*" 2>/dev/null) || { fail 2 "remote-query-failed"; return 2; }
    [[ -n "$refs" ]] || return 0
    while read -r oid ref; do
        if [[ "${1:-}" == "--full" ]]; then
            js=$(lease_json "$oid" "$ref") || return $?
            printf '%s\t%s\n' "${ref#"$NS"/}" "$(tr -d '\n' <<<"$js")"
        else
            printf '%s\t%s\n' "${ref#"$NS"/}" "$oid"
        fi
    done <<<"$refs"
}
cmd_gc() {
    local n refs oid ref js exp; n=$(now); ensure_repo || return 2
    refs=$(g ls-remote origin "$NS/*" 2>/dev/null) || { fail 2 "remote-query-failed"; return 2; }
    [[ -n "$refs" ]] || return 0
    while read -r oid ref; do
        js=$(lease_json "$oid" "$ref") || return $?
        exp=$(jq -r '.expires_at' <<<"$js")
        if [[ "$exp" -le "$n" ]]; then
            push_delete "$ref" "$oid" || return $?
            echo "gc: deleted expired $ref" >&2
        fi
    done <<<"$refs"
}

cmd="${1:-}"; shift || true
case "$cmd" in
    acquire|renew|release|holds|read)
        [[ $# -ge 1 ]] && git check-ref-format "$(ref_of "$1")" >/dev/null 2>&1 \
            || { fail 64 "invalid-key"; exit 64; }
        if [[ "$cmd" == acquire || "$cmd" == renew ]]; then
            ttl="${2:-$DEFAULT_TTL}"
            [[ "$ttl" =~ ^[1-9][0-9]*$ && ${#ttl} -le 8 ]] || { fail 64 "invalid-ttl"; exit 64; }
        fi
        "cmd_$cmd" "$@";;
    list) cmd_list "$@";;
    gc) cmd_gc "$@";;
    *) echo "usage: claim.sh {acquire|renew|release|holds|read|list|gc} <key> [ttl]" >&2; exit 64;;
esac
