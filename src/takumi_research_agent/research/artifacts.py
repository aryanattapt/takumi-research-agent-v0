from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    """Content-addressed immutable artifacts written atomically on one filesystem."""

    def __init__(self, root: Path, *, max_bytes: int = 5_000_000):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def put(self, kind: str, content: str | bytes) -> tuple[str, Path]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", kind):
            raise ValueError("Artifact kind is invalid")
        data = content.encode("utf-8") if isinstance(content, str) else content
        if len(data) > self.max_bytes:
            raise ValueError("Artifact exceeds the configured size limit")
        digest = hashlib.sha256(data).hexdigest()
        directory = self.root / kind / digest[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        final_path = directory / digest
        if final_path.exists():
            self.read(digest, kind=kind)
            return digest, final_path
        fd, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final_path)
            try:
                directory_fd = os.open(directory, os.O_DIRECTORY)
            except (AttributeError, OSError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return digest, final_path

    def read(self, digest: str, *, kind: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", kind):
            raise ValueError("Artifact reference is invalid")
        path = (self.root / kind / digest[:2] / digest).resolve()
        if self.root not in path.parents:
            raise ValueError("Artifact path escaped the artifact root")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError("Referenced artifact is missing") from exc
        if len(data) > self.max_bytes or hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError("Referenced artifact failed its content hash check")
        return data
