from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class FileTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    size_bytes: int
    sha256: str


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, storage_key: str) -> Path:
        key = PurePosixPath(storage_key)
        if key.is_absolute() or ".." in key.parts:
            raise ValueError("Invalid storage key")
        path = (self.root / Path(*key.parts)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Storage key escapes the configured root") from error
        return path

    def put(self, storage_key: str, stream: BinaryIO, max_bytes: int) -> StoredFile:
        destination = self.path_for(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError
                    digest.update(chunk)
                    output.write(chunk)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return StoredFile(size_bytes=size, sha256=digest.hexdigest())

    def delete(self, storage_key: str) -> None:
        self.path_for(storage_key).unlink(missing_ok=True)
