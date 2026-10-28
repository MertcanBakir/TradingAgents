"""Local headless entrypoint. Validation is the default; execution is explicit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from .config import effective_config
from .engine import AnalysisEngine
from .errors import AnalysisServiceError
from .memory import SQLiteMemoryRepository
from .models import AnalysisRequest, ServiceSettings


def _json_file(path: Path):
    if path.stat().st_size > 256 * 1024:
        raise AnalysisServiceError("INPUT_TOO_LARGE")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local analysis adapter; no HTTP server or JWT authentication."
    )
    parser.add_argument("--request", type=Path, required=True, help="AnalysisRequest JSON file")
    parser.add_argument(
        "--settings", type=Path, required=True, help="Trusted server settings JSON file, no secrets"
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        required=True,
        help="Trusted LOCAL identity, not remote authentication",
    )
    parser.add_argument("--analysis-id", type=UUID)
    parser.add_argument("--memory-db", type=Path)
    parser.add_argument(
        "--output", type=Path, help="New JSON file; existing files are never overwritten"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually call providers and persist local pending memory",
    )
    args = parser.parse_args(argv)
    try:
        request = AnalysisRequest.model_validate(_json_file(args.request))
        settings = ServiceSettings.model_validate(_json_file(args.settings))
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise AnalysisServiceError("OUTPUT_ALREADY_EXISTS")
        if not args.execute:
            payload = {
                "validated": True,
                "execution_started": False,
                "request": request.model_dump(mode="json"),
                "effective_config": effective_config(request, settings).model_dump(mode="json"),
            }
        else:
            if args.memory_db is None:
                raise AnalysisServiceError("MEMORY_DATABASE_REQUIRED")
            engine = AnalysisEngine(
                settings=settings, memory=SQLiteMemoryRepository(args.memory_db)
            )
            result = engine.run(request, user_id=args.user_id, analysis_id=args.analysis_id)
            payload = result.model_dump(mode="json")
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
        else:
            sys.stdout.write(text)
        return 0
    except AnalysisServiceError as exc:
        error = {"code": exc.code, "retryable": exc.retryable}
    except (ValidationError, ValueError, TypeError):
        error = {"code": "INVALID_INPUT", "retryable": False}
    except OSError:
        error = {"code": "LOCAL_IO_ERROR", "retryable": False}
    except Exception:
        error = {"code": "LOCAL_SERVICE_FAILED", "retryable": False}
    sys.stderr.write(json.dumps({"error": error}) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
