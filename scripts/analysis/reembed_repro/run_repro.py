#!/usr/bin/env python3
"""Reproduce re-embedding after interrupted crash-recovery refreshes (story S0).

One command: ``python3 scripts/analysis/reembed_repro/run_repro.py`` (see
README.md). Every embedding request goes to a local fake inside a network
sandbox; nothing can reach a real provider.

Exit codes: 0 NOT REPRODUCED (every check passed), 1 REPRODUCED, 2 harness/guard
failure, 3 INCOMPLETE (no check failed but required checks were skipped).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox  # noqa: E402
from child_runner import ChildResult, InterruptSpec, run_child  # noqa: E402
from cli_args import (  # noqa: E402,F401 - re-exported for the self-tests
    CLUSTER_MODE_ENV,
    REPO_ROOT,
    SCRATCH_ROOT,
    archive_tree,
    node_for_run,
    parse_node_sequence,
    work_dir_for,
)
from cli_args import parse_args as _parse_args  # noqa: E402
from content_model import (  # noqa: E402
    ContentModel,
    FileContent,
    all_keys,
    make_chunk_fn,
    missing_content,
    project_id_for,
)
from fake_voyage_server import FakeVoyageServer  # noqa: E402
from git_variant import apply_op, choose_op, commit_sync, current_branch, init_git_repo  # noqa: E402
from index_inspector import (  # noqa: E402
    METADATA_FILE,
    IndexSnapshot,
    index_snapshot,
    metadata_summary,
    parse_progress_json,
    pending_keys,
)
from invariant import FinalState, RunRecord, evaluate  # noqa: E402
from later_checks import ENABLED, evaluate_later  # noqa: E402
from report import write_reports  # noqa: E402
from run_facts import (  # noqa: E402,F401
    content_bound,
    durability_facts,
    files_all_processed,
    imports_from,
    unindexed_at_start,
)
from scenarios import build_cycles  # noqa: E402
from synthetic_repo import add_files, generate_repo, list_repo_files  # noqa: E402

SAMPLES_DIR = SCRATCH_ROOT / "samples"
RUN_MAX_SECONDS = 4 * 3600
EXIT_HARNESS_FAILURE = 2  # verdict exit codes come from Verdict.exit_code
#: The fake provider listens where the sandbox's /etc/hosts sends api.voyageai.com.
FAKE_HOST, FAKE_PORT = "127.0.0.1", 443
_STDERR_TAIL_LINES = 12
_EXAMPLE_LIMIT = 5
_TIMELINE_POLL_SECONDS = 0.5
_TIMELINE_MAX_ENTRIES = 40


class HarnessFailure(RuntimeError):
    """The harness could not produce a trustworthy result."""


def parse_args(argv: List[str]) -> argparse.Namespace:
    return _parse_args(argv, description=__doc__ or "")


def index_command(reconcile: bool) -> List[str]:
    """The server's ``cidx index`` refresh command (``append_server_layout_args``)."""
    from code_indexer.server.utils.index_command_layout import append_server_layout_args

    base = ["cidx", "index", "--fts", "--progress-json"]
    if reconcile:
        base.insert(3, "--reconcile")
    return list(append_server_layout_args(base))


def kind_for(argv: List[str]) -> str:
    return "recovery" if "--reconcile" in argv else "sync"


@dataclass
class PreState:
    node: int
    disk: Set[str]
    branch: Optional[str]
    before: IndexSnapshot
    pending_before: Set[str]
    files: Dict[str, FileContent]
    meta_mtime: Optional[int]
    meta_before: Dict[str, Any]


