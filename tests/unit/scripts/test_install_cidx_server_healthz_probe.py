"""The installer's post-start health check polls the unauthenticated
``GET /healthz`` endpoint, never ``GET /docs``.

Health checks use the unauthenticated /healthz endpoint; /docs requires login
(it answers an unauthenticated caller with a 303 redirect to /login), so a
probe that waits for ``/docs`` to return 200 can never succeed.

The script is sourced in a separate bash process (its ``main`` only runs when
executed directly) and ``start_and_verify_server`` is called with the three
external commands it touches -- ``sudo``, ``curl`` and ``sleep`` -- replaced by
shell functions. The default ``curl`` stand-in answers the way the real server
does: 200 for ``/healthz`` and 303 for ``/docs``.
"""

import subprocess
from pathlib import Path
from typing import NamedTuple

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "install-cidx-server.sh"
PORT = "8765"
HEALTHZ_PROBE = f"curl http://localhost:{PORT}/healthz"

# Records every probed URL (the last argument the script passes to curl) and
# answers like a real server whose API docs require login.
_STUBS = r"""
sudo() { echo "sudo $*" >> "${PROBE_LOG}"; }
sleep() { :; }
curl() {
    local url="${!#}"
    echo "curl ${url}" >> "${PROBE_LOG}"
    case "${url}" in
        */healthz) printf '200' ;;
        */docs) printf '303' ;;
        *) printf '404' ;;
    esac
}
"""

# Overrides the default curl: the node never reports serviceable.
_UNHEALTHY_CURL = r"""
curl() { echo "curl ${!#}" >> "${PROBE_LOG}"; printf '503'; }
"""


class _Run(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    probes: str


def _run_start_and_verify(tmp_path: Path, dry_run: bool, extra_stubs: str = "") -> _Run:
    probe_log = tmp_path / "probe.log"
    probe_log.touch()
    code = (
        f"source '{SCRIPT_PATH}'; {_STUBS}{extra_stubs}\n"
        f"PORT={PORT}; DRY_RUN={'true' if dry_run else 'false'}; "
        "start_and_verify_server"
    )
    result = subprocess.run(
        ["bash", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PROBE_LOG": str(probe_log),
        },
    )
    return _Run(result.returncode, result.stdout, result.stderr, probe_log.read_text())


class TestInstallerHealthCheckProbesHealthz:
    def test_health_check_passes_against_server_whose_docs_require_login(
        self, tmp_path
    ):
        run = _run_start_and_verify(tmp_path, dry_run=False)

        assert run.returncode == 0, run.stdout + run.stderr
        assert HEALTHZ_PROBE in run.probes.splitlines()
        assert "/docs" not in run.probes
        assert "Health check PASS: GET /healthz returned 200" in run.stdout

    def test_dry_run_announces_healthz_probe(self, tmp_path):
        run = _run_start_and_verify(tmp_path, dry_run=True)

        assert run.returncode == 0, run.stdout + run.stderr
        assert f"poll http://localhost:{PORT}/healthz for HTTP 200" in run.stdout
        assert "/docs" not in run.stdout
        assert run.probes == ""

    def test_health_check_fails_when_healthz_never_answers_200(self, tmp_path):
        """The probe still fails loud (same 30 s budget) when /healthz never
        answers 200 -- e.g. the node reports unhealthy (503)."""
        run = _run_start_and_verify(
            tmp_path, dry_run=False, extra_stubs=_UNHEALTHY_CURL
        )

        assert run.returncode != 0
        assert (
            f"Health check FAIL: GET http://localhost:{PORT}/healthz did not "
            "return 200 within 30s (last code: 503)"
        ) in run.stderr
        probed = [line for line in run.probes.splitlines() if line.startswith("curl ")]
        assert len(probed) == 15  # 30 s budget polled every 2 s
        assert set(probed) == {HEALTHZ_PROBE}
