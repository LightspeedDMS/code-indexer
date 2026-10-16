---
name: feedback-never-assert-on-whole-environment
description: "Test hygiene: assert single env keys, not whole environment mappings (failures dump the whole env, noisy and unreadable); transcript exposure itself is NOT an incident per the owner"
metadata:
  type: feedback
---
Never write `assert "X" not in env` / `assert env == {...}` / `print(env)` over a full process environment (os.environ or a copy passed to a subprocess). When such an assertion fails, pytest's assertion rewriting prints the entire mapping, including every provider key in the developer's shell, into the test output, which lands in agent transcripts and is sent to the model API.

**Why:** 2026-10-07 a push-test assertion `"GIT_ASKPASS" not in push_env` failed and printed the full inherited environment (several provider API keys and tokens) into a subagent transcript; the owner was told to rotate. Same pattern as Issue #1327.

**How to apply:**
- Assert on single keys with scalar comparisons: `assert push_env.get("GIT_ASKPASS") == ""`, `assert "GIT_ASKPASS" not in set(push_env)` is still unsafe on failure; use `assert push_env.get(key) is None`.
- In tests that build child environments, start from a minimal explicit env (PATH, HOME) instead of copying os.environ, where the code under test allows it.
- Put this rule in every engineer brief that touches subprocess environments.
