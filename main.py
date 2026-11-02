"""
main.py
-------
Application entry point for VaelDeck — the PC-side "Brain" and router for an
ESP32 + 2.8" ILI9341 Touch Screen DIY Stream Deck / Macro Pad.

Responsibilities:
  1. Configures structured console & rotating file logging.
  2. Initializes `ActionEngine` (`config.json` parser & command executor).
  3. Initializes `SerialManager` (500,000 baud non-blocking TX/RX + auto-reconnect).
  4. Initializes `MediaTracker` (Windows GSMTC "Now Playing", `TIME:HH:MM` clock,
     and 80x80 RGB565 chunked album art streamer).
  5. Starts the `CustomTkinter` + `pystray` System Tray application loop.

Usage:
  - Run silently in the background (System Tray mode, default):
        python main.py --minimized
  - Run and immediately show the configuration window:
        python main.py
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from action_engine import ActionEngine
from gui import StreamDeckApp
from media_tracker import MediaTracker
from serial_manager import SerialManager


def configure_logging(verbose: bool = False) -> None:
    """Sets up thread-aware logging to both stdout and `vael_deck.log`."""
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s | %(levelname)-7s | %(threadName)-20s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(__file__).resolve().parent / "vael_deck.log",
            encoding="utf-8",
        ),
    ]
    logging.basicConfig(level=log_level, format=log_format, datefmt=date_format, handlers=handlers)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VaelDeck — PC-side Router & Controller for ESP32 Macro Pad"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="Path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--art-size",
        type=int,
        choices=[60, 80],
        default=80,
        help="Square album art resolution in pixels (60 or 80; default: 80)",
    )
    parser.add_argument(
        "--endian",
        type=str,
        choices=["big", "little"],
        default="big",
        help="RGB565 byte order: 'big' (MSB first, default for TFT_eSPI) or 'little'",
    )
    parser.add_argument(
        "--show-gui",
        action="store_true",
        help="Open the CustomTkinter window immediately on launch instead of starting minimized in the system tray.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(verbose=args.verbose)
    logger = logging.getLogger("VaelDeck.Main")
    logger.info("Starting VaelDeck PC Controller...")

    # 1. Initialize Action Engine & Load config.json
    action_engine = ActionEngine(config_path=args.config)
    cfg = action_engine.load_config()
    serial_cfg = cfg.get("serial", {})

    # 2. Initialize Non-Blocking Serial Manager (500,000 baud)
    serial_manager = SerialManager(
        command_callback=action_engine.execute_command,
        port=serial_cfg.get("port", "AUTO"),
        baud_rate=int(serial_cfg.get("baud_rate", 500000)),
    )
    action_engine.set_serial_manager(serial_manager)

    # 3. Initialize Windows Media & Clock Tracker (RGB565 Album Art Streamer)
    media_tracker = MediaTracker(
        serial_manager=serial_manager,
        art_size=args.art_size,
        byte_order=args.endian,
        chunk_size=512,
        chunk_delay=0.006,
    )

    # 4. Start Background Worker Threads
    serial_manager.start()
    media_tracker.start()

    def clean_shutdown() -> None:
        logger.info("Stopping background worker threads...")
        media_tracker.stop()
        serial_manager.stop()

    # 5. Initialize CustomTkinter GUI + System Tray Icon
    # By default, starts silently in the system tray unless `--show-gui` is passed.
    app = StreamDeckApp(
        action_engine=action_engine,
        serial_manager=serial_manager,
        on_quit_callback=clean_shutdown,
        start_minimized=not args.show_gui,
    )

    # Handle Ctrl+C gracefully from terminal
    def _sigint_handler(_sig: int, _frame: object) -> None:
        app.after(0, app.shutdown_application)

    signal.signal(signal.SIGINT, _sigint_handler)

    logger.info(
        "VaelDeck is running (%s). Double-click the System Tray icon to open the configurator.",
        "GUI visible" if args.show_gui else "Minimized to System Tray",
    )
    app.mainloop()


if __name__ == "__main__":
    main()
