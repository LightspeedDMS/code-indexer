"""issue-64616: the golden catalog sync -- naming, content identity, the
migration plan and its order, dry run, single-flight, and never deleting."""

from __future__ import annotations

import itertools
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from code_indexer.server.services import golden_catalog_sync as sync
from code_indexer.server.services.golden_catalog_sync import (
    CatalogProject,
    ContentIdentity,
    Registered,
    Rename,
    SyncDeps,
    catalog_aliases,
    content_identity,
    is_legacy_alias,
    plan_sync,
    run_sync,
)
from code_indexer.server.services.job_tracker import DuplicateJobError

HOST = "gitlab.example.com"
GROUP = "acme/private"
GOLDEN = "/data/golden-repos"
PREFIXES = [f"{HOST}/{GROUP}/"]


def project(path: str, branch: str = "master") -> CatalogProject:
    full = f"{GROUP}/{path}"
    return CatalogProject(
        f"{HOST}/{full}".casefold(), f"https://{HOST}/{full}.git", full, branch
    )


def catalog_of(*projects: CatalogProject) -> Dict[str, CatalogProject]:
    return {p.identity: p for p in projects}


def row(
    alias: str,
    content: Optional[CatalogProject],
    repo_url: Optional[str] = None,
    branch: str = "master",
    heal_to: Optional[str] = None,
) -> Registered:
    identity = ContentIdentity(
        content.identity if content else None,
        content.url if content else None,
        branch,
        heal_to,
        "" if content else "no reflog",
    )
    return Registered(
        alias, repo_url or (content.url if content else "https://x/y.git"), identity
    )


def plan(
    catalog: Dict[str, CatalogProject], rows: Sequence[Registered], max_adds: int = 50
):
    return plan_sync(
        catalog, rows, GOLDEN, managed_prefixes=PREFIXES, max_adds=max_adds
    )


# -- naming ------------------------------------------------------------------------


def test_a_unique_leaf_stays_bare_and_a_shared_one_takes_its_shortest_suffix() -> None:
    names = catalog_aliases(
        [
            "acme/private/ideas/innovation-week/example-repo",
            "acme/private/conversion/example-repo",
            "acme/private/tools/acm",
        ]
    )
    assert names == {
        "acme/private/ideas/innovation-week/example-repo": "innovation-week-example-repo",
        "acme/private/conversion/example-repo": "conversion-example-repo",
        "acme/private/tools/acm": "acm",
    }


def test_names_do_not_depend_on_order() -> None:
    paths = [
        "g/a/x/svc",
        "g/b/x/svc",
        "g/c/svc",
        "g/svc-tools",
        "g/d/svc.js",
    ]
    expected = catalog_aliases(paths)
    for permutation in itertools.permutations(paths):
        assert catalog_aliases(list(permutation)) == expected
    assert expected["g/a/x/svc"] == "a-x-svc"
    assert expected["g/b/x/svc"] == "b-x-svc"
    assert expected["g/c/svc"] == "c-svc"
    assert expected["g/d/svc.js"] == "svc-js"
    assert not any(name[-1].isdigit() and "-" in name for name in expected.values())


def test_a_joined_name_that_clashes_with_a_leaf_grows_further() -> None:
    names = catalog_aliases(["g/a-b", "g/x/a/b", "g/y/a/b"])
    assert len(set(n.casefold() for n in names.values())) == 3
    assert names["g/a-b"] == "a-b"
    assert names["g/x/a/b"] == "x-a-b" and names["g/y/a/b"] == "y-a-b"


def test_a_name_ending_in_global_is_suffixed() -> None:
    assert catalog_aliases(["g/thing-global"]) == {
        "g/thing-global": "thing-global-repo"
    }


@pytest.mark.parametrize(
    "alias, leaf, legacy",
    [
        ("example-repo-2", "example-repo", True),
        ("example-repo-13", "example-repo", True),
        ("example-repo-1", "example-repo", False),
        ("example-repo", "example-repo", False),
        ("conversion-example-repo", "example-repo", False),
        ("api-v2", "api", False),
    ],
)
def test_legacy_aliases(alias: str, leaf: str, legacy: bool) -> None:
    assert is_legacy_alias(alias, leaf) is legacy


