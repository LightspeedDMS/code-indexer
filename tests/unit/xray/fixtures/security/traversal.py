# mypy: ignore-errors
# Mixed safe + unsafe Path() and open() calls.
# Used by test_sandbox_real_world.py path traversal scanner scenario.
#
# Tainted (identifier argument — value not known statically):
#   p1 = Path(user_input)
#   p2 = Path(USER_PATH)       <- Name even though looks like a constant; demonstrates
#                                  the limitation of pure AST analysis without dataflow
#   file1 = open(filename)
#
# Safe (string literal argument — value known at parse time):
#   p3 = Path("/etc/passwd")
#   p4 = Path("/static/config.yaml")
#   file2 = open("/var/log/app.log")
#   file3 = open("/static/file.txt")
from pathlib import Path

# Tainted (Name argument)
p1 = Path(user_input)  # noqa: F821
p2 = Path(USER_PATH)  # noqa: F821  NOTE: AST-only analysis cannot tell USER_PATH is constant
file1 = open(filename)  # noqa: F821

# Safe (Constant argument)
p3 = Path("/etc/passwd")
p4 = Path("/static/config.yaml")
file2 = open("/var/log/app.log")
file3 = open("/static/file.txt")
