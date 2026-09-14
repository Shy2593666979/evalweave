from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def child_commands(config: Path, loglevel: str) -> tuple[list[str], list[str]]:
    """Build commands with the current interpreter so the active uv environment is reused."""
    api_command = [
        sys.executable,
        "-m",
        "evalweave.subprocess_runner",
        "api",
        "--config",
        str(config),
    ]
    worker_command = [
        sys.executable,
        "-m",
        "evalweave.subprocess_runner",
        "worker",
        "--config",
        str(config),
        "--loglevel",
        loglevel,
    ]
    return api_command, worker_command


def stop_processes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the EvalWeave API and Celery worker")
    parser.add_argument("--config", type=Path, default=Path("config/application.yaml"))
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()

    commands = child_commands(args.config, args.loglevel)
    processes: list[subprocess.Popen[bytes]] = []
    exit_code = 0
    try:
        for command in commands:
            processes.append(subprocess.Popen(command))

        while True:
            stopped_process = next(
                (process for process in processes if process.poll() is not None),
                None,
            )
            if stopped_process is not None:
                exit_code = stopped_process.returncode or 0
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        stop_processes(processes)

    raise SystemExit(exit_code)
