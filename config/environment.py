from pathlib import Path

from dotenv import load_dotenv

LOCAL_ENVIRONMENT_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_local_environment(path: Path = LOCAL_ENVIRONMENT_FILE) -> None:
    # Existing environment wins; passwords containing ${...} stay literal.
    load_dotenv(path, override=False, interpolate=False)
