# mypy: ignore-errors
# Mixed safe + unsafe string concatenation.
# Used by test_sandbox_real_world.py concatenation injection scanner scenario.
#
# Tainted (binary_operator with identifier descendant):
#   query1  — 'SELECT ...' + user_id        (direct identifier child)
#   log1    — 'Hello ' + name + '!'         (nested: outer has binary_operator child
#                                            which has direct identifier child)
#   msg1    — 'Path: ' + filepath           (direct identifier child)
#
# Safe (no binary_operator RHS, or RHS is a pure string constant):
#   query2  — pure string constant
#   log2    — pure string constant
#   msg2    — pure string constant

# Tainted
query1 = "SELECT * FROM users WHERE id=" + user_id  # noqa: F821
log1 = "Hello " + name + "!"  # noqa: F821
msg1 = "Path: " + filepath  # noqa: F821

# Safe
query2 = "SELECT * FROM users WHERE id=42"
log2 = "Hello world"
msg2 = "Static path: /var/log"
