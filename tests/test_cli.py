from pathlib import Path

from evalweave.cli import child_commands


def test_child_commands_reuse_config_and_forward_worker_loglevel() -> None:
    config = Path("config/custom.yaml")
    api_command, worker_command = child_commands(config, "DEBUG")

    assert api_command[1:] == [
        "-m",
        "evalweave.subprocess_runner",
        "api",
        "--config",
        str(config),
    ]
    assert worker_command[1:] == [
        "-m",
        "evalweave.subprocess_runner",
        "worker",
        "--config",
        str(config),
        "--loglevel",
        "DEBUG",
    ]
