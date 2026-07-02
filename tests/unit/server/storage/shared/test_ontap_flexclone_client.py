"""
Unit tests for OntapFlexCloneClient.

The ONTAP REST API is not available in the test environment, so the
``requests`` library is mocked.  This is the minimum mocking required —
we cannot call a real ONTAP cluster in unit tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.shared.ontap_flexclone_client import (
    OntapFlexCloneClient,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> OntapFlexCloneClient:
    """A pre-configured client instance pointing at a test endpoint."""
    return OntapFlexCloneClient(
        endpoint="100.99.60.248",
        username="fsxadmin",
        password="secret",
        svm_name="sebaV2",
        parent_volume="seba_vol1",
        verify_ssl=False,
    )


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a minimal mock HTTP response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Endpoint normalisation
# ---------------------------------------------------------------------------


def test_endpoint_scheme_prepended_when_missing() -> None:
    """Bare hostname gets https:// prepended."""
    c = OntapFlexCloneClient(
        endpoint="100.99.60.248",
        username="u",
        password="p",
        svm_name="svm",
        parent_volume="vol",
    )
    assert c._endpoint == "https://100.99.60.248"


def test_endpoint_scheme_preserved_when_present() -> None:
    """Existing https:// scheme is not doubled."""
    c = OntapFlexCloneClient(
        endpoint="https://100.99.60.248",
        username="u",
        password="p",
        svm_name="svm",
        parent_volume="vol",
    )
    assert c._endpoint == "https://100.99.60.248"


# ---------------------------------------------------------------------------
# create_clone
# ---------------------------------------------------------------------------


def test_create_clone_sends_correct_post_body(client: OntapFlexCloneClient) -> None:
    """POST body must include svm.name, clone.parent_volume, is_flexclone, nas.path."""
    post_response = _mock_response({"job": {"uuid": "job-abc-123"}}, status_code=202)
    get_response = _mock_response(
        {"records": [{"uuid": "vol-uuid-999", "name": "cidx_clone_myrepo_1700000000"}]}
    )

    with (
        patch("requests.post", return_value=post_response) as mock_post,
        patch("requests.get", return_value=get_response),
    ):
        client.create_clone("cidx_clone_myrepo_1700000000")

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    body = kwargs["json"]

    assert body["svm"]["name"] == "sebaV2"
    assert body["clone"]["parent_volume"]["name"] == "seba_vol1"
    assert body["clone"]["is_flexclone"] is True
    assert body["nas"]["path"] == "/cidx_clone_myrepo_1700000000"
    assert body["name"] == "cidx_clone_myrepo_1700000000"


def test_create_clone_custom_junction_path(client: OntapFlexCloneClient) -> None:
    """Caller-supplied junction_path overrides the default."""
    post_response = _mock_response({"job": {"uuid": "job-xyz"}}, status_code=202)
    get_response = _mock_response(
        {"records": [{"uuid": "vol-uuid-001", "name": "cidx_clone_myrepo_1700000001"}]}
    )

    with (
        patch("requests.post", return_value=post_response),
        patch("requests.get", return_value=get_response),
    ):
        result = client.create_clone(
            "cidx_clone_myrepo_1700000001",
            junction_path="/custom/path",
        )

    assert result["name"] == "cidx_clone_myrepo_1700000001"


def test_create_clone_returns_uuid_name_job_uuid(client: OntapFlexCloneClient) -> None:
    """Return dict contains uuid, name, and job_uuid."""
    post_response = _mock_response({"job": {"uuid": "job-999"}}, status_code=202)
    get_response = _mock_response(
        {"records": [{"uuid": "vol-uuid-777", "name": "cidx_clone_test_111"}]}
    )

    with (
        patch("requests.post", return_value=post_response),
        patch("requests.get", return_value=get_response),
    ):
        result = client.create_clone("cidx_clone_test_111")

    assert result["uuid"] == "vol-uuid-777"
    assert result["name"] == "cidx_clone_test_111"
    assert result["job_uuid"] == "job-999"


def test_create_clone_no_job_in_response(client: OntapFlexCloneClient) -> None:
    """When ONTAP response has no 'job' key, job_uuid is empty string."""
    post_response = _mock_response({}, status_code=202)
    get_response = _mock_response(
        {"records": [{"uuid": "vol-uuid-555", "name": "cidx_clone_test_222"}]}
    )

    with (
        patch("requests.post", return_value=post_response),
        patch("requests.get", return_value=get_response),
    ):
        result = client.create_clone("cidx_clone_test_222")

    assert result["job_uuid"] == ""
    assert result["uuid"] == "vol-uuid-555"


# ---------------------------------------------------------------------------
# delete_clone
# ---------------------------------------------------------------------------


def test_delete_clone_two_step_get_then_delete(client: OntapFlexCloneClient) -> None:
    """delete_clone must GET to resolve UUID then DELETE by UUID."""
    get_response = _mock_response(
        {"records": [{"uuid": "vol-to-delete", "name": "cidx_clone_myrepo_9999"}]}
    )
    delete_response = _mock_response({}, status_code=202)

    with (
        patch("requests.get", return_value=get_response) as mock_get,
        patch("requests.delete", return_value=delete_response) as mock_delete,
    ):
        result = client.delete_clone("cidx_clone_myrepo_9999")

    assert result is True
    mock_get.assert_called_once()
    get_url = mock_get.call_args[0][0]
    assert "storage/volumes" in get_url
    assert mock_get.call_args[1]["params"]["name"] == "cidx_clone_myrepo_9999"

    mock_delete.assert_called_once()
    delete_url = mock_delete.call_args[0][0]
    assert "vol-to-delete" in delete_url


