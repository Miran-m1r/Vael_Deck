"""
serial_manager.py
-----------------
Handles asynchronous, thread-safe UART TX/RX communication with the ESP32.
Includes automatic COM port discovery, hot-plug reconnection, and a chunked
binary transfer protocol for sending RGB565 album art without overflowing the
ESP32's hardware UART RX FIFO / ring buffer.
"""

import logging
import threading
import time
from typing import Callable, Optional, List

import serial
import serial.tools.list_ports

logger = logging.getLogger("VaelDeck.Serial")

# Common USB-UART bridge Vendor IDs / descriptions used on ESP32 boards
# (CP210x, CH340/CH341, FTDI, Espressif native USB)
ESP32_USB_IDENTIFIERS = (
    "CP210",
    "CH340",
    "CH341",
    "CH9102",
    "FTDI",
    "USB Serial",
    "USB-SERIAL",
    "UART",
    "Espressif",
)


class SerialManager:
    """
    Manages the lifecycle of the serial connection to the ESP32 Touch Terminal.
    All writes are serialized via a re-entrant lock so text updates (clock,
    track info) and multi-chunk binary image transfers never interleave.
    """

    def __init__(
        self,
        command_callback: Callable[[str], None],
        port: str = "AUTO",
        baud_rate: int = 500000,
        reconnect_interval: float = 2.5,
    ) -> None:
        """
        :param command_callback: Function invoked whenever a line command arrives from ESP32.
        :param port: Explicit COM port (e.g., 'COM3') or 'AUTO' for automatic detection.
        :param baud_rate: UART speed (default 500,000 bps for rapid RGB565 streaming).
        :param reconnect_interval: Seconds between reconnection attempts on disconnect.
        """
        self.command_callback = command_callback
        self.configured_port = port
        self.baud_rate = baud_rate
        self.reconnect_interval = reconnect_interval

        self._serial: Optional[serial.Serial] = None
        self._tx_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self.connected = False
        self.active_port_name: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle Management
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Starts the non-blocking background serial listener & watchdog thread."""
        if self._rx_thread and self._rx_thread.is_alive():
            return
        self._stop_event.clear()
        self._rx_thread = threading.Thread(
            target=self._connection_loop,
            name="SerialListenerThread",
            daemon=True,
        )
        self._rx_thread.start()
        logger.info(
            "SerialManager started (target port=%s, baud=%d)",
            self.configured_port,
            self.baud_rate,
        )

    def stop(self) -> None:
        """Signals the background thread to terminate and closes the serial port."""
        self._stop_event.set()
        self._close_serial()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=2.0)
        logger.info("SerialManager stopped.")

    def update_connection_params(self, port: str, baud_rate: int = 500000) -> None:
        """Updates port settings at runtime and forces a clean reconnect."""
        if port != self.configured_port or baud_rate != self.baud_rate:
            logger.info("Updating serial parameters to port=%s, baud=%d", port, baud_rate)
            self.configured_port = port
            self.baud_rate = baud_rate
            self._close_serial()

    # ------------------------------------------------------------------
    # Port Discovery & Connection Loop
    # ------------------------------------------------------------------
    @staticmethod
    def list_available_ports() -> List[str]:
        """Returns a list of available COM port device names on the host."""
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]

    def _resolve_port(self) -> Optional[str]:
        """Determines which COM port to open based on configuration or heuristics."""
        if self.configured_port and self.configured_port.upper() != "AUTO":
            return self.configured_port

        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = f"{p.description or ''} {p.manufacturer or ''} {p.hwid or ''}"
            if any(token.lower() in desc.lower() for token in ESP32_USB_IDENTIFIERS):
                logger.debug("Auto-detected ESP32 candidate on %s (%s)", p.device, p.description)
                return p.device

        # Fallback to first available serial port if only one exists
        if len(ports) == 1:
            return ports[0].device
        return None

    def _connect(self) -> bool:
        """Attempts to open the resolved serial port."""
        target_port = self._resolve_port()
        if not target_port:
            return False

        try:
            with self._tx_lock:
                self._serial = serial.Serial(
                    port=target_port,
                    baudrate=self.baud_rate,
                    timeout=0.1,
                    write_timeout=1.5,
                )
                # Prevent unintended ESP32 auto-reset loops on some boards if needed,
                # and flush stale boot logs.
                time.sleep(0.15)
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()

            self.connected = True
            self.active_port_name = target_port
            logger.info("Connected to ESP32 on %s @ %d baud.", target_port, self.baud_rate)
            return True
        except (serial.SerialException, OSError) as exc:
            self.connected = False
            self.active_port_name = None
            logger.debug("Could not open serial port %s: %s", target_port, exc)
            return False

    def _close_serial(self) -> None:
        """Safely closes the underlying pySerial instance."""
        with self._tx_lock:
            self.connected = False
            self.active_port_name = None
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception as exc:
                    logger.debug("Error while closing serial port: %s", exc)
                finally:
                    self._serial = None

    def _connection_loop(self) -> None:
        """Main background loop: maintains connection and reads incoming lines."""
        while not self._stop_event.is_set():
            if not self.connected or self._serial is None or not self._serial.is_open:
                if not self._connect():
                    self._stop_event.wait(self.reconnect_interval)
                    continue

            try:
                if self._serial and self._serial.in_waiting > 0:
                    raw_line = self._serial.readline()
                    if raw_line:
                        cmd = raw_line.decode("utf-8", errors="ignore").strip()
                        if cmd:
                            logger.info("RX <- ESP32: %s", cmd)
                            self._dispatch_command(cmd)
                else:
                    time.sleep(0.01)
            except (serial.SerialException, OSError) as exc:
                logger.warning("Serial link lost (%s). Reconnecting...", exc)
                self._close_serial()
                self._stop_event.wait(self.reconnect_interval)
            except Exception as exc:
                logger.exception("Unexpected error in serial RX loop: %s", exc)
                time.sleep(0.05)

    def _dispatch_command(self, cmd: str) -> None:
        """Invokes the registered callback on a worker thread or inline safely."""
        try:
            self.command_callback(cmd)
        except Exception as exc:
            logger.exception("Error handling command '%s': %s", cmd, exc)

    # ------------------------------------------------------------------
    # Transmission (TX) Protocol Helpers
    # ------------------------------------------------------------------
    def send_line(self, message: str) -> bool:
        """
        Sends a newline-terminated UTF-8 string to the ESP32.
        Example messages:
          - TIME:14:35
          - MEDIA:Title|Artist|Playing
          - LABELS:Mute Mic|Screenshot|Spotify|Terminal|GitHub|YouTube
        """
        if not self.connected or self._serial is None:
            return False

        payload = (message.strip() + "\n").encode("utf-8", errors="replace")
        with self._tx_lock:
            try:
                if self._serial and self._serial.is_open:
                    self._serial.write(payload)
                    self._serial.flush()
                    logger.debug("TX -> ESP32: %s", message.strip())
                    return True
            except (serial.SerialException, OSError) as exc:
                logger.warning("TX failed (%s). Marking disconnected.", exc)
                self.connected = False
        return False

    def send_album_art_rgb565(
        self,
        rgb565_bytes: bytes,
        width: int = 80,
        height: int = 80,
        chunk_size: int = 512,
        chunk_delay: float = 0.006,
    ) -> bool:
        """
        Streams a raw RGB565 byte buffer to the ESP32 using a framed, chunked
        protocol to prevent UART RX FIFO / ring-buffer overflow while the ESP32
        pushes pixels over SPI to the ILI9341 display.

        Wire Protocol:
          1. Header line : ``IMG_START:<width>:<height>:<total_bytes>\\n``
          2. Binary body : Raw RGB565 bytes sent in ``chunk_size`` (512 B) slices
                           with ``chunk_delay`` seconds pause between slices.
          3. Footer line : ``\\nIMG_END\\n``

        Because ``_tx_lock`` is held for the entire frame, periodic ``TIME:`` or
        ``MEDIA:`` lines will never corrupt the binary stream.
        """
        if not self.connected or self._serial is None:
            return False

        total_len = len(rgb565_bytes)
        expected_len = width * height * 2
        if total_len != expected_len:
            logger.error(
                "RGB565 buffer size mismatch: got %d bytes, expected %d (%dx%dx2)",
                total_len,
                expected_len,
                width,
                height,
            )
            return False

        header = f"IMG_START:{width}:{height}:{total_len}\n".encode("ascii")
        footer = b"\nIMG_END\n"

        with self._tx_lock:
            try:
                if not self._serial or not self._serial.is_open:
                    return False

                logger.info(
                    "Streaming RGB565 album art (%dx%d, %d bytes in %d-byte chunks)...",
                    width,
                    height,
                    total_len,
                    chunk_size,
                )

                # 1. Send Header and allow ESP32 parser to switch into binary DMA/read mode
                self._serial.write(header)
                self._serial.flush()
                time.sleep(0.015)

                # 2. Stream raw binary chunks with flow-control pacing
                for offset in range(0, total_len, chunk_size):
                    chunk = rgb565_bytes[offset : offset + chunk_size]
                    self._serial.write(chunk)
                    self._serial.flush()
                    if chunk_delay > 0:
                        time.sleep(chunk_delay)

                # 3. Send Footer
                time.sleep(0.010)
                self._serial.write(footer)
                self._serial.flush()
                logger.info("Album art transfer complete (%d bytes).", total_len)
                return True

            except (serial.SerialException, OSError) as exc:
                logger.warning("Failed during RGB565 transfer: %s", exc)
                self.connected = False
                return False
