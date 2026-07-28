# Local Typing Input Simulator

A macOS-first **prototype**: a small always-on-top overlay that floats above whatever you are
working in. You paste text into it, click into the document where you want that text, and
press **Start**. The overlay captures whichever application currently has keyboard focus and
replays your text into it as individual keyboard events, at the caret you placed.

The overlay is a *non-activating* panel and its buttons never request keyboard focus, so
clicking **Start** leaves the caret exactly where you put it. There is no Dock icon and no menu
bar — quit with the **✕** in the overlay's header.

> "Wherever the cursor is" means the **text caret** in the application that has keyboard focus.
> The overlay never moves or clicks the mouse — that is out of scope for this prototype. You
> place the caret; it types there.

This is a structural prototype. Its purpose is to validate the application skeleton: the
event scheduler, the probabilistic typing behavior, the correction logic, and the safety
controls. It is not a finished product.

---

## Responsible-use scope

The application is intended for:

* validating the architecture, event scheduler and safety controls;
* accessibility and automation experiments on **your own** machine and **your own** documents;
* testing how applications react to a stream of individual key events.

It is deliberately **not** built for evading authorship detection, impersonating a human
author where that matters, or operating anything remotely or unattended. It has no network
code, no analytics, and no background remote control.

You must position the caret yourself, the overlay shows which application it is about to type
into before it starts, and every run is gated behind two global stop hotkeys.

---

## What this prototype deliberately does **not** do

Explicitly deferred, and absent from the code:

* **No AI or machine learning.** The behavior generator is a seeded statistical model.
* **No physical HID hardware.** No USB device, no microcontroller.
* **No Bluetooth.**
* **No mouse movement or clicking.**
* **No clipboard injection.** Text is emitted key by key, never pasted.
* **No browser extension, and no automatic Google Docs navigation.** You navigate.
* **No authorship-detector evasion.**
* **No remote or background control.**
* **No Unicode beyond common US-keyboard ASCII.** No emoji, no dead keys, no input methods,
  no composition events, no alternate keyboard layouts.
* **No tabs.** Tab characters are rejected with a message; they are never converted to spaces.

**The application cannot confirm what Google Docs — or any other target — actually
received.** It knows only what it *emitted*. The shadow-buffer simulator proves the intended
output is correct; it proves nothing about the destination. Always check the result yourself.

---

## Requirements

* macOS (developed and tested on macOS 26; AppKit is required for the focus guard)
* Python 3.11 or newer
* Accessibility permission, and possibly Input Monitoring permission (see below)

The application runs on other platforms only in a degraded form: the focus guard fails
closed, so arming is refused.

---

## Quick start

```bash
git clone https://github.com/aldzandrtc/autotype.git
```

```bash
cd autotype && make app
```

That builds `dist/Typing Simulator.app`. Open it:

```bash
open "dist/Typing Simulator.app"
```

Or put it in your Applications folder:

```bash
make install
```

**Build the app rather than running from source if you can.** macOS assigns Accessibility
permission to the *responsible process* — for a bundle that is the app itself, so you enable
"Typing Simulator" once and it stays enabled. Run from source and the permission belongs to
your terminal instead, which is easy to get wrong and has to be redone whenever you switch
terminals.

## Running from source

```bash
make setup
```

```bash
make run
```

`make setup` creates `.venv` and installs everything into it; nothing is installed
system-wide. Other targets:

| Target | What it does |
| --- | --- |
| `make help` | List every target |
| `make setup` | Create the virtualenv and install the app and dev tools |
| `make run` | Run the overlay from source |
| `make test` | Run the automated test suite |
| `make app` | Build `dist/Typing Simulator.app` |
| `make install` | Build the app and copy it to `/Applications` |
| `make clean` | Remove build output and caches |
| `make distclean` | Also remove the virtualenv |

Requires Python 3.11 or newer. Override the interpreter with
`make PYTHON=/path/to/python3.12 setup`. `Ctrl+C` in the launching terminal quits cleanly.

## macOS permissions

Emitting key events requires macOS **Accessibility** permission. The application probes the
permission state and can ask macOS to show its standard permission prompt; it never tries to
weaken or bypass a grant.

