"""
action_engine.py
----------------
Loads and persists `config.json`, validates macro assignments, tracks dynamic
toggle states for stateful buttons (`BTN_1` Deafen, `BTN_2` Live), and executes
PC-side actions triggered by the ESP32 touch screen (`BTN_1`..`BTN_6`,
`CMD_PLAY`, `CMD_NEXT`, `CMD_PREV`).
"""

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import pyautogui

if TYPE_CHECKING:
    from serial_manager import SerialManager

logger = logging.getLogger("VaelDeck.ActionEngine")

# Disable pyautogui fail-safe corner exception so unattended macro presses
# never crash the background listener if the user's cursor is at (0, 0).
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

BUTTON_IDS: List[str] = [f"BTN_{i}" for i in range(1, 7)]
SUPPORTED_ACTION_TYPES: List[str] = ["hotkey", "launch", "url"]

DEFAULT_CONFIG: Dict[str, Any] = {
    "serial": {
        "port": "AUTO",
        "baud_rate": 500000,
    },
    "buttons": {
        "BTN_1": {"label": "Deafen", "type": "hotkey", "value": "ctrl,shift,d"},
        "BTN_2": {"label": "Live Stream", "type": "hotkey", "value": "alt,f9"},
        "BTN_3": {"label": "Screenshot", "type": "hotkey", "value": "win,shift,s"},
        "BTN_4": {"label": "Spotify", "type": "launch", "value": "spotify.exe"},
        "BTN_5": {"label": "Steam", "type": "launch", "value": "steam://open/main"},
        "BTN_6": {"label": "YouTube", "type": "url", "value": "https://youtube.com"},
    },
}