class Harness:
    def __init__(
        self, args: argparse.Namespace, work: Path, fake: FakeVoyageServer
    ) -> None:
        self.args = args
        self.work = work
        self.fake = fake
        self.repo = work / "repo"
        self.logs = work / "logs"
        self.audit_log = work / "audit.jsonl"
        self.ca_pem = work / "assets" / "ca.pem"
        self.meta_path = self.repo / ".code-indexer" / METADATA_FILE
        self.src = Path(args.src).resolve()
        self.initial_src = (
            Path(args.initial_src).resolve() if args.initial_src else self.src
        )
        cidx = shutil.which("cidx")
        if cidx is None:
            raise HarnessFailure("no 'cidx' entry point on PATH")
        self.cidx: str = cidx
        self.rows: List[Dict[str, Any]] = []
        self.records: List[RunRecord] = []
        self.added_paths: Set[str] = set()
        self.versions: Dict[str, str] = {}
        self.content: Optional[ContentModel] = None
        self.project_id = ""
        self.introduced: Set[str] = set()
        self.boundaries: Dict[str, Any] = {}
        self.final_state: Optional[FinalState] = None
        self._snap: Optional[IndexSnapshot] = None
        self._pending: Set[str] = set()

    # -- environment -----------------------------------------------------
    def env_for(self, src: Path, node: int) -> Dict[str, str]:
        home = self.work / "home"
        server_dir = self.work / f"server-data-node{node}"
        home.mkdir(exist_ok=True)
        server_dir.mkdir(exist_ok=True)
        env = sandbox.child_env(
            os.environ, src, home, server_dir, self.ca_pem, self.audit_log
        )
        if not self.args.no_cluster_mode:
            env[CLUSTER_MODE_ENV] = "1"
        return env

    def check_child_import(self, src: Path) -> None:
        code = "import code_indexer; print(code_indexer.__file__); print(code_indexer.__version__)"
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=self.env_for(src, 0),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not imports_from(out[0], src):
            raise HarnessFailure(f"child imports code_indexer from {out[0]}, not {src}")
        self.versions[str(src)] = out[1]

    def refresh_command(self) -> List[str]:
        """As refresh_scheduler._index_source: any in_progress/failed status reconciles."""
        from code_indexer.server.services.metadata_reader import read_index_states

        return index_command(
            any(
                s.status in ("in_progress", "failed")
                for s in read_index_states(self.repo)
            )
        )

    # -- one run -----------------------------------------------------------
    def _watch_metadata(
        self, stop: threading.Event, timeline: List[Dict[str, Any]], t0: float
    ) -> None:
        last = None
        while (
            not stop.wait(_TIMELINE_POLL_SECONDS)
            and len(timeline) < _TIMELINE_MAX_ENTRIES
        ):
            try:
                summary = metadata_summary(self.repo)
            except ValueError:
                continue  # file replaced mid-read; next poll sees it
            key = (
                summary.get("status"),
                summary.get("total_files_to_index"),
                summary.get("files_processed"),
                summary.get("completed_files"),
            )
            if key != last:
                timeline.append(
                    {
                        "t": round(time.monotonic() - t0, 1),
                        "status": key[0],
                        "total_files_to_index": key[1],
                        "files_processed": key[2],
                        "completed_files": key[3],
                    }
                )
                last = key

    def _missing(
        self, files: Dict[str, FileContent], snap: IndexSnapshot, branch: Optional[str]
    ) -> List[str]:
        return missing_content(
            files, self.project_id, snap.point_ids, snap.hidden_ids_for(branch)
        )

    def _pre_state(self) -> PreState:
        disk = set(list_repo_files(self.repo))
        files = self.content.files(sorted(disk)) if self.content else {}
        self.introduced |= all_keys(files.values())
        return PreState(
            node=node_for_run(
                len(self.rows), self.args.node_sequence, self.args.rotate_node_key
            ),
            disk=disk,
            branch=current_branch(self.repo),
            before=self._snap or index_snapshot(self.repo, with_hashes=True),
            pending_before=self._pending,
            files=files,
            meta_mtime=self.meta_path.stat().st_mtime_ns
            if self.meta_path.exists()
            else None,
            meta_before=metadata_summary(self.repo),
        )

    def _execute(
        self,
        label: str,
        argv: List[str],
        src: Path,
        pre: PreState,
        interrupt: Optional[InterruptSpec],
    ) -> Tuple[ChildResult, List[Dict[str, Any]]]:
        hold = (
            self.args.hold_seconds
            if interrupt and interrupt.trigger == "inflight"
            else 0.0
        )
        self.fake.ledger.begin_run(label, hold_seconds=hold)
        timeline: List[Dict[str, Any]] = []
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_metadata, args=(stop, timeline, time.monotonic())
        )
        watcher.start()

        def planned() -> bool:
            """This run has rewritten the metadata with its plan (status in_progress)."""
            try:
                return (
                    self.meta_path.stat().st_mtime_ns != pre.meta_mtime
                    and metadata_summary(self.repo).get("status") == "in_progress"
                )
            except (OSError, ValueError):
                return False  # replaced mid-read; the next poll sees it

        try:
            result = run_child(
                [self.cidx, *argv[1:]],
                self.repo,
                self.env_for(src, pre.node),
                self.fake.ledger,
                self.logs / label,
                interrupt,
                RUN_MAX_SECONDS,
                planned_probe=planned,
                finalize_probe=lambda: files_all_processed(self.logs / f"{label}.out"),
            )
        finally:
            stop.set()
            watcher.join()
        return result, timeline

    def _post_state(
        self, label: str, pre: PreState, result: ChildResult
    ) -> Dict[str, Any]:
        after = index_snapshot(self.repo, with_hashes=True)
        pending_after = pending_keys(self.repo)
        self._snap, self._pending = after, pending_after
        sent_counts = self.fake.ledger.run_key_counts(label)
        held = {
            k
            for rec in self.fake.ledger.requests(label)
            if rec.request_id in result.unanswered_at_kill
            for k in rec.keys
        }
        durable_after = after.content_hashes | pending_after
        missing: Optional[List[str]] = None
        if self.content and not result.interrupted and result.exit_code == 0:
            missing = self._missing(pre.files, after, pre.branch)
        if result.interrupted:
            self.boundaries[label] = self.fake.ledger.boundary(label, durable_after)
        bound = None
        if self.content:
            bound = content_bound(
                sent_counts,
                all_keys(pre.files.values()),
                pre.before.content_hashes | pre.pending_before,
            )
        return {
            "after": after,
            "pending_after": pending_after,
            "sent_counts": sent_counts,
            "missing": missing,
            "bound": bound,
            "facts": durability_facts(
                sent_counts,
                pre.before.content_hashes | pre.pending_before,
                durable_after,
                result.interrupted,
                held,
                pending_after,
            ),
        }

    def _build_row(
        self,
        label: str,
        kind: str,
        argv: List[str],
        src: Path,
        note: str,
        interrupt: Optional[InterruptSpec],
        pre: PreState,
        result: ChildResult,
        timeline: List[Dict[str, Any]],
        post: Dict[str, Any],
    ) -> Dict[str, Any]:
        after, facts, missing = post["after"], post["facts"], post["missing"]
        meta_after = metadata_summary(self.repo)
        rewritten = (
            self.meta_path.exists()
            and self.meta_path.stat().st_mtime_ns != pre.meta_mtime
        )
        progress = parse_progress_json(
            (self.logs / f"{label}.out").read_text(errors="replace")
        )
        stats = self.fake.ledger.run_stats(label)
        stderr = (self.logs / f"{label}.err").read_text(errors="replace").splitlines()
        return {
            "label": label,
            "kind": kind,
            "node": pre.node,
            "version": self.versions.get(str(src)),
            "reconcile": "--reconcile" in argv,
            "command": " ".join(argv),
            "note": note,
            "interrupt": str(interrupt) if interrupt else None,
            "exit_code": result.exit_code,
            "duration_s": result.duration_s,
            "interrupted": result.interrupted,
            "child_note": result.note,
            "inputs_at_interrupt": result.inputs_at_interrupt,
            "branch": pre.branch,
            "disk_files": len(pre.disk),
            "index_points_before": pre.before.points,
            "index_paths_before": len(pre.before.paths),
            "index_points_after": after.points,
            "index_paths_after": len(after.paths),
            "unindexed_at_start": unindexed_at_start(
                pre.disk,
                pre.before.paths,
                set(self._missing(pre.files, pre.before, pre.branch)),
            ),
            "hot_journal_after": after.hot_journal,
            "planned_progress": progress.file_totals[0]
            if progress.file_totals
            else None,
            "progress_file_totals": progress.file_totals,
            "planned_metadata": meta_after.get("total_files_to_index")
            if rewritten
            else None,
            # Observed (mtime compare): did this run rewrite its metadata plan?
            "metadata_rewritten": rewritten,
            "chunks_sent": stats["inputs"],
            "requests": stats["requests"],
            "dup_prior_runs": stats["dup_prior_runs"],
            "dup_same_run": stats["dup_same_run"],
            "new_unique": stats["new_unique"],
            "reembedded_already_indexed": len(
                set(post["sent_counts"]) & pre.before.content_hashes
            ),
            "inflight_keys": len(facts["inflight_keys"]),
            "held_keys": len(facts["held_keys"]),
            "held_in_pending_after": len(facts["held_in_pending_after"]),
            "pending_keys_after": len(post["pending_after"]),
            "content_missing_after": None if missing is None else len(missing),
            "content_missing_examples": (missing or [])[:_EXAMPLE_LIMIT],
            "boundary": self.boundaries[label]["summary"]
            if label in self.boundaries
            else None,
            "status_before": pre.meta_before.get("status"),
            "status_after": meta_after.get("status"),
            "files_processed_after": meta_after.get("files_processed"),
            "metadata_after": meta_after,
            "metadata_timeline": timeline,
            "stderr_tail": stderr[-_STDERR_TAIL_LINES:],
        }

    def _record(
        self,
        row: Dict[str, Any],
        interrupt: Optional[InterruptSpec],
        post: Dict[str, Any],
    ) -> None:
        planned_files: Optional[int] = row["planned_progress"]
        if (
            planned_files is None
            and row["planned_metadata"] is not None
            and row["status_after"] == "in_progress"
        ):
            planned_files = int(row["planned_metadata"])
        self.records.append(
            RunRecord(
                row["label"],
                row["kind"],
                row["interrupted"],
                row["exit_code"],
                row["chunks_sent"],
                planned_files,
                row["unindexed_at_start"],
                version=row["version"] or "?",
                signal_name=interrupt.name
                if interrupt and row["interrupted"]
                else None,
                trigger=interrupt.trigger if interrupt else None,
                dup_same_run=row["dup_same_run"],
                reembedded_already_indexed=row["reembedded_already_indexed"],
                sent_counts=post["sent_counts"],
                hot_journal_after=row["hot_journal_after"],
                content_missing_after=row["content_missing_after"],
                reconcile=row["reconcile"],
                owed_keys=post["bound"][0] if post["bound"] else None,
                sent_not_owed=post["bound"][1] if post["bound"] else frozenset(),
                plan_written=row["metadata_rewritten"],
                **post["facts"],
            )
        )

    def run_step(
        self,
        label: str,
        kind: str,
        argv: List[str],
        src: Path,
        interrupt: Optional[InterruptSpec],
        note: str = "",
    ) -> Dict[str, Any]:
        pre = self._pre_state()
        result, timeline = self._execute(label, argv, src, pre, interrupt)
        post = self._post_state(label, pre, result)
        row = self._build_row(
            label, kind, argv, src, note, interrupt, pre, result, timeline, post
        )
        self.rows.append(row)
        print(
            f"[{label}] exit={row['exit_code']} {row['child_note']}; sent={row['chunks_sent']} "
            f"re_idx={row['reembedded_already_indexed']} inflight={row['inflight_keys']} "
            f"held={row['held_keys']}/{row['held_in_pending_after']} planned={row['planned_progress']} "
            f"missing={row['content_missing_after']} status={row['status_after']} "
            f"idx={row['index_points_before']}>{row['index_points_after']} {note}",
            flush=True,
        )
        if kind != "setup":
            self._record(row, interrupt, post)
        return row

    # -- sequence ------------------------------------------------------------
    def _init_repo(self) -> None:
        t0 = time.monotonic()
        generate_repo(
            self.repo,
            self.args.files,
            seed=self.args.seed,
            files_per_dir=self.args.files_per_dir,
            dup_groups=self.args.dup_groups,
        )
        if self.args.git:
            init_git_repo(self.repo)
        print(
            f"[setup] generated {self.args.files} files (+{self.args.dup_groups} duplicate groups, "
            f"git={self.args.git}) in {time.monotonic() - t0:.0f}s",
            flush=True,
        )
        self.logs.mkdir()
        self.run_step(
            "init",
            "setup",
            ["cidx", "init", "--embedding-provider", "voyage-ai", "--no-override-file"],
            self.initial_src,
            None,
        )
        if self.rows[-1]["exit_code"] != 0:
            raise HarnessFailure("cidx init failed; see logs/init.err")
        # GoldenRepoManager._write_embedding_providers_to_config: voyage-ai only (no Cohere key).
        config_path = self.repo / ".code-indexer" / "config.json"
        config = json.loads(config_path.read_text())
        config["embedding_providers"] = ["voyage-ai"]
        config_path.write_text(json.dumps(config, indent=2))
        self.project_id = project_id_for(self.repo)
        self.content = ContentModel(self.repo, make_chunk_fn(self.repo))

    def setup(self) -> None:
        self._init_repo()
        initial = self.run_step(
            "initial", "initial", index_command(False), self.initial_src, None
        )
        if (
            self._snap is None
            or self._snap.content_hashes != self.fake.ledger.run_hashes("initial")
        ):
            raise HarnessFailure(
                "index content_hash values differ from the texts the fake embedded; "
                "the already-indexed re-embedding metric would be invalid"
            )
        if (
            initial["exit_code"] != 0
            or initial["status_after"] != "completed"
            or initial["content_missing_after"] != 0
            or initial["chunks_sent"] == 0
        ):
            raise HarnessFailure(
                f"initial index incomplete or content model disagrees with it: {initial['child_note']}, "
                f"status {initial['status_after']}, {initial['content_missing_after']} files' content "
                f"missing, e.g. {initial['content_missing_examples']}"
            )

    def cycles(self) -> None:
        git_rng = random.Random(f"{self.args.seed}:git-ops")
        for i, entry in enumerate(build_cycles(self.args), start=1):
            tag = f"sync-{i:03d}"
            added = add_files(
                self.repo,
                self.args.sync_files,
                tag,
                self.args.seed,
                dup_fraction=self.args.sync_dup_fraction,
            )
            self.added_paths.update(added)
            note = ""
            if self.args.git:
                commit_sync(self.repo, added, tag)
                note = apply_op(self.repo, choose_op(git_rng), i, git_rng)
            argv = self.refresh_command()
            spec = None if entry == "none" else InterruptSpec.parse(entry)
            src = self.initial_src if i <= self.args.old_src_runs else self.src
            self.run_step(f"cycle-{i}", kind_for(argv), argv, src, spec, note)
        if self.args.recovery_delay_seconds > 0:
            # The recovery runs on a later scheduler cycle (see scenarios.py).
            print(
                f"[delay] {self.args.recovery_delay_seconds}s before recovery",
                flush=True,
            )
            time.sleep(self.args.recovery_delay_seconds)
        for j in range(1, self.args.final_runs + 1):
            argv = self.refresh_command()
            self.run_step(f"final-{j}", kind_for(argv), argv, self.src, None)
        self.final_state = self._final_state()
        # A5: one more reconcile and one more incremental must plan and send nothing.
        self.run_step(
            "converge-reconcile", "converge", index_command(True), self.src, None
        )
        self.run_step(
            "converge-incremental", "converge", index_command(False), self.src, None
        )

    def _final_state(self) -> FinalState:
        if self.content is None or self._snap is None:
            raise HarnessFailure(
                "no content model or index snapshot after the final runs"
            )
        files = self.content.files(list_repo_files(self.repo))
        missing = self._missing(files, self._snap, current_branch(self.repo))
        return FinalState(
            status=self.rows[-1]["status_after"],
            content_missing=len(missing),
            content_files=len(files),
            missing_examples=tuple(missing[:_EXAMPLE_LIMIT]),
        )

    def guards(self) -> List[str]:
        problems = list(self.fake.ledger.violations())
        if self.audit_log.exists() and self.audit_log.read_text().strip():
            problems.append(f"child network audit log is not empty: {self.audit_log}")
        if self.fake.ledger.totals()["inputs"] == 0:
            problems.append("the fake provider received no embedding calls")
        return problems

    def run(self) -> int:
        for src in {self.src, self.initial_src}:
            self.check_child_import(src)
        self.setup()
        self.cycles()
        problems = self.guards()
        if self.final_state is None:
            raise HarnessFailure("final state was not captured")
        final = replace(self.final_state, introduced_keys=len(self.introduced))
        verdict = evaluate(self.records, final, evaluate_later(self.src, ENABLED))
        a = self.args
        header = [
            f"scenario: {a.scenario}; versions under test: {self.versions}",
            f"repo: {a.files} files (+{a.dup_groups} duplicate groups, +{len(self.added_paths)} added "
            f"by sync, dup fraction {a.sync_dup_fraction}, git {a.git}), cluster mode: "
            f"{not a.no_cluster_mode}, node sequence: {a.node_sequence}",
            f"cycles: {','.join(build_cycles(a))}",
            f"fake provider totals: {self.fake.ledger.totals()}; introduced keys: {len(self.introduced)}",
        ]
        extra = {
            "args": vars(a),
            "versions": self.versions,
            "fake": self.fake.ledger.to_json(),
        }
        print(
            "\n"
            + write_reports(
                self.work, header, self.rows, extra, self.boundaries, verdict, problems
            ),
            flush=True,
        )
        if problems:
            return EXIT_HARNESS_FAILURE
        return verdict.exit_code  # 0 NOT REPRODUCED, 1 REPRODUCED, 3 INCOMPLETE


