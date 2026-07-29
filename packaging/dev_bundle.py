"""Build a development ``.app`` that can actually hold a permission grant.

The problem this exists to solve
--------------------------------
macOS attributes an Accessibility grant to the *running executable*.  Neither
of the two obvious ways to run this application gives that executable a stable
identity:

``make run`` (straight from source)
    The executable is the virtualenv's interpreter, which is a symlink to a
    Homebrew or system Python.  Granting it access grants it to every Python
    program on the machine, the entry breaks on the next interpreter upgrade,
    and macOS attributes the request to whichever terminal or editor launched
    it anyway.  In practice the grant cannot be made to stick.

``make app`` (the PyInstaller bundle)
    A real ``.app`` with its own identity, and the right answer for using the
    application - but it is ad-hoc signed, so its identity is a hash of its
    contents.  Every rebuild produces a different hash, which silently
    invalidates the grant.  Editing one line of source therefore costs a
    minute of build time *and* a fresh round of permission granting.

This builds a third thing: a real ``.app`` whose main executable is a **copy of
the interpreter**, with the source tree reached through ``PYTHONPATH`` from
outside the bundle.  Because the source lives outside, editing it does not
change the bundle's contents, so the ad-hoc signature - and the permission
granted to it - survives every edit.  Rebuilding takes about a second.

How the pieces fit
------------------
* ``Contents/MacOS/TypingSimulator`` is a copy of the base interpreter, so the
  executable macOS sees, and signs, is inside the bundle.
* ``LSEnvironment`` in ``Info.plist`` supplies ``PYTHONHOME`` (where the copied
  interpreter finds its standard library) and ``PYTHONPATH`` (the checkout's
  ``src`` and the virtualenv's packages).  LaunchServices applies it, which is
  why the bundle must be started with ``open`` rather than by running the
  binary directly.
* ``Contents/Resources/boot/sitecustomize.py`` is imported automatically by
  ``site`` during interpreter start-up, so the application runs with no
  command-line arguments at all and a double-click works.

Nothing here weakens or works around the permission system: the bundle asks
macOS for permission the same way any application does.  It only makes the
thing being asked about stay the same from one build to the next.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import site
import subprocess
import sys
from pathlib import Path

APP_NAME = "Typing Simulator (dev)"

#: Deliberately *not* the release bundle identifier.  The two builds are
#: different binaries in different places, so macOS grants them separately;
#: sharing an identifier would only make the System Settings list ambiguous.
BUNDLE_ID = "local.typing-simulator.dev"

EXECUTABLE_NAME = "TypingSimulator"

BOOT_SOURCE = '''"""Start the overlay as the bundle's interpreter comes up.

``site`` imports this automatically, which is what lets the bundle be launched
with no arguments - by ``open``, or by a double-click in Finder.