# -- content identity ------------------------------------------------------------------


class Git:
    """A clone as the git seam sees it. ``ahead`` is HEAD's commits not on
    ``refs/remotes/origin/<b>``: ``stale_ahead`` before a fetch of origin,
    ``ahead`` after it (a repointed clone's tracking ref is stale until
    fetched)."""

    def __init__(
        self,
        origin: str,
        branch: Optional[str] = "master",
        ahead: int = 0,
        reflog: Optional[List[str]] = None,
        stale_ahead: Optional[int] = None,
        fetch_error: Optional[Exception] = None,
        unborn: bool = False,
    ) -> None:
        self.origin, self.branch, self.ahead = origin, branch, ahead
        self.reflog = reflog or []
        self.stale_ahead = ahead if stale_ahead is None else stale_ahead
        self.fetch_error = fetch_error
        self.unborn = unborn
        self.fetched: List[Optional[str]] = []

    def rev_parse(self, path: str, *args: str, timeout: Any = None) -> str:
        if self.unborn:
            # what git (and git-mcp's typed error) says for an empty clone
            raise subprocess.CalledProcessError(
                128,
                ["git", "rev-parse", "HEAD"],
                stderr="fatal: ambiguous argument 'HEAD': unknown revision",
            )
        return "0123456789abcdef"

    def remote_get_url(self, path: str, name: str, timeout: Any = None) -> str:
        return self.origin

    def symbolic_ref(self, path: str, short: bool = True, timeout: Any = None) -> str:
        if self.branch is None:
            raise subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: ref HEAD is not a symbolic ref"
            )
        return self.branch

    def fetch(self, path: str, remote: str = "origin", **kwargs: Any) -> None:
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetched.append(kwargs.get("ref"))

    def rev_list_count(self, path: str, spec: str, timeout: Any = None) -> int:
        return self.ahead if self.fetched else self.stale_ahead

    def run(
        self, path: str, args: Sequence[str], timeout: Any = None, identity: Any = None
    ):
        return subprocess.CompletedProcess(args, 0, "\n".join(self.reflog), "")

    def push(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("never")


A = f"https://{HOST}/{GROUP}/ideas/innovation-week/example-repo.git"
B = f"https://{HOST}/{GROUP}/conversion/example-repo.git"
A_ID = f"{HOST}/{GROUP}/ideas/innovation-week/example-repo".casefold()
B_ID = f"{HOST}/{GROUP}/conversion/example-repo".casefold()


def test_a_clone_never_repointed_is_its_origins_without_a_fetch() -> None:
    git = Git(B, reflog=["pull: x", f"clone: from {B}"])
    found = content_identity(git, "/c")
    assert (found.identity, found.heal_to, found.branch) == (B_ID, None, "master")
    assert git.fetched == []


def test_content_that_followed_a_repointed_origin_is_origins() -> None:
    """The tools-test acm shape: cloned from A, repointed to B and reset."""
    git = Git(B, ahead=0, reflog=[f"clone: from {A}"])
    found = content_identity(git, "/c")
    assert (found.identity, found.heal_to) == (B_ID, None)
    assert git.fetched == ["master"]


def test_content_that_did_not_follow_origin_is_its_clone_source_and_heals() -> None:
    found = content_identity(
        Git(B, ahead=3, reflog=["reset: x", f"clone: from {A}"]), "/c"
    )
    assert (found.identity, found.heal_to) == (A_ID, A)


def test_a_stale_tracking_ref_never_decides() -> None:
    """Repointed to B but never fetched: refs/remotes/origin/master is still
    A's, so HEAD looks 'followed' -- until origin is actually fetched."""
    git = Git(B, stale_ahead=0, ahead=4, reflog=[f"clone: from {A}"])
    found = content_identity(git, "/c")
    assert (found.identity, found.heal_to) == (A_ID, A)
    assert git.fetched == ["master"]


def test_a_failed_fetch_is_unknown() -> None:
    git = Git(B, reflog=[f"clone: from {A}"], fetch_error=RuntimeError("unreachable"))
    found = content_identity(git, "/c")
    assert found.identity is None and "could not be determined" in found.why_unknown


def test_content_that_cannot_be_determined_is_unknown() -> None:
    found = content_identity(Git(B, branch=None, reflog=["commit: x"]), "/c")
    assert found.identity is None and "HEAD is detached" in found.why_unknown


def test_an_empty_clone_is_its_origins_without_a_probe() -> None:
    """An empty repository (unborn HEAD): no content, nothing to follow --
    the clone holds its origin; the probe (which errors there) never runs."""
    git = Git(B, unborn=True, fetch_error=RuntimeError("would error"))
    found = content_identity(git, "/c")
    assert (found.identity, found.heal_to) == (B_ID, None)
    assert git.fetched == []


def test_an_empty_clone_whose_origin_was_repointed_is_its_clone_sources() -> None:
    git = Git(B, unborn=True, reflog=[f"clone: from {A}"], fetch_error=RuntimeError())
    found = content_identity(git, "/c")
    assert (found.identity, found.heal_to) == (A_ID, A)


def test_a_rev_parse_failure_that_is_not_unborn_is_unknown() -> None:
    class Broken(Git):
        def rev_parse(self, path: str, *args: str, timeout: Any = None) -> str:
            raise subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: bad object"
            )

    found = content_identity(Broken(B, reflog=[f"clone: from {A}"]), "/c")
    assert found.identity is None and "git read failed" in found.why_unknown