def inner_main(args: argparse.Namespace) -> int:
    work = work_dir_for(args.name)
    problems = sandbox.verify_isolation()
    if problems:
        print("GUARD FAILURE: sandbox not isolated: " + "; ".join(problems), flush=True)
        return EXIT_HARNESS_FAILURE
    sys.path.insert(0, str(Path(args.src).resolve()))
    fake: Optional[FakeVoyageServer] = None
    try:
        # Built and started inside the try: any failure here is a harness
        # failure (exit 2), never an uncaught exception exiting 1.
        fake = FakeVoyageServer(sandbox.SENTINEL_API_KEY)
        fake.start(
            FAKE_HOST,
            FAKE_PORT,
            str(work / "assets" / "leaf.pem"),
            str(work / "assets" / "leaf.key"),
        )
        return Harness(args, work, fake).run()
    except HarnessFailure as exc:
        print(f"HARNESS FAILURE: {exc}", flush=True)
        return EXIT_HARNESS_FAILURE
    except Exception:  # noqa: BLE001 - a crash must never read as exit 1 (REPRODUCED)
        traceback.print_exc()
        print("HARNESS FAILURE: unexpected error (traceback above)", flush=True)
        return EXIT_HARNESS_FAILURE
    finally:
        if fake is not None:
            fake.stop()