> ### Read this if nothing gets typed
>
> Without Accessibility permission, macOS discards synthetic key events **silently**.
> `CGEventPost` returns no error, and `pynput`'s listener even reports itself as running. So
> the symptom is not an error — it is *nothing at all happening*.
>
> Because that failure is invisible, the overlay checks the permission before every run and
> refuses to start without it, showing an orange banner with **Request permission**, **Open
> settings** and **Re-check** buttons. If the banner is showing, no amount of clicking Start
> will type anything until the permission is granted — that is deliberate.
>
> **Press "Request permission" first.** The button requests whichever permission the banner
> currently names. Reading a permission never registers this process with macOS. An entry you
> added by hand with the **+** button records the app's identity at that moment, so a later
> rebuild produces a binary the entry no longer matches — the switch stays on and stops
> working. Requesting makes macOS register the build that is running now.

Two distinct TCC services are involved, both controlled by the single Accessibility switch:
`kTCCServiceAccessibility` (read via `AXIsProcessTrusted()`) and `kTCCServicePostEvent` (read
via `CGPreflightPostEventAccess()`), which is the one that actually decides whether
`CGEventPost` reaches another application. When those two disagree, the grant is stale rather
than absent, and the banner says so instead of telling you to enable something you already
enabled.

**Check every permission from the command line:**

```bash
.venv/bin/python -c "from typing_simulator.safety.permissions import permission_status; print(permission_status())"
```

If `accessibility` or `post_events` is `False`:

1. Open **System Settings → Privacy & Security → Accessibility**.
2. Enable the right thing. If you built the app with `make app`, that is **Typing Simulator**
   itself. If you are running from source, it is the application that *launches* Python — your
   terminal (Terminal, iTerm2, Ghostty, …) or your IDE, because macOS assigns the permission to
   the responsible parent process rather than to the Python script. The overlay's banner names
   whichever applies to how you started it.
3. If it is already listed and enabled, **do not toggle it** — that never fixes anything. The
   entry itself has gone stale: remove it with the **−** button and let the app ask again
   (**Request permission** in the banner), or run `make reset-permissions`.
4. **Quit that application completely with ⌘Q and reopen it.** This is the step people miss:
   macOS decides a process's trust when it launches, so an already-running terminal keeps the
   old, denied answer no matter what you change in Settings.
5. Re-run the check above. Only when both fields are `True` will typing work.

If the terminal route keeps failing, add the Python binary itself with the **+** button. Find
its real path with:

```bash
.venv/bin/python -c "import sys, os; print(os.path.realpath(sys.executable))"
```

**Input Monitoring** is a separate permission, used by the global hotkey listener, and enabling
Accessibility does **not** enable it. A global event monitor installs successfully without it
and then simply never fires, so the application preflights the permission instead of trusting
that the monitor was created: without it, the abort hotkey would be dead while appearing to
work. Typing is refused until it is granted. Use **Request permission** in the banner, or grant
the same application access under **Privacy & Security → Input Monitoring**, then restart it.

If permission is missing:

* the application does not crash and does not type anything;
* the overlay shows the banner and **disables Start**;
* the same explanation is shown if a run is attempted anyway.

---

## Global hotkeys

| Shortcut | Effect |
| --- | --- |
| `Control` + `Option` + `P` | Pause, or resume |
| `Control` + `Option` + `Escape` | Abort immediately |

Both are *stop* controls, and that is deliberate: they work from any application, so you can
halt a run while your document is frontmost and typing is under way. They are shown permanently
in the overlay's footer.

Starting has no shortcut — it is a deliberate action taken from the overlay itself.

**Fail closed:** the hotkey listener must be verified active before typing may start. If it
cannot start, the application refuses to type at all.

---

## How a run works

1. Paste your text into the overlay. The character count and per-character validation update as
   you type.
2. Set speed, timing variation, typo rate, corrections, and optionally a seed.
3. Click into the document where you want the text and place the caret.
4. The overlay's header line shows, live, **where Start would send the text** — for example
   `Will type into: Google Chrome`. Check it before you start.
5. Click **Start**. The application:
   * validates the settings and the text;
   * generates the complete event plan up front;
   * replays that plan through the in-memory shadow buffer and refuses to continue unless it
     reproduces your text exactly;
   * verifies the global hotkey listener is running;
   * hands keyboard focus back to your document if clicking Start pulled it onto the overlay;
   * watches, and logs, where focus actually is until it matches the application it named;
   * begins emitting, one key press at a time, the moment it matches.
