# Design notes

Engineering rationale for the Local Typing Input Simulator: why the pieces are shaped the way
they are, which macOS behaviours forced those shapes, and what is deliberately out of scope.

[README.md](README.md) is the usage guide. This file is for changing the code.

---

## Scope, and what is deliberately absent

This is a structural prototype. Its purpose is to validate the application skeleton — the event
scheduler, the probabilistic typing behavior, the correction logic, and the safety controls —
not to be a finished product.

Explicitly deferred, and absent from the code:

* **No AI or machine learning.** The behavior generator is a seeded statistical model.
* **No physical HID hardware.** No USB device, no microcontroller. **No Bluetooth.**
* **No mouse movement or clicking.** The user places the caret.
* **No clipboard injection.** Text is emitted key by key, never pasted.
* **No browser extension, and no automatic Google Docs navigation.**
* **No authorship-detector evasion.**
* **No remote or background control.** No network code, no analytics.
* **No Unicode beyond common US-keyboard ASCII.** No emoji, no dead keys, no input methods, no
  composition events, no alternate keyboard layouts.
* **No tabs.** Tab characters are rejected with a message; never converted to spaces.

**The application cannot confirm what the target actually received.** It knows only what it
*emitted*. The shadow-buffer simulator proves the intended output is correct; it proves nothing
about the destination.

---

## Architecture

```
src/typing_simulator/
  application.py        wiring: picks the concrete backend, guard and generator
  config.py             TypingSettings and every tunable constant, in seconds
  errors.py             specific, user-safe error types
  domain/
    events.py           immutable KeyDown / KeyUp / Delay, NormalizedKey, TypingPlan
    state.py            AppState and the allowed-transition table
  behavior/
    base.py             BehaviorGenerator protocol - the swap point
    probabilistic.py    the seeded statistical generator
    keyboard_map.py     supported characters, normalization, QWERTY adjacency
  scheduler/
    clock.py            injectable Clock; RealClock and FakeClock
    scheduler.py        interruptible execution, progress, pause/resume/abort
  backends/
    base.py             KeyboardBackend protocol + held-key tracking
    quartz_backend.py   macOS default: CGEvent, one press and release at a time
    pynput_backend.py   portable fallback (see the note below)
    recording_backend.py records calls; used by every test
  safety/
    controller.py       the state machine and the safety rules
    permissions.py      TCC probes, the permission prompt, and who a grant
                        actually belongs to
    focus_guard.py      AppKit frontmost-application lookup
    caret_guard.py      Accessibility cursor-position snapshots
    hotkeys.py          global hotkeys, fail-closed
  simulation/
    text_buffer.py      the shadow buffer and the plan validation gate
  ui/
    overlay_window.py   the floating panel; buttons derive from AppState
    macos_overlay.py    accessory policy + non-activating NSPanel setup
    theme.py            light/dark macOS palettes and the stylesheet
    worker.py           worker-thread callbacks -> Qt signals
packaging/
  dev_bundle.py         the development .app that can hold a permission grant
  TypingSimulator.spec  the PyInstaller release bundle
```

The generator sits behind a `BehaviorGenerator` protocol. Everything else — the interface, the
scheduler, the safety controller and the keyboard backends — depends on that protocol, never on
the probabilistic implementation. Replacing it means writing one class that returns a
`TypingPlan` whose events replay to the requested text; no other module changes.

---

## Safety rules, and why each exists

* **Permission is a hard precondition.** `AXIsProcessTrusted()` and
  `CGPreflightPostEventAccess()` are both checked before every run, because without them macOS
  discards key events with no error at all. A probe that *fails* counts as denied, not as
  unknown: an unreadable permission is not evidence that typing would work. A missing permission
  disables Start rather than producing a silent no-op.
* **Stop hotkeys are verified, not assumed.** Installing a global event monitor *succeeds*
  whatever the permissions say — `addGlobalMonitorForEventsMatchingMask_handler_` returns a
  perfectly valid token — and the handler is then simply never called. Trusting the return value
  would mean running with a dead abort shortcut, the one failure this application must never
  have.
* **Nothing runs unverified.** Every plan is generated in full and replayed through the shadow
  buffer before typing can start. A plan that does not reproduce the text is rejected as an
  internal error.
* **The app refuses to type into itself.** If the overlay holds keyboard focus when Start is
  pressed, the run is refused. This is the backstop for the case where the non-activating panel
  behavior fails to apply.
