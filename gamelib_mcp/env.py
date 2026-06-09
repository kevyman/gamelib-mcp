"""Environment loading helpers."""

from pathlib import Path

from dotenv import load_dotenv


_PROJECT_DOTENV = Path(__file__).resolve().parents[1] / ".env"


def load_project_dotenv(path: str | Path | None = None) -> bool:
    dotenv_path = Path(path) if path is not None else _PROJECT_DOTENV
    try:
        if not dotenv_path.is_file():
            return False
    except OSError:
        return False

    return load_dotenv(dotenv_path)
