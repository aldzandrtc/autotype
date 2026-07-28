"""Error types for the typing simulator.

These live in a single module (rather than next to the code that raises them)
because every layer - domain, behavior, scheduler, safety and UI - needs to
reference them, and a shared module avoids import cycles.

Every error carries a ``user_message`` that is safe to show in the interface.
User messages must never contain the pasted text.
"""

from __future__ import annotations


class TypingSimulatorError(Exception):
    """Base class for all errors raised by the application."""

    #: Short, user facing description.  Subclasses may override.
    default_message = "An unexpected error occurred."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)

    @property
    def user_message(self) -> str:
        """Message that is safe to display in the interface."""
        return str(self)


# --------------------------------------------------------------------------
# Input / settings validation
# --------------------------------------------------------------------------


class ValidationError(TypingSimulatorError):
    """Base class for recoverable, user-correctable problems."""


class EmptyInputError(ValidationError):
    default_message = "Enter or paste some text before arming."


class InputTooLongError(ValidationError):
    default_message = "The text is longer than the configured maximum."

    def __init__(self, length: int, maximum: int) -> None:
        super().__init__(
            f"The text is {length:,} characters long; the maximum is {maximum:,}. "
            "Shorten the text or raise MAX_TEXT_LENGTH in config.py."
        )
        self.length = length
        self.maximum = maximum


class UnsupportedCharacterError(ValidationError):
    """Raised when the text contains characters this prototype cannot type."""

    default_message = "The text contains characters this prototype cannot type."

    def __init__(self, descriptions: list[str]) -> None:
        joined = ", ".join(descriptions[:8])
        if len(descriptions) > 8:
            joined += f", and {len(descriptions) - 8} more"
        super().__init__(
            "The text contains characters this prototype cannot type: "
            f"{joined}. Unsupported characters are never removed silently - "
            "edit the text and try again."
        )
        self.descriptions = descriptions


class InvalidSettingsError(ValidationError):
    default_message = "One or more typing settings are out of range."


# --------------------------------------------------------------------------
# Plan / execution
# --------------------------------------------------------------------------


class PlanValidationError(TypingSimulatorError):
    """The generated plan does not reproduce the requested text."""

    default_message = (
        "Internal error: the generated event plan does not reproduce the "
        "requested text. Nothing was typed."
    )


class SchedulerError(TypingSimulatorError):
    default_message = "The typing scheduler failed."


class KeyboardBackendError(TypingSimulatorError):
    default_message = "The keyboard backend failed to emit a key event."


class HotkeyError(TypingSimulatorError):
    default_message = (
        "The global hotkey listener could not be started. Typing is disabled "
        "until Control+Option+P and Control+Option+Escape are available."
    )


class MissingPermissionError(TypingSimulatorError):
    """macOS Accessibility / Input Monitoring permission is missing."""

    default_message = (
        "macOS permission appears to be missing. Open System Settings -> "
        "Privacy & Security -> Accessibility (and Input Monitoring) and allow "
        "the application that runs this prototype, then restart it."
    )


class FocusTargetError(TypingSimulatorError):
    """The frontmost application is not a usable, or the expected, target."""

    default_message = "The frontmost application is not a valid typing target."


class InvalidStateTransitionError(TypingSimulatorError):
    """An operation was requested that the current state does not allow."""

    def __init__(self, current: object, requested: object) -> None:
        super().__init__(
            f"Cannot move from {getattr(current, 'name', current)} to "
            f"{getattr(requested, 'name', requested)}."
        )
        self.current = current
        self.requested = requested


class AbortedError(TypingSimulatorError):
    """Internal control-flow signal raised inside the scheduler on abort."""

    default_message = "Typing was aborted."