def test_an_empty_registered_clone_matches_its_catalog_entry() -> None:
    """The tools-test empty repos: neither unknown nor a name conflict."""
    empty = project("data/jar-project")
    found = content_identity(Git(empty.url, unborn=True), "/c")
    result = plan(catalog_of(empty), [Registered("jar-project", empty.url, found)])
    assert result.unknown == {} and result.conflicts == []
    assert result.adds == [] and result.renames == []


# -- the plan ----------------------------------------------------------------------------

EVO_IW = project("ideas/innovation-week/example-repo", "development")
EVO_CONV = project("conversion/example-repo", "master")
ACM = project("tools/acm")


def test_the_evolution_rows_are_fixed() -> None:
    """example-repo-2's row says innovation-week but its clone holds
    conversion/example-repo@master; example-repo-3 holds innovation-week."""
    result = plan(
        catalog_of(EVO_IW, EVO_CONV, ACM),
        [
            row("example-repo-2", EVO_CONV, repo_url=EVO_IW.url, branch="master"),
            row("example-repo-3", EVO_IW, branch="development"),
            row("acm", ACM),
        ],
    )
    assert [(r.old, r.new, r.repo_url, r.branch) for r in result.renames] == [
        ("example-repo-2", "conversion-example-repo", EVO_CONV.url, "master"),
        ("example-repo-3", "innovation-week-example-repo", None, None),
    ]
    assert result.renames[0].reason == "misassigned"
    assert result.adds == [] and result.conflicts == []


def test_an_alias_already_right_for_its_content_keeps_its_name() -> None:
    result = plan(
        catalog_of(ACM, EVO_CONV),
        [row("acm", ACM), row("my-hand-named-example-repo", EVO_CONV)],
    )
    assert result.renames == [] and result.adds == []


def test_seeder_in_place_rows_keep_their_file_url_form() -> None:
    result = plan(
        catalog_of(EVO_IW, EVO_CONV),
        [row("example-repo-3", EVO_IW, repo_url=f"file://{GOLDEN}/example-repo-3")],
    )
    assert result.renames[0].repo_url == f"file://{GOLDEN}/innovation-week-example-repo"
    assert result.renames[0].branch == "master", "the checked-out branch"


def test_renames_free_a_name_before_it_is_taken() -> None:
    """'example-repo' holds conversion content; its name is innovation-week's
    target only after it moves -- order: free it first."""
    only_conv = project("conversion/example-repo")
    only_iw = project("ideas/example-repo")
    result = plan(
        catalog_of(only_conv, only_iw),
        [
            row("example-repo-2", only_iw),
            row("example-repo", only_conv, repo_url=only_iw.url),
        ],
    )
    names = catalog_aliases([only_conv.path, only_iw.path])
    order = [(r.old, r.new) for r in result.renames]
    assert order == [
        ("example-repo", names[only_conv.path]),
        ("example-repo-2", names[only_iw.path]),
    ] or all(new != "example-repo" for _, new in order)
    taken = {"example-repo", "example-repo-2"}
    for old, new in order:
        assert new not in taken, f"{new} taken when {old} is renamed"
        taken.discard(old)
        taken.add(new)


