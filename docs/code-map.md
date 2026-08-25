# Downloader Bot code map

## Entry points

`main.py` delegates execution to the package entry point.

`downloader_bot/__main__.py` starts one of the supported roles:

- `bot` — Telegram runtime;
- `worker` — background downloader;
- `migrate` — database migration role.

## Bootstrap

`downloader_bot/bootstrap`

Creates runtime dependencies and loads settings.

Important files:

- `container.py` — dependency assembly;
- `runtime.py` — role lifecycle;
- `settings.py` — typed configuration.

## Download pipeline

`application/use_cases.py` contains orchestration logic.

`infrastructure/download.py` performs actual media retrieval.

`infrastructure/platforms.py` handles provider-specific resolution.

## Persistence

`infrastructure/database.py` stores durable job information.

`infrastructure/redis_streams.py` provides queue and event transport.

## Telegram delivery

`adapters/telegram/gateway.py` handles Telegram communication.

`adapters/telegram/presenter.py` converts internal state into user-facing messages.

`adapters/telegram/router.py` maps updates to application actions.

## Testing strategy

The repository includes acceptance tests that validate architecture, domain behavior, runtime components, and Telegram-facing flows while keeping external dependencies isolated where possible.