6. The overlay stays exactly where it is, fully visible, showing live progress. Collapse it
   yourself with the **⌃** button if you want it smaller.
7. Pause, resume or abort at any point, from the overlay or the global hotkeys.

**There is no countdown.** A fixed delay would only ever be a guess at when focus has settled.
Instead the application *watches* where focus actually goes, and begins emitting the moment it
matches the application the overlay named — usually within a single poll. If focus never gets
there within `FOCUS_MATCH_TIMEOUT_SECONDS`, it types nothing and says so.

Every step is logged, so a run that does not start can be diagnosed from the terminal:

```
Start requested. Expected target=TextEdit (pid 4212); frontmost now=TextEdit (pid 4212); overlay holds key window=False
Focus watch: frontmost=TextEdit (pid 4212), overlay holds key window=False
Focus matched TextEdit (pid 4212); typing now
```

## Safety behavior

* **Permission is a hard precondition.** `AXIsProcessTrusted()` and
  `CGPreflightPostEventAccess()` are both checked before every run, because without them macOS
  discards key events with no error at all. A probe that *fails* counts as denied, not as
  unknown: an unreadable permission is not evidence that typing would work. A missing
  permission disables Start rather than producing a silent no-op.
* **Stop hotkeys are verified, not assumed.** `CGPreflightListenEventAccess()` is checked
  before typing, because installing a global event monitor succeeds without Input Monitoring
  permission and the handler is then never called. Trusting the monitor's return value would
  mean running with a dead abort shortcut.
* **You always know where it will type.** The frontmost application is shown live in the
  overlay while idle, before anything is emitted.
* **Nothing runs unverified.** Every plan is generated in full and replayed through the shadow
  buffer before typing can start. A plan that does not reproduce the text is rejected as an
  internal error.
* **The app refuses to type into itself.** If the overlay holds keyboard focus when you press
  Start, the run is refused with an explanation — nothing is typed. This is the backstop for the
  case where the non-activating panel behavior fails to apply.
* **Focus loss pauses, and returning resumes.** While typing, the frontmost application is
  polled every 250 ms. If it is no longer the captured target, the run pauses immediately, held
  keys are released, and a warning names the application that took focus. Switch back and typing
  **resumes by itself** — no button to press. A pause *you* asked for (the button or
  `⌃⌥P`) is never resumed automatically; coming back to the document is not a request to start
  typing again.
* **Abort is responsive.** Delays are never one uninterruptible sleep; they are waited on an
  event that abort sets, with a 20 ms tick as the worst case. The automated test asserts abort
  takes effect in under 100 ms.
* **Every exit path releases held keys.** Completion, abort, exception, focus-pause and window
  close all run `release_all()`, which attempts every tracked key even if one release fails.
* **Nothing is persisted.** The pasted text is never written to disk, never passed to the
  logging system, and never sent anywhere. There are no analytics and no network calls.
* **One job at a time.** A second run cannot start while one is in flight.

### Focus handling

Keeping the caret where the user put it is the whole trick of an overlay, and it takes three
layers, because any one of them can fail on a given macOS version:

1. **The panel does not activate.** A non-activating `NSPanel` plus an accessory activation
   policy means clicking it should not make this application active.
2. **Buttons do not take focus.** Every button has `Qt::NoFocus`. A Qt button normally grabs
   keyboard focus when clicked, which would make the panel the key window and pull the keyboard
   off your document — losing the opening characters of the run.
3. **Typing waits for focus to actually match.** The overlay continuously notes the last
   application that was genuinely in front — that is the one it names, and the only one it will
   type into. After Start it reactivates that application if necessary, then watches until the
   frontmost application really is that one *and* the overlay holds no key window. Only then is
   the first key emitted. Every observation is logged. If it never matches within
   `FOCUS_MATCH_TIMEOUT_SECONDS`, nothing is typed.

**Coming back.** A pause caused by focus loss is marked as automatic, and the same monitor that
noticed the loss watches for the return. Once the target is frontmost again, the overlay holds no
key window, **and the text cursor is still where typing stopped**, the run resumes from exactly
where it left off. A manual pause is left alone.

### Checking the cursor, not just the application

