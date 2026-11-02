"""
gui.py
------
Provides a dark-themed CustomTkinter configuration window integrated with a
background `pystray` system tray icon.

Key Behaviors:
  - Runs silently in the system tray on startup (or can be opened immediately).
  - Double-clicking (default tray action) or selecting "Open Configurator" opens
    the CustomTkinter window on the main UI thread.
  - Clicking the window's [X] button hides (`withdraw()`) the window back to the
    system tray instead of terminating the background router.
  - Clicking "Save & Apply" persists `config.json` immediately, updates serial
    port settings if changed, and broadcasts updated button labels to the ESP32
    without requiring an application restart.
"""

import logging
import threading
from typing import Any, Dict, Optional

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

from action_engine import ActionEngine, BUTTON_IDS, SUPPORTED_ACTION_TYPES
from serial_manager import SerialManager

logger = logging.getLogger("VaelDeck.GUI")

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class ButtonConfigRow(ctk.CTkFrame):
    """Represents a single macro button (`BTN_1`..`BTN_6`) configuration row."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        btn_id: str,
        initial_data: Dict[str, str],
    ) -> None:
        super().__init__(master, corner_radius=8)
        self.btn_id = btn_id

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)
        self.grid_columnconfigure(3, weight=2)

        # Button Badge
        self.badge = ctk.CTkLabel(
            self,
            text=btn_id,
            width=72,
            height=32,
            corner_radius=6,
            fg_color=("#1F6AA5", "#1F538D"),
            text_color="white",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.badge.grid(row=0, column=0, padx=(12, 8), pady=10)

        # Display Label Entry
        self.label_entry = ctk.CTkEntry(
            self,
            placeholder_text="Button Label (e.g., Mute)",
            width=140,
        )
        self.label_entry.insert(0, initial_data.get("label", btn_id))
        self.label_entry.grid(row=0, column=1, padx=6, pady=10, sticky="ew")

        # Action Type Dropdown
        current_type = initial_data.get("type", "hotkey").lower()
        if current_type not in SUPPORTED_ACTION_TYPES:
            current_type = "hotkey"
        self.type_var = ctk.StringVar(value=current_type)
        self.type_menu = ctk.CTkOptionMenu(
            self,
            values=SUPPORTED_ACTION_TYPES,
            variable=self.type_var,
            width=115,
            command=self._on_type_changed,
        )
        self.type_menu.grid(row=0, column=2, padx=6, pady=10)

        # Action Value Entry
        self.value_entry = ctk.CTkEntry(
            self,
            placeholder_text=self._placeholder_for_type(current_type),
        )
        self.value_entry.insert(0, initial_data.get("value", ""))
        self.value_entry.grid(row=0, column=3, padx=(6, 12), pady=10, sticky="ew")

    @staticmethod
    def _placeholder_for_type(action_type: str) -> str:
        placeholders = {
            "hotkey": "e.g., ctrl,shift,s",
            "launch": "e.g., spotify.exe or C:\\path\\app.exe",
            "url": "e.g., https://github.com",
        }
        return placeholders.get(action_type, "Enter value...")

    def _on_type_changed(self, selected_type: str) -> None:
        self.value_entry.configure(
            placeholder_text=self._placeholder_for_type(selected_type)
        )

    def get_data(self) -> Dict[str, str]:
        return {
            "label": self.label_entry.get().strip() or self.btn_id,
            "type": self.type_var.get().strip().lower(),
            "value": self.value_entry.get().strip(),
        }

    def set_data(self, data: Dict[str, str]) -> None:
        self.label_entry.delete(0, "end")
        self.label_entry.insert(0, data.get("label", self.btn_id))

        action_type = data.get("type", "hotkey").lower()
        if action_type not in SUPPORTED_ACTION_TYPES:
            action_type = "hotkey"
        self.type_var.set(action_type)

        self.value_entry.delete(0, "end")
        self.value_entry.insert(0, data.get("value", ""))
        self._on_type_changed(action_type)


class StreamDeckApp(ctk.CTk):
    """
    Main CustomTkinter application window + System Tray controller.
    """

    def __init__(
        self,
        action_engine: ActionEngine,
        serial_manager: SerialManager,
        on_quit_callback: Optional[ Any ] = None,
        start_minimized: bool = False,
    ) -> None:
        super().__init__()
        self.action_engine = action_engine
        self.serial_manager = serial_manager
        self.on_quit_callback = on_quit_callback

        self.title("VaelDeck — ESP32 Macro Pad Controller")
        self.geometry("760x540")
        self.minsize(680, 500)

        # Intercept window close [X] -> hide to system tray instead of exiting
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.rows: Dict[str, ButtonConfigRow] = {}
        self._tray_icon: Optional[pystray.Icon] = None
        self._tray_thread: Optional[threading.Thread] = None

        self._build_ui()
        self._setup_system_tray()
        self._poll_connection_status()

        if start_minimized:
            self.withdraw()

    # ------------------------------------------------------------------
    # UI Layout Construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 1. Top Header & Serial Bar
        header = ctk.CTkFrame(self, corner_radius=10)
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text="ESP32 Macro Pad & Media Router",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title_lbl.grid(row=0, column=0, padx=16, pady=12, sticky="w")

        self.conn_status_lbl = ctk.CTkLabel(
            header,
            text="● Disconnected",
            text_color="#E57373",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.conn_status_lbl.grid(row=0, column=1, padx=12, pady=12, sticky="e")

        # Port Selector
        port_lbl = ctk.CTkLabel(header, text="COM Port:")
        port_lbl.grid(row=0, column=2, padx=(8, 4), pady=12)

        cfg = self.action_engine.load_config()
        current_port = cfg.get("serial", {}).get("port", "AUTO")
        available_ports = ["AUTO"] + self.serial_manager.list_available_ports()
        if current_port not in available_ports:
            available_ports.append(current_port)

        self.port_var = ctk.StringVar(value=current_port)
        self.port_menu = ctk.CTkOptionMenu(
            header,
            values=available_ports,
            variable=self.port_var,
            width=110,
        )
        self.port_menu.grid(row=0, column=3, padx=4, pady=12)

        refresh_btn = ctk.CTkButton(
            header,
            text="↻",
            width=34,
            command=self._refresh_ports,
        )
        refresh_btn.grid(row=0, column=4, padx=(2, 14), pady=12)

        # 2. Macro Buttons Configuration Container
        list_frame = ctk.CTkScrollableFrame(
            self,
            label_text="Macro Button Assignments (BTN_1 – BTN_6)",
            corner_radius=10,
        )
        list_frame.grid(row=1, column=0, padx=16, pady=8, sticky="nsew")
        list_frame.grid_columnconfigure(0, weight=1)

        buttons_cfg = cfg.get("buttons", {})
        for idx, btn_id in enumerate(BUTTON_IDS):
            row = ButtonConfigRow(
                list_frame,
                btn_id=btn_id,
                initial_data=buttons_cfg.get(btn_id, {}),
            )
            row.grid(row=idx, column=0, padx=6, pady=5, sticky="ew")
            self.rows[btn_id] = row

        # 3. Bottom Action Bar
        footer = ctk.CTkFrame(self, corner_radius=10)
        footer.grid(row=2, column=0, padx=16, pady=(8, 16), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self.feedback_lbl = ctk.CTkLabel(
            footer,
            text="Tip: Close window [X] to minimize silently to System Tray.",
            text_color="gray70",
            font=ctk.CTkFont(size=12),
        )
        self.feedback_lbl.grid(row=0, column=0, padx=16, pady=12, sticky="w")

        hide_btn = ctk.CTkButton(
            footer,
            text="Minimize to Tray",
            fg_color="transparent",
            border_width=1,
            width=130,
            command=self.hide_to_tray,
        )
        hide_btn.grid(row=0, column=1, padx=6, pady=12)

        save_btn = ctk.CTkButton(
            footer,
            text="Save & Apply",
            width=150,
            font=ctk.CTkFont(weight="bold"),
            command=self.save_and_apply,
        )
        save_btn.grid(row=0, column=2, padx=(6, 16), pady=12)

    # ------------------------------------------------------------------
    # Actions & Live Status
    # ------------------------------------------------------------------
    def _refresh_ports(self) -> None:
        ports = ["AUTO"] + self.serial_manager.list_available_ports()
        current = self.port_var.get()
        if current not in ports:
            ports.append(current)
        self.port_menu.configure(values=ports)
        self.feedback_lbl.configure(
            text=f"Detected {len(ports) - 1} serial port(s).",
            text_color="gray80",
        )

    def _poll_connection_status(self) -> None:
        """Updates the live connection badge every 1,000 ms."""
        if self.serial_manager.connected:
            port_name = self.serial_manager.active_port_name or "Serial"
            self.conn_status_lbl.configure(
                text=f"● Connected ({port_name} @ {self.serial_manager.baud_rate})",
                text_color="#66BB6A",
            )
        else:
            self.conn_status_lbl.configure(
                text=f"● Searching ({self.serial_manager.configured_port})...",
                text_color="#FFA726",
            )
        self.after(1000, self._poll_connection_status)

    def save_and_apply(self) -> None:
        """Persists UI fields to `config.json` and hot-reloads Serial/ActionEngine."""
        current_cfg = self.action_engine.load_config()
        selected_port = self.port_var.get().strip() or "AUTO"
        baud_rate = int(current_cfg.get("serial", {}).get("baud_rate", 500000))

        new_cfg: Dict[str, Any] = {
            "serial": {
                "port": selected_port,
                "baud_rate": baud_rate,
            },
            "buttons": {
                btn_id: row.get_data() for btn_id, row in self.rows.items()
            },
        }

        if self.action_engine.save_config(new_cfg):
            self.serial_manager.update_connection_params(
                port=selected_port,
                baud_rate=baud_rate,
            )
            # Push updated button captions to ESP32 if connected
            self.serial_manager.send_line(self.action_engine.get_button_labels_payload())
            self.feedback_lbl.configure(
                text="✓ Configuration saved & applied immediately!",
                text_color="#66BB6A",
            )
        else:
            self.feedback_lbl.configure(
                text="✗ Error saving config.json. Check logs.",
                text_color="#E57373",
            )

    # ------------------------------------------------------------------
    # System Tray Integration (`pystray`)
    # ------------------------------------------------------------------
    @staticmethod
    def _create_tray_icon_image() -> Image.Image:
        """Generates a crisp 64x64 Stream Deck grid icon in memory via Pillow."""
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Rounded dark chassis background
        draw.rounded_rectangle(
            (2, 2, size - 2, size - 2),
            radius=12,
            fill=(24, 28, 36, 255),
            outline=(58, 134, 255, 255),
            width=2,
        )

        # Draw 3x2 macro key grid
        cols, rows = 3, 2
        pad_x, pad_y = 10, 14
        cell_w, cell_h = 12, 14
        gap_x, gap_y = 4, 6
        colors = [
            (31, 106, 165),
            (46, 196, 182),
            (255, 159, 28),
            (231, 29, 54),
            (131, 56, 236),
            (58, 134, 255),
        ]
        idx = 0
        for r in range(rows):
            for c in range(cols):
                x0 = pad_x + c * (cell_w + gap_x)
                y0 = pad_y + r * (cell_h + gap_y)
                draw.rounded_rectangle(
                    (x0, y0, x0 + cell_w, y0 + cell_h),
                    radius=3,
                    fill=colors[idx % len(colors)],
                )
                idx += 1
        return img

    def _setup_system_tray(self) -> None:
        """Starts the `pystray` icon in a daemon thread."""
        # Setting `default=True` on "Open Configurator" binds double-click on Windows
        # to open the GUI window immediately.
        menu = pystray.Menu(
            pystray.MenuItem(
                "Open Configurator",
                self._on_tray_open,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit VaelDeck", self._on_tray_quit),
        )
        self._tray_icon = pystray.Icon(
            name="VaelDeck",
            icon=self._create_tray_icon_image(),
            title="VaelDeck — ESP32 Macro Pad",
            menu=menu,
        )
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run,
            name="SystemTrayThread",
            daemon=True,
        )
        self._tray_thread.start()

    def _on_tray_open(
        self,
        icon: Optional[pystray.Icon] = None,
        item: Optional[pystray.MenuItem] = None,
    ) -> None:
        """Schedules window restoration safely onto the Tkinter main thread."""
        self.after(0, self.show_window)

    def _on_tray_quit(
        self,
        icon: Optional[pystray.Icon] = None,
        item: Optional[pystray.MenuItem] = None,
    ) -> None:
        """Cleanly stops tray icon, background threads, and exits the app."""
        self.after(0, self.shutdown_application)

    def show_window(self) -> None:
        """Restores and focuses the CustomTkinter window from the system tray."""
        cfg = self.action_engine.load_config()
        buttons_cfg = cfg.get("buttons", {})
        for btn_id, row in self.rows.items():
            if btn_id in buttons_cfg:
                row.set_data(buttons_cfg[btn_id])
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self) -> None:
        """Hides the window so the app continues running silently in the system tray."""
        self.withdraw()
        logger.info("GUI hidden to system tray.")

    def shutdown_application(self) -> None:
        """Stops system tray, invokes shutdown hooks, and destroys root window."""
        logger.info("Shutting down VaelDeck application...")
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        if callable(self.on_quit_callback):
            try:
                self.on_quit_callback()
            except Exception as exc:
                logger.error("Error in shutdown callback: %s", exc)
        self.quit()
        self.destroy()
