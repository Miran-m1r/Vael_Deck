# Vael_Deck — DIY ESP32 Stream Deck & Macro Pad

**Vael_Deck** is a full-stack hardware + software DIY Stream Deck / Macro Pad system built around an **ESP32** and a **2.8" ILI9341 (320×240) SPI Touch Screen** (`TFT_eSPI` + `XPT2046_Touchscreen`).

The ESP32 acts as an ultra-responsive "Flat Tablet UI" touch terminal, while the multi-threaded Python PC controller ("The Brain") handles Windows Media Session ("Now Playing") tracking, real-time 80×80 **RGB565** album art streaming over 500,000 baud UART, dynamic stateful button toggles, and a **CustomTkinter + System Tray** configurator.

---

## ✨ Key Features

- **Non-Blocking 500,000 Baud Serial Router (`serial_manager.py`)**
  - Automatic ESP32 USB-UART port detection (`CP210x`, `CH340`, `CH9102`, `FTDI`, `Espressif`) and hot-plug auto-reconnection.
  - Thread-safe TX locking so clock updates, media strings, and multi-chunk binary album art transfers never interleave.
- **Real-Time Windows Media & RGB565 Album Art Streamer (`media_tracker.py`)**
  - Integrates with Windows 10/11 `GlobalSystemMediaTransportControlsSessionManager` (`winsdk`) to fetch live track title, artist, playback status, and album art.
  - Resizes thumbnails to `80x80` (or `60x60`) using `Pillow` Lanczos resampling, converts pixels to **16-bit RGB565**, and streams them in **512-byte chunks** (`IMG_START` $\rightarrow$ `[BYTES]` $\rightarrow$ `IMG_END`) with inter-chunk pacing to prevent ESP32 UART buffer overflow.
  - Syncs the host system time (`TIME:HH:MM`) every minute.
- **Stateful Action Engine (`action_engine.py`)**
  - Executes macro actions from `config.json` without restarting:
    1. `hotkey` — Multi-key shortcuts via `pyautogui` (e.g., `ctrl,shift,d`).
    2. `launch` — Application / executable launcher via `os.startfile` / `subprocess`.
    3. `url` — Opens web or protocol links (`https://`, `spotify:`, `steam://`).
  - Tracks stateful toggle buttons (`BTN_1` **Deafen** & `BTN_2` **Live Stream**) and immediately pushes visual state feedback (`UPDATE_BTN_1:ACTIVE` / `INACTIVE`) back to the ESP32.
- **Dark-Themed CustomTkinter GUI & System Tray (`gui.py`)**
  - Runs silently in the background via `pystray`. Double-clicking the tray icon opens the configuration window.
  - **Save & Apply** persists changes to `config.json` atomically and updates the ESP32 immediately without restarting the app.
- **Flat Tablet UI ESP32 Firmware (`esp32_firmware/VaelDeck_ESP32_Firmware.ino`)**
  - 320×240 dark `#121212` interface with 80×80 album art slot, flat media transport controls (`Prev`, `Play/Pause`, `Next`), live track banner, smooth clock, and a 3×2 grid of vector-drawn macro buttons with touch flash feedback.

---

## 📁 Project Structure

```text
vael-deck/
├── README.md
├── .gitignore
├── requirements.txt
├── config.json
├── main.py                                    # Entry point, CLI args & thread lifecycle
├── serial_manager.py                          # Non-blocking 500kbaud UART TX/RX & chunked image transfer
├── media_tracker.py                           # Windows GSMTC Now Playing + Pillow RGB565 converter
├── action_engine.py                           # Stateful macro executor & config.json manager
├── gui.py                                     # CustomTkinter dark UI + pystray System Tray integration
└── esp32_firmware/
    └── VaelDeck_ESP32_Firmware.ino            # Complete ESP32 C++ firmware (TFT_eSPI + XPT2046)
```

---

## 🔌 Serial Wire Protocol (`500000` Baud)

| Direction | Frame | Description |
| :--- | :--- | :--- |
| **ESP32 $\rightarrow$ PC** | `BTN_1\n` … `BTN_6\n` | Triggers the macro assigned to button 1–6 in `config.json` |
| **ESP32 $\rightarrow$ PC** | `CMD_PREV\n`, `CMD_PLAY\n`, `CMD_NEXT\n` | Triggers system media transport keys (`prevtrack`, `playpause`, `nexttrack`) |
| **PC $\rightarrow$ ESP32** | `TIME:HH:MM\n` | Updates the top-right clock display |
| **PC $\rightarrow$ ESP32** | `MEDIA:<Title>\|<Artist>\|<Status>\n` | Updates the track banner and Play/Pause icon (`Playing` / `Paused` / `Stopped`) |
| **PC $\rightarrow$ ESP32** | `UPDATE_BTN_1:ACTIVE\n` / `INACTIVE\n` | Toggles the red diagonal mute slash on **BTN_1 (Deafen)** |
| **PC $\rightarrow$ ESP32** | `UPDATE_BTN_2:ACTIVE\n` / `INACTIVE\n` | Toggles the solid red broadcast fill inside **BTN_2 (Live Stream)** |
| **PC $\rightarrow$ ESP32** | `IMG_START:80:80:12800\n` + `[12800 B]` + `\nIMG_END\n` | Streams 80×80 Big-Endian RGB565 album art in 512-byte chunks |

---

## 🚀 Getting Started

### 1. PC Controller Setup (Python 3.10+)

```bash
# Install dependencies
pip install -r requirements.txt

# Run silently in the System Tray (double-click the tray icon to open GUI)
python main.py

# Or launch with the CustomTkinter GUI window open immediately
python main.py --show-gui
```

#### Optional CLI Flags
- `--show-gui` : Opens the configuration window immediately on startup.
- `--art-size {60,80}` : Album art resolution in pixels (default: `80`).
- `--endian {big,little}` : RGB565 byte order (default: `big` for `TFT_eSPI`).
- `--verbose` : Enable `DEBUG` logging to console and `vael_deck.log`.

### 2. ESP32 Firmware Setup (Arduino IDE / PlatformIO)

1. Install the following libraries via Arduino Library Manager:
   - **`TFT_eSPI`** by Bodmer
   - **`XPT2046_Touchscreen`** by Paul Stoffregen
2. Configure your `TFT_eSPI/User_Setup.h` for your **ILI9341** SPI pins and make sure `LOAD_GLCD`, `LOAD_FONT2`, `LOAD_FONT4`, and `SMOOTH_FONT` are enabled.
3. Open [`esp32_firmware/VaelDeck_ESP32_Firmware.ino`](esp32_firmware/VaelDeck_ESP32_Firmware.ino), verify `TOUCH_CS` / `TOUCH_IRQ` pins for your board, and upload to your ESP32.