Coming back to the right *application* is not enough. If you click somewhere else in the document
before returning, resuming would splice the remaining text into the wrong place. So when a run
pauses, the overlay snapshots the insertion point through the Accessibility API — the focused UI
element's role, title and `AXSelectedTextRange`, whose location is the caret offset — and compares
it before resuming.

* **Cursor unchanged** → resumes automatically.
* **Cursor moved** → does *not* resume. A warning names where it should go back to, and typing
  continues by itself the moment you put it back.
* **Cursor not reported** → resumes on the application check alone, and says so. Many editors
  expose no caret at all; Google Docs draws its own, so this check cannot see it. Best-effort by
  design: an unreadable cursor never blocks a run.

Pressing **Resume** yourself always proceeds — you can see the screen, so it is your call — but it
warns if the cursor is not where typing stopped.

   The target is recorded on **pid alone**. An earlier version also required "we do not hold
   focus" before recording it, which meant that if the overlay ever looked focused, no target
   was ever recorded and the app would refuse to type no matter what the user did.

Detecting this correctly needs two different questions, not one. `NSWorkspace.frontmostApplication()`
can still name your document application while the overlay holds the key window, because an
accessory app does not always register as "frontmost". So the guard also asks
`NSApplication` whether we hold a **key window** — *are we ourselves receiving keystrokes?* —
and treats a yes as "the overlay has focus" no matter what the workspace reports. Being
"active" is not enough on its own: an accessory app can report `isActive` while having no window
that accepts key events, in which case keystrokes still go to the document. The same check pauses a run and blocks
a resume.

### Known limitation of the focus guard

The guard sees **applications**, nothing finer. It cannot tell that a different text field,
window, browser tab or document became active *inside the same application*. Switching from
one Google Doc to another in the same browser looks like no change at all, and typing
continues into whatever now has the cursor. The prototype does not inspect browser contents
and never tries to determine whether Google Docs is open.

---

## Your pointer and keyboard during a run

Feeding the system a stream of synthetic key events is intrusive by nature, and the overlay says
so plainly while a run is in flight.

Two macOS defaults make it much worse, and both are switched off:

* Posting an event **suppresses real hardware input for 0.25 s** by default. During continuous
  typing that window never closes, so the mouse and keyboard appear frozen or jumpy for the
  entire run. The event source sets the suppression interval to zero and permits all events
  during suppression.
* A `NULL` event source **combines with the current hardware modifier state**, so a modifier you
  happen to be holding leaks into every synthetic keystroke. A private-state source is used
  instead, and modifier flags are always set explicitly rather than inherited.

Even so, expect some interference: your own typing during a run will interleave with the
emitted events, and the pointer may still feel less responsive than usual. The overlay shows a
warning to that effect whenever a run is active. If it becomes a problem, press `⌃⌥⎋`.

## Supported characters

| Supported | Rejected |
| --- | --- |
| `a`–`z`, `A`–`Z` | Tab characters |
| `0`–`9` | Emoji |
| Space | Accented and non-Latin letters |
| Newline (typed as Enter) | Curly quotes `’` `“` `”`, en/em dashes |
| ``` `-=[]\;',./~!@#$%^&*()_+{}|:"<>? ``` | Any other non-ASCII character |

* `CRLF` and lone `CR` line endings are converted to `LF`. That is the only change made to
  your text.
* Unsupported characters are **reported, never removed**. The validation line names each one
  (for example `U+2019 RIGHT SINGLE QUOTATION MARK '’'`) so you can fix them yourself.
* Input is capped at 10,000 characters. The cap lives in `MAX_TEXT_LENGTH` in
  [`config.py`](src/typing_simulator/config.py) and is configurable in code.

## Typing behavior

Speed comes from the target WPM using the conventional five-characters-per-word approximation:

```
baseline interval = 60 / (WPM x 5)
```

The interval is never constant. Each delay is the baseline multiplied by a locally correlated
speed burst, independent jitter, and a context multiplier that lengthens pauses at word
boundaries, after commas and semicolons, after sentence-ending punctuation, and around
newlines. Occasional hesitations are added on top. Every result is clamped so no delay can be
negative or unreasonably long. All constants live in
[`config.py`](src/typing_simulator/config.py).

