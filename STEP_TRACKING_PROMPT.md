> **Depends on:** the existing `type: "action"` support in control-center (see
> `app.py` / `README.md`'s "Services & Apps" section) and GigBuddy's
> `scripts/calibrate_gigbuddy.sh`. Don't re-implement either — read them first.

## Context

The "Calibrate GigBuddy" action is used to capture 3 different Dasher app screen
states (offer, arrived-at-merchant, mark-picked-up) for feeding into GigBuddy's
`CALIBRATION_PROMPT.md`. Today the button is a single stateless "Run" — there's
no way to tell, from the dashboard, which of the 3 screens you're supposed to be
capturing next, and no way to start over if you get out of order. This matters
because the person tapping the button is usually mid-delivery on a phone, not at
a desk reading docs.

This prompt adds a lightweight step-tracking UI on top of the existing action
mechanism. It does **not** change what the capture script itself does — the
script (`calibrate_gigbuddy.sh`) already auto-detects which screen it actually
captured from the dump content and names the file accordingly. The step tracker
here is purely a reminder to the *human* of what to go find next; it never
blocks or validates the capture, since the dashboard has no way to know what's
really on the phone's screen at that moment.

## Part 1 — extend the action schema (optional `steps` field)

In the `type: "action"` handling in `app.py`:

1. Support an optional `"steps"` array on an action entry, each item
   `{"id": "...", "label": "..."}` — e.g.:
   ```json
   {"type": "action", "id": "calibrate-gigbuddy", "name": "Calibrate GigBuddy",
    "repo_id": "gigbuddy", "command": "bash scripts/calibrate_gigbuddy.sh", "timeout": 60,
    "steps": [
      {"id": "offer", "label": "Offer screen"},
      {"id": "arrived", "label": "Arrived at merchant"},
      {"id": "picked-up", "label": "Mark picked up"}
    ]}
   ```
2. Actions without a `steps` field keep working exactly as they do today — this
   is additive, not a breaking change to the schema.
3. Persist per-action progress (current step index, 0-based) in a small
   gitignored JSON file alongside `projects.json` (e.g. `action_progress.json`),
   keyed by `{project_id}/{action_id}`. Follow whatever read/write pattern
   `projects.json` already uses in `app.py` rather than inventing a new one.

## Part 2 — dashboard behavior

For an action that has `steps`:

1. Show the current step's label next to the Run button, e.g. "Capturing:
   Offer screen (1 of 3)". This is what tells the person on the road what
   screen to go get before tapping Run.
2. On a **successful** run, advance the persisted index to the next step. On a
   **failed** run, leave the index where it is — don't force the person to
   re-capture a screen they already got just because a later attempt errored.
3. After the last step succeeds, show something like "All screens captured —
   tap Reset to start over, or Run to capture the last screen again" and stop
   auto-advancing (repeated runs stay on the last step rather than going out of
   bounds).
4. Add a small **Reset** control next to the step indicator (a link or small
   button, not as prominent as Run) that sets the index back to 0. Confirm
   before resetting if that's consistent with how other destructive-ish
   actions in this dashboard are handled; otherwise a plain click is fine
   given this only resets a label, not any actual captured data.
5. Keep this usable on a mobile browser view, same as the Run button itself —
   the step label and Reset control need to be visible and tappable on a phone
   screen, not just desktop.

## Part 3 — update GigBuddy's own declaration

Add the `steps` array shown in Part 1 to GigBuddy's `services` block in its
own `README.md`, matching the 3 states `calibrate_gigbuddy.sh` already
auto-detects (`offer`, `arrived`, `picked-up`). Keep the labels short enough to
fit next to a button on a phone screen.

## Constraints

- No change to `calibrate_gigbuddy.sh` itself — it keeps auto-detecting state
  from the capture independently of whatever step the dashboard thinks is
  "next." The two aren't wired together; the step indicator is a human
  reminder only.
- No change to actions that don't declare `steps` — this must be fully
  backward compatible with every other action/service already declared across
  other projects' docs.
- `action_progress.json` (or whatever you name it) should follow the same
  gitignore treatment as `projects.json` — it's local dashboard state, not
  something that belongs in git history.

## When done

Report back: the exact file the progress state is persisted in, what the step
indicator and Reset control look like/are labeled in the dashboard, and
confirm the 3-step GigBuddy declaration was added to GigBuddy's `README.md`.
