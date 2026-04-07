import os

_ENV_LOADED = False


def _iter_candidate_env_files():
    current = os.getcwd()
    while True:
        yield os.path.join(current, ".env")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def _load_env_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue

                if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]

                os.environ.setdefault(key, value)
    except OSError:
        return


def load_environment() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    for candidate in _iter_candidate_env_files():
        if os.path.isfile(candidate):
            _load_env_file(candidate)
            break

    _ENV_LOADED = True
