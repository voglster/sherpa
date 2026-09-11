"""Corpus health: staleness and embedding backlog.

Shared by `status` and by `search`, because a query answered from a stale corpus
is worse than a query that fails — the agent reports that Jim promised nobody
anything and nothing indicates the corpus simply stopped three weeks ago.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from sherpa.corpus.config import Config


@dataclass
class Health:
    workspace: str
    last_sync: float
    stale: bool
    unembedded: int
    failed_channels: int
    backfilling: int = 0
    unswept_threads: int = 0


def check(conn: sqlite3.Connection, config: Config, *, now=time.time) -> list[Health]:
    reports = []
    for workspace in config.workspaces:
        row = conn.execute(
            "SELECT max(updated_at) AS last, sum(status = 'failed') AS failed"
            " FROM sync_state WHERE workspace = ?",
            (workspace.name,),
        ).fetchone()
        last = row["last"] or 0.0
        unembedded = conn.execute(
            "SELECT count(*) FROM chunks WHERE workspace = ? AND embedded = 0",
            (workspace.name,),
        ).fetchone()[0]
        # A corpus still reading its own history is incomplete in the way that
        # matters most to "did I miss anything?", and a first backfill runs for
        # days. Nothing else here can see it: staleness, embedding backlog and
        # channel failures are all clean throughout.
        backfilling = conn.execute(
            "SELECT count(*) FROM backfill_state WHERE workspace = ? AND complete = 0",
            (workspace.name,),
        ).fetchone()[0]
        unswept = conn.execute(
            "SELECT count(*) FROM thread_sweeps WHERE workspace = ? AND swept = 0",
            (workspace.name,),
        ).fetchone()[0]
        reports.append(
            Health(
                workspace=workspace.name,
                last_sync=last,
                stale=(now() - last) > config.stale_after_hours * 3600,
                unembedded=unembedded,
                failed_channels=row["failed"] or 0,
                backfilling=backfilling,
                unswept_threads=unswept,
            )
        )
    return reports


def is_healthy(reports: list[Health]) -> bool:
    return all(
        not r.stale
        and r.unembedded == 0
        and r.failed_channels == 0
        and r.backfilling == 0
        and r.unswept_threads == 0
        for r in reports
    )


def warnings(reports: list[Health]) -> list[str]:
    lines = []
    for report in reports:
        if report.last_sync == 0:
            lines.append(f"{report.workspace}: never synced — results are empty or partial")
        elif report.stale:
            age = (time.time() - report.last_sync) / 3600
            lines.append(f"{report.workspace}: last synced {age:.0f}h ago — results may be incomplete")
        if report.unembedded:
            lines.append(f"{report.workspace}: {report.unembedded} chunks unembedded — semantic recall is degraded")
        if report.failed_channels:
            lines.append(f"{report.workspace}: {report.failed_channels} channels failed to sync")
        if report.backfilling:
            lines.append(
                f"{report.workspace}: {report.backfilling} channels still backfilling"
                " — history before the current window is not in the corpus yet"
            )
        if report.unswept_threads:
            lines.append(
                f"{report.workspace}: {report.unswept_threads} threads never swept"
                " — their replies are not in the corpus yet"
            )
    return lines