def test_delete_clone_idempotent_when_volume_not_found(
    client: OntapFlexCloneClient,
) -> None:
    """delete_clone returns True when the volume does not exist (idempotent)."""
    get_response = _mock_response({"records": []})

    with (
        patch("requests.get", return_value=get_response),
        patch("requests.delete") as mock_delete,
    ):
        result = client.delete_clone("cidx_clone_nonexistent_0000")

    assert result is True
    mock_delete.assert_not_called()


def test_delete_clone_returns_false_when_uuid_missing(
    client: OntapFlexCloneClient,
) -> None:
    """delete_clone returns False when the volume record has no uuid field."""
    get_response = _mock_response({"records": [{"name": "cidx_clone_no_uuid"}]})

    with (
        patch("requests.get", return_value=get_response),
        patch("requests.delete") as mock_delete,
    ):
        result = client.delete_clone("cidx_clone_no_uuid")

    assert result is False
    mock_delete.assert_not_called()


def test_delete_clone_propagates_delete_error(client: OntapFlexCloneClient) -> None:
    """delete_clone propagates HTTP errors from the DELETE call."""
    import requests as req_lib

    get_response = _mock_response(
        {"records": [{"uuid": "some-uuid", "name": "cidx_clone_err"}]}
    )
    delete_response = MagicMock()
    delete_response.raise_for_status.side_effect = req_lib.HTTPError("500 Server Error")

    with (
        patch("requests.get", return_value=get_response),
        patch("requests.delete", return_value=delete_response),
    ):
        with pytest.raises(req_lib.HTTPError):
            client.delete_clone("cidx_clone_err")


# ---------------------------------------------------------------------------
# get_volume_info
# ---------------------------------------------------------------------------


def test_get_volume_info_returns_none_when_no_records(
    client: OntapFlexCloneClient,
) -> None:
    """get_volume_info returns None when ONTAP reports zero records."""
    get_response = _mock_response({"records": []})

    with patch("requests.get", return_value=get_response):
        result = client.get_volume_info("nonexistent_vol")

    assert result is None


def test_get_volume_info_returns_first_record(client: OntapFlexCloneClient) -> None:
    """get_volume_info returns the first record dict."""
    record = {"uuid": "abc-123", "name": "my_vol", "state": "online"}
    get_response = _mock_response({"records": [record]})

    with patch("requests.get", return_value=get_response):
        result = client.get_volume_info("my_vol")

    assert result == record


def test_get_volume_info_sends_name_filter(client: OntapFlexCloneClient) -> None:
    """get_volume_info passes the volume name as a query parameter."""
    get_response = _mock_response({"records": []})

    with patch("requests.get", return_value=get_response) as mock_get:
        client.get_volume_info("target_vol")

    params = mock_get.call_args[1]["params"]
    assert params["name"] == "target_vol"


# ---------------------------------------------------------------------------
# list_clones
# ---------------------------------------------------------------------------


def test_list_clones_filters_by_prefix(client: OntapFlexCloneClient) -> None:
    """list_clones returns only volumes whose name starts with the prefix."""
    records = [
        {"uuid": "u1", "name": "cidx_clone_repo_a_111"},
        {"uuid": "u2", "name": "cidx_clone_repo_b_222"},
        {"uuid": "u3", "name": "seba_vol1"},  # should be excluded
        {"uuid": "u4", "name": "other_volume"},  # should be excluded
    ]
    get_response = _mock_response({"records": records})

    with patch("requests.get", return_value=get_response):
        result = client.list_clones()

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert names == {"cidx_clone_repo_a_111", "cidx_clone_repo_b_222"}


def test_list_clones_custom_prefix(client: OntapFlexCloneClient) -> None:
    """list_clones respects a caller-supplied prefix."""
    records = [
        {"uuid": "u1", "name": "custom_prefix_abc"},
        {"uuid": "u2", "name": "cidx_clone_xyz"},
    ]
    get_response = _mock_response({"records": records})

    with patch("requests.get", return_value=get_response):
        result = client.list_clones(prefix="custom_prefix_")

    assert len(result) == 1
    assert result[0]["name"] == "custom_prefix_abc"


def test_list_clones_empty_when_no_matching_volumes(
    client: OntapFlexCloneClient,
) -> None:
    """list_clones returns empty list when no volumes match the prefix."""
    get_response = _mock_response({"records": [{"uuid": "u1", "name": "seba_vol1"}]})

    with patch("requests.get", return_value=get_response):
        result = client.list_clones()

    assert result == []


# ---------------------------------------------------------------------------
# get_clone_count
# ---------------------------------------------------------------------------


def test_get_clone_count_returns_zero_when_no_clones(
    client: OntapFlexCloneClient,
) -> None:
    """get_clone_count returns 0 when no CIDX clones exist."""
    get_response = _mock_response({"records": []})

    with patch("requests.get", return_value=get_response):
        count = client.get_clone_count()

    assert count == 0


def test_get_clone_count_returns_correct_count(client: OntapFlexCloneClient) -> None:
    """get_clone_count returns the number of cidx_clone_ volumes."""
    records = [
        {"uuid": "u1", "name": "cidx_clone_repo_a_111"},
        {"uuid": "u2", "name": "cidx_clone_repo_b_222"},
        {"uuid": "u3", "name": "cidx_clone_repo_c_333"},
    ]
    get_response = _mock_response({"records": records})

    with patch("requests.get", return_value=get_response):
        count = client.get_clone_count()

    assert count == 3


# ---------------------------------------------------------------------------
# DEFAULT_CLONE_PREFIX class constant
# ---------------------------------------------------------------------------


def test_default_clone_prefix_constant() -> None:
    """DEFAULT_CLONE_PREFIX has the expected project-convention value."""
    assert OntapFlexCloneClient.DEFAULT_CLONE_PREFIX == "cidx_clone_"