def test_a_rename_cycle_is_a_conflict_not_a_half_rename() -> None:
    first, second = project("x/a"), project("y/a")
    names = catalog_aliases([first.path, second.path])
    # rows hold each other's target names and are misassigned
    result = plan(
        catalog_of(first, second),
        [
            row(names[second.path], first, repo_url=second.url),
            row(names[first.path], second, repo_url=first.url),
        ],
    )
    assert result.renames == []
    assert len(result.conflicts) == 2


def test_new_projects_are_added_and_bounded_per_pass() -> None:
    projects = [project(f"svc/p{i}") for i in range(5)]
    result = plan(catalog_of(*projects), [], max_adds=2)
    assert [a for a, _ in result.adds] == ["p0", "p1"]
    assert result.deferred_adds == ["p2", "p3", "p4"]


def test_an_add_never_takes_a_registered_name() -> None:
    result = plan(catalog_of(ACM), [row("acm", project("other/thing"))])
    assert result.adds == []
    assert "taken" in result.conflicts[0]


def test_vanished_projects_are_reported_never_removed() -> None:
    gone = project("old/retired")
    elsewhere = CatalogProject("github.com/x/y", "https://github.com/x/y.git", "x/y")
    result = plan(
        catalog_of(ACM), [row("acm", ACM), row("retired", gone), row("y", elsewhere)]
    )
    assert result.vanished == [("retired", gone.identity)]
    assert "retired" not in str(result.renames) and result.adds == []


def test_duplicates_and_unknowns_are_reported() -> None:
    result = plan(
        catalog_of(ACM),
        [row("acm", ACM), row("acm-2", ACM), row("mystery", None)],
    )
    assert result.duplicates == {ACM.identity: ["acm", "acm-2"]}
    assert result.unknown == {"mystery": "no reflog"}


def test_a_repointed_origin_is_healed_first() -> None:
    result = plan(catalog_of(EVO_IW), [row("example-repo-3", EVO_IW, heal_to=EVO_IW.url)])
    assert result.heals == [("example-repo-3", EVO_IW.url)]


# -- the pass ----------------------------------------------------------------------------


class Tracker:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.done: List[Tuple[str, Any]] = []

    def register_job_if_no_conflict(self, **job: Any) -> None:
        if self.busy:
            raise DuplicateJobError(
                "golden_catalog_sync", "__golden_catalog_sync__", "j0"
            )

    def complete_job(self, job_id: str, result: Any = None) -> None:
        self.done.append(("complete", result))

    def fail_job(self, job_id: str, error: str = "") -> None:
        self.done.append(("fail", error))


class Recorder:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def rename(self, step: Rename) -> None:
        self.calls.append(("rename", f"{step.old}->{step.new}"))

    def add(self, alias: str, project: CatalogProject) -> str:
        self.calls.append(("add", alias))
        return "job"

    def set_origin(self, alias: str, url: str) -> None:
        self.calls.append(("heal", alias))


def deps(
    recorder: Recorder,
    tracker: Optional[Tracker] = None,
    catalog: Any = None,
) -> SyncDeps:
    rows = [
        {"alias": "example-repo-2", "repo_url": EVO_IW.url, "clone_path": "/c/e2"},
        {"alias": "cidx-meta", "repo_url": "local://cidx-meta"},
        {"alias": "ws-0123abcd", "repo_url": "file:///w"},
    ]
    gits = {"/c/e2": Git(B, ahead=0)}

    class RoutingGit(Git):
        def __init__(self) -> None:
            pass

        def __getattribute__(self, name: str) -> Any:
            if name in (
                "remote_get_url",
                "symbolic_ref",
                "rev_list_count",
                "run",
                "fetch",
                "rev_parse",
            ):
                return lambda path, *a, **k: getattr(gits[path], name)(path, *a, **k)
            return object.__getattribute__(self, name)

    return SyncDeps(
        golden_repos_dir=GOLDEN,
        list_rows=lambda: rows,
        git=RoutingGit(),
        load_catalog=catalog or (lambda: catalog_of(EVO_IW, EVO_CONV)),
        managed_prefixes=PREFIXES,
        rename_repo=recorder.rename,
        add=recorder.add,
        set_origin=recorder.set_origin,
        job_tracker=tracker,
    )


