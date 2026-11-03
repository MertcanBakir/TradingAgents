# Provider connections API — backend schema v2

Backend contract for the frontend implementer. This PR contains no frontend changes.
The feature is not live until the manual transition in
[`../deploy/transitions/README.md`](../deploy/transitions/README.md) is completed.

## Identity and access

Every `/v1/*` route requires `Authorization: Bearer <Supabase access token>`.
The backend verifies the token online with Supabase; it derives the user ID and
never accepts a user ID or an administrative role from the request body.

Only the configured `personal_user_id` and `allowed_user_ids` may enter. Invitees
must have a non-anonymous Supabase account. The existing explicitly configured
personal owner may keep their anonymous account. An empty invite list does not
open the service to everyone. Settings changes require service recreation.

The personal ChatGPT session belongs only to `personal_user_id`. Its provider is
not returned to invitees; personal analysis requests by invitees return 403.
The owner can alternatively use their own API connections just like invitees.

## Discover providers and models

`GET /v1/providers` returns enabled provider/model choices, for example:

```json
{
  "providers": [
    {
      "provider": "openai",
      "auth_type": "api_key",
      "models": ["operator-approved-model"],
      "model_options": [{"id": "operator-approved-model", "name": "operator-approved-model"}]
    }
  ],
  "unavailable": {"chatgpt": "PERSONAL_OWNER_ONLY"}
}
```

`allowed_models` in trusted server settings is the model allowlist. Do not show
all upstream CLI models as if they were enabled on this backend. Operators can
configure any supported simple API-key provider: `openai`, `anthropic`, `google`,
`xai`, `deepseek`, `qwen`, `qwen-cn`, `glm`, `glm-cn`, `minimax`, `minimax-cn`,
`openrouter`, `mistral`, `kimi`, `groq`, `nvidia`. The user only supplies keys for
providers they choose; one connection may serve both tiers.

Models remain subject to the user's provider account access and API billing.
This release does not query a per-user model catalog or permit arbitrary model
IDs/endpoints from the request. A server model allowlist is not proof of account
entitlement. Ollama, Azure, Bedrock and arbitrary OpenAI-compatible endpoints are
outside this credential contract.

## Create, list, verify, remove a connection

`POST /v1/connections`:

```json
{"provider":"openai","name":"Personal OpenAI","api_key":"<entered by user>"}
```

201 response:

```json
{"id":"<connection UUID>","provider":"openai","name":"Personal OpenAI","status":"active"}
```

The backend makes the provider adapter's model-list request before storing the
connection. No paid completion is used for this check. A successful catalog
request is not a guarantee of sufficient quota or access to every model; some
providers expose public catalogs, so it is not universally proof of key validity.
Do not describe a connection as having unlimited or confirmed funded access.
A provider without the supported verification route returns an explicit error.

`GET /v1/connections` returns `{"items":[...]}` with active connections' `id`,
`provider`, `name`, `status`, `created_at`, `verified_at`. Keys and ciphertext are
never returned. Maximum ten active connections per user. Names are optional,
trimmed, at most 80 characters and must not contain control characters.

`POST /v1/connections/{id}/verify` repeats the same limited check and updates the
verification timestamp. `DELETE /v1/connections/{id}` revokes the connection and
clears its encrypted secret. Removing a foreign/missing ID is an idempotent no-op;
it cannot revoke another user's connection. To replace a key, create a new
connection and revoke the old one. There is no in-place provider/key reassignment.

Do not persist the entered key in app preferences, analytics, URLs or logs. Send
it via HTTPS, clear the input after completion, and retain only the connection ID.
The backend encrypts keys with owner/connection/provider-bound AES-GCM; encryption
keys remain in protected server settings, outside the database.

## Start an API-key analysis

`POST /v1/analyses`, with `Idempotency-Key: <new UUID for this logical request>`:

```json
{
  "auth_type":"api_key",
  "request":{"ticker":"NVDA","analysis_date":"2026-11-02","language":"tr"},
  "models":{
    "quick":{"connection_id":"<OpenAI connection UUID>","model":"<enabled OpenAI model>"},
    "deep":{"connection_id":"<Anthropic connection UUID>","model":"<enabled Anthropic model>"}
  }
}
```

The provider is derived from the selected connection, never supplied by the
client. Connections must belong to the caller and be active. Both tiers may use
the same connection, different providers, or different connections to the same
provider. Each model client receives its own selected credential; it does not
fall back to another user's or server environment key.

The new API-key contract rejects top-level `connection_id`, `quick_model`,
`deep_model`, raw keys, user IDs and custom endpoint/configuration fields. It
replaces the old single-connection API-key body. Job and bindings commit together;
there is no legacy-job conversion or silent fallback when bindings are missing.

The existing owner-only personal ChatGPT body remains:

```json
{
  "auth_type":"personal_chatgpt",
  "request":{"ticker":"NVDA","analysis_date":"2026-11-02"},
  "quick_model":"<owner catalog model>",
  "deep_model":"<owner catalog model>"
}
```

It contains no `models` or connection IDs. Mixing personal ChatGPT with API-key
providers within one analysis is deliberately unsupported in this release.

202 returns job metadata. `GET /v1/analyses`, `GET /v1/analyses/{id}` and
`GET /v1/analyses/{id}/report` remain owner-scoped. Job metadata includes
`auth_type` and safe `models` provider/model snapshots, never credentials.
Follow the existing response for status/stage/finished timestamps and error code.
The chosen model snapshot and binding identities cannot be changed after creation.

Retried submission with the same idempotency key and same parsed payload returns
the original job. Reusing it with different model/connection selections returns
409 `IDEMPOTENCY_CONFLICT`. Do not generate a new key merely because the submission
response was lost.

## Capacity, failure and billing

One globally active analysis remains the initial admission limit. There is no
SQS integration, unbounded waiting queue or per-user parallel scheduling in this
release. 429 `CAPACITY_FULL` means the request was not accepted. Per-user daily
analysis and connection-rate limits also apply. A 202 job may still be `reserved`
briefly until the persistent worker claims it.

Missing/revoked connections and access/quota/temporary provider failures produce
explicit failures. The worker rechecks active ownership and model policy when
execution starts. Revoking a key does not recall an in-flight provider request
or instantly remove a key already loaded by a running child. Revocation at the
provider is a separate action. No automatic account switching or paid fallback.

Errors use `{"error":{"code":"...","retryable":false}}`:

| HTTP | Examples |
| --- | --- |
| 401 | `AUTHENTICATION_REQUIRED` |
| 403 | `ACCESS_DENIED` |
| 404 | `CONNECTION_NOT_FOUND`, `ANALYSIS_NOT_FOUND` |
| 409 | `IDEMPOTENCY_CONFLICT`, `CONNECTION_LIMIT`, `REPORT_NOT_READY` |
| 422 | `INVALID_REQUEST`, `MODEL_NOT_ALLOWED`, `PROVIDER_NOT_AVAILABLE`, `PROVIDER_ACCESS_ERROR`, `PROVIDER_VERIFICATION_UNSUPPORTED` |
| 429 | `CAPACITY_FULL`, `DAILY_ANALYSIS_LIMIT`, `RATE_LIMITED` |
| 503 | Auth/provider/storage/backend temporary availability errors |

For asynchronous failures, inspect the job's `error_code` as well as its terminal
status. Model-list verification and unit tests do not guarantee funded provider
access. Real paid model tests require separately supplied test credentials and
explicit authorization; none are part of this PR's CI.