def outer_main(args: argparse.Namespace, argv: List[str]) -> int:
    work = work_dir_for(args.name)
    if work.exists():
        shutil.rmtree(work_dir_for(args.name))
    work.mkdir(parents=True)
    assets = sandbox.prepare_sandbox_assets(work / "assets")
    (work / "repo").mkdir()
    read_fd, handshake = sandbox.open_handshake()
    try:
        cmd = sandbox.build_sandbox_command(
            assets,
            [
                sys.executable,
                str(Path(__file__).resolve()),
                HANDSHAKE_OPTION,
                handshake,
                *argv,
            ],
            cwd=str(work),
            tmpfs_dirs=[] if args.disk else [work / "repo"],
        )
        rc = subprocess.run(cmd, pass_fds=(read_fd,)).returncode
    finally:
        os.close(read_fd)
    exit_code = trusted_exit(rc, work / "report.json")
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("report.txt", "report.json"):
        if (work / name).exists():
            shutil.copy2(work / name, SAMPLES_DIR / f"{args.name}-{name}")
    if not args.keep:
        shutil.rmtree(work_dir_for(args.name))
    print(
        f"exit {exit_code} (sandbox child rc {rc}); report kept in {SAMPLES_DIR}",
        flush=True,
    )
    return exit_code


_VERDICT_EXITS = (0, 1, 3)  # NOT REPRODUCED, REPRODUCED, INCOMPLETE