def test_a_dry_run_changes_nothing_and_reports_the_plan() -> None:
    recorder, tracker = Recorder(), Tracker()
    result = run_sync(deps(recorder, tracker), "dry-run")
    assert recorder.calls == []
    report = result.as_dict()["plan"]
    assert report["renames"][0]["new"] == "conversion-example-repo"
    assert [a["alias"] for a in report["adds"]] == ["innovation-week-example-repo"]
    assert tracker.done[0][0] == "complete"


def test_apply_heals_then_renames_then_adds() -> None:
    recorder = Recorder()
    result = run_sync(deps(recorder, Tracker()), "apply")
    assert recorder.calls == [
        ("rename", "example-repo-2->conversion-example-repo"),
        ("add", "innovation-week-example-repo"),
    ]
    assert result.applied["failed"] == {}


def test_a_pass_already_running_elsewhere_is_skipped() -> None:
    recorder = Recorder()
    result = run_sync(deps(recorder, Tracker(busy=True)), "apply")
    assert result.single_flight_skipped and recorder.calls == []


def test_a_catalog_failure_changes_nothing() -> None:
    recorder, tracker = Recorder(), Tracker()

    def broken() -> Dict[str, CatalogProject]:
        raise RuntimeError("GitLab group 'acme/private' listing failed: HTTP 502")

    result = run_sync(deps(recorder, tracker, broken), "apply")
    assert recorder.calls == [] and "502" in (result.error or "")
    assert tracker.done[0][0] == "fail"


def test_a_failed_rename_skips_only_the_renames_that_need_its_name() -> None:
    plan_ = sync.SyncPlan(
        renames=[
            Rename("a-2", "x-a"),
            Rename("b", "y-b"),  # independent
            Rename("c-2", "a-2"),  # needs a-2's name, still held
            Rename("d-2", "c-2"),  # needs c-2's name, still held in turn
        ],
        adds=[("new", project("z/new"))],
    )
    calls: List[str] = []

    def rename(step: Rename) -> None:
        calls.append(step.old)
        if step.old == "a-2":
            raise RuntimeError("busy")

    deps_ = SyncDeps(
        GOLDEN, list, None, dict, PREFIXES, rename, lambda a, p: "j", lambda a, u: None
    )
    applied = sync.apply_plan(deps_, plan_)
    assert calls == ["a-2", "b"]
    assert sorted(applied["skipped"]) == ["rename:c-2", "rename:d-2"]
    assert list(applied["added"]) == ["new"]


def test_off_runs_nothing() -> None:
    assert run_sync(deps(Recorder()), "off").error


def test_nothing_in_the_sync_can_delete_a_repository() -> None:
    """Never-delete: the pass has no removal step at all."""
    assert not any(
        "remove" in f or "delete" in f for f in SyncDeps.__dataclass_fields__
    )


# -- the GitLab listing -----------------------------------------------------------------


class Response:
    def __init__(self, status: int, items: Any, pages: Optional[str] = None) -> None:
        self.status_code = status
        self._items = items
        self.headers = {"x-total-pages": pages} if pages else {}

    def json(self) -> Any:
        return self._items


def test_the_group_listing_pages_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    pages = {
        1: [
            {"http_url_to_repo": A, "default_branch": "development"},
            {"http_url_to_repo": f"https://{HOST}/{GROUP}/old.git", "archived": True},
            {
                "http_url_to_repo": f"https://{HOST}/{GROUP}/m.git",
                "marked_for_deletion_on": "x",
            },
        ],
        2: [
            {"http_url_to_repo": f"https://{HOST}/{GROUP}/d-deletion_scheduled-12.git"},
            {"http_url_to_repo": B, "default_branch": "master"},
        ],
    }
    seen: List[Dict[str, Any]] = []

    def get(url: str, params: Dict[str, Any], headers: Dict[str, str], timeout: float):
        seen.append(params)
        assert headers == {"PRIVATE-TOKEN": "tok"}
        assert "/groups/acme%2Fprivate/projects" in url
        return Response(200, pages[params["page"]], "2")

    monkeypatch.setattr(httpx, "get", get)
    projects = sync.GitLabGroupLister(HOST, "tok").projects(GROUP)
    assert [p.url for p in projects] == [A, B]
    assert projects[0].default_branch == "development"
    assert seen[0]["include_subgroups"] == "true" and seen[0]["archived"] == "false"


