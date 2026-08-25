import os
import sys
import time

HEARTBEAT_FILE = "/tmp/bot_heartbeat"
MAX_HEARTBEAT_AGE_SECONDS = 60.0


def check_heartbeat() -> bool:
    if not os.path.exists(HEARTBEAT_FILE):
        print(f"Heartbeat file {HEARTBEAT_FILE} does not exist", file=sys.stderr)
        return False
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        age = time.time() - mtime
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                ts = float(content)
                age = min(age, time.time() - ts)
        if age > MAX_HEARTBEAT_AGE_SECONDS:
            print(
                f"Heartbeat stale: age={age:.1f}s > {MAX_HEARTBEAT_AGE_SECONDS}s",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - healthchecks must convert all failures to unhealthy
        print(f"Heartbeat check failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    # PostgreSQL and Redis have dedicated Compose healthchecks. Re-importing their
    # Python clients in this 30-second probe caused a full CPU core and ~50 MiB
    # allocation burst, while the heartbeat already proves the bot event loop is alive.
    return 0 if check_heartbeat() else 1


if __name__ == "__main__":
    raise SystemExit(main())
