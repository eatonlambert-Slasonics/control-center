> **Depends on:** the existing `type: "action"` mechanism (see `README.md`'s
> "Services & Apps" section), GigBuddy's `scripts/calibrate_gigbuddy.sh` and
> `CALIBRATION_PROMPT.md`, and the fact that this dashboard already knows how
> to launch `claude --remote-control` in a tmux session for a project (see the
> `claude` tool notes in `README.md`'s "Running" section). Read all of these
> in full before writing anything — this prompt wires existing pieces
> together, it doesn't reinvent them.

## Context

Capturing calibration data (offer / arrived / picked-up screens) is now a
one-tap action from the road. The remaining step — feeding those captures to
Claude Code, updating the parsers, and deploying — still requires sitting at a
terminal. This adds a second action that runs that whole tail end
unattended: it kicks off Claude Code non-interactively against
`CALIBRATION_PROMPT.md`, and on success merges to `main` and installs the
recalibrated APK on every connected device.

**This is fully automated end-to-end, by explicit choice** — Claude Code's
edits get merged and deployed without a human review step in between. Build
it that way; don't add an approval gate.

## Part 1 — headless Claude Code run on tbot

Add a script, e.g. `scripts/finish_calibration.sh`, in the GigBuddy repo that:

1. Checks the `calibration-captures` branch actually has new captures since
   `main` last merged it (bail with a clear message if there's nothing new to
   calibrate against — don't burn a Claude Code run for no reason).
2. Runs Claude Code **non-interactively** (`claude -p` / headless/print mode —
   check current Claude Code CLI docs for the right flag, since this needs to
   run to completion unattended over SSH, not open an interactive session) in
   the GigBuddy repo, with `CALIBRATION_PROMPT.md`'s full content as the
   prompt, working from a branch that has the new captures merged in locally
   first (or pass the branch name and let the prompt/Claude Code check it out
   — your call, document whichever).
3. On success: merges the calibration branch and Claude Code's parser changes
   into `main` and pushes.
4. Runs the `android-device-deploy` skill (per the existing GigBuddy
   build/deploy pipeline) to install the updated debug APK on every connected
   device (Galaxy S25, Pixel 6 Pro) via the whitebox tunnel.
5. On any failure at any stage (Claude Code errors, merge conflict, build
   failure, deploy failure), stop immediately, leave `main` untouched, and
   report exactly which stage failed — don't half-merge or half-deploy.
6. Logs enough that a later `get_logs` (or equivalent) call can show what
   happened, since nobody will be watching a terminal live.

## Part 2 — control-center action

Add a second action to GigBuddy's `services` declaration in its own
`README.md`, alongside the existing `calibrate-gigbuddy` one:

```json
{"type": "action", "id": "finish-gigbuddy-calibration", "name": "Finish GigBuddy Calibration",
 "repo_id": "gigbuddy", "command": "bash scripts/finish_calibration.sh", "timeout": 180}
```

Use the max allowed timeout (per `app.py`'s existing 180s cap) since this
chains a Claude Code run, a merge, a build, and a device install — check
whether that's actually going to be enough wall-clock time in practice and
flag it in your final report if 180s looks tight; if it needs to be able to
run longer than any action currently supports, say so rather than silently
producing something that'll always time out.

## Constraints

- No approval/review gate — this is intentionally full-auto per the person's
  explicit choice. Don't add a "confirm before merge" step.
- Still must not add any `performAction()` / `performGlobalAction()` /
  `dispatchGesture()` calls to the app itself — that constraint from
  `CALIBRATION_PROMPT.md` still applies to whatever Claude Code produces here,
  headless or not.
- Reuse the existing `android-device-deploy` skill for the install step rather
  than re-implementing device discovery/install.
- If the calibration branch has no new captures, exit cleanly without running
  Claude Code, merging, or deploying anything.

## When done

Report back: where `finish_calibration.sh` lives, the exact button
label/timeout in the dashboard, whether 180s is realistically enough for a
full run (and what you'd recommend if not), and what the failure output looks
like in the dashboard if a stage fails partway through.
