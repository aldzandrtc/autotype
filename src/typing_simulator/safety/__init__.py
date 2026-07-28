"""Safety: the state machine, the focus guard and the global hotkeys."""

from typing_simulator.safety.controller import (
    ControllerCallbacks,
    SafetyController,
    TargetApplication,
)
from typing_simulator.safety.focus_guard import (
    FocusGuard,
    FrontmostApp,
    MacFocusGuard,
    create_focus_guard,
)
from typing_simulator.safety.hotkeys import HotkeyService, PynputHotkeyService
from typing_simulator.safety.permissions import (
    PermissionStatus,
    PermissionSubject,
    accessibility_permission_granted,
    describe_permission_remedy,
    input_monitoring_permission_granted,
    permission_holder_name,
    permission_status,
    permission_subject,
    post_event_permission_granted,
    request_accessibility_permission,
    request_input_monitoring_permission,
    typing_permission_granted,
)

__all__ = [
    "ControllerCallbacks",
    "FocusGuard",
    "FrontmostApp",
    "HotkeyService",
    "MacFocusGuard",
    "PermissionStatus",
    "PermissionSubject",
    "PynputHotkeyService",
    "SafetyController",
    "TargetApplication",
    "accessibility_permission_granted",
    "create_focus_guard",
    "describe_permission_remedy",
    "input_monitoring_permission_granted",
    "permission_holder_name",
    "permission_status",
    "permission_subject",
    "post_event_permission_granted",
    "request_accessibility_permission",
    "request_input_monitoring_permission",
    "typing_permission_granted",
]
