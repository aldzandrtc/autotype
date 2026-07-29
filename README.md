# Local Typing Input Simulator

A small always-on-top overlay for macOS that types text for you, one keystroke at a time.

Paste text into the overlay, click into the document where you want it, and press **Start**. The
overlay types your text into the caret you placed, at a speed and rhythm you control.

It never moves or clicks your mouse, never uses the clipboard, and never sends anything over a
network. You place the caret; it types there.

> **macOS only.** It needs AppKit for focus tracking. On other platforms it refuses to type
> rather than doing something unverified.

This is a prototype. Design notes and rationale live in [DESIGN.md](DESIGN.md).

---

## Responsible use

This is intended for accessibility and automation experiments on **your own** machine and **your
own** documents, and for testing how applications react to a stream of individual key events.

It is deliberately not built for evading authorship detection, impersonating a human author
where that matters, or operating anything remotely or unattended.

You position the caret yourself, the overlay shows which application it is about to type into
before it starts, and every run can be stopped from anywhere with a global hotkey.

**Always check the result yourself.** The app knows what it *emitted*; it cannot confirm what the
target application actually received.

---

## Requirements

* macOS 12 or newer
* Python 3.11 or newer
* Accessibility permission (the app asks for it on first launch)

---

## Install

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

Or install it properly:

```bash
make install
```

macOS grants permission **per copy of the app**, so if you use `make install`, grant permission
to the one in `/Applications` and run that one from then on.

### Working on the code

Use `make dev` instead. It builds a development app that keeps its permission grant when you edit
the source, and rebuilds in about a second:

```bash
make dev
```

Avoid `make run` unless you know you want it — run straight from source, macOS attributes the
permission to your *terminal* rather than to this app, so you end up granting your terminal
access to control your computer, and it has to be redone whenever you switch terminals.

| Target | What it does |
| --- | --- |
| `make app` | Build `dist/Typing Simulator.app` |
| `make install` | Build it and copy it to `/Applications` |
| `make dev` | Build and open the development app (use this while editing) |
| `make run` | Run straight from source (cannot hold a permission grant) |
| `make test` | Run the test suite |
| `make reset-permissions` | Clear stale permission entries for the app |
| `make reset-dev-permissions` | The same, for the development app |
| `make clean` | Remove build output and caches |
| `make help` | List every target |

---

## Granting permission

macOS discards synthetic keystrokes **silently** without permission — no error, just nothing
happening. So the app checks first and disables **Start** until it has what it needs, showing a
banner with a checklist:

```
✓ Accessibility    ✕ Post events    –  Input Monitoring (not needed)
```

**Press "Grant permission" and follow the prompt.** That is the whole procedure. One button,
because which of several possible fixes is needed depends on macOS internals you should not have
to think about:

* the first press asks macOS, which shows its standard dialog with a one-click **Open System
  Settings**;
* if macOS has stopped asking — it only offers each dialog once per launch — the button clears
  this app's own permission entries and restarts it, which makes the prompt appear again;
* if you have already granted it and the app just cannot see it yet, the button restarts to pick
  it up.

Nothing is ever cleared while a permission still reads as granted, and no other application's
permissions are touched.

**If you grant permission in System Settings while the app is running, it will restart itself.**
That is expected and takes a second — macOS only tells a program what it may do when it starts,
so a running copy cannot see a grant you just made. Your pasted text is not preserved across the
restart.

`Input Monitoring` normally shows as *not needed*: the stop hotkeys use an API that the
Accessibility permission already covers. You do not have to grant it.

### If it still will not work

1. Press **Grant permission** again.
2. Check what macOS actually reports:

   ```bash
   .venv/bin/python -c "from typing_simulator.safety.permissions import permission_status; print(permission_status())"
   ```

   Typing works when `accessibility` and `post_events` are both `True`.
3. Open **System Settings → Privacy & Security → Accessibility** and enable **Typing
   Simulator** (or **Typing Simulator (dev)** for the development build). The banner names
   whichever applies to how you started it.
4. If it is already listed and switched **on**, do not toggle it — that never fixes anything. The
   entry has gone stale. Run `make reset-permissions` (or `make reset-dev-permissions`), or
   select the row and remove it with **−**, then let the app ask again.
5. Quit the app completely and reopen it.

---

## Using it

1. **Paste your text** into the overlay. The character count and validation update as you type.
2. **Set how it types** (all optional):

   | Setting | Range | What it does |
   | --- | --- | --- |
   | Speed | 20–120 WPM | Target typing speed |
   | Variation | Low / Medium / High | How much the rhythm wanders |
   | Typos | 0–5% | Rate of deliberate mistakes, each one corrected |
   | Correct mistakes | on / off | Off means no mistakes are made at all |
   | Seed | any number | Same seed and settings reproduce the same run exactly |

   **Duration** shows an estimate as you change these.
