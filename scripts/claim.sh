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
#   claim.sh acquire <key> [ttl_seconds]   # 0 acquired (or renewed mine) · 1 held by another · 2 error
#   claim.sh renew   <key> [ttl_seconds]   # 0 renewed · 1 lost (taken over / gone) · 2 error
#   claim.sh release <key>                 # 0 released (or wasn't mine / already gone)
#   claim.sh holds   <key>                 # 0 I hold it and it's unexpired · 1 otherwise
#   claim.sh read    <key>                 # print the lease JSON (empty if unclaimed)
#   claim.sh list    [--full]              # list live claim refs (--full fetches each lease)
#   claim.sh gc                            # CAS-delete expired claims
#
# Env: CLAIM_REPO (default TauCetiProject/TauCeti), TAUCETI_WORKER_ID (default host-pid),
#      CLAIM_TTL (default 1500), CLAIM_GITDIR_BASE (per-repo scratch parent),
#      CLAIM_GITDIR (explicit scratch object store override).
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

# A private scratch repo just for building + pushing claim objects (no work-repo checkout needed).
ensure_repo() {
    if [[ ! -d "$GITDIR" ]]; then
        mkdir -p "$(dirname "$GITDIR")"
        git init -q --bare "$GITDIR"
    fi
    git -C "$GITDIR" remote get-url origin >/dev/null 2>&1 \
        || git -C "$GITDIR" remote add origin "$URL"
    git -C "$GITDIR" remote set-url origin "$URL"
}
g() {
    local limit="${CLAIM_COMMAND_TIMEOUT:-30}" out pid watchdog rc; [[ "$limit" =~ ^[1-9][0-9]*$ ]] || { echo "claim: invalid command timeout: $limit" >&2; return 2; }
    out=$(mktemp "$GITDIR/.claim-command.XXXXXX") || return 2; git -C "$GITDIR" "$@" <&0 >"$out" 2>&1 & pid=$!
    ( sleeper=; trap '[[ -n "$sleeper" ]] && kill "$sleeper" 2>/dev/null; exit 0' TERM; sleep "$limit" & sleeper=$!; wait "$sleeper"; kill -KILL "$pid" 2>/dev/null ) & watchdog=$!
    wait "$pid"; rc=$?; kill "$watchdog" 2>/dev/null; wait "$watchdog" 2>/dev/null
    cat "$out"; rm -f "$out"; [[ "$rc" -eq 137 ]] && return 124; return "$rc"
}

empty_tree() { g hash-object -t tree -w /dev/null; }

# remote_oid REF — current oid of REF on origin, or "" if absent.
remote_oid() { g ls-remote origin "$1" 2>/dev/null | awk 'NR==1{if (length($1)!=40 || $1~/[^0-9a-f]/) exit 2; print $1}'; }

# lease_json OID — the JSON lease stored in the orphan commit OID (fetched on demand).
lease_json() {
    local oid="$1"
    g cat-file -e "$oid" 2>/dev/null || g fetch -q --no-tags origin "$2" 2>/dev/null || return 1
    g cat-file commit "$oid" 2>/dev/null | sed '1,/^$/d'
}

valid_lease_json() {
    lease_json "$2" "$3" | jq -ce --arg resource "$1" 'select(type == "object" and .schema == "tauceti-claim/v1"
          and (.owner | type) == "string" and (.owner | length) > 0
          and (.expires_at | type) == "number" and .expires_at == (.expires_at | floor) and (.expires_at | tostring | test("^[0-9]+$"))
          and .resource == $resource)'
}

# build_oid JSON — write an orphan commit (empty tree) whose message is JSON; print its oid.
# `commit-tree` only reads stdin when explicitly told to use it. Without `-F -`, the lease payload
# silently becomes an empty commit message, so `holds`/`renew` cannot recover the owner and every
# safe push fails closed as "lease lost". Keep the JSON in the commit message, where lease_json reads it.
build_oid() { printf '%s' "$1" | g commit-tree "$(empty_tree)" -F -; }

# payload KEY EXPIRES — the lease JSON for a claim I'm taking now.
payload() {
    local n; n=$(now)
    jq -nc --arg s "tauceti-claim/v1" --arg o "$WID" --arg h "$(hostname)" \
        --argjson pid "$$" --argjson aq "$n" --argjson ex "$2" --arg res "$1" \
        --arg observed "${CLAIM_OBSERVED_OID:-}" \
        '{schema:$s, owner:$o, host:$h, pid:$pid, acquired_at:$aq, expires_at:$ex,
          resource:$res, observed_branch_oid:($observed | if . == "" then null else . end)}'
}

# push_cas REF EXPECTED NEWOID — CAS push (EXPECTED="" ⇒ create-only). 0 win, 1 lost/rejected.
push_cas() {
    local out
    out=$(g push --force-with-lease="$1:$2" origin "$3:$1" 2>&1)
    if [[ $? -eq 0 ]]; then return 0; fi
    grep -qiE 'rejected|stale info|failed to push' <<<"$out" && return 1
    echo "claim: unexpected push error on $1: $out" >&2; return 2
}
push_delete() { g push --force-with-lease="$1:$2" origin ":$1" >/dev/null 2>&1; }