def trusted_exit(rc: int, report_json: Path) -> int:
    """A verdict exit code (0/1/3) only when report.json confirms it.

    Anything else -- a crash, a signal, a missing or unreadable report, or a
    report whose verdict disagrees with the child's code -- is a harness
    failure (2), so a broken run can never read as REPRODUCED.
    """
    if rc not in _VERDICT_EXITS:
        return EXIT_HARNESS_FAILURE
    try:
        verdict_exit = json.loads(report_json.read_text())["verdict"]["exit_code"]
    except (OSError, ValueError, KeyError, TypeError):
        return EXIT_HARNESS_FAILURE
    return rc if verdict_exit == rc else EXIT_HARNESS_FAILURE


#: Parent-to-child dispatch: "<fd>:<sha256>" of a one-time token in a pipe.
HANDSHAKE_OPTION = "--sandbox-handshake"


def split_handshake(argv: List[str]) -> Tuple[Optional[str], List[str]]:
    """(handshake argument or None, the remaining argv)."""
    if len(argv) >= 2 and argv[0] == HANDSHAKE_OPTION:
        return argv[1], argv[2:]
    return None, list(argv)


def main(argv: List[str]) -> int:
    handshake, rest = split_handshake(argv)
    args = parse_args(rest)
    if handshake is None:
        return outer_main(args, rest)
    try:
        sandbox.accept_handshake(handshake)
    except sandbox.HandshakeError as exc:
        print(f"GUARD FAILURE: {exc}", flush=True)
        return EXIT_HARNESS_FAILURE
    return inner_main(args)  # which still proves the namespace is isolated


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
