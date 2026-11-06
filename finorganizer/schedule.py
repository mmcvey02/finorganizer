"""The background check that sends notifications while FinOrganizer is closed.

Installed only for the packaged desktop app, which runs itself with ``--notify``:
- Windows: a Task Scheduler task, every 4 hours, catching up after the computer was off.
- macOS: a launchd agent in ~/Library/LaunchAgents, every 4 hours and at login.
The check opens each profile's data, syncs banks if they're due, sends what's due and
exits; it shows no window. Nothing is installed when running from source.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

TASK_NAME = "FinOrganizer Notifications"
LABEL = "com.mmcvey02.finorganizer.notify"
INTERVAL_HOURS = 4


def _frozen():
    return bool(getattr(sys, "frozen", False))


def supported():
    return _frozen() and (os.name == "nt" or sys.platform == "darwin")


def command(db_path):
    """The program and arguments the background check runs."""
    return [sys.executable, "--notify", "--db", db_path]


def _run(cmd):
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console window flash
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ------------------------------------------------------------------- Windows

def task_xml(cmd):
    args = " ".join('"%s"' % a for a in cmd[1:])
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Sends FinOrganizer notifications (bills, budgets, activity).</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2026-01-01T07:05:00</StartBoundary>
      <Repetition><Interval>PT%dH</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT5M</Delay></LogonTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>
</Task>
""" % (INTERVAL_HOURS, escape(cmd[0]), escape(args))


def _install_windows(cmd):
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(task_xml(cmd))
        r = _run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", path])
    finally:
        os.remove(path)
    if r.returncode != 0:
        raise OSError("couldn't set up the background check: %s" % (r.stderr or r.stdout).strip())


# --------------------------------------------------------------------- macOS

def plist_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")


def plist_bytes(cmd, log_dir):
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": cmd, "StartInterval": INTERVAL_HOURS * 3600,
        "RunAtLoad": True, "ProcessType": "Background", "LowPriorityIO": True,
        "StandardOutPath": os.path.join(log_dir, "notifications-run.log"),
        "StandardErrorPath": os.path.join(log_dir, "notifications-run.log"),
    })


def _install_mac(cmd, log_dir):
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(plist_bytes(cmd, log_dir))
    domain = "gui/%d" % os.getuid()
    _run(["launchctl", "bootout", "%s/%s" % (domain, LABEL)])  # replace an older copy
    r = _run(["launchctl", "bootstrap", domain, path])
    if r.returncode != 0:
        r = _run(["launchctl", "load", "-w", path])  # older macOS
        if r.returncode != 0:
            raise OSError("couldn't set up the background check: %s" % (r.stderr or r.stdout).strip())


# ------------------------------------------------------------------ either

def install(db_path):
    if not supported():
        raise OSError("background checks need the installed FinOrganizer app for Windows or Mac")
    cmd = command(db_path)
    if os.name == "nt":
        _install_windows(cmd)
    else:
        _install_mac(cmd, os.path.dirname(db_path))


def uninstall():
    if not supported():
        return
    if os.name == "nt":
        _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    else:
        _run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
        try:
            os.remove(plist_path())
        except OSError:
            pass


def installed():
    if not supported():
        return False
    if os.name == "nt":
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    return os.path.exists(plist_path())


def status():
    if not supported():
        return {"supported": False, "installed": False,
                "reason": "Background checks run in the installed FinOrganizer app for Windows or Mac. "
                          "Here, checks run only while FinOrganizer is open."}
    return {"supported": True, "installed": installed(), "every_hours": INTERVAL_HOURS}
