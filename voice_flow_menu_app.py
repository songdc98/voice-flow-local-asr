#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBorderlessWindowMask,
    NSColor,
    NSGraphicsContext,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSEvent,
    NSMakeRect,
    NSScreen,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSFloatingWindowLevel,
)
from Foundation import NSObject
import objc


BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv/bin/python"
VOICE_FLOW = BASE_DIR / "voice_flow.py"
LOG_DIR = BASE_DIR / "logs"
PID_FILE = BASE_DIR / "voice_flow.pid"
CONFIG_PATH = BASE_DIR / "config.json"
STATUS_PATH = BASE_DIR / "voice_flow_status.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


class VoiceHUDView(NSView):
    level = 0.0
    display_level = 0.0
    state = "idle"

    def setLevel_state_(self, level, state):
        target = max(0.0, min(float(level), 1.0))
        next_state = str(state)
        if next_state != self.state:
            self.display_level = target
        else:
            self.display_level = self.display_level * 0.72 + target * 0.28
        self.level = target
        self.state = next_state
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty_rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        center_y = h / 2

        inset = max(1.0, min(w, h) * 0.06)
        circle_rect = NSMakeRect(inset, inset, w - inset * 2, h - inset * 2)
        bg = NSBezierPath.bezierPathWithOvalInRect_(
            circle_rect
        )
        NSColor.colorWithCalibratedWhite_alpha_(0.95, 0.90).setFill()
        bg.fill()

        NSGraphicsContext.saveGraphicsState()
        bg.addClip()
        top = NSBezierPath.bezierPathWithRect_(NSMakeRect(0, h / 2, w, h / 2))
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.94, 0.10, 0.14, 0.92).setFill()
        top.fill()
        NSGraphicsContext.restoreGraphicsState()

        ring_inset = inset + 0.5
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(ring_inset, ring_inset, w - ring_inset * 2, h - ring_inset * 2)
        )
        ring.setLineWidth_(0.9)
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.52).setStroke()
        ring.stroke()

        level = 0.18 if self.state == "processing" else self.display_level
        alpha = 0.78 if self.state != "processing" else 0.58
        start_x = w * 0.22
        step = w * 0.056
        quiet = level < 0.045 and self.state != "processing"
        amp = h * (0.05 + level * 0.28)

        values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] if quiet else [
            0.00, 0.00, -0.20, 0.55, -0.72, 0.98, -0.52, 0.35, -0.12, 0.00, 0.00
        ]
        path = NSBezierPath.bezierPath()
        path.moveToPoint_((start_x, center_y))
        for index, value in enumerate(values[1:], start=1):
            path.lineToPoint_((start_x + step * index, center_y + amp * value))
        path.setLineWidth_(max(1.6, w * 0.032))
        path.setLineCapStyle_(1)
        path.setLineJoinStyle_(1)
        NSColor.colorWithCalibratedWhite_alpha_(0.03, alpha).setStroke()
        path.stroke()


class VoiceFlowApp(NSObject):
    child = None
    hud_window = None
    hud_view = None
    config = None

    def applicationDidFinishLaunching_(self, notification):
        self.config = load_config()
        LOG_DIR.mkdir(exist_ok=True)
        self.build_hud()
        self.start_child()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, "checkChild:", None, True
        )
        interval = float(self.config.get("hud", {}).get("poll_interval_seconds", 0.06))
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "pollStatus:", None, True
        )

    def build_hud(self):
        if not self.config.get("hud", {}).get("enabled", True):
            return
        size = float(self.config.get("hud", {}).get("size", 132))
        rect = NSMakeRect(0, 0, size, size)
        self.hud_view = VoiceHUDView.alloc().initWithFrame_(rect)
        self.hud_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSBorderlessWindowMask,
            NSBackingStoreBuffered,
            False,
        )
        self.hud_window.setOpaque_(False)
        self.hud_window.setBackgroundColor_(NSColor.clearColor())
        self.hud_window.setLevel_(NSFloatingWindowLevel)
        self.hud_window.setIgnoresMouseEvents_(True)
        self.hud_window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.hud_window.setContentView_(self.hud_view)
        self.hud_window.orderOut_(None)

    def screen_for_mouse(self):
        point = NSEvent.mouseLocation()
        for screen in NSScreen.screens():
            frame = screen.frame()
            if (
                point.x >= frame.origin.x
                and point.x <= frame.origin.x + frame.size.width
                and point.y >= frame.origin.y
                and point.y <= frame.origin.y + frame.size.height
            ):
                return screen
        return NSScreen.mainScreen()

    def position_hud(self):
        if self.hud_window is None:
            return
        screen = self.screen_for_mouse()
        frame = screen.visibleFrame()
        size = self.hud_window.frame().size
        hud_config = self.config.get("hud", {})
        margin_x = float(hud_config.get("margin_x", 34))
        margin_y = float(hud_config.get("margin_y", 34))
        position = str(hud_config.get("position", "bottom_right"))
        if position == "bottom_center":
            x = frame.origin.x + (frame.size.width - size.width) / 2
        else:
            x = frame.origin.x + frame.size.width - size.width - margin_x
        y = frame.origin.y + margin_y
        self.hud_window.setFrameOrigin_((x, y))

    def pollStatus_(self, timer):
        if self.hud_window is None or self.hud_view is None:
            return
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.hud_window.orderOut_(None)
            return

        state = status.get("state", "idle")
        updated_at = float(status.get("updated_at", 0))
        if time.time() - updated_at > 2.0:
            self.hud_window.orderOut_(None)
            return

        if state in {"recording", "processing"}:
            self.position_hud()
            self.hud_view.setLevel_state_(float(status.get("level", 0.0)), state)
            self.hud_window.orderFrontRegardless()
        else:
            self.hud_window.orderOut_(None)

    def set_status(self, text: str):
        pass

    def stop_stale_voice_processes(self):
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except Exception:
            return

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            pid_text, _, command = stripped.partition(" ")
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            if self.child is not None and pid == self.child.pid:
                continue
            if str(VOICE_FLOW) not in command or "voice_flow_menu_app.py" in command:
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass

    def start_child(self):
        if self.child is not None and self.child.poll() is None:
            self.set_status("Running")
            return

        self.stop_stale_voice_processes()
        time.sleep(0.2)
        log_path = LOG_DIR / "voice_flow.out"
        log = open(log_path, "a", buffering=1)
        env = os.environ.copy()
        env["VOICE_FLOW_NATIVE_PASTE"] = "1"
        self.child = subprocess.Popen(
            [str(PYTHON), str(VOICE_FLOW), "--signal-server"],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
        )
        PID_FILE.write_text(str(self.child.pid), encoding="utf-8")
        self.set_status("Running")

    def stop_child(self):
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(os.getpgid(self.child.pid), signal.SIGTERM)
            except Exception:
                self.child.terminate()
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.child.pid), signal.SIGKILL)
                except Exception:
                    self.child.kill()
        self.child = None
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self.set_status("Stopped")

    @objc.IBAction
    def restart_(self, sender):
        self.stop_child()
        time.sleep(0.2)
        self.start_child()

    @objc.IBAction
    def quit_(self, sender):
        self.stop_child()
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, notification):
        self.stop_child()

    def checkChild_(self, timer):
        if self.child is None:
            self.set_status("Stopped")
            return
        if self.child.poll() is None:
            self.set_status("Running")
            return
        self.set_status("Stopped")

    def send_notification(self, message: str):
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'display notification "{message}" with title "Voice Flow"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = VoiceFlowApp.alloc().init()
    app.setDelegate_(delegate)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
