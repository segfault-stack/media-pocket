# Downloader Bot v2 architecture

The active implementation is a modular monolith with two runtime roles:

- `python -m downloader_bot bot` owns Telegram polling, status messages, callbacks, and result delivery.
- `python -m downloader_bot worker` resolves providers, downloads media, and writes job-scoped artifacts.

Both roles share PostgreSQL as canonical state, Redis Streams as the delivery/progress transport, and the `downloads` volume as artifact storage. `python -m downloader_bot migrate` is the only schema-init role.

## Dependency rule

`bootstrap → adapters/infrastructure → application → domain`

`domain` and `application` are framework-free. The offline architecture acceptance test enforces that they do not import aiogram, SQLAlchemy, Redis, or HTTP clients and that no production module imports removed legacy packages. Importing `main` does not read configuration or construct resources.

## Durable pipeline

1. Telegram converts updates into `SubmitDownloadCommand`.
2. PostgreSQL atomically inserts an active job and its outbox record. A partial unique index deduplicates active requests by user and URL.
3. The bot-side outbox publisher writes job IDs to the `downloads` Redis Stream.
4. Workers use the `download-workers` consumer group. Unacknowledged messages can be reclaimed with `XAUTOCLAIM`; queued/retrying rows are reconciled from PostgreSQL on startup.
5. Workers publish stage/percentage events to `download-progress` and persist an artifact manifest in the job directory.
6. The bot process throttles edits to stage changes, 2% changes, or two seconds, claims `ready → delivering`, and sends media.
7. A process restart in `delivering` never automatically resends media. The user gets an explicit manual retry action.

Queued cancellation becomes terminal immediately. Running cancellation is cooperative and checked between streamed chunks and stages. Retryable failures use bounded exponential backoff with jitter.

## Operations

Compose provides `bot`, scalable `worker`, `migrate`, PostgreSQL, Redis with AOF, and cookie sync. PostgreSQL, Redis, downloads, and logs use separate persistent volumes. Workers never receive or construct a Telegram client.

The legacy handlers, callback-bag services, global context, and compatibility wrappers have been removed. The only production entrypoint is the explicit `downloader_bot` bootstrap.
