"""Transactional, forward-only TradingAgents migrations on an existing FinBot DB.

This is not a replacement for FinBot's complete Supabase migration chain. The
three historical files must already exist in Supabase's migration history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from pglast import ast, parse_sql, split
from pglast.enums import AlterTableType, TransactionStmtKind
from psycopg.conninfo import conninfo_to_dict

BASELINE = {
    "20261019000000_analysis_backend.sql": "cfeb3abff994f3723b284adfb9cf0d3229d6e6ea1789f1a0352782938598cba4",
    "20261022000000_personal_analysis_auth.sql": "41cc7d7857728c7c0b4e90398f1cb19127b9ea8f2bfa3915b2d8b81c06350893",
    "20261024000000_dynamic_chatgpt_model_catalog.sql": "5cb98ac22540ab2a2a5ff12be31f3ac2ac6c5a7c4c9b8a24869fa77a1c0b9c68",
}
LOCK_ID = 710195
HISTORY = "supabase_migrations.schema_migrations"
LEDGER = "tradingagents_deploy.migrations"


@dataclass(frozen=True)
class Migration:
    file: str
    checksum: str
    statements: tuple[str, ...]
    baseline: bool = False

    @property
    def version(self):
        return self.file.split("_", 1)[0]

    @property
    def name(self):
        return self.file.split("_", 1)[1][:-4]


def expanding_statement(node):
    """Deliberately narrow SQL subset compatible with an old running image.

    Destructive changes, backfills, functions and nontransactional operations
    require a separately reviewed rollout. This is not a SQL security sandbox.
    """
    relation = getattr(node, "relation", None) or getattr(node, "table", None)
    if isinstance(node, ast.CommentStmt):
        return (isinstance(node.object, tuple) and len(node.object) >= 2
                and node.object[0].sval == "analysis_backend")
    if isinstance(node, ast.GrantStmt):
        return (node.is_grant and not node.grant_option and node.grantees
                and all(role.rolename == "lucrari_analysis_backend" for role in node.grantees)
                and node.objects
                and all(isinstance(obj, ast.RangeVar) and obj.schemaname == "analysis_backend"
                        for obj in node.objects))
    if not relation or relation.schemaname != "analysis_backend":
        return False
    if isinstance(node, ast.CreateStmt):
        return (not node.if_not_exists and node.relation.relpersistence == "p"
                and not node.inhRelations and not node.partbound and not node.ofTypename)
    if isinstance(node, ast.IndexStmt):
        return not node.concurrent and not node.unique and not node.if_not_exists
    if isinstance(node, ast.CreatePolicyStmt):
        return bool(node.roles) and all(r.rolename == "lucrari_analysis_backend" for r in node.roles)
    if isinstance(node, ast.AlterTableStmt):
        return bool(node.cmds) and all(
            cmd.subtype == AlterTableType.AT_EnableRowSecurity
            or (cmd.subtype == AlterTableType.AT_AddColumn
                and not cmd.missing_ok and isinstance(cmd.def_, ast.ColumnDef)
                and not cmd.def_.constraints and not cmd.def_.is_not_null
                # SERIAL expands into NOT NULL + nextval() even without a
                # ColumnDef constraint/default in the raw parse tree.
                and cmd.def_.typeName.names[-1].sval.lower() not in {
                    "serial", "smallserial", "bigserial", "serial2", "serial4", "serial8"}
                and cmd.def_.identity in (None, "", "\u0000")
                and cmd.def_.generated in (None, "", "\u0000"))
            for cmd in node.cmds
        )
    return False


def statements(text, *, baseline=False):
    nodes = [item.stmt for item in parse_sql(text)]
    parts = list(split(text))
    if (len(nodes) >= 2 and isinstance(nodes[0], ast.TransactionStmt)
            and nodes[0].kind == TransactionStmtKind.TRANS_STMT_BEGIN
            and isinstance(nodes[-1], ast.TransactionStmt)
            and nodes[-1].kind == TransactionStmtKind.TRANS_STMT_COMMIT):
        nodes, parts = nodes[1:-1], parts[1:-1]
    if not nodes or any(isinstance(n, ast.TransactionStmt) for n in nodes):
        raise ValueError("Empty migration or embedded transaction control")
    if not baseline and not all(expanding_statement(node) for node in nodes):
        raise ValueError("Migration requires a manual rollout: SQL is outside the additive subset")
    return tuple(parts)


def load(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != 1 or not isinstance(manifest.get("migrations"), list):
        raise ValueError("Invalid migration manifest")
    entries = manifest.get("baseline", [])
    if {e["file"]: e["sha256"] for e in entries} != BASELINE or len(entries) != len(BASELINE):
        raise ValueError("Historical baseline is immutable")
    result = []
    for baseline, group in ((True, entries), (False, manifest["migrations"])):
        for entry in group:
            name = entry["file"]
            if not re.fullmatch(r"[0-9]{14}_[a-z][a-z0-9_]*\.sql", name):
                raise ValueError("Invalid migration filename")
            if not baseline and entry.get("backwards_compatible") is not True:
                raise ValueError("Migration needs explicit backwards-compatible review")
            path = directory / name
            if path.is_symlink():
                raise ValueError("Migration symlinks are forbidden")
            raw = path.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            if checksum != entry["sha256"]:
                raise ValueError("Migration checksum mismatch")
            result.append(Migration(name, checksum, statements(raw.decode(), baseline=baseline), baseline))
    versions = [m.version for m in result]
    if versions != sorted(set(versions)) or versions[:3] != sorted(f.split("_")[0] for f in BASELINE):
        raise ValueError("Migration versions must be unique and increasing after baseline")
    if {p.name for p in directory.glob("*.sql")} != {m.file for m in result}:
        raise ValueError("Every SQL file must be declared in the manifest")
    return result


def plan(migrations, history, ledger):
    """Never infer 'already applied' from table existence or repair remote history."""
    known = {m.version: m for m in migrations}
    for version, (name, checksum) in ledger.items():
        if version not in known or (known[version].name, known[version].checksum) != (name, checksum):
            raise ValueError("Applied migration was removed or changed")
        if history.get(version) != name:
            raise ValueError("Migration ledger and Supabase history disagree")
    pending = []
    for migration in migrations:
        version = migration.version
        if migration.baseline:
            if history.get(version) != migration.name:
                raise ValueError("FinBot baseline must already be applied with matching names")
        elif version not in ledger:
            if version in history:
                raise ValueError("Untracked migration already exists in Supabase; reconcile manually")
            if history and version <= max(history):
                raise ValueError("Pending migration predates shared Supabase history")
            pending.append(migration)
    return pending


def run(conn, migrations, *, apply=False):
    # One transaction covers the whole batch, ledger and Supabase history.
    # A failed SQL file cannot leave earlier files from this batch committed.
    with conn.transaction():
        if not apply:
            conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '120s'")
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            raise ValueError("Another TradingAgents migration is running")
        if not conn.execute("SELECT to_regclass(%s)", (HISTORY,)).fetchone()[0]:
            raise ValueError("Supabase migration history is missing")
        if apply:
            conn.execute(f"LOCK TABLE {HISTORY} IN SHARE ROW EXCLUSIVE MODE")
        history = dict(conn.execute(f"SELECT version,name FROM {HISTORY}").fetchall())
        exists = conn.execute("SELECT to_regclass(%s)", (LEDGER,)).fetchone()[0]
        ledger = {v: (n, h) for v, n, h in conn.execute(f"SELECT version,name,sha256 FROM {LEDGER}")} if exists else {}
        pending = plan(migrations, history, ledger)
        if apply:
            conn.execute("CREATE SCHEMA IF NOT EXISTS tradingagents_deploy")
            conn.execute("REVOKE ALL ON SCHEMA tradingagents_deploy FROM PUBLIC, anon, authenticated, lucrari_analysis_backend")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {LEDGER} (
                version text PRIMARY KEY, name text NOT NULL, sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
            conn.execute(f"REVOKE ALL ON {LEDGER} FROM PUBLIC, anon, authenticated, lucrari_analysis_backend")
            for migration in pending:
                for statement in migration.statements:
                    conn.execute(statement, prepare=False)
                conn.execute(f"INSERT INTO {HISTORY}(version,name,statements) VALUES (%s,%s,%s)",
                             (migration.version, migration.name, list(migration.statements)))
            # Prevent a new table from becoming an exposed Supabase endpoint.
            unsafe = conn.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='analysis_backend' AND c.relkind IN ('r','p')
                AND (NOT c.relrowsecurity
                  OR has_table_privilege('anon',c.oid,'SELECT,INSERT,UPDATE,DELETE')
                  OR has_table_privilege('authenticated',c.oid,'SELECT,INSERT,UPDATE,DELETE'))""").fetchall()
            if unsafe:
                raise ValueError("Backend tables require RLS and no mobile grants")
            for migration in migrations:
                if migration.version not in ledger:
                    conn.execute(f"INSERT INTO {LEDGER}(version,name,sha256) VALUES (%s,%s,%s)",
                                 (migration.version, migration.name, migration.checksum))
        return {"status": "migrated" if apply else "checked",
                "baseline_verified": sum(m.baseline for m in migrations),
                "pending": [m.version for m in pending], "applied": len(pending) if apply else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--validate", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        migrations = load(args.directory)
        if args.validate:
            print(json.dumps({"status": "validated", "files": len(migrations)}))
            return
        from tradingagents.service.backend.runtime import settings_from_env
        dsn = settings_from_env().database_url.get_secret_value()
        info = conninfo_to_dict(dsn)
        if info.get("host") not in {"localhost", "127.0.0.1", "::1"} and info.get("sslmode") != "verify-full":
            raise ValueError("Verified database TLS required")
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10, prepare_threshold=None) as conn:
            print(json.dumps(run(conn, migrations, apply=args.apply)))
    except Exception:
        # SQL diagnostics, DSNs and settings can contain private information.
        print(json.dumps({"status": "failed", "code": "DATABASE_MIGRATION_FAILED"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
