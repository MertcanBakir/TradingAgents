"""Migration planning has no database access; historical SQL cannot be replayed."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pglast import Error

from deploy.personal import migrate

SOURCE = Path(__file__).resolve().parents[1] / "deploy/migrations"


def bundle(tmp_path):
    destination = tmp_path / "migrations"
    shutil.copytree(SOURCE, destination)
    # Synthetic migrations remain independent of future real production files.
    manifest = destination / "manifest.json"
    data = json.loads(manifest.read_text())
    for entry in data["migrations"]:
        (destination / entry["file"]).unlink()
    data["migrations"] = []
    manifest.write_text(json.dumps(data))
    return destination


def add_file(directory, text, name="20261030000000_add_optional_note.sql", **metadata):
    raw = text.encode()
    (directory / name).write_bytes(raw)
    manifest = directory / "manifest.json"
    data = json.loads(manifest.read_text())
    data["migrations"].append({"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
                               "backwards_compatible": True, **metadata})
    manifest.write_text(json.dumps(data))
    return migrate.load(directory)


def history(migrations):
    return {m.version: m.name for m in migrations if m.baseline}


def test_actual_baseline_is_valid_and_is_never_pending():
    migrations = [m for m in migrate.load(SOURCE) if m.baseline]
    assert len(migrations) == 3
    assert migrate.plan(migrations, history(migrations), {}) == []
    with pytest.raises(ValueError, match="baseline"):
        migrate.plan(migrations, {}, {})


def test_only_new_explicitly_compatible_migrations_are_pending(tmp_path):
    files = add_file(bundle(tmp_path), "BEGIN; ALTER TABLE analysis_backend.jobs ADD COLUMN note text; COMMIT;")
    pending = migrate.plan(files, history(files), {})
    assert [m.version for m in pending] == ["20261030000000"]
    assert len(pending[0].statements) == 1


@pytest.mark.parametrize("sql", [
    "BEGIN; ALTER TABLE analysis_backend.jobs ADD COLUMN note text; COMMIT; SELECT 1;",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN note text; COMMIT;",
    "ROLLBACK;", "VACUUM;", "CREATE INDEX CONCURRENTLY x ON analysis_backend.jobs(created_at);",
    "DROP TABLE analysis_backend.jobs;", "DELETE FROM analysis_backend.jobs;",
    "ALTER TABLE analysis_backend.jobs DROP COLUMN status;",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN required text NOT NULL;",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN code text DEFAULT 'changed';",
    "ALTER TABLE public.instruments ADD COLUMN note text;",
    "GRANT SELECT ON analysis_backend.jobs TO authenticated;",
    "DO $$ BEGIN EXECUTE 'DROP TABLE analysis_backend.jobs'; END $$;",
    "SELECT pg_sleep(999);", "", "-- comment only", "THIS IS NOT SQL;",
    "COMMENT ON TABLE public.instruments IS 'other project';",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN IF NOT EXISTS note text;",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN sequence_id SERIAL;",
    "ALTER TABLE analysis_backend.jobs ADD COLUMN sequence_id BIGSERIAL;",
    "CREATE TABLE analysis_backend.extra () INHERITS (analysis_backend.jobs);",
])
def test_nonadditive_nontransactional_or_embedded_commit_is_rejected(sql):
    with pytest.raises((ValueError, Error)):
        migrate.statements(sql)


def test_sql_parser_preserves_literals_and_function_style_comments():
    text = "COMMENT ON TABLE analysis_backend.jobs IS 'commit; rollback; BEGIN';"
    assert len(migrate.statements(text)) == 1


@pytest.mark.parametrize("change", ["checksum", "baseline", "unlisted", "review", "duplicate"])
def test_invalid_manifest_is_rejected(tmp_path, change):
    root = bundle(tmp_path)
    manifest = root / "manifest.json"
    data = json.loads(manifest.read_text())
    first = root / data["baseline"][0]["file"]
    if change == "checksum":
        first.write_text(first.read_text() + "\n-- modified\n")
    elif change == "baseline":
        data["baseline"][0]["sha256"] = "a" * 64
    elif change == "unlisted":
        (root / "20261030000000_unknown.sql").write_text("SELECT 1;")
    elif change == "review":
        with pytest.raises(ValueError, match="review"):
            add_file(root, "ALTER TABLE analysis_backend.jobs ADD COLUMN note text;", backwards_compatible=False)
        return
    else:
        data["baseline"].append(data["baseline"][0])
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        migrate.load(root)


def test_history_drift_external_apply_backdating_and_removed_files_fail_closed(tmp_path):
    files = add_file(bundle(tmp_path), "ALTER TABLE analysis_backend.jobs ADD COLUMN note text;")
    remote = history(files)
    new = files[-1]
    with pytest.raises(ValueError, match="matching names"):
        migrate.plan(files, {**remote, files[0].version: "wrong_name"}, {})
    with pytest.raises(ValueError, match="Untracked"):
        migrate.plan(files, {**remote, new.version: new.name}, {})
    with pytest.raises(ValueError, match="predates"):
        migrate.plan(files, {**remote, "20261101000000": "other_finbot_change"}, {})
    ledger = {new.version: (new.name, new.checksum)}
    assert migrate.plan(files, {**remote, new.version: new.name}, ledger) == []
    with pytest.raises(ValueError, match="removed or changed"):
        migrate.plan(files[:-1], {**remote, new.version: new.name}, ledger)
    with pytest.raises(ValueError, match="disagree"):
        migrate.plan(files, remote, ledger)


def test_ledger_checksum_mismatch_and_repeated_versions_are_rejected(tmp_path):
    root = bundle(tmp_path)
    files = add_file(root, "ALTER TABLE analysis_backend.jobs ADD COLUMN note text;")
    new = files[-1]
    with pytest.raises(ValueError, match="changed"):
        migrate.plan(files, {**history(files), new.version: new.name}, {new.version: (new.name, "0" * 64)})
    with pytest.raises(ValueError, match="unique"):
        add_file(root, "ALTER TABLE analysis_backend.jobs ADD COLUMN other text;", "20261030000000_other.sql")


def test_cli_failure_does_not_print_sql_or_private_settings(tmp_path, monkeypatch, capsys):
    root = bundle(tmp_path)
    path = root / next(iter(migrate.BASELINE))
    path.write_text("DO $$ BEGIN RAISE EXCEPTION 'FAKE_PRIVATE_DATA'; END $$;")
    monkeypatch.setattr("sys.argv", ["runner", "--directory", str(root), "--validate"])
    with pytest.raises(SystemExit) as error:
        migrate.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "FAKE_PRIVATE_DATA" not in output.out + output.err
    assert json.loads(output.out)["code"] == "DATABASE_MIGRATION_FAILED"
