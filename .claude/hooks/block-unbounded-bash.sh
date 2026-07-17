#!/usr/bin/env bash
# PreToolUse(Bash) guard. Two rules:
#   GUARD 1 -- BLOCK long-running FOREGROUND commands (must be run_in_background).
#   GUARD 2 -- BLOCK unbounded background wait-loops that lack a `timeout`.
#
# GUARD 1 (added 2026-07-12, the maintainer): a blocking foreground Bash call makes the
# agent UNABLE to read the user's messages until it returns -- so replies land
# minutes late + act on stale state (the .126 live-test lag). Long foreground
# work (SSH probes with sleeps, sampling/poll loops, `-f` tails, `timeout>=30`,
# apt install/upgrade) must be run with run_in_background:true (the agent gets a
# completion notification) or split into a short (<~5s) probe.
#
# GUARD 2 (2026-06-16): run_in_background loops are NOT bounded by
# BASH_MAX_TIMEOUT_MS; an `until ! pgrep -f X; do sleep 3; done` whose pgrep
# matched its own command line looped ~15h. Unbounded loops must be `timeout`-wrapped
# or use the Monitor tool.
#
# Exit 2 = BLOCK + feed stderr back to the agent. Exit 0 = allow. FAIL-OPEN: any
# parse problem -> allow (never lock the agent out; it can still Edit this file).
set -uo pipefail
input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0
bg="$(printf '%s' "$input" | jq -r '.tool_input.run_in_background // false' 2>/dev/null || echo false)"
# agent_id is EMPTY for the MAIN agent, POPULATED for Task/Agent-spawned subagents (verified
# 2026-07-12). GUARD 1 (responsiveness) applies to the MAIN agent only -- a subagent has no live
# user to stay responsive to, so it may run long foreground commands freely. GUARD 2 is universal.
AGENT_ID="$(printf '%s' "$input" | jq -r '.agent_id // empty' 2>/dev/null || true)"

# ---------------- GUARD 1a: remote-host access must be DELEGATED (MAIN agent only) ----------------
# ssh/scp from the MAIN agent -> BLOCK. Remote-host work (SSH probing, remote diagnostics/deploy)
# must go to a SUBAGENT (Agent/Task tool) so the grind stays out of the main context -- the main
# agent's reasoning degrades over a long inline session (the maintainer 2026-07-12). Subagents ssh freely.
# Word-boundary match (ssh/scp/sshpass as a command word); paths like '.ssh/config' do NOT match.
if [ -z "$AGENT_ID" ] && printf '%s' "$cmd" | grep -qE '(^|[[:space:];&|(])(ssh|scp|sshpass)[[:space:]]'; then
  {
    echo "BLOCKED: remote-host access (ssh/scp) from the MAIN agent. Delegate remote-host work"
    echo "(SSH probing, remote diagnostics/deploy) to a SUBAGENT via the Agent/Task tool -- it keeps"
    echo "the grind out of the main context. Subagents may ssh freely; spawn one for a one-off check."
  } >&2
  exit 2
fi

# ---------------- GUARD 1: long FOREGROUND commands (MAIN agent only) ----------------
if [ "$bg" != "true" ] && [ -z "$AGENT_ID" ]; then
  long=0
  # a sampling/poll LOOP that sleeps (for/while/seq ... sleep N)
  if printf '%s' "$cmd" | grep -qE '(seq|for[[:space:]]|while[[:space:]]|;[[:space:]]*do)' \
     && printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])sleep[[:space:]]+[0-9]'; then long=1; fi
  # a long single sleep (>= 10s)
  printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])sleep[[:space:]]+([1-9][0-9]|[0-9]{3,})([^0-9]|$)' && long=1
  # follow tails
  printf '%s' "$cmd" | grep -qE '(journalctl|tail)[^|]*[[:space:]]-f([[:space:]]|$)' && long=1
  # explicit long timeout (>= 30s)
  printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])timeout[[:space:]]+([3-9][0-9]|[0-9]{3,})([[:space:]]|$)' && long=1
  # apt install/upgrade (slow, network)
  printf '%s' "$cmd" | grep -qE '(^|[^[:alnum:]_])apt(-get)?[[:space:]]+(install|upgrade|dist-upgrade|full-upgrade)([[:space:]]|$)' && long=1
  if [ "$long" = "1" ]; then
    {
      echo "BLOCKED: long-running FOREGROUND command. A blocking foreground call makes you"
      echo "unable to read the user's messages until it returns (stale replies during live tests)."
      echo "Fix: re-run this with run_in_background:true (you get a completion notification and"
      echo "stay responsive), or split it into a short (<~5s) probe. See CLAUDE.md."
    } >&2
    exit 2
  fi
fi

# ---------------- GUARD 2: unbounded background wait-loops ----------------
# Already bounded by an outer `timeout ...` -> allow.
if printf '%s' "$cmd" | grep -qE '(^|[;&|([:space:]])timeout[[:space:]]'; then
  exit 0
fi
if printf '%s' "$cmd" | grep -qE '(^|[;&|([:space:]])until[[:space:]]' \
   || printf '%s' "$cmd" | grep -qE 'while[[:space:]]+(true|:|1|\[[[:space:]]*1[[:space:]]*\])([[:space:]]|;|$)'; then
  {
    echo "BLOCKED: unbounded loop (until / while true) without a 'timeout' bound."
    echo "Background loops are NOT timed here and will hang forever (the ~15h stall)."
    echo "Fix: wrap it ->  timeout 120 bash -c 'until <cond>; do sleep 3; done'"
    echo "  or use the Monitor tool (it has an enforced timeout_ms)."
  } >&2
  exit 2
fi
exit 0