3. **Click into your document** and place the caret where you want the text.
4. **Check the target line.** The overlay shows, live, where Start would send the text:

   ```
   ●  Will type into  ·  TextEdit
   ```

   If it says the frontmost application is unknown, or that the overlay has focus, click into
   your document again.
5. **Press Start.** There is no countdown — the app watches where focus actually goes and starts
   typing the moment it is on the application it named. Focus stays in your document; clicking
   Start does not steal it.
6. **Watch or walk away.** The overlay shows live progress and stays on top. Collapse it with the
   **⌃** button in its header if it is in the way.

Quit with the **✕** in the overlay's header. There is no Dock icon and no menu-bar item — that is
deliberate, so it never steals focus.

### Stopping it

| Shortcut | Effect |
| --- | --- |
| `Control` `Option` `P` | Pause, or resume |
| `Control` `Option` `Escape` | Abort immediately |

Both work from **any** application, so you can stop a run while your document is frontmost.
Starting has no shortcut on purpose. There are matching buttons in the overlay.

If the hotkeys cannot be started, the app refuses to type at all.

### Switching away mid-run

Typing **pauses by itself** if you switch to another application, and **resumes by itself** when
you come back — provided the cursor is still where typing stopped. If you moved it, the run waits
and tells you where to put it back; press **Resume** to carry on from wherever the cursor is now
instead.

A pause *you* asked for stays paused until you resume it.

### While it is typing

Your mouse and keyboard may feel slightly laggy, and anything you type yourself will interleave
with what the app is typing. Let it finish, or press `⌃⌥⎋`. The overlay warns about this whenever
a run is active.

---

## What it can type

| Supported | Rejected |
| --- | --- |
| `a`–`z`, `A`–`Z` | Tab characters |
| `0`–`9` | Emoji |
| Space | Accented and non-Latin letters |
| Newline | Curly quotes `’` `“` `”`, en/em dashes |
| ``` `-=[]\;',./~!@#$%^&*()_+{}|:"<>? ``` | Any other non-ASCII character |

* Windows and old-Mac line endings are converted to standard newlines. That is the only change
  made to your text.
* Unsupported characters are **reported, never silently removed**. The validation line names each
  one — for example `U+2019 RIGHT SINGLE QUOTATION MARK '’'` — so you can fix them yourself.
  Curly quotes and em dashes pasted from a word processor are the usual culprits.
* Up to 10,000 characters per run.
* Whatever you set, the text that arrives always matches the text you pasted. Deliberate typos
  are always corrected.

Your text is never written to disk, never logged, and never sent anywhere.

---

## Troubleshooting

**Nothing is typed and there is no error.**
Missing permission — see [Granting permission](#granting-permission). If the banner is *not*
showing, the caret is probably not in an editable field: click directly into the text area and
check the `Will type into · …` line before starting.

**I granted permission but the banner says it is not in effect.**
The app is ad-hoc signed, so macOS identifies it by a hash of its contents and every rebuild
invalidates the old entry — System Settings keeps showing it switched on while the app is told it
has no permission. Press **Grant permission**, or run `make reset-permissions`.

**It restarted itself.**
Expected, once, right after you grant permission in System Settings. See
[Granting permission](#granting-permission).

**The run pauses immediately after starting.**
Something took focus — a notification, Spotlight, a background app activating. Return to your
document and it resumes on its own.

**Characters come out in the wrong order, doubled, or missing.**
The destination could not keep up. Lower the WPM. Web editors including Google Docs drop or
reorder input under fast synthetic keystrokes; the app cannot detect or correct this.

**Text landed in the wrong document or browser tab.**
The overlay tracks *applications*, not individual windows, tabs or fields. Switching documents
inside the same app is invisible to it. Check the target line before starting.

**"The overlay still has keyboard focus."**
Focus could not be handed back. Click into your document and press Start again.

**The overlay vanished when I switched apps.**
It should stay visible and float above full-screen windows. If it disappears, the native panel
setup failed — check the log.

**I cannot find it in the Dock or Cmd-Tab.**
Deliberate: it runs as an accessory app so it never steals focus. Quit with the **✕** in its
header.

**Smart quotes or autocorrect changed the result.**
The target application rewrote what it received. Turn off smart substitutions there — in Google
Docs, Tools → Preferences.

**It crashed.**
A Python traceback is printed before the process dies. Check the log, and the newest
`Python-*.ips` under `~/Library/Logs/DiagnosticReports/` for the native stack.

### Logs

When running as an app bundle there is no terminal to write to, so the log goes to:

```
~/Library/Logs/Typing Simulator/typing-simulator.log
```

It records the permission state at startup, every focus observation, and every state change. It
never contains your pasted text.

---

## Contributing

Read [DESIGN.md](DESIGN.md) first — it explains why the permission handling, focus tracking and
macOS API choices are shaped the way they are, and several of them are non-obvious enough that
changing them without reading it will reintroduce a fixed bug.

```bash
make test
```

The suite never emits a real key event and never touches the real permission database.

---

## License

[Apache License 2.0](LICENSE).