* **Focus loss pauses, and returning resumes.** The frontmost application is polled every
  250 ms while typing. A pause the *user* asked for is never resumed automatically; coming back
  to the document is not a request to start typing again.
* **Abort is responsive.** Delays are never one uninterruptible sleep; they are waited on an
  event that abort sets, with a 20 ms tick as the worst case. The test suite asserts abort takes
  effect in under 100 ms.
* **Every exit path releases held keys.** Completion, abort, exception, focus-pause and window
  close all run `release_all()`, which attempts every tracked key even if one release fails.
* **Nothing is persisted.** The pasted text is never written to disk, never passed to the
  logging system, and never sent anywhere.
* **One job at a time.** A second run cannot start while one is in flight.

### There is no countdown

A fixed delay would only ever be a guess at when focus has settled. Instead the application
*watches* where focus actually goes and begins emitting the moment it matches the application
the overlay named — usually within a single poll. If focus never gets there within
`FOCUS_MATCH_TIMEOUT_SECONDS`, it types nothing and says so. Every observation is logged:

```
Start requested. Expected target=TextEdit (pid 4212); frontmost now=TextEdit (pid 4212); overlay holds key window=False
Focus watch: frontmost=TextEdit (pid 4212), overlay holds key window=False
Focus matched TextEdit (pid 4212); typing now
```

---

## Focus handling

Keeping the caret where the user put it takes three layers, because any one of them can fail on
a given macOS version:

1. **The panel does not activate.** A non-activating `NSPanel` plus an accessory activation
   policy means clicking it should not make this application active.
2. **Buttons do not take focus.** Every button has `Qt::NoFocus`. A Qt button normally grabs
   keyboard focus when clicked, which would make the panel the key window and pull the keyboard
   off the user's document — losing the opening characters of the run.
3. **Typing waits for focus to actually match.** The overlay continuously notes the last
   application that was genuinely in front — that is the one it names, and the only one it will
   type into. After Start it reactivates that application if necessary, then watches until the
   frontmost application really is that one *and* the overlay holds no key window.

### Two different questions

`NSWorkspace.frontmostApplication()` can still name the user's document application while the
overlay holds the key window, because an accessory app does not always register as "frontmost".
So the guard also asks `NSApplication` whether we hold a **key window** — *are we ourselves
receiving keystrokes?* — and treats a yes as "the overlay has focus" no matter what the
workspace reports. Being "active" is not enough on its own: an accessory app can report
`isActive` while having no window that accepts key events, in which case keystrokes still go to
the document.

The target is recorded on **pid alone**. An earlier version also required "we do not hold focus"
before recording it, which meant that if the overlay ever looked focused, no target was ever
recorded and the app would refuse to type no matter what the user did.

### Checking the cursor, not just the application

Coming back to the right *application* is not enough. If the user clicks somewhere else in the
document before returning, resuming would splice the remaining text into the wrong place. So
when a run pauses, the overlay snapshots the insertion point through the Accessibility API — the
focused UI element's role, title and `AXSelectedTextRange`, whose location is the caret offset —
and compares it before resuming.

* **Cursor unchanged** → resumes automatically.
* **Cursor moved** → does *not* resume. A warning names where to go back to, and typing
  continues by itself the moment it is put back.
* **Cursor not reported** → resumes on the application check alone, and says so. Many editors
  expose no caret at all; Google Docs draws its own, so this check cannot see it. Best-effort by
  design: an unreadable cursor never blocks a run.

Pressing **Resume** manually always proceeds — the user can see the screen, so it is their call
— but it warns if the cursor is not where typing stopped.

### Known limitation

The guard sees **applications**, nothing finer. It cannot tell that a different text field,
window, browser tab or document became active *inside the same application*. Switching from one
Google Doc to another in the same browser looks like no change at all. The prototype does not
inspect browser contents and never tries to determine whether Google Docs is open.

---

## macOS permissions: three services, not one

| Service | Read with | Gates |
| --- | --- | --- |
| `kTCCServiceAccessibility` | `AXIsProcessTrusted()` | the Accessibility API, and `NSEvent` global monitors |
| `kTCCServicePostEvent` | `CGPreflightPostEventAccess()` | whether `CGEventPost` reaches another application |
| `kTCCServiceListenEvent` | `CGPreflightListenEventAccess()` | `CGEventTap` — observing other apps' keystrokes |

