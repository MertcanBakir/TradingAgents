"""Internal orphan cleanup. Dry-run unless an operator explicitly opts in."""

from datetime import UTC, datetime, timedelta


def cleanup_orphans(repository, storage, *, apply=False, now=None):
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=24)
    counts = {"orphan_candidates": 0, "deleted": 0, "delete_failed": 0, "dry_run": not apply}
    for row in repository.orphan_reports(cutoff):
        path = row["name"]
        try:
            storage._url(path)
        except Exception:
            continue
        # 24h age exceeds the maximum worker lifetime. An old unreferenced
        # upload cannot race a valid worker's later completion.
        with repository.transaction() as conn:
            exists = conn.execute(
                "SELECT id FROM analysis_backend.jobs WHERE report_path=%s", (path,)
            ).fetchone()
        if exists:
            continue
        counts["orphan_candidates"] += 1
        if apply:
            counts["deleted" if storage.discard(path) else "delete_failed"] += 1
    return counts
