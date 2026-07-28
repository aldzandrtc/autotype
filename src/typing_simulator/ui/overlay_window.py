"""The floating overlay.

A frameless, always-on-top panel that sits above whatever the user is working
in.  It does not take keyboard focus when its buttons are clicked (see
:mod:`typing_simulator.ui.macos_overlay`), so the caret stays exactly where the
user left it and **Start** types straight into it.

The overlay never moves or clicks the mouse.  "Wherever the cursor is" means
the text caret in the application that currently has keyboard focus - the user
places it themselves, and the overlay shows which application that is, live,
before anything is typed.

Button availability and status text are derived from the current
:class:`~typing_simulator.domain.state.AppState`.  The pasted text is never
written to disk, never logged, and never shown anywhere except in the editor.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QIntValidator,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from typing_simulator import config
from typing_simulator.behavior.keyboard_map import find_unsupported, normalize_line_endings
from typing_simulator.behavior.probabilistic import rough_duration_estimate
from typing_simulator.config import TypingSettings, VariationLevel
from typing_simulator.domain.state import AppState
from typing_simulator.errors import TypingSimulatorError
from typing_simulator.safety.controller import SafetyController, TargetApplication
from typing_simulator.safety.permissions import PermissionStatus, describe_permission_remedy
from typing_simulator.scheduler.scheduler import Progress, RunResult, RunStatus
from typing_simulator.ui import theme
from typing_simulator.ui.worker import ControllerBridge

logger = logging.getLogger(__name__)

#: Compact macOS glyph form; the spelled-out names live in the tooltip.
HOTKEY_REMINDER = "⌃⌥P  Pause / Resume          ⌃⌥⎋  Abort"

HOTKEY_TOOLTIP = (
    f"{config.HOTKEY_PAUSE_RESUME_LABEL}: pause or resume\n"
    f"{config.HOTKEY_ABORT_LABEL}: abort immediately\n"
    "These work from any application, while typing is under way."
)

POINTER_WARNING = (
    "While typing, your mouse and keyboard may feel laggy or jump — the "
    "system is being fed synthetic key events. Let it finish, or press "
    "⌃⌥⎋ to abort."
)

SUPPORTED_HINT = (
    "Letters, digits, spaces, newlines and common US punctuation. Tabs, emoji "
    "and other Unicode are rejected, never silently removed."
)

ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

INPUT_MONITORING_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
)

#: How often the overlay re-reads the permissions while the banner is showing.
#: Granting a permission in System Settings takes effect immediately, so the
#: banner clearing itself is what tells the user it worked - waiting for them to
#: find a "Re-check" button makes a granted permission look like it failed.
PERMISSION_POLL_INTERVAL_MS = 1500

#: Dot shown next to the state name in the header pill.
STATE_TONES: dict[AppState, str] = {
    AppState.IDLE: "muted",
    AppState.VALIDATING: "muted",
    AppState.ARMING: "warn",
    AppState.ARMED: "warn",
    AppState.RUNNING: "ok",
    AppState.PAUSED: "warn",
    AppState.COMPLETED: "ok",
    AppState.ABORTED: "bad",
    AppState.ERROR: "bad",
}


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


def set_tone(widget: QWidget, tone: str) -> None:
    """Apply a semantic colour that survives a light/dark theme switch."""
    widget.setProperty("tone", tone)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class DragBar(QWidget):
    """Header strip that moves the frameless overlay."""

    def __init__(self, window: QWidget) -> None:
        super().__init__()
        self._window = window
        self._offset: QPoint | None = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self._offset = (
                event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._offset is not None:
            self._window.move(event.globalPosition().toPoint() - self._offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)


class OverlayWindow(QWidget):
    def __init__(
        self,
        controller: SafetyController,
        bridge: ControllerBridge,
        *,
        backend_error: str | None = None,
        permission_granted: bool | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._bridge = bridge
        self._backend_error = backend_error
        self._permission_granted = permission_granted
        self._permission_status: PermissionStatus | None = None
        self._hotkeys_blocked = False
        self._settings_url = ACCESSIBILITY_SETTINGS_URL
        self._text_is_valid = False
        self._should_be_visible = False
        self._closing = False

        self.setWindowTitle("Typing Simulator")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(452)

        self._build_ui()
        self._make_controls_focus_free()
        self._connect_signals()
        self._apply_theme()

        self._on_text_changed()
        self._apply_state(AppState.IDLE)
        self._refresh_permission_banner()
        if self._backend_error:
            self._set_error(self._backend_error)
        self._move_to_default_corner()
        # Deferred to the first turn of the event loop so the overlay is on
        # screen before a system dialog appears in front of it.
        QTimer.singleShot(0, self._request_permission_on_launch)

    # -- construction ------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.PANEL_MARGINS)  # room for the drop shadow

        self._panel = QFrame()
        self._panel.setObjectName("panel")
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(44)
        self._shadow.setOffset(0, 10)
        self._panel.setGraphicsEffect(self._shadow)
        outer.addWidget(self._panel)

        layout = QVBoxLayout(self._panel)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(11)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_permission_banner())
        layout.addWidget(self._build_target_row())
        self._body = self._build_body()
        layout.addWidget(self._body)
        layout.addWidget(self._build_controls())

    def _make_controls_focus_free(self) -> None:
        """Stop buttons from asking for keyboard focus.

        A Qt button normally takes focus when clicked, which makes the panel
        become the key window and pulls keyboard focus off the user's document
        - so the first characters would go nowhere.  Only the text editor and
        the settings fields genuinely need focus; buttons never do.
        """
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _build_header(self) -> QWidget:
        bar = DragBar(self)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel("Typing Simulator")
        title.setObjectName("title")
        row.addWidget(title)
        row.addStretch(1)

        self.state_dot = QLabel("●")
        self.state_label = QLabel()
        set_tone(self.state_label, "muted")
        row.addWidget(self.state_dot)
        row.addWidget(self.state_label)

        self.collapse_button = QPushButton("⌃")
        self.collapse_button.setObjectName("glyph")
        self.collapse_button.setFixedSize(19, 19)
        self.collapse_button.setToolTip("Collapse or expand")
        row.addWidget(self.collapse_button)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("glyph")
        self.close_button.setFixedSize(19, 19)
        self.close_button.setToolTip("Quit")
        row.addWidget(self.close_button)
        return bar

    def _build_permission_banner(self) -> QWidget:
        self._banner = QFrame()
        self._banner.setObjectName("banner")
        layout = QVBoxLayout(self._banner)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(7)

        self.banner_label = QLabel()
        self.banner_label.setWordWrap(True)
        set_tone(self.banner_label, "warn")
        layout.addWidget(self.banner_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        # Listed first because it is the one that usually works.  Reading the
        # permission never registers this process with macOS, so an entry added
        # by hand can stop matching the binary; asking macOS to prompt
        # re-registers the running identity and makes the switch take effect.
        self.request_button = QPushButton("Request permission")
        self.request_button.setObjectName("chip")
        self.request_button.setToolTip(
            "Ask macOS for the permission named in this banner. Use this first: "
            "it registers this exact build with the system, which an entry "
            "added by hand may no longer match."
        )
        self.open_settings_button = QPushButton("Open settings")
        self.open_settings_button.setObjectName("chip")
        self.recheck_button = QPushButton("Re-check")
        self.recheck_button.setObjectName("chip")
        row.addWidget(self.request_button)
        row.addWidget(self.open_settings_button)
        row.addWidget(self.recheck_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._banner.hide()
        return self._banner

    def _build_target_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.target_dot = QLabel("●")
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        self.target_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.target_dot, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.target_label, 1)
        return row

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Paste the text here. It is never saved to disk or logged."
        )
        mono = QFont()
        mono.setFamilies(["Menlo", "Monaco", "Courier New"])
        mono.setPointSize(12)
        self.editor.setFont(mono)
        self.editor.setFixedHeight(126)
        layout.addWidget(self.editor)

        info = QHBoxLayout()
        info.setSpacing(8)
        info.setContentsMargins(2, 0, 2, 0)
        self.character_count_label = QLabel()
        set_tone(self.character_count_label, "muted")
        self.validation_label = QLabel()
        self.validation_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        info.addWidget(self.character_count_label)
        info.addStretch(1)
        info.addWidget(self.validation_label)
        layout.addLayout(info)

        layout.addWidget(self._build_settings())

        hint = QLabel(SUPPORTED_HINT)
        set_tone(hint, "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return body

    def _build_settings(self) -> QWidget:
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        def caption(text: str) -> QLabel:
            label = QLabel(text)
            set_tone(label, "caption")
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            return label

        self.wpm_spin = QSpinBox()
        self.wpm_spin.setRange(config.WPM_MIN, config.WPM_MAX)
        self.wpm_spin.setValue(config.WPM_DEFAULT)
        self.wpm_spin.setSuffix(" WPM")
        grid.addWidget(caption("Speed"), 0, 0)
        grid.addWidget(self.wpm_spin, 0, 1)

        self.variation_combo = QComboBox()
        for level in VariationLevel:
            self.variation_combo.addItem(level.value.capitalize(), level)
        self.variation_combo.setCurrentIndex(
            self.variation_combo.findData(VariationLevel.MEDIUM)
        )
        grid.addWidget(caption("Variation"), 0, 2)
        grid.addWidget(self.variation_combo, 0, 3)

        self.typo_spin = QDoubleSpinBox()
        self.typo_spin.setRange(config.TYPO_RATE_MIN * 100, config.TYPO_RATE_MAX * 100)
        self.typo_spin.setSingleStep(0.5)
        self.typo_spin.setDecimals(1)
        self.typo_spin.setValue(config.TYPO_RATE_DEFAULT * 100)
        self.typo_spin.setSuffix(" %")
        grid.addWidget(caption("Typos"), 1, 0)
        grid.addWidget(self.typo_spin, 1, 1)

        self.corrections_check = QCheckBox("Correct mistakes")
        self.corrections_check.setChecked(True)
        self.corrections_check.setToolTip(
            "Every deliberate mistake is corrected, so the final text always "
            "matches. With this off, no mistakes are introduced at all."
        )
        grid.addWidget(self.corrections_check, 1, 2, 1, 2)

        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("random")
        self.seed_edit.setValidator(QIntValidator(0, 2_147_483_647, self))
        grid.addWidget(caption("Seed"), 2, 0)
        grid.addWidget(self.seed_edit, 2, 1)

        self.estimate_label = QLabel("—")
        grid.addWidget(caption("Duration"), 2, 2)
        grid.addWidget(self.estimate_label, 2, 3)
        return host

    def _build_controls(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.pause_button = QPushButton("Pause")
        self.abort_button = QPushButton("Abort")
        self.abort_button.setObjectName("danger")
        self.reset_button = QPushButton("Reset")
        for button in (
            self.start_button,
            self.pause_button,
            self.abort_button,
            self.reset_button,
        ):
            button.setMinimumHeight(30)
            buttons.addWidget(button)
        buttons.setStretch(0, 3)
        layout.addLayout(buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)

        self.progress_detail_label = QLabel("Not started.")
        set_tone(self.progress_detail_label, "muted")
        layout.addWidget(self.progress_detail_label)

        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.pointer_warning_label = QLabel(POINTER_WARNING)
        self.pointer_warning_label.setWordWrap(True)
        set_tone(self.pointer_warning_label, "warn")
        self.pointer_warning_label.hide()
        layout.addWidget(self.pointer_warning_label)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        set_tone(self.error_label, "bad")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        separator = QFrame()
        separator.setObjectName("divider")
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        self.hotkey_label = QLabel(HOTKEY_REMINDER)
        set_tone(self.hotkey_label, "muted")
        self.hotkey_label.setToolTip(HOTKEY_TOOLTIP)
        self.hotkey_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hotkey_label)
        return host

    def _connect_signals(self) -> None:
        self.editor.textChanged.connect(self._on_text_changed)
        self.wpm_spin.valueChanged.connect(self._update_estimate)
        self.variation_combo.currentIndexChanged.connect(self._update_estimate)
        self.typo_spin.valueChanged.connect(self._update_estimate)
        self.corrections_check.toggled.connect(self._update_estimate)

        self.start_button.clicked.connect(self.start_typing)
        self.pause_button.clicked.connect(self._on_pause_resume)
        self.abort_button.clicked.connect(self._on_abort)
        self.reset_button.clicked.connect(self._on_reset)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        self.close_button.clicked.connect(self.close)
        self.recheck_button.clicked.connect(self._on_recheck_permission)
        self.request_button.clicked.connect(self._on_request_permission)
        self.open_settings_button.clicked.connect(self._on_open_settings)

        self._bridge.stateChanged.connect(self._on_state_changed)
        self._bridge.progressChanged.connect(self._on_progress)
        self._bridge.statusChanged.connect(self._set_status)
        self._bridge.warningRaised.connect(self._set_warning)
        self._bridge.errorRaised.connect(self._set_error)
        self._bridge.targetCaptured.connect(self._on_target_captured)
        self._bridge.runFinished.connect(self._on_run_finished)

        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(config.FRONTMOST_PREVIEW_INTERVAL_MS)
        self._preview_timer.timeout.connect(self._refresh_target_preview)
        self._preview_timer.start()
        self._refresh_target_preview()

        # Only runs while something is actually wrong, so the common case costs
        # nothing; see PERMISSION_POLL_INTERVAL_MS for why it exists at all.
        self._permission_timer = QTimer(self)
        self._permission_timer.setInterval(PERMISSION_POLL_INTERVAL_MS)
        self._permission_timer.timeout.connect(self._refresh_permission_banner)

        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _scheme: self._apply_theme())
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_application_state_changed)

    # -- theme -------------------------------------------------------------
    def _apply_theme(self) -> None:
        hints = QApplication.styleHints()
        scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else None
        dark = scheme is not Qt.ColorScheme.Light
        palette = theme.DARK if dark else theme.LIGHT
        self.setStyleSheet(theme.stylesheet(palette))
        self._shadow.setColor(QColor(0, 0, 0, palette.shadow_alpha))
        # Re-polish so `tone` properties pick up the new palette.
        for widget in self.findChildren(QWidget):
            if widget.property("tone") is not None:
                set_tone(widget, widget.property("tone"))

    def _move_to_default_corner(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:  # pragma: no cover - always present in practice
            return
        available = screen.availableGeometry()
        self.adjustSize()
        self.move(available.right() - self.width() - 16, available.top() + 16)

    # -- permission banner -------------------------------------------------
    def _refresh_permission_banner(self) -> None:
        """Re-probe every permission and show or hide the banner accordingly."""
        status = self._controller.permission_status()
        self._permission_status = status
        self._permission_granted = self._controller.check_permission()
        # Input Monitoring gates the abort hotkey, and typing is refused
        # without a working abort - so it blocks Start just as hard.
        self._hotkeys_blocked = status.input_monitoring is False

        message = self._permission_message(status)
        if message:
            self.banner_label.setText(message)
            self._banner.show()
            self._start_permission_polling()
        else:
            self._banner.hide()
            self._stop_permission_polling()

        self._settings_url = (
            INPUT_MONITORING_SETTINGS_URL
            if self._permission_granted is not False and self._hotkeys_blocked
            else ACCESSIBILITY_SETTINGS_URL
        )
        self._apply_state(self._controller.state)
        self.adjustSize()

    def _permission_message(self, status: PermissionStatus) -> str:
        """The banner text, or ``""`` when there is nothing to warn about.

        The symptom is stated before the remedy, and the two permissions are
        never merged into one sentence: telling someone to enable Accessibility
        when Accessibility is already on is exactly how a missing Input
        Monitoring grant gets misread as a broken application.
        """
        remedy = describe_permission_remedy(status)
        if self._permission_granted is False:
            symptom = (
                "Key events would be silently discarded, so nothing would be "
                "typed."
            )
            if status.stale_grant_suspected:
                symptom = (
                    "Permission is switched on but is not in effect for this "
                    "build, so nothing would be typed."
                )
            return f"{symptom} {remedy}"
        if self._hotkeys_blocked:
            return (
                f"{config.HOTKEY_PAUSE_RESUME_LABEL} and "
                f"{config.HOTKEY_ABORT_LABEL} would never fire, so a run could "
                "not be stopped from another application. Typing is refused "
                f"until they work. {remedy}"
            )
        return ""

    def _start_permission_polling(self) -> None:
        if not self._closing and not self._permission_timer.isActive():
            self._permission_timer.start()

    def _stop_permission_polling(self) -> None:
        self._permission_timer.stop()

    def _request_permission_on_launch(self) -> None:
        """Ask macOS for a blocking permission at startup, the way a normal app does.

        Only reading ``AXIsProcessTrusted()`` leaves this application invisible
        to macOS until the user hunts it down with the "+" button - and an
        entry added that way is pinned to the build it was added for, so it
        quietly stops working on the next one.  Asking properly puts the
        request in front of the user *and* registers the identity that is
        actually running.

        macOS shows the dialog at most once per identity, so this is not a
        prompt on every launch, and it is skipped entirely when there is
        nothing to ask for.
        """
        blocked = self._permission_granted is False or self._hotkeys_blocked
        if self._closing or not blocked:
            return
        permission = (
            "Input Monitoring"
            if self._permission_granted is not False and self._hotkeys_blocked
            else "Accessibility"
        )
        logger.info("%s permission is missing; asking macOS to prompt.", permission)
        self._on_request_permission()

    def _on_recheck_permission(self) -> None:
        self._refresh_permission_banner()

    def _on_request_permission(self) -> None:
        """Ask macOS itself for the permission named in the current banner.

        macOS shows the prompt at most once per identity, so a second press can
        look like it did nothing.  The re-probe afterwards is what makes the
        outcome visible either way.
        """
        try:
            self._controller.request_permission()
        except Exception:  # noqa: BLE001 - a refused prompt is not a crash
            logger.exception("Requesting the blocking permission failed")
        self._refresh_permission_banner()

    def _on_open_settings(self) -> None:
        QDesktopServices.openUrl(QUrl(self._settings_url))

    # -- live target preview -----------------------------------------------
    def _refresh_target_preview(self) -> None:
        """Show, before anything is typed, where Start would send the text."""
        if self._controller.state is not AppState.IDLE:
            return
        app = self._controller.current_frontmost()
        if app is None:
            self._set_target_text(
                "Frontmost application unknown — Start will be refused.", "bad"
            )
        elif self._controller.frontmost_is_self(app):
            self._set_target_text(
                "The overlay has focus. Click into your document first.", "warn"
            )
        else:
            self._set_target_text(f"Will type into  ·  {app.name}", "ok")

    def _set_target_text(self, message: str, tone: str) -> None:
        set_tone(self.target_label, tone)
        set_tone(self.target_dot, tone)
        self.target_label.setText(message)

    # -- text validation ---------------------------------------------------
    def _on_text_changed(self) -> None:
        text = normalize_line_endings(self.editor.toPlainText())
        count = len(text)
        maximum = config.MAX_TEXT_LENGTH
        self.character_count_label.setText(f"{count:,} / {maximum:,}")

        if count == 0:
            self._text_is_valid = False
            self._set_validation("Paste some text", "muted")
        elif count > maximum:
            self._text_is_valid = False
            self._set_validation(f"{count - maximum:,} over the limit", "bad")
        else:
            unsupported = find_unsupported(text)
            if unsupported:
                self._text_is_valid = False
                shown = ", ".join(u.describe() for u in unsupported[:2])
                if len(unsupported) > 2:
                    shown += f", +{len(unsupported) - 2} more"
                self._set_validation(f"Unsupported: {shown}", "bad")
            elif not text.strip():
                self._text_is_valid = False
                self._set_validation("Whitespace only", "bad")
            else:
                self._text_is_valid = True
                self._set_validation("All characters supported", "ok")

        self._update_estimate()
        self._apply_state(self._controller.state)

    def _set_validation(self, message: str, tone: str) -> None:
        set_tone(self.validation_label, tone)
        self.validation_label.setText(message)

    def _update_estimate(self) -> None:
        if not self._text_is_valid:
            self.estimate_label.setText("—")
            return
        try:
            settings = self.current_settings()
            settings.validate()
        except TypingSimulatorError:
            self.estimate_label.setText("—")
            return
        estimate = rough_duration_estimate(self.editor.toPlainText(), settings)
        self.estimate_label.setText(f"~{format_duration(estimate)}")

    # -- settings ----------------------------------------------------------
    def current_settings(self) -> TypingSettings:
        seed_text = self.seed_edit.text().strip()
        return TypingSettings(
            wpm=self.wpm_spin.value(),
            variation=self.variation_combo.currentData(),
            typo_rate=self.typo_spin.value() / 100.0,
            corrections_enabled=self.corrections_check.isChecked(),
            seed=int(seed_text) if seed_text else None,
        )

    def _settings_widgets(self) -> list[QWidget]:
        return [
            self.editor,
            self.wpm_spin,
            self.variation_combo,
            self.typo_spin,
            self.corrections_check,
            self.seed_edit,
        ]

    # -- collapsing --------------------------------------------------------
    def toggle_collapsed(self, collapsed: bool | None = None) -> None:
        target = not self._body.isVisible() if collapsed is None else collapsed
        self._body.setVisible(not target)
        self.collapse_button.setText("⌄" if target else "⌃")
        self.adjustSize()
        self.resize(self.width(), self.sizeHint().height())

    @property
    def is_collapsed(self) -> bool:
        return not self._body.isVisible()

    # -- actions -----------------------------------------------------------
    def start_typing(self) -> None:
        """Prepare the run; typing begins once focus is seen on the target."""
        if self._controller.state is not AppState.IDLE:
            return
        self._clear_error()
        if self._backend_error:
            self._set_error(self._backend_error)
            return
        try:
            settings = self.current_settings()
            plan = self._controller.prepare(self.editor.toPlainText(), settings)
        except TypingSimulatorError as exc:
            self._set_error(exc.user_message)
            self._refresh_permission_banner()
            return
        except Exception as exc:  # noqa: BLE001 - never dump a traceback into the UI
            logger.exception("Preparing the run failed")
            self._set_error(f"Could not start ({type(exc).__name__}).")
            return

        self.estimate_label.setText(f"~{format_duration(plan.estimated_duration)}")
        self.progress_bar.setValue(0)
        self.progress_detail_label.setText(plan.statistics.summary())
        self._begin_typing()

    def _begin_typing(self) -> None:
        """Hand over to the controller, which waits for focus to match."""
        try:
            self._controller.begin_typing()
        except TypingSimulatorError as exc:
            self._set_error(exc.user_message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Starting the run failed")
            self._set_error(f"Could not start typing ({type(exc).__name__}).")

    def _on_pause_resume(self) -> None:
        self._clear_error()
        try:
            if self._controller.state is AppState.PAUSED:
                self._controller.resume()
            else:
                self._controller.pause("Paused from the overlay.")
        except TypingSimulatorError as exc:
            self._set_error(exc.user_message)

    def _on_abort(self) -> None:
        self._clear_error()
        try:
            self._controller.abort("Aborted from the overlay.")
        except TypingSimulatorError as exc:
            self._set_error(exc.user_message)

    def _on_reset(self) -> None:
        self._clear_error()
        self.progress_bar.setValue(0)
        self.progress_detail_label.setText("Not started.")
        try:
            self._controller.reset()
        except TypingSimulatorError as exc:
            self._set_error(exc.user_message)
        self._refresh_target_preview()

    # -- signal handlers (interface thread) --------------------------------
    def _on_state_changed(self, _old: AppState, new: AppState) -> None:
        if new is AppState.RUNNING:
            # A focus warning is stale the moment typing is under way again.
            self._clear_error()
        self._apply_state(new)

    def _on_progress(self, progress: Progress) -> None:
        self.progress_bar.setValue(int(progress.fraction * 100))
        self.progress_detail_label.setText(
            f"{progress.net_characters:,} chars  ·  "
            f"{progress.event_index:,}/{progress.total_events:,} events  ·  "
            f"~{format_duration(progress.remaining_seconds)} left"
        )

    def _on_target_captured(self, target: TargetApplication) -> None:
        self._set_target_text(f"Typing into  ·  {target.name}", "ok")

    def _on_run_finished(self, result: RunResult) -> None:
        if result.status is RunStatus.COMPLETED:
            self.progress_bar.setValue(100)
            self.progress_detail_label.setText(
                f"{result.net_characters:,} characters typed "
                f"({result.characters_typed:,} key presses). Check the result "
                "yourself — the overlay cannot see what the target received."
            )

    def _on_application_state_changed(self, _state) -> None:
        """Keep the overlay on screen when another application takes over.

        Qt hides ``Qt::Tool`` windows on macOS when the application
        deactivates; the overlay must stay put, since being useful while
        another app is frontmost is its entire purpose.
        """
        if self._should_be_visible and not self.isVisible():
            self.show()

    # -- state-driven interface --------------------------------------------
    def _apply_state(self, state: AppState) -> None:
        self.state_label.setText(state.name)
        tone = STATE_TONES.get(state, "muted")
        set_tone(self.state_label, tone)
        set_tone(self.state_dot, tone)

        blocked = (
            bool(self._backend_error)
            or self._permission_granted is False
            or self._hotkeys_blocked
        )
        can_start = state is AppState.IDLE and self._text_is_valid and not blocked
        self.start_button.setEnabled(can_start)
        self.start_button.setToolTip(
            self._blocked_reason() if blocked
            else "Start typing at the caret in the target application"
        )
        self.pause_button.setEnabled(state in (AppState.RUNNING, AppState.PAUSED))
        self.pause_button.setText("Resume" if state is AppState.PAUSED else "Pause")
        self.pause_button.setToolTip(
            "Switch away and typing pauses by itself; come back and it resumes."
            if state is AppState.RUNNING
            else "Return to the target application and typing resumes on its own."
        )

        self.pointer_warning_label.setVisible(
            state in (AppState.RUNNING, AppState.PAUSED)
        )
        self.abort_button.setEnabled(
            state in (AppState.ARMING, AppState.ARMED, AppState.RUNNING, AppState.PAUSED)
        )
        self.reset_button.setEnabled(state.is_terminal)

        # Lock everything that would invalidate the prepared plan.
        locked = state.is_active
        self._lock_settings(locked)

    def _blocked_reason(self) -> str:
        """Why Start is unavailable - named precisely enough to be actionable."""
        if self._backend_error:
            return "No keyboard backend is available, so nothing can be typed."
        if self._permission_granted is False:
            return "Accessibility permission is required before typing can start."
        return (
            "Input Monitoring permission is required, because typing is only "
            f"allowed while {config.HOTKEY_ABORT_LABEL} can stop it."
        )

    def _lock_settings(self, locked: bool) -> None:
        for widget in self._settings_widgets():
            widget.setEnabled(not locked)
        self.editor.setReadOnly(locked)

    # -- status ------------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _set_warning(self, message: str) -> None:
        set_tone(self.error_label, "warn")
        self.error_label.setText(message)
        self.error_label.show()
        self.adjustSize()

    def _set_error(self, message: str) -> None:
        set_tone(self.error_label, "bad")
        self.error_label.setText(message)
        self.error_label.show()
        self.adjustSize()

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._should_be_visible = True

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing while arming, running or paused performs full cleanup.

        Guarded against re-entry: quitting the application closes the window,
        whose close handler would otherwise quit the application again.
        """
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._should_be_visible = False
        self._preview_timer.stop()
        self._permission_timer.stop()
        try:
            self._controller.shutdown()
        except Exception:  # noqa: BLE001 - shutdown must never block closing
            logger.exception("Shutdown cleanup failed")
        event.accept()
        # Quit on the next turn of the event loop, never from inside this
        # handler: closing is often *already* part of a quit, and calling
        # quit() re-entrantly from a close handler segfaults Qt.
        QTimer.singleShot(0, QApplication.quit)