The first two are controlled by the single **Accessibility** switch but are *different services*
and can disagree. When they do, the grant is stale rather than absent, and the banner says so
instead of telling the user to enable something they already enabled.

`kTCCServiceListenEvent` lives under its own **Input Monitoring** switch and is not implied by
Accessibility.

### Which permission gates the hotkeys

This depends entirely on the implementation, so each one declares it via
`requires_input_monitoring` rather than the caller guessing — guessing is what previously refused
to type over a permission the hotkeys never needed:

* `NSEventHotkeyService` (the macOS default) installs `NSEvent` global monitors, which Apple
  gates on the process being **trusted for accessibility**. It needs no Input Monitoring at all,
  which is why the checklist shows that row as *not needed* and it never blocks Start.
* `PynputHotkeyService` (the portable fallback) installs a `CGEventTap`, which
  `kTCCServiceListenEvent` is exactly what gates. It preflights Input Monitoring.

Preflighting Input Monitoring for the `NSEvent` implementation was doubly wrong: it refused over
an irrelevant permission, and because that preflight answers from process start-up and never
updates, it kept refusing even after the user granted it.

### Why the app must ask, not just look

Reading `AXIsProcessTrusted()` never registers the process with TCC. A user who adds the app by
hand with the **+** button gets an entry whose recorded code requirement is pinned to whatever
the binary looked like at that moment. For a locally built, ad-hoc signed bundle that
requirement includes the `cdhash`, so the next rebuild produces a binary the entry no longer
matches: System Settings still shows the row switched on while `AXIsProcessTrusted()` keeps
answering `False`.

`AXIsProcessTrustedWithOptions` with the prompt option makes macOS register the identity of the
process running *right now*. That is what turns an enabled-but-ineffective switch back into a
working grant.

The stale state also needs `CGRequestPostEventAccess()` specifically. A process already trusted
for Accessibility gets `True` straight back from `AXIsProcessTrustedWithOptions` without any
prompt, so without asking about Post Events by name there is nothing the application can do
about it from the inside.

### Why granting sometimes needs a restart

The probes do not age the same way, and the difference produces a failure that looks exactly
like a broken application:

* `AXIsProcessTrusted()` asks macOS afresh every time, so it starts answering `True` the moment
  the switch is flipped.
* `CGPreflightPostEventAccess()` and `CGPreflightListenEventAccess()` answer from a decision the
  process was handed when it **started**, and keep answering it for as long as it runs.

So a run launched before the grant watches Accessibility go green while the other two stay red
for ever, with nothing wrong in System Settings at all. Only a restart changes their minds — and
the fix for a genuinely stale grant, clearing the entry, would throw away the permission just
granted. `PermissionStatus.needs_restart_to_apply` distinguishes them, keyed on Post Events
alone: one switch grants Accessibility and Post Events together, so those two disagreeing can
only be a stale reading. Input Monitoring is a separate switch that is genuinely allowed to
differ, and folding it in would report "restart to apply" at someone who simply has not granted
it.

### The restart marker

The restart happens at most once per attempt, and "per attempt" is load-bearing. The new process
is told through `TYPING_SIMULATOR_RESTARTED` *why* it was restarted:

| Value | Meaning |
| --- | --- |
| `apply` | restarted to pick up a grant made since start-up; nothing was cleared |
| `reset` | restarted after clearing our own TCC entries, so macOS prompts again |

Only a previous `apply` suppresses another restart-to-apply. An unrecognised value — what an
older build wrote — is read as `apply`, because suppressing one useful restart is a far smaller
failure than a restart loop the user cannot interrupt.

**This distinction is the fix for a real bug.** Recording nothing but "we restarted" conflated
the two. Pressing **Grant permission** clears the entries and restarts, and macOS then
immediately asks the user to grant the permission — so the grant they made a second later found
the single allowance already spent. The banner sat on `✓ Accessibility  ✕ Post events` doing
nothing until the button was pressed a second time, which is exactly what it looked like from
the outside: granting the permission did not work.

The marker is dropped as soon as the permissions read as working, so a later grant in the same
run is picked up too.

### Never clear a live grant

