"""The wiki tests' per-app golden-repos directory: fresh, and never leaked."""

import gc

from fastapi import FastAPI

from tests.unit.server.wiki.wiki_test_helpers import app_golden_repos_dir


def test_each_app_gets_its_own_directory_with_aliases() -> None:
    first, second = FastAPI(), FastAPI()
    one, two = app_golden_repos_dir(first), app_golden_repos_dir(second)
    assert one != two
    assert (one / "aliases").is_dir() and (two / "aliases").is_dir()
    assert one.name.endswith("-golden-repos")


def test_the_directory_goes_with_its_app() -> None:
    app = FastAPI()
    path = app_golden_repos_dir(app, suffix="-golden-x")
    (path / "aliases" / "x-global.json").write_text("{}")
    del app
    gc.collect()
    assert not path.exists()
