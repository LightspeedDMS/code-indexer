"""The SDK's own version (semver). Read by hatch at build time."""

# Bump rules (semver): MAJOR for a breaking change to any public name, MINOR
# for an addition (a new capability name, hook or DTO field with a default),
# PATCH for a fix that changes no interface. While MAJOR is 0, a MINOR bump is
# treated as breaking, so add-ons should pin ``>=0.N,<0.N+1``.
__version__ = "0.7.0"