Deliberate mistakes are limited to three kinds — adjacent-key substitution, accidental
duplicate, and transposed adjacent letters — using an explicit US QWERTY adjacency map. Each
one is followed by a correction (pause, backspaces, pause, retype), and a cooldown prevents
errors clustering. **The final text always matches what you asked for**; that is enforced by
the shadow buffer, not merely intended. Turning corrections off introduces no mistakes at all,
because an uncorrected mistake would change your text.

Given the same seed and settings, the plan is reproduced exactly.

---

## Running the tests

```bash
pytest
```

The suite is deterministic (fixed seeds) and **never emits a real key event** — every test uses
the recording backend, a fake focus guard and a fake hotkey service. It covers the generator,
the shadow buffer, exact output across many passages and seeds, the scheduler (ordering,
pause, resume, abort responsiveness, cleanup on exceptions, progress monotonicity, mutual
exclusion) and the safety layer (hotkey gating, self-target refusal, focus-loss pause, resume
verification, abort from every state, invalid transitions).

---

## Manual testing checklist

Automated tests cannot prove that real key events land correctly in a real application. Work
through this list by hand, starting in a scratch document you do not mind damaging.

1. Type 20 characters into a plain text editor (TextEdit).
2. Type a sentence with capitalization and punctuation.
3. Type multiple paragraphs, including an empty line.
4. Type into a manually focused Google Doc.
5. Pause and resume halfway through, with the button and with `Control+Option+P`.
6. Abort during a long delay (low WPM makes this easy to hit).
7. Abort while a key event is being processed (high WPM).
8. Switch applications while typing; confirm it pauses automatically and names the application
   that took focus.
9. Return to the original application and confirm typing resumes on its own, from where it
   stopped.
9b. Switch away, click somewhere *else* in the same document, and confirm typing does **not**
    resume; put the cursor back where it stopped and confirm it does.
10. Deny Accessibility permission and confirm the failure is safe: a clear message, no crash,
    no typing.
11. Close the overlay while typing; confirm no key stays held (check for runaway repeats).
12. Run at 20, 50 and 120 WPM.
13. Run with a 0% typo rate and with a 5% typo rate.
14. Verify the final text manually, character by character, against the source.

Overlay-specific checks:

15. **Click Start with the caret in another app and confirm focus does not move** — the text
    must land in that app, not in the overlay. This is the core of the overlay model.
16. Confirm **no characters are lost at the start** of a run: the full text must arrive, with
    nothing missing from the beginning.
17. Click into the overlay's text box, then press Start; confirm focus is handed back to your
    document and the text still arrives there in full.
18. Confirm the live `Will type into: …` line tracks whatever you click into.
19. Confirm the overlay stays visible and on top while another application is active, including
    over a full-screen window.
20. Drag the overlay by its header; collapse and expand it with the **–** / **+** button.
21. Watch the terminal during a run and confirm the focus log names the application you expect.

---

## Troubleshooting