def test_a_failed_group_listing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response(502, None))
    with pytest.raises(RuntimeError, match="502"):
        sync.GitLabGroupLister(HOST, "tok").projects(GROUP)


def test_groups_without_a_token_refuse_and_explicit_urls_are_checked() -> None:
    with pytest.raises(RuntimeError, match="no GitLab token"):
        sync.load_catalog([GROUP], [], None)
    with pytest.raises(RuntimeError, match="not a git remote URL"):
        sync.load_catalog([], ["/not/a/url"], None)
    catalog = sync.load_catalog([], ["https://u:secret@github.com/acme/tool.git"], None)
    (only,) = catalog.values()
    assert only.url == "https://github.com/acme/tool.git" and only.leaf == "tool"


def test_the_mode_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sync.MODE_ENV, raising=False)
    assert sync.sync_mode() == "off"
    monkeypatch.setenv(sync.MODE_ENV, "Apply")
    assert sync.sync_mode() == "apply"
    monkeypatch.setenv(sync.MODE_ENV, "yes")
    assert sync.sync_mode() == "off"


def test_the_scheduled_pass_does_nothing_while_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import catalog_sync_scheduling as scheduling

    monkeypatch.delenv(sync.MODE_ENV, raising=False)
    assert scheduling._catalog_pass(SimpleNamespace()) == {"mode": "off"}


# -- empty projects and failed adds (issue-64616 apply-pass failures) ------------------

NOW = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def test_the_group_listing_marks_empty_projects_and_keeps_upstream_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    items = [
        {
            "http_url_to_repo": A,
            "default_branch": None,
            "empty_repo": True,
            "last_activity_at": "2026-10-20T10:00:00.000Z",
        },
        {"http_url_to_repo": B, "default_branch": "main", "empty_repo": False},
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response(200, items, "1"))
    empty, full = sync.GitLabGroupLister(HOST, "tok").projects(GROUP)
    assert empty.empty and empty.default_branch is None
    assert empty.last_activity_at == "2026-10-20T10:00:00.000Z"
    assert not full.empty and full.default_branch == "main"


def test_an_empty_project_is_never_added_but_still_shapes_names() -> None:
    empty = replace(EVO_IW, empty=True, default_branch=None)
    result = plan(catalog_of(empty, EVO_CONV), [])
    assert [a for a, _ in result.adds] == ["conversion-example-repo"]
    assert result.skipped_adds == {
        "innovation-week-example-repo": sync.EMPTY_PROJECT_REASON
    }
    assert result.as_dict()["skipped_adds"] == result.skipped_adds


def test_a_registered_empty_clone_is_still_renamed_to_its_catalog_name() -> None:
    empty = replace(EVO_CONV, empty=True, default_branch=None)
    result = plan(catalog_of(empty, EVO_IW), [row("example-repo-2", empty)])
    assert [(r.old, r.new) for r in result.renames] == [
        ("example-repo-2", "conversion-example-repo")
    ]


class History:
    def __init__(self, jobs: List[Dict[str, Any]], broken: bool = False) -> None:
        self.jobs, self.broken = jobs, broken
        self.queries: List[Dict[str, Any]] = []

    def query_jobs(self, **kw: Any) -> List[Dict[str, Any]]:
        self.queries.append(kw)
        if self.broken:
            raise RuntimeError("store down")
        return self.jobs


def add_job(
    alias: str, status: str, at: datetime, url: str = A, error: str = "boom"
) -> Dict[str, Any]:
    return {
        "job_id": f"j-{alias}-{status}",
        "repo_alias": alias,
        "status": status,
        "metadata": {"repo_url": url},
        "completed_at": at.isoformat(),
        "created_at": at.isoformat(),
        "error": f"Git clone failed with code 1\n{error}",
    }


IW = "innovation-week-example-repo"


def holds(*jobs: Dict[str, Any], broken: bool = False) -> Any:
    return sync.failed_add_holds(History(list(jobs), broken), now=NOW, retry_after=DAY)


