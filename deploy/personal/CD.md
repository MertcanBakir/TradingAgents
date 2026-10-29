# Personal EC2 continuous delivery

CD publishes the exact Linux ARM64 production image that CI smoke-tested. It
never rebuilds an image after the CI gate. A new GitHub workflow run does not
update EC2 until `PRODUCTION_DEPLOY_ENABLED` is explicitly set to `true`.

## Flow and trust boundaries

1. A `push` to `main` runs CI, including the full Python matrix and worker tests.
2. After the production smoke test, CI saves the image and a manifest containing
   its portable config digest, source commit, CI run ID/attempt and archive SHA-256. The image
   artifact is retained for **one day**; test XML artifacts remain available for
   14 days. PR runs never export deployment images or receive AWS credentials.
3. `CD` is triggered by completed `CI` runs on main, or manually with a CI run ID.
   Before requesting AWS credentials it verifies the source repository, trusted
   workflow ID, `push` event, main branch, successful aggregate gate, exact run
   attempt, artifact and the current main commit. Obsolete runs are skipped.
4. The image-publish job verifies the archive config digest, loaded ARM64 rootfs
   layers, execution configuration and source label, then obtains temporary AWS credentials with GitHub OIDC. It publishes
   to the dedicated immutable ECR repository under a commit/run/attempt tag.
5. The production job is disabled during initial setup. When enabled, it checks
   main again and invokes the restricted `FinBotDeployTradingAgents` SSM document.
   It waits for this exact instance's command invocation and verifies the helper's
   confirmation; accepting a send-command request is not deployment success.

CD runs are serialized separately from CI and are not cancelled by a later push.
A server-side lock also prevents overlap if a GitHub runner stops while SSM is
still executing. The pipeline does not start stopped EC2 instances automatically.

## AWS and GitHub configuration

- Account: `500888846202`; region: `eu-west-1`.
- ECR: `lucrari/tradingagents-personal` (immutable tags).
- EC2: `i-0cc2d47020ed7347c`; existing instance role remains unchanged.
- CloudFormation stack: `finbot-tradingagents-cd`, from `cd-infrastructure.json`.
- Publish role: `FinBotTradingAgentsGitHubPublishRole`. Can authenticate to ECR
  and upload/describe images only in this repository; has no SSM permissions.
- Deploy role: `FinBotTradingAgentsGitHubDeployRole`. Can send only the custom
  deployment document to this EC2, and read command completion. It cannot use
  `AWS-RunShellScript`, change IAM, publish images, or read secret stores.
- The `*` resources for ECR authorization and SSM command-invocation reads are
  required by those APIs; resource-level actions remain narrowly scoped.
- GitHub environments `image-publish` and `production` permit only branch `main`.
  Role trust checks audience `sts.amazonaws.com` and this repository's immutable
  owner/repository IDs plus the respective environment. No long-lived access key
  is stored in GitHub or added to the instance.

Repository variables:

| Variable | Value |
| --- | --- |
| `AWS_PUBLISH_ROLE_ARN` | ARN of `FinBotTradingAgentsGitHubPublishRole` |
| `AWS_DEPLOY_ROLE_ARN` | ARN of `FinBotTradingAgentsGitHubDeployRole` |
| `PRODUCTION_DEPLOY_ENABLED` | **`false` until the first live deployment is approved** |

The immutable OIDC subject prefix in the template is specific to
`MertcanBakir/TradingAgents` (owner ID `151876718`, repository ID `1436778428`).
Repository rename/transfer or custom OIDC subjects require reviewing this trust
policy. Do not substitute a wildcard subject to work around authentication errors.

## Host installation and deployment behavior

The helper is root-owned at
`/usr/local/libexec/finbot-tradingagents/deploy.py`, with configuration at
`/etc/finbot-tradingagents/cd-config.json`. It supports the host's Python 3.9
without installing Python packages. The SSM document checks the helper's SHA-256
against the version in the workflow checkout before running it. Updating helper
code therefore requires a reviewed, versioned host installation as well as merging the code.
The SSM document selects `versions/<SHA256>.py` and verifies its hash, with a
hash-checked fallback to the original helper for older CD runs.

Non-secret host configuration:

```json
{
  "directory": "/opt/lucrari",
  "backup_directory": "/srv/lucrari-data/private/deploy-backups",
  "health_url": "https://52.209.149.36/healthz",
  "health_timeout": 180
}
```

The helper:

- Validates the incoming digest and source commit, existing immutable image,
  private `.env` permissions, Compose configuration and database access.
- Requires the running API/worker/maintenance images to agree with `.env`; it
  refuses to silently overwrite an inconsistent host configuration.
- Pulls the new image using the instance role and temporary Docker login storage.
  Verifies ARM64 architecture and the baked source revision before changing `.env`.
- Checks the image-packaged migration manifest/history read-only, then creates a
  private `.env` backup and applies the compatible migration batch transactionally.
  A migration failure stops before any container restart or image change.
- Preserves ownership/permissions and atomically
  changes only `LUCRARI_IMAGE` in the original configuration.
- Recreates only `api`, `worker`, `maintenance` with `--no-deps --no-build`.
  It does not replace the server's Compose file, Caddyfile, Caddy container or
  volumes, CA mount, settings or ChatGPT session.
- Requires two consecutive checks with correct running images, verified HTTPS
  `/healthz` and a nonempty fresh catalog refreshed after the database preflight
  timestamp. This checks worker model discovery, not only API liveness.
- On failure after mutation, restores the complete original `.env`, recreates
  the previous services and verifies them. The deployment is still reported as
  failed even when rollback succeeds. If rollback also fails, the private backup
  is retained and operator intervention is required.

An active analysis may be interrupted by a deployment, as explicitly accepted.
The preflight records its count but does not wait for it to finish. A single EC2
deployment can briefly interrupt API availability; this is not zero downtime.

## Setup, first release and recovery

Prepare/review the files and PR before the first live release. Provision the
CloudFormation template with `CAPABILITY_NAMED_IAM`, create the custom document
from `ssm-deploy.json`, install the root-owned helper/configuration, and verify
all role and environment restrictions. These setup steps do not deploy an image.

After the PR is merged, main CI creates the candidate image. With production
disabled, CD can validate OIDC and ECR publication without restarting containers.
After explicit first-live approval, enable production and manually run `CD` on
main with that successful **main push CI run ID**. Do not use the PR CI run ID.
Future main pushes then deploy automatically after successful CI.

If an artifact expires, rerun the original main-push CI run (not a manual CI
dispatch, which intentionally cannot produce a deployment candidate), then use
the new attempt. A CI run for an older main commit is deliberately rejected.

Inspect the GitHub CD summary for the command ID, commit and ECR digest. On a
runner timeout, inspect that SSM invocation before retrying: its server command
may still be running. A nonzero SSM command remains a failure; never suppress it
to make a workflow appear green.

Database delivery is described in [MIGRATIONS.md](MIGRATIONS.md). Only reviewed
additive SQL in the tested image is applied, before service recreation. Historical
migrations are verified and never replayed. Successful schema changes remain
applied if the image rolls back; there is no automatic schema downgrade.
Historical image and private-backup retention, EBS snapshots and disaster recovery
are separate operational tasks; this pipeline does not prune rollback images.