**Nothing is typed, and no error appears.**
Almost always missing Accessibility permission — see the boxed section under
[macOS permissions](#macos-permissions), and run the one-line check there. If the overlay shows
its orange banner, that is the cause. If the banner is *not* showing, the caret is probably not
in an editable field: click directly into the text area and check the `Will type into · …`
line before starting.

**The app dies with `trace trap` the moment I press Start.**
Fixed. This was `pynput`'s listener calling HIToolbox from a worker thread — see
[Why macOS does not use pynput at runtime](#why-macos-does-not-use-pynput-at-runtime). If you
see any hard crash now, `faulthandler` is enabled, so a Python traceback is printed before the
process dies; the full native stack is in the newest `Python-*.ips` under
`~/Library/Logs/DiagnosticReports/`.

**"Accessibility permission is not granted…" but I already granted it.**
The usual cause is a **stale grant**, not a missing one. The app bundle is ad-hoc signed, so
macOS identifies it by a hash of its contents; every `make app` produces a different hash, and
an existing entry then no longer matches. System Settings keeps showing "Typing Simulator" with
the switch on while the app is told it has no permission. Toggling that switch does not help,
because the entry itself is the problem.

In order:

1. Press **Request permission** in the banner. This registers the build that is running now.
2. If the banner is still there, clear the stale entry and relaunch:

```bash
make reset-permissions
```

3. Failing that, remove the entry by hand: **System Settings → Privacy & Security →
   Accessibility**, select the row, press **−**, then relaunch the app and accept its prompt.

`make app` now runs `make reset-permissions` for you after every build, so this should not
recur. The banner distinguishes the two cases: it says permission "is switched on but is not in
effect for this build" when the grant is stale, and names a missing permission plainly
otherwise.

Running **from source**, there is a second cause: macOS attributes the permission to the
application that launched Python — your terminal or IDE — not to the script. The banner names
whichever application that actually is. Quit that application with ⌘Q, not just the overlay,
and open it again; macOS decides a process's trust at launch, so a running terminal keeps the
old answer. Building the bundle avoids the whole problem.

**The banner says Input Monitoring, and Accessibility is already on.**
They are separate switches and one does not imply the other. Input Monitoring gates the global
pause and abort hotkeys, and typing is refused while those cannot stop a run — deliberately.
Enable the same application under **Privacy & Security → Input Monitoring** and restart it.

**"macOS refused to deliver a key event" / "not trusted".**
Same cause, reported at emission time instead of before the run.

**"The global hotkey listener could not be started."**
Input Monitoring is missing. The application refuses to type without working stop hotkeys —
this is deliberate.

**The run pauses immediately after it starts.**
Something took focus from the captured target (a notification, Spotlight, a background app
activating). Return to the target and resume.

**Characters appear out of order, doubled, or missing in the target.**
The application emitted them correctly — the shadow buffer proves that — but the destination
could not keep up. Lower the WPM. Web editors, including Google Docs, drop or reorder input
under fast synthetic key streams. This is a known limitation, not something the prototype can
detect or correct.

**Text lands in the wrong document or the wrong tab.**
The focus guard only sees applications. Switching documents or tabs inside the same
application is invisible to it. Check the destination before arming.

**The first characters go missing, or land nowhere.**
Fixed. Clicking Start could pull keyboard focus onto the overlay, so the opening characters were
emitted while your document was no longer listening. The overlay now hands focus back before any
key is emitted — see [Focus handling](#focus-handling).

**"The overlay still has keyboard focus."**
Focus could not be returned to your document. Click into the document where you want the text
and press Start again.

**The overlay disappears when I switch applications.**
It should not; it is configured to stay visible and to float above full-screen windows. If it
does vanish, the native panel configuration failed. Check the startup log for a warning.

**I cannot find the app in the Dock or Cmd-Tab.**
That is deliberate — it runs as an accessory app so it never steals focus. Quit with the **✕**
in the overlay's header.

**Autocorrect or smart quotes changed the result.**
The target application rewrote what it received. Disable smart substitutions there
(in Google Docs: Tools → Preferences).

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
```

### The glass

The overlay is built as translucent glass rather than a solid panel:

* a real **`NSVisualEffectView`** sits behind the Qt content, blurring the desktop underneath.
  Qt cannot do this — it has no access to the window server's backdrop — so the effect view is
  inserted beneath Qt's content view, inset to the panel's frame, and the panel is painted
  semi-transparently on top of it;
* a **vertical gradient**, lighter at the top, reads as light falling across a curved surface;
* a **bright hairline rim**, brightest along the top edge, stands in for a specular highlight;
* **frosted inner surfaces** — fields and buttons are white at low alpha, so the blurred backdrop
  tints them — with pill-shaped buttons and a 22 px corner radius.

It follows the system appearance, restyles itself when macOS switches between light and dark, and
leaves checkboxes and steppers unstyled so Qt draws the real native controls. If the blur cannot
be installed, it degrades to a flat translucent panel that still looks correct.

### Why macOS does not use `pynput` at runtime

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

Both sides of the problem are avoided:

* **Emission** uses `CGEventPost` (`quartz_backend.py`), which is safe from any thread and needs
  no layout lookup. Events carry a US-QWERTY virtual key code *and* an explicit Unicode string,
  and their modifier flags are always set explicitly rather than inherited.
* **Hotkeys** use `NSEvent` global and local monitors (`NSEventHotkeyService`), whose handlers
  run on the main thread as part of the Cocoa event delivery Qt already pumps. There is no
  listener thread at all.

The `pynput` implementations remain in the tree as the non-macOS fallback.

The generator sits behind a `BehaviorGenerator` protocol. Everything else — the interface, the
scheduler, the safety controller and the keyboard backends — depends on that protocol, never on
the probabilistic implementation. Replacing it means writing one class that returns a
`TypingPlan` whose events replay to the requested text; no other module changes.
