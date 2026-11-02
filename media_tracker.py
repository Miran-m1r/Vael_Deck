"""
media_tracker.py
----------------
Polls the Windows 10/11 Global System Media Transport Controls (GSMTC) via
`winsdk` for real-time "Now Playing" metadata and album art, plus dispatches
the current system clock (`TIME:HH:MM`) every minute.

Album Art Pipeline:
  1. Reads the WinRT `IRandomAccessStreamReference` thumbnail stream into memory.
  2. Crops/resizes the image to 80x80 pixels (configurable, e.g. 60x60 or 80x80)
     using high-quality Lanczos resampling via `Pillow` (PIL).
  3. Converts 24-bit RGB888 pixels into 16-bit **RGB565** (5 bits Red, 6 bits Green,
     5 bits Blue).
  4. **Endianness Specification:** Default is **Big-Endian** (`>H`, MSB first),
     which matches the native byte order of `TFT_eSPI::pushImage()` (with
     `setSwapBytes(true)` or standard SPI MSB-first streaming) and `Adafruit_ILI9341`
     on the ESP32. Little-Endian (`<H`) is also supported via a constructor flag.
  5. Streams the resulting 12,800-byte (80x80x2) buffer to `SerialManager` in
     512-byte chunks with inter-chunk pacing delays.
"""

import asyncio
import datetime
import io
import logging
import struct
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageOps

from serial_manager import SerialManager

logger = logging.getLogger("VaelDeck.MediaTracker")

# Attempt to import Windows Runtime (WinRT) Media Control & Storage Stream APIs.
# Gracefully degrade on non-Windows development hosts or if winsdk is absent.
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaSessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winsdk.windows.storage.streams import DataReader, Buffer, InputStreamOptions

    WINSDK_AVAILABLE = True
except ImportError:
    WINSDK_AVAILABLE = False
    logger.warning(
        "winsdk package not available on this OS/environment. "
        "Clock updates (TIME:HH:MM) will run normally; Windows media polling is disabled."
    )


@dataclass
class NowPlayingState:
    title: str = ""
    artist: str = ""
    status: str = "Stopped"

    @property
    def track_signature(self) -> str:
        """Unique key used to detect track changes for album art extraction."""
        return f"{self.title.strip()}__{self.artist.strip()}"


