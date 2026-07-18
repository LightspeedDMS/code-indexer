---
name: feedback-agent-stall-detection-needs-reply-not-just-mtime
description: "Don't kill a background agent based on output-file mtime staleness alone, even after a ping shows \"queued for delivery\" — wait for an actual reply or a second independent signal before treating it as stalled"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ede890d5-0358-4ba6-a741-0ed8d5e2db8f
---

Killed a tdd-engineer agent (code-indexer session, 2026-07-17) after its output-file mtime stayed static for ~21 minutes despite two SendMessage pings (both returned "queued for delivery at its next tool round" — never actually delivered/answered before I killed it). The kill notification revealed the agent had been alive and correct the whole time: it had completed a thorough 5-cluster root-cause investigation via git blame (verified accurate), and was mid-edit on the first fix when killed. Redispatching with its own findings handed over avoided most of the lost work, but the kill itself was unnecessary and wasted the agent's in-flight edit.

**Why**: some agents go long stretches between output-file flushes while doing deep multi-tool-call investigation (many sequential Bash/Read/git-blame calls) without that showing up as new content in the monitored `.output` file. Output-file mtime is a weak stall signal on its own — a live, productive agent can look identical to a dead one by that metric alone. A ping that returns "queued for delivery" is NOT confirmation of delivery or non-response — it just means the message entered a queue; the agent may not have reached its next tool round yet, especially if it's mid-long-running-command.

**How to apply**: When a user asks for active stall monitoring on background agents:
- Use output-file mtime staleness as a trigger to *ping*, not as grounds to kill.
- After pinging, wait for either (a) an actual reply/status update from the agent, or (b) mtime advancing, before concluding a stall — don't kill on the very next check if the ping's delivery status was only "queued," not confirmed received/answered.
- If genuinely uncertain, prefer one more wait cycle over killing — the cost of an unnecessary kill (lost in-flight work, wasted investigation) is generally higher than the cost of one extra 5-minute wait.
- If you do decide to kill despite ambiguity, save the agent's last-known status report (if it sent one) so a restart can skip re-doing completed investigation work.