``os._exit`` is deliberate: returning from here would drop the interpreter into
its normal start-up path and then into the REPL, with no terminal attached.
"""

import os
import sys


def _main() -> int:
    from typing_simulator.application import run

    return run([sys.argv[0] or "TypingSimulator"])


try:
    _code = _main()
except SystemExit as exit_error:  # pragma: no cover - exercised by hand
    _code = int(exit_error.code or 0)
except BaseException:  # pragma: no cover - exercised by hand
    import traceback

    traceback.print_exc()
    _code = 1

os._exit(_code)
'''


def build(destination: Path, source_root: Path) -> Path:
    """Create the bundle at ``destination`` and ad-hoc sign it."""
    if sys.platform != "darwin":
        raise SystemExit("The development bundle is macOS-only.")

    interpreter = _interpreter_to_copy()

    app = destination / f"{APP_NAME}.app"
    contents = app / "Contents"
    boot = contents / "Resources" / "boot"

    # A full rebuild rather than an update: a leftover file from an earlier
    # layout would be sealed into the signature and change the identity.
    if app.exists():
        shutil.rmtree(app)
    (contents / "MacOS").mkdir(parents=True)
    boot.mkdir(parents=True)

    shutil.copy2(interpreter, contents / "MacOS" / EXECUTABLE_NAME)
    (boot / "sitecustomize.py").write_text(BOOT_SOURCE, encoding="utf-8")

    with open(contents / "Info.plist", "wb") as handle:
        plistlib.dump(_info_plist(boot, source_root), handle)

    _sign(app)
    return app


def _interpreter_to_copy() -> Path:
    """The binary to put in ``Contents/MacOS``, which must not re-exec.

    ``bin/python3.x`` of a framework build looks like the interpreter and is
    not: it is a ~50 KB stub whose job is to ``exec``
    ``Resources/Python.app/Contents/MacOS/Python`` so that the process becomes
    a GUI-capable application.  Copying *that* into the bundle produces a
    process that immediately replaces itself with Homebrew's binary, outside
    the bundle and signed ``org.python.python``.

    macOS then answers two different questions two different ways, which is
    exactly the symptom: Accessibility is attributed to the *responsible*
    process - still the bundle, because that is what LaunchServices started -
    and comes out granted, while Post Events is attributed to the binary
    actually running and is refused forever.  Accessibility green, Post events
    permanently red.

    Copying the real interpreter instead means nothing re-execs, so the
    process stays inside the bundle and carries the bundle's identity for
    every question macOS asks about it.
    """
    framework_binary = (
        Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    )
    if framework_binary.exists():
        return framework_binary

    interpreter = Path(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
    if not interpreter.exists():  # pragma: no cover - defensive
        raise SystemExit(f"Could not find the base interpreter at {interpreter}.")
    return interpreter


def _info_plist(boot: Path, source_root: Path) -> dict[str, object]:
    return {
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "12.0",
        # Accessory app: no Dock icon, no menu bar, never steals focus.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "LSEnvironment": {
            # Where the copied interpreter finds its standard library.
            "PYTHONHOME": sys.base_prefix,
            "PYTHONPATH": ":".join(_python_path(boot, source_root)),
            # Keeping __pycache__ out of the checkout is tidiness; keeping it
            # out of the *bundle* is not optional, because anything written
            # inside would break the signature that holds the grant.
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }


def _python_path(boot: Path, source_root: Path) -> list[str]:
    """Boot directory first, then the checkout, then the virtualenv.

    The boot directory has to come first: ``site`` imports ``sitecustomize``
    from the path, and if anything else on it provides one instead, the bundle
    starts an interpreter that does nothing.
    """
    entries = [str(boot.resolve()), str(source_root.resolve())]
    try:
        installed = site.getsitepackages()
    except AttributeError:  # pragma: no cover - some virtualenv layouts
        installed = []
    for directory in installed:
        if directory not in entries:
            entries.append(directory)
    if len(entries) == 2:
        raise SystemExit(
            "Could not find the installed dependencies. Run `make setup` first."
        )
    return entries


def _sign(app: Path) -> None:
    """Ad-hoc sign the bundle.

    Required, not cosmetic: macOS will not grant Accessibility to an unsigned
    bundle, and an arm64 binary that has been copied has an invalid signature
    until it is replaced.
    """
    result = subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Could not sign the development bundle, so macOS would never grant "
            f"it Accessibility:\n{result.stderr.strip()}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist", default="dist", help="Directory to build the bundle into."
    )
    parser.add_argument(
        "--source",
        default="src",
        help="The checkout's source root, put on the bundle's PYTHONPATH.",
    )
    arguments = parser.parse_args(argv)

    destination = Path(arguments.dist)
    destination.mkdir(parents=True, exist_ok=True)
    app = build(destination, Path(arguments.source))

    print(f"Built: {app}")
    print(f"Open it with:  open \"{app}\"")
    print(
        "Its identity does not change when you edit the source, so the "
        "permission you grant it stays granted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