`reset_permissions()` is scoped to the application's own bundle identifier, read from its own
`Info.plist` rather than hard-coded. `tccutil reset <service>` with no identifier revokes that
permission for *every* application on the machine, so a missing identifier returns `False`
rather than falling back to the unscoped form. Run from source there is no bundle identifier at
all, and the grant belongs to whatever launched the interpreter — clearing *that* would revoke a
permission the user granted to their terminal for everything else they do.

`restart_for_permission()` additionally refuses to clear anything while any of the three
services still reads as granted. Clearing exists only to get a prompt back, and a grant that is
in force does not need one; throwing it away to ask for it again would destroy the thing the
user came to the button to obtain.

### Who a grant belongs to

TCC does not grant permissions to "the Python script" — it grants them to the responsible
process. `permission_subject()` works out which that is, because naming the wrong one is why
toggling a switch so often changes nothing:

* inside a `.app`, the bundle itself;
* otherwise, the nearest ancestor process that is an app bundle (found via `ps`, which needs no
  extra permission of its own);
* failing that, the interpreter — named by its **real** path. A virtualenv's `bin/python` is a
  symlink, TCC records what it points at, and an entry added for the symlink can never match the
  process asking.

`_own_bundle_path()` is keyed on where the executable *is*, not on `sys.frozen`. macOS
attributes a grant to the running executable, so any interpreter inside a bundle — the frozen
build, and equally the development bundle, which is an ordinary interpreter — is one whose grant
belongs to that bundle. Asking about the location gets both right; asking about `sys.frozen`
gets the second one wrong.

---

## The development bundle

`packaging/dev_bundle.py` exists because neither obvious way to run this gives the executable a
stable identity:

* **From source**, the executable is the virtualenv's interpreter — a symlink to a Homebrew or
  system Python shared with everything else on the machine, which macOS additionally attributes
  to whichever terminal launched it. The grant cannot be made to stick.
* **`make app`** produces a real `.app` with its own identity, but it is ad-hoc signed, so that
  identity is a hash of its contents. Every rebuild invalidates the grant, making a one-line
  edit cost a minute of build time *and* a fresh round of permission granting.

The dev bundle is a real `.app` whose main executable is a **copy of the interpreter**, with the
source tree reached through `PYTHONPATH` from outside the bundle. Because the source lives
outside, editing it does not change the bundle's contents, so the ad-hoc signature — and the
permission granted to it — survives every edit. Rebuilding takes about a second.

Three details are load-bearing:

* **Copy the real interpreter, not `bin/python3.x`.** On a framework build that file is a ~50 KB
  stub whose job is to `exec` `Resources/Python.app/Contents/MacOS/Python` so the process becomes
  GUI-capable. Copying the stub produces a process that immediately replaces itself with
  Homebrew's binary, *outside* the bundle and signed `org.python.python`. macOS then answers two
  questions two ways: Accessibility is attributed to the responsible process — still the bundle,
  because that is what LaunchServices started — and comes out granted, while Post Events is
  attributed to the binary actually running and is refused forever. Accessibility green, Post
  events permanently red.
* **`PYTHONDONTWRITEBYTECODE=1`.** Anything written inside the bundle breaks the signature that
  holds the grant.
* **Ad-hoc signing is required, not cosmetic.** macOS will not grant Accessibility to an
  unsigned bundle, and a copied arm64 binary has an invalid signature until it is replaced.

The bundle must be started with `open` rather than by running the binary directly, because
`LSEnvironment` in `Info.plist` is what supplies `PYTHONHOME` and `PYTHONPATH`, and only
LaunchServices applies it.

It uses a deliberately different bundle identifier (`local.typing-simulator.dev`) from the
release build: the two are different binaries in different places, so macOS grants them
separately, and sharing an identifier would only make the System Settings list ambiguous.

---

## Why macOS does not use `pynput` at runtime

`pynput` resolves key codes through HIToolbox's Text Input Source APIs
(`TISCopyCurrentKeyboardInputSource`, `TSMGetInputSourceProperty`) using `ctypes`, from its own
worker threads. Current macOS asserts that those functions run on the main dispatch queue, so
the process dies with `SIGTRAP` — a bare `trace trap` with no Python traceback — as soon as a
real key event reaches the listener:

```
_dispatch_assert_queue_fail
dispatch_assert_queue
HIToolbox  islGetInputSourceListWithAdditions
HIToolbox  TSMGetInputSourceProperty
libffi -> _ctypes -> ... -> thread_run
```

