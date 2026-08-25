# Downloader Bot code analysis

## Overview

Downloader Bot is implemented as a layered Telegram media processing system. The codebase is organized around a modular monolith with explicit runtime roles instead of a single long-running bot process.

## Main components

### Domain layer

Location: `downloader_bot/domain`

Contains the business model and state rules:

- download jobs and their lifecycle states;
- domain errors;
- framework-independent objects.

The layer avoids Telegram, database, queue, and HTTP dependencies, which keeps business rules testable.

### Application layer

Location: `downloader_bot/application`

Contains use cases and ports:

- job submission;
- cancellation;
- progress handling;
- delivery workflows;
- provider and storage interfaces.

The application layer coordinates behavior without knowing concrete infrastructure implementations.

### Telegram adapters

Location: `downloader_bot/adapters/telegram`

Responsibilities:

- receiving Telegram updates;
- converting user actions into application commands;
- rendering progress and results;
- sending final media.

The Telegram client is isolated from workers.

### Infrastructure

Location: `downloader_bot/infrastructure`

Contains external integrations:

- PostgreSQL persistence;
- Redis Streams transport;
- media downloading;
- provider resolution;
- artifact storage.

## Runtime flow

1. A Telegram request enters through the bot adapter.
2. The application creates a download job.
3. PostgreSQL stores canonical state.
4. Redis Streams distributes work to workers.
5. Workers download and produce artifacts.
6. Progress events are returned to the bot.
7. The bot delivers completed media.

## Strengths

- Clear dependency direction.
- Good separation between bot and worker responsibilities.
- Persistent job state allows recovery after restart.
- Offline tests can validate most logic without external services.
- Explicit settings and container construction simplify deployment.

## Areas to monitor

- External providers such as YouTube and social platforms can change behavior independently.
- Artifact cleanup and storage growth require operational monitoring.
- Cooperative cancellation depends on checkpoints inside download stages.
- Queue recovery logic should remain covered by integration tests.
