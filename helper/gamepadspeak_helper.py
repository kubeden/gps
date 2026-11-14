#!/usr/bin/env python3
"""GamepadSpeak helper for WoW Forever. Cross-platform (macOS, Windows, Linux).

Press the trigger button: start recording the mic.
Press again: stop, transcribe locally with Whisper, then open the WoW chat box,
type the text and press Enter. The game only ever sees ordinary key presses.

Settings (trigger button, hotkey) come from the addon's SavedVariables file,
written by `/gps setup` in game. Everything runs locally; no audio leaves the PC.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# SDL must be configured before pygame is imported.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np  # noqa: E402
import pygame  # noqa: E402
import sounddevice as sd  # noqa: E402
from pygame._sdl2 import controller as sdl_controller  # noqa: E402
from pynput.keyboard import Controller as KeyboardController, Key  # noqa: E402

SAMPLE_RATE = 16_000
DEFAULT_CLOSE_COMMAND = "/click InputFunctionBindingButton_PAD2 LeftButton 1"
SYSTEM = platform.system()  # "Darwin", "Windows", "Linux"


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# ---------------------------------------------------------------------------
# WoW install + addon settings
# ---------------------------------------------------------------------------

def default_wow_dir() -> Path:
    if SYSTEM == "Darwin":
        return Path("/Applications/World of Warcraft/_forever_")
    if SYSTEM == "Windows":
        for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("ProgramFiles", r"C:\Program Files")):
            p = Path(base) / "World of Warcraft" / "_forever_"
            if p.exists():
                return p
        return Path(r"C:\Program Files (x86)\World of Warcraft\_forever_")
    # Linux: Wine/Lutris/Steam prefixes vary; the user passes --wow-dir.
    return Path.home() / "Games" / "world-of-warcraft" / "drive_c" / "Program Files (x86)" / "World of Warcraft" / "_forever_"


@dataclass(frozen=True)
class AddonSettings:
    trigger: str | None = None
    hotkey: str | None = None
    close_command: str | None = None


class SavedVariables:
    """Reads GamepadSpeakDB from WTF/Account/<acct>/SavedVariables/GamepadSpeak.lua."""

    def __init__(self, wow_dir: Path):
        self.wow_dir = wow_dir
        self._mtime: float | None = None
        self.settings = AddonSettings()

    @property
    def path(self) -> Path | None:
        accounts = self.wow_dir / "WTF" / "Account"
        if not accounts.is_dir():
            return None
        candidates = [p for p in accounts.glob("*/SavedVariables/GamepadSpeak.lua") if p.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def refresh(self) -> bool:
        """Re-read the file if it changed. Returns True when settings changed."""
        p = self.path
        if p is None:
            return False
        mtime = p.stat().st_mtime
        if self._mtime == mtime:
            return False
        self._mtime = mtime
        text = p.read_text(encoding="utf-8", errors="replace")
        new = AddonSettings(trigger=self._value("trigger", text), hotkey=self._value("hotkey", text),
                            close_command=self._value("closeCommand", text))
        changed = new != self.settings
        self.settings = new
        return changed

    @staticmethod
    def _value(key: str, text: str) -> str | None:
        m = re.search(r'\["%s"\]\s*=\s*"([^"]*)"' % key, text)
        return m.group(1) if m and m.group(1) else None


# ---------------------------------------------------------------------------
# Controller (SDL game controller API, same button model WoW uses)
# ---------------------------------------------------------------------------

# SDL_GameControllerButton enum values, for constants older pygame builds don't export.
SDL_BUTTON_VALUES = {
    "CONTROLLER_BUTTON_A": 0, "CONTROLLER_BUTTON_B": 1, "CONTROLLER_BUTTON_X": 2, "CONTROLLER_BUTTON_Y": 3,
    "CONTROLLER_BUTTON_BACK": 4, "CONTROLLER_BUTTON_GUIDE": 5, "CONTROLLER_BUTTON_START": 6,
    "CONTROLLER_BUTTON_LEFTSTICK": 7, "CONTROLLER_BUTTON_RIGHTSTICK": 8,
    "CONTROLLER_BUTTON_LEFTSHOULDER": 9, "CONTROLLER_BUTTON_RIGHTSHOULDER": 10,
    "CONTROLLER_BUTTON_DPAD_UP": 11, "CONTROLLER_BUTTON_DPAD_DOWN": 12,
    "CONTROLLER_BUTTON_DPAD_LEFT": 13, "CONTROLLER_BUTTON_DPAD_RIGHT": 14,
    "CONTROLLER_BUTTON_MISC1": 15, "CONTROLLER_BUTTON_PADDLE1": 16, "CONTROLLER_BUTTON_PADDLE2": 17,
    "CONTROLLER_BUTTON_PADDLE3": 18, "CONTROLLER_BUTTON_PADDLE4": 19, "CONTROLLER_BUTTON_TOUCHPAD": 20,
    "CONTROLLER_AXIS_TRIGGERLEFT": 4, "CONTROLLER_AXIS_TRIGGERRIGHT": 5,
}


def _const(name: str) -> int | None:
    return getattr(pygame, name, SDL_BUTTON_VALUES.get(name))


# WoW PAD name -> SDL controller button constant name.
PAD_TO_SDL_BUTTON = {
    "PAD1": "CONTROLLER_BUTTON_A",
    "PAD2": "CONTROLLER_BUTTON_B",
    "PAD3": "CONTROLLER_BUTTON_X",
    "PAD4": "CONTROLLER_BUTTON_Y",
    "PAD5": "CONTROLLER_BUTTON_MISC1",
    "PADSOCIAL": "CONTROLLER_BUTTON_BACK",
    "PADSYSTEM": "CONTROLLER_BUTTON_GUIDE",
    "PADFORWARD": "CONTROLLER_BUTTON_START",
    "PADLSTICK": "CONTROLLER_BUTTON_LEFTSTICK",
    "PADRSTICK": "CONTROLLER_BUTTON_RIGHTSTICK",
    "PADLSHOULDER": "CONTROLLER_BUTTON_LEFTSHOULDER",
    "PADRSHOULDER": "CONTROLLER_BUTTON_RIGHTSHOULDER",
    "PADDUP": "CONTROLLER_BUTTON_DPAD_UP",
    "PADDDOWN": "CONTROLLER_BUTTON_DPAD_DOWN",
    "PADDLEFT": "CONTROLLER_BUTTON_DPAD_LEFT",
    "PADDRIGHT": "CONTROLLER_BUTTON_DPAD_RIGHT",
    "PADBACK": "CONTROLLER_BUTTON_TOUCHPAD",
    "PADPADDLE1": "CONTROLLER_BUTTON_PADDLE1",
    "PADPADDLE2": "CONTROLLER_BUTTON_PADDLE2",
    "PADPADDLE3": "CONTROLLER_BUTTON_PADDLE3",
    "PADPADDLE4": "CONTROLLER_BUTTON_PADDLE4",
}
PAD_TO_SDL_AXIS = {
    "PADLTRIGGER": "CONTROLLER_AXIS_TRIGGERLEFT",
    "PADRTRIGGER": "CONTROLLER_AXIS_TRIGGERRIGHT",
}
TRIGGER_AXIS_THRESHOLD = 16_000  # of 32767


class ControllerWatcher:
    """Polls SDL on the calling thread (SDL wants the main thread on macOS)."""

    def __init__(self, on_press):
        self.on_press = on_press
        self.trigger: str | None = None
        self.raw_button: int | None = None
        self._button_const: int | None = None
        self._axis_const: int | None = None
        self._axis_active = False
        self._last_press = 0.0
        self._controllers: dict[int, object] = {}
        self._joysticks: dict[int, object] = {}

    def set_trigger(self, trigger: str | None, raw_button: int | None = None) -> None:
        self.trigger = trigger
        self.raw_button = raw_button
        self._button_const = self._axis_const = None
        if raw_button is not None:
            log(f"Watching raw joystick button {raw_button}")
            return
        if not trigger:
            return
        if trigger in PAD_TO_SDL_AXIS:
            self._axis_const = _const(PAD_TO_SDL_AXIS[trigger])
        elif trigger in PAD_TO_SDL_BUTTON:
            self._button_const = _const(PAD_TO_SDL_BUTTON[trigger])
            if self._button_const is None:
                log(f"This SDL build has no {PAD_TO_SDL_BUTTON[trigger]}; pick another button in /gps setup")
        else:
            log(f"Unknown trigger '{trigger}'")
        if self._button_const is not None or self._axis_const is not None:
            log(f"Watching {trigger}")

    def start(self) -> None:
        pygame.init()
        sdl_controller.init()
        pygame.joystick.init()
        for i in range(sdl_controller.get_count()):
            self._open(i)
        if not self._controllers and not self._joysticks:
            log("No controller found yet; waiting for one to connect")

    def _open(self, device_index: int) -> None:
        try:
            if sdl_controller.is_controller(device_index):
                c = sdl_controller.Controller(device_index)
                self._controllers[c.as_joystick().get_instance_id()] = c
                log(f"Controller: {c.name}")
            else:
                j = pygame.joystick.Joystick(device_index)
                self._joysticks[j.get_instance_id()] = j
                log(f"Joystick (no SDL mapping, raw buttons only): {j.get_name()}")
        except pygame.error as e:
            log(f"Could not open device {device_index}: {e}")

    def _fire(self) -> None:
        now = time.monotonic()
        if now - self._last_press < 0.25:
            return
        self._last_press = now
        self.on_press()

    def pump(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.CONTROLLERDEVICEADDED:
                self._open(event.device_index)
            elif event.type == pygame.CONTROLLERDEVICEREMOVED:
                self._controllers.pop(event.instance_id, None)
                log("Controller disconnected")
            elif event.type == pygame.JOYDEVICEADDED and self.raw_button is not None:
                self._open(event.device_index)
            elif event.type == pygame.CONTROLLERBUTTONDOWN and self._button_const is not None:
                if event.button == self._button_const:
                    self._fire()
            elif event.type == pygame.CONTROLLERAXISMOTION and self._axis_const is not None:
                if event.axis == self._axis_const:
                    pressed = event.value > TRIGGER_AXIS_THRESHOLD
                    if pressed and not self._axis_active:
                        self._fire()
                    self._axis_active = pressed
            elif event.type == pygame.JOYBUTTONDOWN and self.raw_button is not None:
                if event.button == self.raw_button:
                    self._fire()

    def describe(self) -> list[str]:
        out = [f"{c.name} (mapped)" for c in self._controllers.values()]
        out += [f"{j.get_name()} (raw only)" for j in self._joysticks.values()]
        return out


# ---------------------------------------------------------------------------
# Audio + transcription
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self, device=None):
        self.device = device
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._chunks = []
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            device=self.device, blocksize=1024, callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log(f"Audio status: {status}")
        with self._lock:
            self._chunks.append(indata[:, 0].copy())

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype="float32")
            self._chunks = []
        return audio


class Transcriber:
    def __init__(self, model_name: str, language: str | None, device: str, compute_type: str):
        from faster_whisper import WhisperModel  # heavy import, keep it local

        t0 = time.monotonic()
        log(f"Loading Whisper model '{model_name}' ({device}/{compute_type})...")
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.language = language
        log(f"Model ready in {time.monotonic() - t0:.1f}s")

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size < SAMPLE_RATE // 4:
            return ""
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=1, best_of=1,
            vad_filter=True, condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments)
        return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Keystroke injection
# ---------------------------------------------------------------------------

# Some keys don't exist on every platform's pynput (no Insert on macOS, for example).
_KEY_NAMES = {
    "ENTER": "enter", "SPACE": "space", "TAB": "tab", "ESCAPE": "esc",
    "BACKSPACE": "backspace", "DELETE": "delete", "INSERT": "insert",
    "HOME": "home", "END": "end", "PAGEUP": "page_up", "PAGEDOWN": "page_down",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
    **{f"F{i}": f"f{i}" for i in range(1, 21)},
}
SPECIAL_KEYS = {name: getattr(Key, attr) for name, attr in _KEY_NAMES.items() if hasattr(Key, attr)}
MODIFIERS = {"CTRL": Key.ctrl, "SHIFT": Key.shift, "ALT": Key.alt, "META": Key.cmd, "CMD": Key.cmd}


@dataclass(frozen=True)
class Hotkey:
    key: object
    modifiers: tuple

    @classmethod
    def parse(cls, binding: str) -> "Hotkey | None":
        parts = binding.upper().split("-")
        parts = [p for p in parts if p] or ["-"]
        name = parts[-1]
        key = SPECIAL_KEYS.get(name)
        if key is None:
            if len(name) == 1:
                key = name.lower()
            else:
                return None
        mods = []
        for m in parts[:-1]:
            if m not in MODIFIERS:
                return None
            mods.append(MODIFIERS[m])
        return cls(key=key, modifiers=tuple(mods))


class Injector:
    def __init__(self, char_delay: float = 0.002):
        self.kb = KeyboardController()
        self.char_delay = char_delay

    def press_hotkey(self, hk: Hotkey) -> None:
        for m in hk.modifiers:
            self.kb.press(m)
            time.sleep(0.005)
        self.kb.press(hk.key)
        time.sleep(0.01)
        self.kb.release(hk.key)
        for m in reversed(hk.modifiers):
            time.sleep(0.005)
            self.kb.release(m)

    def type_text(self, text: str) -> None:
        for ch in text:
            self.kb.press(ch)
            self.kb.release(ch)
            time.sleep(self.char_delay)

    def _enter(self) -> None:
        self.kb.press(Key.enter)
        self.kb.release(Key.enter)

    def deliver(self, text: str, open_key: Hotkey | None, close_key: Hotkey | None,
                close_command: str | None) -> None:
        if open_key is not None:
            self.press_hotkey(open_key)
            time.sleep(0.12)
        self.type_text(text)
        time.sleep(0.04)
        self._enter()
        if close_command:
            # The box keeps focus after a send in gamepad style. This slash
            # command clicks the addon's secure button, which lets Blizzard
            # code deactivate the box; typed + Enter keeps it fully secure.
            time.sleep(0.15)
            self.type_text(close_command)
            time.sleep(0.03)
            self._enter()
        if close_key is not None:
            time.sleep(0.10)
            self.press_hotkey(close_key)


def frontmost_app_name() -> str | None:
    """Best effort per platform. None means 'unknown'."""
    try:
        if SYSTEM == "Darwin":
            try:
                from AppKit import NSWorkspace  # type: ignore
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                return app.localizedName() if app else None
            except ImportError:
                out = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2,
                )
                return out.stdout.strip() or None
        if SYSTEM == "Windows":
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or None
        if SYSTEM == "Linux":
            out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                                 capture_output=True, text=True, timeout=2)
            return out.stdout.strip() or None
    except Exception:
        return None
    return None


def wow_is_frontmost() -> bool | None:
    name = frontmost_app_name()
    if name is None:
        return None
    n = name.lower()
    # macOS reports the game as "Wow"; Windows titles say "World of Warcraft".
    return "warcraft" in n or n.startswith("wow") or "blizzard" in n


# ---------------------------------------------------------------------------
# Sounds (short generated tones, no platform audio APIs needed)
# ---------------------------------------------------------------------------

def tone(freq: float, seconds: float = 0.08, volume: float = 0.2) -> np.ndarray:
    t = np.linspace(0, seconds, int(44_100 * seconds), endpoint=False)
    env = np.minimum(1.0, np.minimum(t / 0.01, (seconds - t) / 0.02))
    return (volume * env * np.sin(2 * np.pi * freq * t)).astype("float32")


class Sounds:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.start_tone = tone(880)
        self.stop_tone = np.concatenate([tone(660), tone(440)])
        self.error_tone = tone(220, 0.2)

    def play(self, which: np.ndarray) -> None:
        if not self.enabled:
            return
        try:
            sd.play(which, 44_100)
        except Exception as e:  # never let a beep break the flow
            log(f"Sound failed: {e}")


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class Coordinator:
    IDLE, RECORDING, FINALIZING = "idle", "recording", "finalizing"

    def __init__(self, args):
        self.args = args
        self.saved = SavedVariables(Path(args.wow_dir))
        self.watcher = ControllerWatcher(self.on_trigger)
        self.recorder = Recorder(device=args.input_device)
        self.injector = Injector()
        self.sounds = Sounds(not args.silent)
        self.transcriber: Transcriber | None = None
        self.hotkey: Hotkey | None = None
        self.close_key: Hotkey | None = None
        self.state = self.IDLE
        self.record_start = 0.0
        self._lock = threading.Lock()

    def close_command(self) -> str | None:
        choice = self.args.close_command
        if choice.lower() == "none":
            return None
        if choice.lower() == "auto":
            return self.saved.settings.close_command or DEFAULT_CLOSE_COMMAND
        return choice

    def apply_settings(self) -> None:
        trigger = self.args.button or self.saved.settings.trigger
        self.watcher.set_trigger(trigger, self.args.raw_button)
        # How the chat box gets opened before typing. Default is the game's own
        # Enter binding (OPENCHAT), which keeps addon code out of the chat path.
        choice = self.args.open_key
        if choice.lower() == "none":
            self.hotkey = None
        elif choice.lower() == "addon":
            self.hotkey = Hotkey.parse(self.saved.settings.hotkey or "")
            if self.hotkey is None:
                log(f"Addon hotkey '{self.saved.settings.hotkey}' unavailable; using Enter instead")
                self.hotkey = Hotkey.parse("ENTER")
        else:
            self.hotkey = Hotkey.parse(choice)
            if self.hotkey is None:
                log(f"Can't parse --open-key '{choice}'; using Enter")
                self.hotkey = Hotkey.parse("ENTER")
        self.close_key = None if self.args.close_key.lower() == "none" else Hotkey.parse(self.args.close_key)
        if self.close_key is None and self.args.close_key.lower() != "none":
            log(f"Can't parse --close-key '{self.args.close_key}'; not closing chat")
        log(f"Settings: trigger={trigger or 'none'} open-chat={choice} close-command={self.close_command() or 'none'}")
        if not trigger and self.args.raw_button is None:
            log("No trigger yet. In game: /gps setup, then press a controller button.")

    def run(self) -> None:
        self.transcriber = Transcriber(self.args.model, self.args.language, self.args.device, self.args.compute_type)
        self.saved.refresh()
        if self.saved.path is None:
            log(f"Addon settings file not found under {self.saved.wow_dir / 'WTF'}. "
                "Install the addon, run /gps setup in game (it reloads the UI to save).")
        self.apply_settings()
        self.watcher.start()
        log("Ready. Press the trigger to start recording.")

        last_poll = 0.0
        while True:
            self.watcher.pump()
            now = time.monotonic()
            if now - last_poll > 2.0:
                last_poll = now
                if self.saved.refresh():
                    log("Addon settings changed")
                    self.apply_settings()
            if self.state == self.RECORDING and now - self.record_start > self.args.max_seconds:
                log("Max duration reached, stopping")
                self.end_recording()
            time.sleep(0.01)

    def on_trigger(self) -> None:
        with self._lock:
            if self.state == self.IDLE:
                self.begin_recording()
            elif self.state == self.RECORDING:
                self.end_recording()
            else:
                log("Still finalizing, ignoring press")

    def begin_recording(self) -> None:
        try:
            self.recorder.start()
        except Exception as e:
            log(f"Could not start audio: {e}")
            self.sounds.play(self.sounds.error_tone)
            return
        self.state = self.RECORDING
        self.record_start = time.monotonic()
        self.sounds.play(self.sounds.start_tone)
        log("Recording...")

    def end_recording(self) -> None:
        if self.state != self.RECORDING:
            return
        self.state = self.FINALIZING
        audio = self.recorder.stop()
        self.sounds.play(self.sounds.stop_tone)
        log(f"Stopped after {time.monotonic() - self.record_start:.1f}s, transcribing...")
        threading.Thread(target=self._finish, args=(audio,), daemon=True).start()

    def _finish(self, audio: np.ndarray) -> None:
        t0 = time.monotonic()
        try:
            text = self.transcriber.transcribe(audio) if self.transcriber else ""
        except Exception as e:
            log(f"Transcription failed: {e}")
            text = ""
        ms = int((time.monotonic() - t0) * 1000)
        try:
            if not text:
                log(f"Nothing transcribed ({ms}ms)")
                self.sounds.play(self.sounds.error_tone)
                return
            log(f"Transcript ({ms}ms): {text}")
            front = wow_is_frontmost()
            if front is False and not self.args.any_app:
                log(f"WoW is not the frontmost app ({frontmost_app_name()}); not typing")
                self.sounds.play(self.sounds.error_tone)
                return
            self.injector.deliver(text, self.hotkey, self.close_key, self.close_command())
            log("Sent")
        finally:
            self.state = self.IDLE


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_check(args) -> None:
    saved = SavedVariables(Path(args.wow_dir))
    saved.refresh()
    print(f"Platform:       {SYSTEM} / Python {platform.python_version()} / pygame {pygame.version.ver} (SDL {'.'.join(map(str, pygame.get_sdl_version()))})")
    print(f"WoW dir:        {args.wow_dir} {'(ok)' if Path(args.wow_dir).exists() else '(MISSING)'}")
    print(f"Settings file:  {saved.path or 'not found'}")
    trigger = args.button or saved.settings.trigger
    print(f"Trigger:        {trigger or 'none'}")
    hk = saved.settings.hotkey
    print(f"Hotkey:         {hk or 'none'} -> {'parsed' if hk and Hotkey.parse(hk) else 'unparsed'}")
    print(f"Whisper model:  {args.model} ({args.device}/{args.compute_type}), language={args.language or 'auto'}")
    try:
        dev = sd.query_devices(kind="input")
        print(f"Mic:            {dev['name']}")
    except Exception as e:
        print(f"Mic:            none ({e})")
    watcher = ControllerWatcher(lambda: None)
    watcher.start()
    time.sleep(0.3)
    watcher.pump()
    names = watcher.describe()
    print(f"Controllers:    {len(names)}")
    for n in names:
        print(f"  - {n}")
    print(f"Frontmost app:  {frontmost_app_name() or 'unknown'} (WoW: {wow_is_frontmost()})")


def main() -> None:
    ap = argparse.ArgumentParser(description="GamepadSpeak helper: controller-triggered voice to WoW chat")
    ap.add_argument("--wow-dir", default=str(default_wow_dir()), help="WoW flavor directory (the _forever_ folder)")
    ap.add_argument("--button", help="Override the trigger button from the addon, e.g. PADSOCIAL")
    ap.add_argument("--raw-button", type=int, help="Use a raw joystick button index instead of an SDL mapping")
    ap.add_argument("--language", help="Speech language code, e.g. en or bg (default: auto-detect)")
    ap.add_argument("--model", default="base", help="Whisper model: tiny, base, small, medium, large-v3 (default: base)")
    ap.add_argument("--device", default="auto", help="Whisper device: auto, cpu, cuda")
    ap.add_argument("--compute-type", default="int8", help="Whisper compute type (default: int8)")
    ap.add_argument("--input-device", help="Mic device name or index for sounddevice")
    ap.add_argument("--close-command", default="auto",
                    help="Slash command typed after sending to leave the chat box. 'auto' (default) uses the one "
                         "the addon computed (the gamepad Back button's click target), 'none' skips it, "
                         "or give a command such as '/click InputFunctionBindingButton_PAD2 LeftButton 1'")
    ap.add_argument("--close-key", default="none",
                    help="Extra key pressed after sending, e.g. ESCAPE. Default none: the box closes by itself "
                         "when chat was opened via the addon hotkey")
    ap.add_argument("--silent", action="store_true", help="No start/stop sounds")
    ap.add_argument("--open-key", default="ENTER",
                    help="Key that opens chat before typing: ENTER (the game's Open Chat, default), "
                         "'addon' (the addon's hotkey; taints the gamepad UI in WoW Forever, avoid), "
                         "any binding like CTRL-SHIFT-F12, or 'none'")
    ap.add_argument("--max-seconds", type=float, default=60, help="Auto-stop recording after this long")
    ap.add_argument("--any-app", action="store_true", help="Type even if WoW is not the frontmost app")
    ap.add_argument("--check", action="store_true", help="Print status and exit")
    args = ap.parse_args()

    if args.input_device is not None and args.input_device.isdigit():
        args.input_device = int(args.input_device)

    if args.check:
        run_check(args)
        return

    try:
        Coordinator(args).run()
    except KeyboardInterrupt:
        log("Bye")


if __name__ == "__main__":
    main()
