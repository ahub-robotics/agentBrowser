"""Load dotenv configuration explicitly, without exposing credentials."""

import os
from pathlib import Path

from dotenv import load_dotenv


class EnvironmentFileError(ValueError):
    """An explicitly selected environment file could not be loaded."""


def _load_file(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise EnvironmentFileError("Environment file does not exist or is not a regular file.")
    try:
        # Supplying a stream avoids dotenv's implicit search of parent directories.
        with path.open(encoding="utf-8") as stream:
            load_dotenv(stream=stream, override=False)
    except (OSError, UnicodeError) as error:
        raise EnvironmentFileError("Environment file could not be read as UTF-8 text.") from error
    return path


def load_environment(env_file: str | Path | None = None) -> tuple[Path, ...]:
    """Load an explicit file, or local .env followed by AGENTRUN_ENV_FILE.

    Existing process variables always win. Without an explicit argument, values
    in local .env take precedence over the referenced file; relative references
    resolve beside local .env. A missing default .env is harmless, while an
    explicitly supplied path or reference must exist. No file is read on import.
    """
    if env_file is not None:
        return (_load_file(Path(env_file)),)

    local = Path.cwd() / ".env"
    loaded = []
    if local.exists():
        loaded.append(_load_file(local))
    reference = os.environ.get("AGENTRUN_ENV_FILE")
    if reference:
        path = Path(reference).expanduser()
        if not path.is_absolute():
            path = local.parent / path
        if path.resolve() not in loaded:
            loaded.append(_load_file(path))
    return tuple(loaded)