def test_an_add_that_just_failed_is_held_with_its_error() -> None:
    hold = holds(add_job(IW, "failed", NOW - timedelta(minutes=15), error="no master"))
    reason = hold(IW, EVO_IW)
    assert reason and "no master" in reason and "j-" in reason
    result = plan_sync(
        catalog_of(EVO_IW, EVO_CONV),
        [],
        GOLDEN,
        managed_prefixes=PREFIXES,
        max_adds=5,
        hold_add=hold,
    )
    assert [a for a, _ in result.adds] == ["conversion-example-repo"]
    assert result.skipped_adds == {IW: reason}


@pytest.mark.parametrize(
    "jobs, project_",
    [
        # the newest job for the alias succeeded
        (
            [
                add_job(IW, "completed", NOW - timedelta(minutes=5)),
                add_job(IW, "failed", NOW - timedelta(hours=1)),
            ],
            EVO_IW,
        ),
        # the failure is older than the retry window
        ([add_job(IW, "failed", NOW - DAY - timedelta(seconds=1))], EVO_IW),
        # upstream changed after the failure
        (
            [add_job(IW, "failed", NOW - timedelta(hours=2))],
            replace(EVO_IW, last_activity_at=(NOW - timedelta(hours=1)).isoformat()),
        ),
        # the failure was another project's under this name
        ([add_job(IW, "failed", NOW - timedelta(hours=1), url=B)], EVO_IW),
        # no history for the alias
        ([add_job("other", "failed", NOW)], EVO_IW),
    ],
)
def test_a_failed_add_is_retried_when_something_changed(
    jobs: List[Dict[str, Any]], project_: CatalogProject
) -> None:
    assert holds(*jobs)(IW, project_) is None


def test_the_newest_add_job_is_chosen_by_time_not_by_row_order() -> None:
    older_failed = add_job(IW, "failed", NOW - timedelta(hours=1))
    newer_ok = add_job(IW, "completed", NOW - timedelta(minutes=5))
    # oldest first: the later success still releases the add
    assert holds(older_failed, newer_ok)(IW, EVO_IW) is None
    older_ok = add_job(IW, "completed", NOW - timedelta(hours=2))
    newer_failed = add_job(IW, "failed", NOW - timedelta(minutes=10))
    # oldest first: the later failure still holds it
    assert holds(older_ok, newer_failed)(IW, EVO_IW) is not None


def test_an_unreadable_job_history_holds_nothing() -> None:
    assert holds(add_job(IW, "failed", NOW), broken=True)(IW, EVO_IW) is None
    assert sync.failed_add_holds(None, now=NOW, retry_after=DAY)(IW, EVO_IW) is None


def test_a_pass_reads_the_add_history_and_skips_a_failed_add() -> None:
    class HistoryTracker(Tracker):
        def query_jobs(self, **kw: Any) -> List[Dict[str, Any]]:
            assert kw["operation_type"] == sync.ADD_OPERATION
            failed = add_job(IW, "failed", datetime.now(timezone.utc))
            return [failed]

    recorder = Recorder()
    result = run_sync(deps(recorder, HistoryTracker()), "apply")
    assert ("add", IW) not in recorder.calls
    assert result.plan is not None and IW in result.plan.skipped_adds


def test_catalog_adds_skip_the_ls_remote_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing already proved the project exists; git-mcp's ls-remote pages
    a many-ref answer and the backend refuses it (front-end/fresca)."""
    calls: List[Dict[str, Any]] = []

    class Manager:
        golden_repos_dir = GOLDEN
        _git_backend = None
        job_tracker = None

        def list_golden_repos(self) -> List[Dict[str, Any]]:
            return []

        def add_golden_repo(self, **kw: Any) -> str:
            calls.append(kw)
            return "job-1"

    monkeypatch.delenv(sync.GROUPS_ENV, raising=False)
    built = sync.deps_from_manager(Manager())
    assert built.add("acm", ACM) == "job-1"
    (call,) = calls
    assert call["skip_pre_flight_git_validation"] is True
    assert call["default_branch"] == ACM.default_branch
    assert built.failed_add_retry_seconds == sync.DEFAULT_FAILED_ADD_RETRY_SECONDS