class ActionEngine:
    """
    Thread-safe configuration manager, stateful toggle tracker, and command router.
    Reads `config.json` dynamically, dispatches macro or media commands, and sends
    visual state feedback (`UPDATE_BTN_x:ACTIVE|INACTIVE`) back to the ESP32.
    """

    def __init__(
        self,
        config_path: str = "config.json",
        serial_manager: Optional["SerialManager"] = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.serial_manager: Optional["SerialManager"] = serial_manager
        self._lock = threading.RLock()
        self.config: Dict[str, Any] = {}

        # Dynamic state tracking for toggle buttons (BTN_1 = Deafen, BTN_2 = Live)
        self.button_states: Dict[str, bool] = {
            "BTN_1": False,
            "BTN_2": False,
        }

        self.load_config()

    def set_serial_manager(self, serial_manager: "SerialManager") -> None:
        """
        Allows setter injection of `SerialManager` when `ActionEngine` is
        instantiated prior to `SerialManager` in `main.py`.
        """
        self.serial_manager = serial_manager

    def sync_all_button_states(self) -> None:
        """
        Pushes the current toggle states of `BTN_1` and `BTN_2` to the ESP32
        (useful upon initial serial connection or reconnection).
        """
        if not self.serial_manager:
            return
        with self._lock:
            for btn_id, is_active in self.button_states.items():
                state_str = "ACTIVE" if is_active else "INACTIVE"
                self.serial_manager.send_line(f"UPDATE_{btn_id}:{state_str}")

    # ------------------------------------------------------------------
    # Configuration Persistence
    # ------------------------------------------------------------------
    def load_config(self) -> Dict[str, Any]:
        """Loads and validates `config.json`, creating it with defaults if missing."""
        with self._lock:
            if not self.config_path.exists():
                logger.info("Config file missing; creating default at %s", self.config_path)
                self.config = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save_config(self.config)
                return self.config

            try:
                with self.config_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                self.config = self._normalize_config(loaded)
                logger.debug("Configuration loaded from %s", self.config_path)
            except Exception as exc:
                logger.error(
                    "Failed to parse %s (%s). Falling back to defaults.",
                    self.config_path,
                    exc,
                )
                self.config = json.loads(json.dumps(DEFAULT_CONFIG))
            return self.config

    def save_config(self, new_config: Dict[str, Any]) -> bool:
        """Atomically writes the updated configuration to `config.json`."""
        with self._lock:
            try:
                normalized = self._normalize_config(new_config)
                temp_path = self.config_path.with_suffix(".tmp")
                with temp_path.open("w", encoding="utf-8") as fp:
                    json.dump(normalized, fp, indent=2)
                temp_path.replace(self.config_path)
                self.config = normalized
                logger.info("Configuration saved to %s", self.config_path)
                return True
            except Exception as exc:
                logger.exception("Failed to save configuration: %s", exc)
                return False

    @staticmethod
    def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures all required keys (`serial` and `BTN_1`..`BTN_6`) exist."""
        normalized: Dict[str, Any] = {
            "serial": {
                "port": str(raw.get("serial", {}).get("port", "AUTO")),
                "baud_rate": int(raw.get("serial", {}).get("baud_rate", 500000)),
            },
            "buttons": {},
        }
        raw_buttons = raw.get("buttons", {})
        for btn_id in BUTTON_IDS:
            default_btn = DEFAULT_CONFIG["buttons"][btn_id]
            entry = raw_buttons.get(btn_id, default_btn)
            action_type = str(entry.get("type", default_btn["type"])).lower().strip()
            if action_type not in SUPPORTED_ACTION_TYPES:
                action_type = "hotkey"
            normalized["buttons"][btn_id] = {
                "label": str(entry.get("label", btn_id)).strip() or btn_id,
                "type": action_type,
                "value": str(entry.get("value", "")).strip(),
            }
        return normalized

    def get_button_labels_payload(self) -> str:
        """
        Builds a pipe-delimited string of button labels so the ESP32 can
        optionally update its on-screen touch button captions:
        ``LABELS:Deafen|Live Stream|Screenshot|Spotify|Steam|YouTube``
        """
        with self._lock:
            labels = [
                self.config.get("buttons", {}).get(btn_id, {}).get("label", btn_id)
                for btn_id in BUTTON_IDS
            ]
        return "LABELS:" + "|".join(labels)

    # ------------------------------------------------------------------
    # Command Execution & Dynamic State Feedback
    # ------------------------------------------------------------------
    def execute_command(self, command: str) -> None:
        """
        Entry point invoked by SerialManager when a line arrives from ESP32.
        Supported commands:
          - Macro buttons : `BTN_1` .. `BTN_6`
          - Media buttons : `CMD_PLAY`, `CMD_NEXT`, `CMD_PREV`, `CMD_MUTE`,
                            `CMD_VOL_UP`, `CMD_VOL_DOWN`
        """
        cmd = command.strip().upper()
        if not cmd:
            return

        # 1. Built-in Media Transport Commands
        media_key_map = {
            "CMD_PLAY": "playpause",
            "CMD_PAUSE": "playpause",
            "CMD_NEXT": "nexttrack",
            "CMD_PREV": "prevtrack",
            "CMD_MUTE": "volumemute",
            "CMD_VOL_UP": "volumeup",
            "CMD_VOL_DOWN": "volumedown",
        }
        if cmd in media_key_map:
            key = media_key_map[cmd]
            logger.info("Executing built-in media transport command: %s -> %s", cmd, key)
            try:
                pyautogui.press(key)
            except Exception as exc:
                logger.error("Failed to send media key '%s': %s", key, exc)
            return

        # 2. Macro Buttons (Reloads config from disk to ensure zero-restart reactivity)
        self.load_config()
        with self._lock:
            btn_cfg = self.config.get("buttons", {}).get(cmd)

        if not btn_cfg:
            logger.warning("Received unrecognized command from ESP32: '%s'", command)
            return

        action_type = btn_cfg.get("type", "").lower()
        value = btn_cfg.get("value", "").strip()
        label = btn_cfg.get("label", cmd)

        if not value:
            logger.warning("Button %s (%s) has an empty action value.", cmd, label)
            return

        logger.info("Triggering %s (%s): type=%s, value='%s'", cmd, label, action_type, value)

        if action_type == "hotkey":
            self._run_hotkey(value)
        elif action_type == "launch":
            self._run_launch(value)
        elif action_type == "url":
            self._run_url(value)
        else:
            logger.error("Unsupported action type '%s' for %s", action_type, cmd)
            return

        # 3. Dynamic Toggle State & Visual Feedback for BTN_1 (Deafen) and BTN_2 (Live)
        if cmd in self.button_states:
            with self._lock:
                self.button_states[cmd] = not self.button_states[cmd]
                is_active = self.button_states[cmd]

            state_str = "ACTIVE" if is_active else "INACTIVE"
            feedback_msg = f"UPDATE_{cmd}:{state_str}"
            logger.info("Toggled %s state -> %s", cmd, feedback_msg)

            if self.serial_manager is not None:
                self.serial_manager.send_line(feedback_msg)
            else:
                logger.warning(
                    "Cannot send '%s' to ESP32: serial_manager is not attached.",
                    feedback_msg,
                )

    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------
    @staticmethod
    def _run_hotkey(value: str) -> None:
        """
        Parses a comma- or plus-separated shortcut string (e.g., 'ctrl,shift,d'
        or 'ctrl+shift+d') and triggers `pyautogui.hotkey(*keys)`.
        """
        try:
            normalized = value.replace("+", ",")
            keys = [k.strip().lower() for k in normalized.split(",") if k.strip()]
            if not keys:
                return
            alias_map = {
                "control": "ctrl",
                "windows": "win",
                "super": "win",
                "cmd": "command",
                "escape": "esc",
            }
            resolved_keys = [alias_map.get(k, k) for k in keys]
            pyautogui.hotkey(*resolved_keys)
        except Exception as exc:
            logger.error("Hotkey execution failed for '%s': %s", value, exc)

    @staticmethod
    def _run_launch(value: str) -> None:
        """
        Launches an application executable, shortcut, or command with arguments.
        Uses `os.startfile` on Windows when a direct path/executable is given,
        and falls back to `subprocess.Popen` for CLI commands with arguments.
        """
        try:
            expanded = os.path.expandvars(os.path.expanduser(value))
            if sys.platform == "win32":
                if os.path.exists(expanded) or (" " not in expanded):
                    try:
                        os.startfile(expanded)  # type: ignore[attr-defined]
                        return
                    except OSError:
                        pass

                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
                subprocess.Popen(
                    expanded,
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
            else:
                args = shlex.split(expanded)
                subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except Exception as exc:
            logger.error("Application launch failed for '%s': %s", value, exc)

    @staticmethod
    def _run_url(value: str) -> None:
        """Opens a URL in the user's default web browser."""
        try:
            url = value
            if not url.startswith(("http://", "https://", "spotify:", "steam://")):
                url = "https://" + url
            webbrowser.open(url, new=2)
        except Exception as exc:
            logger.error("Failed to open URL '%s': %s", value, exc)
