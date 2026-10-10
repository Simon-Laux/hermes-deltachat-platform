---
name: handoff
description: Finish a change and hand it to the user as a reviewed PR. Covers tests, a changelog entry, review subagents, fixing what they find, opening the PR against main, waiting for CI, and a fixed-format handoff message. Use when the user says "handoff", "/handoff", "finish this", "open the pr", or "review and open a pr". It does not merge.
---

# Handoff

The user reviews every PR before merging. This skill does everything up to that point, so
the review is a decision and not a hunt for missing pieces. Run every step; skip one only when
it clearly can't apply (say which and why in the handoff).

## 1. Branch

The branch starts from `origin/main` and the PR targets `main`. If it is behind, rebase it now,
not after review.

## 2. Tests

- **Fixes:** add a regression test that fails without the fix. Check it really does by
  reverting the fix in the working tree, running the test, and restoring the fix. Don't use
  `git stash` for this, because its stack is shared with other sessions.
- **Features:** test the main path and the edge cases you can think of (empty input,
  limits, a dead RPC server, a missing optional Hermes API).
- **Code that calls Hermes:** the suite mocks Hermes in `tests/conftest.py`, and most bugs
  found in live use were mismatches with real Hermes (a wrong skill name, a dropped
  `reply_to_text`, a hardcoded STT model). Check every Hermes signature, attribute and
  config key you use against the installed source (see "Finding Hermes Source" in
  `Agents.md`). Add to `tests/hermes_contract/` when the contract can be tested.
- Run the whole suite and pyflakes as described in `Agents.md`. Both must pass.

## 3. Changelog

Add an entry under `## Unreleased` in `CHANGELOG.md` for anything people running the adapter
would notice. Write it for them: what changed and what they need to do, not how. Internal
refactors and test-only changes get no entry.

## 4. Review subagents

Start these in parallel, each with the diff (`git diff origin/main...HEAD`) and the reason
for the change:

1. **Bugs and regressions:** what breaks for existing users, plus wrong behaviour on the new path.
2. **Edge cases and tests:** inputs and states the tests don't cover, and races with the
   event loop or the RPC server.
3. **Simplification** (only for diffs over ~150 lines or ones that touch the
   sending/editing path): use the `simplify` and `ponytail-review` skills.

Tell every reviewer:
- Each finding needs a `file:line` and a concrete failure scenario.
- Claims about Hermes need a `file:line` in the installed Hermes source, and claims about
  Delta Chat RPC need the method in `deltachat-rpc-openrpc.json`. Findings that can't be
  verified are dropped, not passed on.

## 5. Triage findings

**Fix these without asking:**
- bugs
- missing tests
- wrong docs or comments
- small simplifications
- anything where the fix has no real downside

**Ask the user only about:**
- behaviour changes users would notice
- security or privacy trade-offs
- design choices with more than one reasonable answer
- scope growth

**File as an issue on our own repo:** pre-existing problems outside the PR's scope.

If the user asked to be consulted on each point, do that instead.

If the fixes changed more than wording, run one more short review round on the fix commits
only.

## 6. Open the PR

Push the branch and open the PR against `main` on `Simon-Laux/hermes-deltachat-platform`. The
body says what changed and why, how it was tested, and the manual test steps from step 8.
Then wait for CI with `gh pr checks <n> --watch` and fix any failures.

## 7. Don't merge

Merge only when the user says so in this conversation.

## 8. Handoff message

End with exactly this structure, kept short:

```
**PR #<n>**: <url>
**Verdict:** ready to merge | needs your decision on <x> | blocked by <y>
**Tested automatically:** <new tests, suite result, CI status>
**Test by hand:** <the concrete things to try in Delta Chat, and what you should see>, or
  "nothing, <why>"
**Review:** <n> findings fixed; deferred: <issue links>, or none
**Questions:** <only the decisions that need you>, or none
```

"Test by hand" names real actions ("send a 60-line reply with streaming on; expect two
messages, neither folded"), not "test the feature".
