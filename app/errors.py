"""Exceptions shared across modules."""


class ConfigError(ValueError):
    """The users file is malformed."""


class InvalidInput(ValueError):
    """The client sent something unusable. The message is shown to the user."""