This only bites once the event tap is *live*, which requires Accessibility permission — so it
looks like "it crashes only when the permission finally works."

Both sides are avoided:

* **Emission** uses `CGEventPost` (`quartz_backend.py`), which is safe from any thread and needs
  no layout lookup. Events carry a US-QWERTY virtual key code *and* an explicit Unicode string,
  so applications that inspect key codes behave normally and the character is correct whatever
  layout is active.
* **Hotkeys** use `NSEvent` global and local monitors (`NSEventHotkeyService`), whose handlers
  run on the main thread as part of the Cocoa event delivery Qt already pumps. There is no
  listener thread at all.

The `pynput` implementations remain in the tree as the non-macOS fallback.

---

## The pointer during a run

Two macOS defaults make synthetic typing feel broken, and both are switched off in
`_create_event_source`:

* Posting an event **suppresses real hardware input for 0.25 s** by default. During continuous
  typing that window never closes, so the mouse and keyboard appear frozen or jumpy for the
  entire run. The event source sets the suppression interval to zero and permits all events
  during suppression.
* A `NULL` event source **combines with the current hardware modifier state**, so a modifier the
  user happens to be holding leaks into every synthetic keystroke. A private-state source is
  used instead, and modifier flags are always set explicitly rather than inherited — a synthetic
  `p` carrying Control+Option would otherwise re-trigger the pause hotkey through our own global
  monitor.

---

## Typing model

Speed comes from the target WPM using the conventional five-characters-per-word approximation:

```
baseline interval = 60 / (WPM x 5)
```

The interval is never constant. Each delay is the baseline multiplied by:

* a **locally correlated speed burst** — an AR(1) process in log space, so consecutive
  characters share a coherent fast or slow burst instead of each delay being drawn
  independently;
* **independent jitter**, log-normal, scaled by the variation level;
* a **context multiplier** that lengthens pauses at word boundaries, after commas and
  semicolons, after sentence-ending punctuation, and around newlines.

Occasional hesitations are added on top. Every result is clamped into
`[MIN_DELAY_SECONDS, MAX_DELAY_SECONDS]`, so no delay can be negative or unreasonable whatever
was sampled. All constants live in [`config.py`](src/typing_simulator/config.py).

Deliberate mistakes are limited to three kinds — adjacent-key substitution, accidental
duplicate, and transposed adjacent letters — using an explicit US QWERTY adjacency map built
from the physical key stagger. Each one is always followed by a correction sequence (pause,
backspaces, pause, retype), and `TYPO_COOLDOWN_CHARACTERS` prevents errors clustering. Mistakes
are only made on plain ASCII letters not adjacent to a newline, so corrections stay unambiguous.

**The final text always matches what was asked for**, enforced by the shadow buffer rather than
merely intended. Turning corrections off introduces no mistakes at all, because an uncorrected
mistake would change the user's text.

Given the same seed and settings, the plan is reproduced exactly.

---

## The interface

The overlay is translucent glass rather than a solid panel: a vertical gradient that reads as
light falling across a curved surface, a bright hairline rim standing in for a specular
highlight, and frosted inner surfaces with pill-shaped buttons. It follows the system
appearance and restyles itself when macOS switches between light and dark, leaving checkboxes
and steppers unstyled so Qt draws the real native controls.

**The glass is done entirely in the stylesheet, not with an `NSVisualEffectView`.** A real
system blur behind the panel would be better, but there is no safe place to put the effect view.
Qt's own `NSView` *is* the window's content view, so adding the effect view to it makes it a
child — and children draw above their parent, covering the whole interface with a blank
rectangle. Re-parenting Qt's view under a container so the effect can sit beneath it does
render, but it takes the view out from under Qt's ownership: the window then logs "Already
setting window visible!" and segfaults on close. The stylesheet approach is stable, testable,
and renders the same everywhere.

Two smaller interface details worth knowing:

* **`clicked` carries a `checked` argument.** Connecting it straight to a slot with an optional
  parameter silently passes `False`, so the collapse button asked to *expand* every time and the
  panel could never be collapsed. It goes through a lambda that drops the argument.
* **`Ctrl+C` needs two things.** Qt's event loop blocks inside C++, so Python never reaches a
  bytecode boundary and the `SIGINT` handler never runs — a repeating timer hands control back
  often enough for it to fire. And the handler itself must do *nothing but set a flag*: Python
  runs signal handlers at whatever bytecode boundary it reaches, routinely mid-Qt-callback, and
  quitting from there re-enters Qt's event dispatch and segfaults.

