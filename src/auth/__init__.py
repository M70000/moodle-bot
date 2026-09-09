"""Módulo de autenticação Moodle / MinhaUFMG."""


def __getattr__(name: str):
    if name == "MoodleAuth":
        from src.auth.moodle_auth import MoodleAuth
        return MoodleAuth
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MoodleAuth"]