class MediaTracker:
    """
    Background worker that owns a dedicated asyncio event loop to query
    Windows Media Control sessions and push `TIME:`, `MEDIA:`, and chunked
    RGB565 album art frames over Serial to the ESP32.
    """

    def __init__(
        self,
        serial_manager: SerialManager,
        art_size: int = 80,
        byte_order: str = "big",
        poll_interval: float = 1.5,
        chunk_size: int = 512,
        chunk_delay: float = 0.006,
    ) -> None:
        """
        :param serial_manager: Active SerialManager instance.
        :param art_size: Square pixel dimension for album art (e.g., 80 for 80x80 or 60 for 60x60).
        :param byte_order: 'big' (Big-Endian RGB565, MSB first) or 'little' (Little-Endian RGB565).
        :param poll_interval: Seconds between GSMTC media checks.
        :param chunk_size: Bytes per UART chunk when streaming RGB565 binary data.
        :param chunk_delay: Delay in seconds between UART chunks.
        """
        self.serial = serial_manager
        self.art_size = art_size
        self.byte_order = byte_order.lower()
        self.poll_interval = poll_interval
        self.chunk_size = chunk_size
        self.chunk_delay = chunk_delay

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._last_time_sent: str = ""
        self._last_media_payload: str = ""
        self._last_art_signature: str = ""
        self._was_connected: bool = False

    # ------------------------------------------------------------------
    # Thread Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Starts the background asyncio thread for media & clock tracking."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_entry,
            name="MediaTrackerThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "MediaTracker started (art=%dx%d, endian=%s).",
            self.art_size,
            self.art_size,
            self.byte_order,
        )

    def stop(self) -> None:
        """Stops the background media & clock loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)
        logger.info("MediaTracker stopped.")

    def _thread_entry(self) -> None:
        """Runs a dedicated asyncio event loop inside the worker thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_loop())
        except Exception as exc:
            logger.exception("Unhandled fatal exception in MediaTracker loop: %s", exc)
        finally:
            loop.close()

    # ------------------------------------------------------------------
    # Core Polling Loop
    # ------------------------------------------------------------------
    async def _async_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Detect fresh ESP32 connection/reconnection so we immediately
                # push current clock, metadata, and album art to populate the screen.
                if self.serial.connected and not self._was_connected:
                    self._last_time_sent = ""
                    self._last_media_payload = ""
                    self._last_art_signature = ""
                self._was_connected = self.serial.connected

                if self.serial.connected:
                    self._check_and_send_time()
                    if WINSDK_AVAILABLE:
                        await self._check_and_send_media()
            except Exception as exc:
                logger.debug("Non-fatal error in MediaTracker cycle: %s", exc)

            # Responsive sleep that wakes immediately if stop() is called
            await asyncio.sleep(self.poll_interval)

    def _check_and_send_time(self) -> None:
        """Sends `TIME:HH:MM` whenever the system minute changes."""
        now_str = datetime.datetime.now().strftime("%H:%M")
        if now_str != self._last_time_sent:
            if self.serial.send_line(f"TIME:{now_str}"):
                self._last_time_sent = now_str
                logger.info("Sent clock sync: TIME:%s", now_str)

    # ------------------------------------------------------------------
    # Windows GSMTC Media Session Inspection
    # ------------------------------------------------------------------
    async def _check_and_send_media(self) -> None:
        """
        Queries Windows GlobalSystemMediaTransportControlsSessionManager for
        the current session, sends `MEDIA:<title>|<artist>|<status>`, and
        streams RGB565 album art whenever the song changes.
        """
        try:
            manager = await MediaSessionManager.request_async()
        except Exception as exc:
            logger.debug("Could not acquire Windows MediaSessionManager: %s", exc)
            return

        session = manager.get_current_session()
        if session is None:
            # All media players (Spotify, Browser, etc.) are closed
            idle_payload = "MEDIA:No Media|---|Stopped"
            if idle_payload != self._last_media_payload:
                if self.serial.send_line(idle_payload):
                    self._last_media_payload = idle_payload
                    self._last_art_signature = ""
            return

        try:
            info = session.get_playback_info()
            status_str = self._map_playback_status(
                info.playback_status if info else None
            )
            props = await session.try_get_media_properties_async()
        except Exception as exc:
            logger.debug("Transient error reading GSMTC session properties: %s", exc)
            return

        if props is None:
            return

        title = self._sanitize_text(props.title or "Unknown Track")
        artist = self._sanitize_text(props.artist or props.album_artist or "Unknown Artist")
        state = NowPlayingState(title=title, artist=artist, status=status_str)

        media_payload = f"MEDIA:{state.title}|{state.artist}|{state.status}"
        if media_payload != self._last_media_payload:
            if self.serial.send_line(media_payload):
                self._last_media_payload = media_payload
                logger.info("Now Playing updated: %s - %s (%s)", state.artist, state.title, state.status)

        # If the track changed and has a valid title, fetch & transmit RGB565 album art
        if state.title and state.track_signature != self._last_art_signature:
            self._last_art_signature = state.track_signature
            if props.thumbnail is not None:
                raw_img_bytes = await self._read_thumbnail_bytes(props.thumbnail)
                if raw_img_bytes:
                    rgb565_payload = self.image_bytes_to_rgb565(
                        raw_img_bytes,
                        width=self.art_size,
                        height=self.art_size,
                        byte_order=self.byte_order,
                    )
                    if rgb565_payload:
                        self.serial.send_album_art_rgb565(
                            rgb565_bytes=rgb565_payload,
                            width=self.art_size,
                            height=self.art_size,
                            chunk_size=self.chunk_size,
                            chunk_delay=self.chunk_delay,
                        )

    @staticmethod
    def _map_playback_status(status_enum: object) -> str:
        if not WINSDK_AVAILABLE or status_enum is None:
            return "Stopped"
        mapping = {
            PlaybackStatus.PLAYING: "Playing",
            PlaybackStatus.PAUSED: "Paused",
            PlaybackStatus.STOPPED: "Stopped",
            PlaybackStatus.CHANGING: "Changing",
        }
        return mapping.get(status_enum, "Stopped")

    @staticmethod
    def _sanitize_text(text: str, max_len: int = 48) -> str:
        """Removes protocol delimiters (`|` and newlines) and truncates safely."""
        cleaned = text.replace("|", "-").replace("\r", " ").replace("\n", " ").strip()
        if len(cleaned) > max_len:
            return cleaned[: max_len - 3] + "..."
        return cleaned

    @staticmethod
    async def _read_thumbnail_bytes(thumbnail_ref: object) -> Optional[bytes]:
        """Reads a WinRT `IRandomAccessStreamReference` into a Python `bytes` object."""
        stream = None
        reader = None
        try:
            stream = await thumbnail_ref.open_read_async()  # type: ignore[attr-defined]
            size = int(stream.size)
            if size <= 0 or size > 15 * 1024 * 1024:
                return None

            winrt_buffer = Buffer(size)
            await stream.read_async(winrt_buffer, size, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(winrt_buffer)
            byte_array = bytearray(size)
            reader.read_bytes(byte_array)
            return bytes(byte_array)
        except Exception as exc:
            logger.debug("Failed to extract WinRT thumbnail stream: %s", exc)
            return None
        finally:
            try:
                if reader is not None:
                    reader.close()
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # RGB565 Color Conversion Engine
    # ------------------------------------------------------------------
    @staticmethod
    def image_bytes_to_rgb565(
        image_bytes: bytes,
        width: int = 80,
        height: int = 80,
        byte_order: str = "big",
    ) -> Optional[bytes]:
        """
        Resizes an input image (JPEG/PNG/BMP/WebP) to exactly `width x height`
        and converts every pixel into 16-bit RGB565 format:

            Bit layout (16-bit integer):
            [ R4 R3 R2 R1 R0 | G5 G4 G3 G2 G1 G0 | B4 B3 B2 B1 B0 ]
              15 .......... 11  10 ........... 5   4 ........... 0

            Formula:
            R5 = (R8 >> 3) & 0x1F
            G6 = (G8 >> 2) & 0x3F
            B5 = (B8 >> 3) & 0x1F
            RGB565 = (R5 << 11) | (G6 << 5) | B5

        :param image_bytes: Encoded source image bytes.
        :param width: Target width in pixels (60 or 80).
        :param height: Target height in pixels (60 or 80).
        :param byte_order:
            - ``'big'``    -> Big-Endian (`>H`, MSB first: `[HighByte, LowByte]`).
            - ``'little'`` -> Little-Endian (`<H`, LSB first: `[LowByte, HighByte]`).
        :return: Raw bytearray of length `width * height * 2` bytes.
        """
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                # Flatten any alpha transparency onto a dark background and convert to RGB888
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    background = Image.new("RGB", img.size, (18, 18, 18))
                    rgba = img.convert("RGBA")
                    background.paste(rgba, mask=rgba.split()[3])
                    rgb_img = background
                else:
                    rgb_img = img.convert("RGB")

                # Center-crop & resize to exact square dimensions using Lanczos filter
                resized = ImageOps.fit(
                    rgb_img,
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )

                raw_rgb = resized.tobytes()  # 3 bytes per pixel: R, G, B
                num_pixels = width * height
                out = bytearray(num_pixels * 2)

                pack_fmt = ">H" if byte_order.lower() == "big" else "<H"
                out_idx = 0
                for i in range(0, len(raw_rgb), 3):
                    r = raw_rgb[i]
                    g = raw_rgb[i + 1]
                    b = raw_rgb[i + 2]
                    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                    struct.pack_into(pack_fmt, out, out_idx, rgb565)
                    out_idx += 2

                return bytes(out)
        except Exception as exc:
            logger.error("Failed converting album art to RGB565: %s", exc)
            return None
