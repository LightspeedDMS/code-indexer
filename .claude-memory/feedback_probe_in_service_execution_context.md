---
name: feedback_probe_in_service_execution_context
description: "A service's behaviour must be probed as the SERVICE user with the unit's PATH/HOME — an SSH login measures your own environment, and user-site packages silently shadow the real install"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-15T13:17:25.029Z
---

Never conclude what a server's subprocess executes by running the command from your own
interactive SSH login. `sys.path`, `PATH`, `HOME` and Python's per-user site directory all differ
between your login and the service account, and Python puts `~/.local/lib/pythonX.Y/site-packages`
AHEAD of `/usr/local/lib/...`, so a stale user-level install silently shadows the real one — for
you only.

**Why:** on 2026-09-15 I ran `which cidx` and `python3 -c "import code_indexer; print(__version__)"`
over SSH as my own user on a staging node, read 11.66.0 against a server running 12.58.0, and filed
a priority-1 bug claiming every indexing subprocess ran 92 minor versions behind — that staging had
been validating nothing on the indexing path for months. It was false. The server runs as a
dedicated service user; in THAT context the same `cidx` reported 12.58.0 from the same editable
checkout as the server. The 11.66.0 was a stale `pip install -e .` in my own home directory pointing
at an abandoned dev clone. The operator's reaction — "there should be ONE version of the software,
and if it does, then the CLI is the same as server" — was correct, and my measurement was the
artifact.

The trap is that every individual observation was true. `which cidx` really did resolve there, the
shebang really was `#!/usr/bin/python3`, that interpreter really did import 11.66.0. Only the
CONTEXT was wrong, and nothing in the output hints at that.

**How to apply:** before asserting anything about what a service runs, take the execution context
from the unit file, not from your shell:

```bash
systemctl show <svc> -p User -p Environment --value        # get the real user + PATH + HOME
sudo -u <service-user> env HOME=<unit HOME> PATH=<unit PATH> <command>
```

For a version-parity question specifically, compare the two interpreters directly and check whether
each is an editable pointer to the SAME tree (two editable installs at the same path are ONE
version, not a duplicate):

```bash
python -c "import pkg; print(pkg.__version__, pkg.__file__)"   # for each interpreter
```

Also sweep for shadowing duplicates rather than trusting one probe — an install can exist in the
system location, a pipx venv, and any user's `~/.local`, and `pip show` reports only the first one
that interpreter finds:

```bash
ls <each site-packages>/ | grep -i <pkg>                  # system, /usr/local, pipx venv, ~/.local
```

Related: [[feedback_grep_absence_is_not_evidence]] (one probe's spelling is not the capability),
[[feedback_study_anomalies_deeply]] (root-cause with facts), and
[[project_test_gates_flake_under_load]], whose own lesson is the same shape — I measured in a
polluted context and called load artifacts real regressions the same night.