renew_owned() {
    local key="$1" ref="$2" cur="$3" target="$4" oid="$5" rc next js owner exp
    push_cas "$ref" "$cur" "$oid"; rc=$?
    [[ "$rc" -eq 1 ]] || return "$rc"
    next=$(remote_oid "$ref") || return 2; [[ -n "$next" ]] || return 1
    js=$(valid_lease_json "$key" "$next" "$ref") || return 2
    owner=$(jq -r '.owner' <<<"$js"); exp=$(jq -r '.expires_at' <<<"$js")
    [[ "$owner" == "$WID" ]] || return 1; [[ "$exp" -ge "$target" ]] && return 0
    push_cas "$ref" "$next" "$oid"  # one retry, only under a freshly verified own ref
}

cmd_acquire() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner exp n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref") || return 2
    if [[ -n "$cur" ]]; then
        js=$(valid_lease_json "$key" "$cur" "$ref") || return 2
        owner=$(jq -r '.owner' <<<"$js"); exp=$(jq -r '.expires_at' <<<"$js")
        if [[ "$owner" != "$WID" && "$exp" -gt "$n" ]]; then
            return 1   # someone else holds a live lease
        fi
        # mine (renew) or expired (takeover): CAS against the observed oid
        local oid target=$((n+ttl)); oid=$(build_oid "$(payload "$key" "$target")") || return 2
        if [[ "$owner" == "$WID" ]]; then renew_owned "$key" "$ref" "$cur" "$target" "$oid"; return $?; fi
        push_cas "$ref" "$cur" "$oid"; return $?
    fi
    local oid; oid=$(build_oid "$(payload "$key" "$((n+ttl))")") || return 2
    push_cas "$ref" "" "$oid"   # create-only
}

cmd_renew() {
    local key="$1" ttl="${2:-$DEFAULT_TTL}" ref cur js owner n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref") || return 2; [[ -z "$cur" ]] && return 1
    js=$(valid_lease_json "$key" "$cur" "$ref") || return 2; owner=$(jq -r '.owner' <<<"$js")
    [[ "$owner" == "$WID" ]] || return 1   # lost / taken over
    local oid target=$((n+ttl)); oid=$(build_oid "$(payload "$key" "$target")") || return 2
    renew_owned "$key" "$ref" "$cur" "$target" "$oid"
}

cmd_release() {
    local key="$1" ref cur js owner
    ref=$(ref_of "$key"); ensure_repo
    cur=$(remote_oid "$ref") || return 2; [[ -z "$cur" ]] && return 0
    js=$(valid_lease_json "$key" "$cur" "$ref") || return 2; owner=$(jq -r '.owner' <<<"$js")
    [[ "$owner" == "$WID" ]] || return 0   # not mine — leave it
    push_delete "$ref" "$cur" || return 2
}

cmd_holds() {
    local key="$1" ref cur js owner exp n
    ref=$(ref_of "$key"); n=$(now); ensure_repo
    cur=$(remote_oid "$ref") || return 2; [[ -z "$cur" ]] && return 1
    js=$(valid_lease_json "$key" "$cur" "$ref") || return 2
    owner=$(jq -r '.owner' <<<"$js"); exp=$(jq -r '.expires_at' <<<"$js")
    [[ "$owner" == "$WID" && "$exp" -gt "$n" ]]
}

cmd_read() {
    local ref cur; ref=$(ref_of "$1"); ensure_repo
    cur=$(remote_oid "$ref") || return 2; [[ -z "$cur" ]] && return 0; valid_lease_json "$1" "$cur" "$ref" || return 2
}

cmd_list() {
    ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local key="${ref#"$NS"/}"
        if [[ "${1:-}" == "--full" ]]; then
            printf '%s\t%s\n' "$key" "$(lease_json "$oid" "$ref" | tr -d '\n')"
        else
            printf '%s\t%s\n' "$key" "$oid"
        fi
    done
}

cmd_gc() {
    local n; n=$(now); ensure_repo
    g ls-remote origin "$NS/*" 2>/dev/null | while read -r oid ref; do
        local js exp; js=$(lease_json "$oid" "$ref"); exp=$(jq -r '.expires_at // 0' <<<"$js" 2>/dev/null)
        if [[ "$exp" =~ ^[0-9]+$ && "$exp" -le "$n" ]]; then
            push_delete "$ref" "$oid" && echo "gc: deleted expired $ref" >&2
        fi
    done
}

cmd="${1:-}"; shift || true
case "$cmd" in
    acquire) cmd_acquire "$@";;
    renew)   cmd_renew "$@";;
    release) cmd_release "$@";;
    holds)   cmd_holds "$@";;
    read)    cmd_read "$@";;
    list)    cmd_list "$@";;
    gc)      cmd_gc "$@";;
    *) echo "usage: claim.sh {acquire|renew|release|holds|read|list|gc} <key> [ttl]" >&2; exit 64;;
esac
