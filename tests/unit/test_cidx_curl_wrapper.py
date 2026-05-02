"""Aggregator module for cidx-curl.sh wrapper tests (Story #929 Item #2a).

Running `pytest tests/unit/test_cidx_curl_wrapper.py` discovers all wrapper
tests through this module. The actual test logic lives in four focused modules
split per MESSI rule 6 (modules >500 lines) and clean-code class-size rules:

  test_cidx_curl_wrapper_sanity.py      — file presence and runtime deps
  test_cidx_curl_wrapper_validation.py  — banned flags, scheme, port, userinfo
  test_cidx_curl_wrapper_cidrs.py       — loopback, operator CIDRs, IPv6, config
  test_cidx_curl_wrapper_dns_rebinding.py — --resolve pin injection

pytest discovers classes imported into this namespace automatically.
"""

from tests.unit.test_cidx_curl_wrapper_sanity import (
    TestWrapperFilePresence,
    TestWrapperRuntimeDependencies,
)
from tests.unit.test_cidx_curl_wrapper_validation import (
    TestAllowedFlagsAccepted,
    TestAtSignPrefixRejected,
    TestInvalidPortRejected,
    TestMultiUrlRejected,
    TestOutputPathRestricted,
    TestRejectedFlagsBlocked,
    TestSchemeAndUrlValidation,
    TestUserinfoBypassRejected,
)
from tests.unit.test_cidx_curl_wrapper_cidrs import (
    TestDecimalIpEncodingRejected,
    TestGracefulConfigDegradation,
    TestIPv6Handling,
    TestLoopbackAlwaysOn,
    TestOperatorCidrExtendsLoopback,
    TestPublicNetworkRejected,
    TestUrlWithComplexPath,
)
from tests.unit.test_cidx_curl_wrapper_dns_rebinding import (
    TestDnsRebindingMitigation,
)

__all__ = [
    "TestWrapperFilePresence",
    "TestWrapperRuntimeDependencies",
    "TestAllowedFlagsAccepted",
    "TestAtSignPrefixRejected",
    "TestInvalidPortRejected",
    "TestMultiUrlRejected",
    "TestOutputPathRestricted",
    "TestRejectedFlagsBlocked",
    "TestSchemeAndUrlValidation",
    "TestUserinfoBypassRejected",
    "TestDecimalIpEncodingRejected",
    "TestGracefulConfigDegradation",
    "TestIPv6Handling",
    "TestLoopbackAlwaysOn",
    "TestOperatorCidrExtendsLoopback",
    "TestPublicNetworkRejected",
    "TestUrlWithComplexPath",
    "TestDnsRebindingMitigation",
]
