from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("missing process type: api or worker")

    process_type = sys.argv.pop(1)
    if process_type == "api":
        from evalweave.main import api_main

        api_main()
    elif process_type == "worker":
        from evalweave.workers.app import worker_main

        worker_main()
    elif process_type == "scheduler":
        from evalweave.workers.app import scheduler_main

        scheduler_main()
    else:
        raise SystemExit(f"unknown process type: {process_type}")


if __name__ == "__main__":
    main()