---

## Tests

```bash
make test
```

The suite is deterministic (fixed seeds) and **never emits a real key event** — every test uses
the recording backend, a fake focus guard and a fake hotkey service. Qt runs under the
`offscreen` platform plugin, set in `conftest.py` before any test module imports PySide6.

It covers the generator, the shadow buffer, exact output across many passages and seeds, the
scheduler (ordering, pause, resume, abort responsiveness, cleanup on exceptions, progress
monotonicity, mutual exclusion), the safety layer (hotkey gating, self-target refusal,
focus-loss pause, resume verification, abort from every state, invalid transitions), the
permission probes and remedies, and — in `test_overlay_permissions.py` — the overlay's own
restart and escalation decisions against a fake controller.

That last module exists because the restart bug lived in a layer that had no tests at all: the
probes and the controller were both correct, and the overlay simply declined to act on them.

Two things the tests deliberately do *not* assert:

* **Never `assert not scheduler.is_paused` straight after seeing `RUNNING`.** The state changes
  before the worker thread wakes, deliberately — resuming the scheduler first would emit keys
  while the state still said `PAUSED`, and the focus monitor ignores every state but `RUNNING`.
  Wait for it instead.
* **Nothing touches the real TCC database.** Every probe and request is monkeypatched, so the
  suite gives the same answer on a machine where the permissions happen to be granted as on one
  where they are not — which matters, because the behaviour under test is precisely what the
  application does when they disagree.

### Manual testing checklist

Automated tests cannot prove that real key events land correctly in a real application. Work
through this by hand, in a scratch document.

1. Type 20 characters into TextEdit.
2. Type a sentence with capitalization and punctuation.
3. Type multiple paragraphs, including an empty line.
4. Type into a manually focused Google Doc.
5. Pause and resume halfway through, with the button and with `⌃⌥P`.
6. Abort during a long delay (low WPM makes this easy to hit).
7. Abort while a key event is being processed (high WPM).
8. Switch applications while typing; confirm it pauses and names the application that took
   focus.
9. Return and confirm typing resumes on its own, from where it stopped.
10. Switch away, click somewhere *else* in the same document, and confirm typing does **not**
    resume; put the cursor back and confirm it does.
11. Deny Accessibility permission and confirm the failure is safe: a clear message, no crash, no
    typing.
12. Close the overlay while typing; confirm no key stays held (check for runaway repeats).
13. Run at 20, 50 and 120 WPM; with a 0% and a 5% typo rate.
14. Verify the final text manually, character by character, against the source.

Overlay-specific:

15. **Click Start with the caret in another app and confirm focus does not move** — the text must
    land in that app, not in the overlay. This is the core of the overlay model.
16. Confirm **no characters are lost at the start** of a run.
17. Click into the overlay's text box, then press Start; confirm focus is handed back and the
    text still arrives in full.
18. Confirm the live `Will type into · …` line tracks whatever you click into.
19. Confirm the overlay stays visible and on top while another application is active, including
    over a full-screen window.
20. Drag the overlay by its header; collapse and expand it with the **⌃** / **⌄** button.
21. Check the log during a run and confirm the focus log names the application you expect.

Permission-specific — these are the paths that were broken, and they cannot be checked from
source, only from `make dev` or `make app`:

22. With nothing granted, launch and confirm macOS prompts by itself.
23. Grant Accessibility in System Settings *while the app is running*, and confirm it restarts
    on its own and comes back with everything green. No second button press.
24. Press **Grant permission** with nothing granted, confirm it clears and restarts, then grant
    in System Settings and confirm step 23 still happens — the reset must not consume the
    restart.
25. With everything granted, confirm the banner is gone and Start is enabled.

---

## Known limitations

* **Applications, not fields.** See [Known limitation](#known-limitation) above.
* **Fast synthetic input outruns web editors.** Google Docs and similar drop or reorder input
  under fast synthetic key streams. The shadow buffer proves what was emitted; the destination
  is not something the prototype can detect or correct. Lowering WPM is the only mitigation.
* **The target may rewrite the text.** Autocorrect and smart substitutions act after delivery.
* **Off macOS it fails closed.** The focus guard reports "cannot determine", so arming is
  refused rather than allowed unverified.
