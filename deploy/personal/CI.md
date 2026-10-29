# TradingAgents CI

`.github/workflows/ci.yml` runs for pull requests, pushes to `main` and version
branches, and manual dispatches. It does not publish images or deploy to AWS.
It needs no repository secrets, AWS credentials, Supabase project or ChatGPT session.
Successful main pushes additionally export the tested runtime image for the
separate [opt-in CD pipeline](CD.md); CI itself never requests AWS credentials.

## Checks

- **tests**: the non-integration suite with `dev,backend` dependencies on Python
  3.11–3.14, plus a Python 3.12 run in the America/New_York time zone.
- **clean-install smoke**: imports the core package and CLI after an installation
  without optional dependencies.
- **ruff**: checks the entire repository.
- **production image (ARM64)**: builds the `Dockerfile.worker` test runner, then
  runs the non-integration suite in a container with no external network using
  the worker's constrained Python 3.13 dependencies. Separately builds the final runtime image and runs
  `container_smoke.py` as its non-root user with a read-only filesystem and no
  external network. Checks API startup, `/healthz`, unauthenticated route denial,
  and session initialization using fabricated configuration.
- **CI passed**: succeeds only when every required job succeeds. This is the
  stable check name to require in the GitHub `main` branch rules.

The workflow alone does not enforce merge protection. After a successful GitHub
run, configure `main` to require a pull request and the **CI passed** status check.

## Downloadable reports and Action versions

Every Python matrix run uploads a uniquely named JUnit XML artifact, and the
ARM64 worker run uploads `pytest-worker-arm64-py3.13`. Open a workflow run's
**Artifacts** section to download them. Reports are retained for 14 days and
upload steps run even after a test failure. A setup failure or a terminated
runner may prevent a report from being generated; in that case use the job log.
Reports contain test names, durations and failure diagnostics. Tests run with
fabricated credentials and no production secrets are supplied to CI.

The ARM64 test container is kept until its XML report is copied, including when
pytest returns a failure, then removed. The Docker `test` target still runs
the suite during a local build for compatibility with existing commands.

All external Actions in the CI and release workflows use full commit SHAs,
with their version labels in comments. Update these SHAs deliberately after
reviewing the upstream releases; moving a version tag cannot change the code
used by these workflows.

## Local verification

```sh
python -m pip install -e '.[dev,backend]'
python -m pip check
python -m pytest -q --junitxml=test-results/junit.xml
ruff check .

docker build --platform linux/arm64 -f Dockerfile.worker --target test -t tradingagents-worker-tests:ci .
docker build --platform linux/arm64 -f Dockerfile.worker -t tradingagents-worker:ci .
docker run --rm --platform linux/arm64 \
  --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 256 \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
  --mount "type=bind,source=$(pwd)/deploy/personal/container_smoke.py,target=/tmp/container_smoke.py,readonly" \
  --entrypoint python tradingagents-worker:ci /tmp/container_smoke.py
```

Docker must be running. An ARM64 host or ARM64 emulation is required locally;
GitHub uses the native `ubuntu-24.04-arm` runner.

## Database integration tests

`tests/test_hosted_backend_postgres.py` is explicitly excluded by the default
`not integration` selection. It requires local PostgreSQL binaries and the
separate FinBot repository's complete canonical migration chain and SQL acceptance
files. The three migration copies in `deploy/migrations/` are not a standalone
replacement for that chain. Database integration coverage needs a separate setup;
this workflow does not claim to validate real PostgreSQL migrations or live
Supabase behavior.
