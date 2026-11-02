/**
 * ============================================================================
 * VaelDeck_ESP32_Firmware.ino
 * ============================================================================
 * Target Hardware:
 *   - MCU     : ESP32 (ESP32-WROOM / DevKit V1 / CYD ESP32-2432S028R)
 *   - Display : 2.8" ILI9341 320x240 SPI TFT (driven via TFT_eSPI)
 *   - Touch   : XPT2046 Resistive Touch Controller (driven via XPT2046_Touchscreen)
 *
 * Libraries Required (Arduino Library Manager):
 *   1. TFT_eSPI by Bodmer (Ensure User_Setup.h is configured for your ILI9341 pins
 *      and `#define LOAD_GLCD`, `#define LOAD_FONT2`, `#define LOAD_FONT4`,
 *      `#define SMOOTH_FONT` are enabled).
 *   2. XPT2046_Touchscreen by Paul Stoffregen.
 *
 * Architecture & Serial Wire Protocol (500,000 Baud):
 *   - Outgoing Touch Events (ESP32 -> PC):
 *       Macro Grid : "BTN_1\n" .. "BTN_6\n"
 *       Media Bar  : "CMD_PREV\n", "CMD_PLAY\n", "CMD_NEXT\n"
 *   - Incoming Commands (PC -> ESP32):
 *       1. "TIME:HH:MM\n"                  -> Redraws top-right clock
 *       2. "UPDATE_BTN_1:ACTIVE|INACTIVE\n"-> Toggles BTN_1 (Deafen) icon state
 *       3. "UPDATE_BTN_2:ACTIVE|INACTIVE\n"-> Toggles BTN_2 (Live) icon state
 *       4. "MEDIA:Title|Artist|Status\n"   -> Updates Play/Pause icon & track bar
 *       5. "IMG_START:80:80:12800\n"       -> Streams 12,800 raw RGB565 bytes
 *          followed by "\nIMG_END\n"          directly into the album art buffer.
 * ============================================================================
 */

#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <XPT2046_Touchscreen.h>

// ============================================================================
// 1. HARDWARE PIN & TOUCH CALIBRATION DEFINITIONS
// ============================================================================
// Adjust TOUCH_CS and TOUCH_IRQ to match your ESP32 wiring (default SPI VSPI/HSPI)
#ifndef TOUCH_CS
  #define TOUCH_CS  33
#endif
#ifndef TOUCH_IRQ
  #define TOUCH_IRQ 36
#endif

// Raw XPT2046 ADC calibration bounds for 320x240 landscape orientation (Rotation 1)
#define TS_MINX 240
#define TS_MAXX 3800
#define TS_MINY 240
#define TS_MAXY 3800

// Serial speed (matches Python SerialManager 500,000 bps)
#define SERIAL_BAUD_RATE     500000
// Enlarged hardware UART RX ring buffer so 512-byte binary chunks never drop bytes
#define UART_RX_BUFFER_BYTES 4096

// ============================================================================
// 2. COLOR PALETTE (16-Bit RGB565, Flat Tablet UI Theme)
// ============================================================================
// Convert #RRGGBB to RGB565: ((R & 0xF8) << 8) | ((G & 0xFC) << 3) | (B >> 3)
#define COLOR_BG_DARK        0x1082  // #121212 - Main Tablet Background
#define COLOR_CARD_BG        0x2104  // #212121 - Macro Button & Top Card Surface
#define COLOR_CARD_FLASH     0x52AA  // #555555 - Lighter Gray Touch Flash Feedback
#define COLOR_CARD_BORDER    0x3186  // #333333 - Subtle 1px Card Outline
#define COLOR_WHITE          0xFFFF  // #FFFFFF - Primary Icon & Clock Color
#define COLOR_TEXT_MUTED     0x9CD3  // #9E9E9E - Secondary Captions / Track Text
#define COLOR_ALERT_RED      0xF986  // #FF3333 - Active Deafen Slash / Live Fill
#define COLOR_SPOTIFY_GREEN  0x1EC9  // #1DB954 - Spotify Brand Accent
#define COLOR_STEAM_BLUE     0x3B9F  // #3B73FF - Steam Brand Accent
#define COLOR_YOUTUBE_RED    0xF800  // #FF0000 - YouTube Brand Accent

// ============================================================================
// 3. SCREEN LAYOUT GEOMETRY (320x240 Landscape)
// ============================================================================
#define SCREEN_W       320
#define SCREEN_H       240

// Top Bar (Y: 8 .. 88, Height: 80px)
#define ALBUM_X        10
#define ALBUM_Y        8
#define ALBUM_SIZE     80
#define ALBUM_BYTES    (ALBUM_SIZE * ALBUM_SIZE * 2) // 12,800 bytes

// Media Transport Controls (Top Center, X: 98 .. 208, Y: 8 .. 88)
#define MEDIA_PREV_X   100
#define MEDIA_PLAY_X   136
#define MEDIA_NEXT_X   172
#define MEDIA_BTN_Y    20
#define MEDIA_BTN_W    32
#define MEDIA_BTN_H    36

// Clock Region (Top Right, X: 212 .. 310, Y: 8 .. 88)
#define CLOCK_X        212
#define CLOCK_Y        14
#define CLOCK_W        98
#define CLOCK_H        52

// Bottom Macro Pad Grid (3 Columns x 2 Rows, Y: 98 .. 232)
#define GRID_COLS      3
#define GRID_ROWS      2
#define BTN_COUNT      6
#define GRID_START_X   10
#define GRID_START_Y   98
#define BTN_W          94
#define BTN_H          62
#define GRID_GAP_X     9
#define GRID_GAP_Y     10
#define BTN_RADIUS     10

// ============================================================================
// 4. GLOBAL INSTANCES & APPLICATION STATE
// ============================================================================
TFT_eSPI tft = TFT_eSPI();
XPT2046_Touchscreen ts(TOUCH_CS, TOUCH_IRQ);

struct MacroButton {
  int16_t x;
  int16_t y;
  int16_t w;
  int16_t h;
  const char* cmd;
  bool isActive; // Used by BTN_1 (Deafen) and BTN_2 (Live Stream)
};

MacroButton macroButtons[BTN_COUNT];

// Dynamic UI State
String currentClock      = "00:00";
String currentTrackTitle = "No Media";
bool   isMediaPlaying    = false;

// Non-blocking Serial Line Parser Buffer
String serialLineBuffer  = "";

// Dedicated 12.8 KB RAM buffer for 80x80 RGB565 Album Art
uint16_t albumArtBuffer[ALBUM_SIZE * ALBUM_SIZE];

// Touch Debounce Tracking
uint32_t lastTouchTimeMs = 0;
const uint32_t TOUCH_DEBOUNCE_MS = 220;

// Forward Declarations
void initButtonGeometry();
void drawFullUI();
void drawAlbumArtPlaceholder();
void drawMediaControls(int8_t flashIndex = -1);
void drawTrackBanner();
void drawClock();
void drawMacroButton(uint8_t idx, bool flash = false);
void drawIconDeafen(int16_t cx, int16_t cy, bool active);
void drawIconLiveStream(int16_t cx, int16_t cy, bool active);
void drawIconScreenshot(int16_t cx, int16_t cy);
void drawIconSpotify(int16_t cx, int16_t cy);
void drawIconSteam(int16_t cx, int16_t cy);
void drawIconYouTube(int16_t cx, int16_t cy);
void handleSerialIncoming();
void processSerialCommand(const String& line);
void receiveAlbumArtBinary(uint16_t width, uint16_t height, uint32_t totalBytes);
void handleTouchInput();

// ============================================================================
// 5. SETUP & MAIN LOOP
// ============================================================================
void setup() {
  // Expand hardware UART0 RX buffer before calling Serial.begin() so 500kbaud
  // binary bursts never overflow while SPI transactions are active.
  Serial.setRxBufferSize(UART_RX_BUFFER_BYTES);
  Serial.begin(SERIAL_BAUD_RATE);

  // Initialize TFT Display
  tft.init();
  tft.setRotation(1); // 320x240 Landscape
  // Python MediaTracker sends Big-Endian RGB565 (>H, MSB first).
  // Setting setSwapBytes(false) ensures pushImage((uint16_t*)) sends the exact byte stream.
  tft.setSwapBytes(false);

  // Initialize Touch Screen
  ts.begin();
  ts.setRotation(1);

  serialLineBuffer.reserve(128);
  initButtonGeometry();
  drawFullUI();
}

void loop() {
  handleSerialIncoming();
  handleTouchInput();
}

// ============================================================================
// 6. UI GEOMETRY & COMPONENT RENDERING
// ============================================================================
void initButtonGeometry() {
  static const char* CMD_NAMES[BTN_COUNT] = {
    "BTN_1", "BTN_2", "BTN_3", "BTN_4", "BTN_5", "BTN_6"
  };

  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    uint8_t col = i % GRID_COLS;
    uint8_t row = i / GRID_COLS;
    macroButtons[i].x = GRID_START_X + col * (BTN_W + GRID_GAP_X);
    macroButtons[i].y = GRID_START_Y + row * (BTN_H + GRID_GAP_Y);
    macroButtons[i].w = BTN_W;
    macroButtons[i].h = BTN_H;
    macroButtons[i].cmd = CMD_NAMES[i];
    macroButtons[i].isActive = false;
  }
}

void drawFullUI() {
  tft.fillScreen(COLOR_BG_DARK);
  drawAlbumArtPlaceholder();
  drawMediaControls(-1);
  drawClock();
  drawTrackBanner();

  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    drawMacroButton(i, false);
  }
}

void drawAlbumArtPlaceholder() {
  tft.fillRoundRect(ALBUM_X, ALBUM_Y, ALBUM_SIZE, ALBUM_SIZE, 8, COLOR_CARD_BG);
  tft.drawRoundRect(ALBUM_X, ALBUM_Y, ALBUM_SIZE, ALBUM_SIZE, 8, COLOR_CARD_BORDER);

  // Minimalist beamed musical note placeholder in center of 80x80 box
  int16_t cx = ALBUM_X + ALBUM_SIZE / 2;
  int16_t cy = ALBUM_Y + ALBUM_SIZE / 2;
  tft.fillCircle(cx - 8, cy + 8, 5, COLOR_TEXT_MUTED);
  tft.fillCircle(cx + 8, cy + 5, 5, COLOR_TEXT_MUTED);
  tft.fillRect(cx - 4, cy - 12, 3, 20, COLOR_TEXT_MUTED);
  tft.fillRect(cx + 12, cy - 15, 3, 20, COLOR_TEXT_MUTED);
  tft.fillTriangle(cx - 4, cy - 12, cx + 15, cy - 15, cx + 15, cy - 10, COLOR_TEXT_MUTED);
  tft.fillTriangle(cx - 4, cy - 12, cx - 4, cy - 7, cx + 15, cy - 10, COLOR_TEXT_MUTED);
}

/**
 * Draws the 3 flat minimalist media transport buttons (Prev, Play/Pause, Next).
 * @param flashIndex -1 = normal, 0 = flash Prev, 1 = flash Play, 2 = flash Next
 */
void drawMediaControls(int8_t flashIndex) {
  const int16_t xs[3] = { MEDIA_PREV_X, MEDIA_PLAY_X, MEDIA_NEXT_X };

  for (int8_t i = 0; i < 3; i++) {
    uint16_t bg = (flashIndex == i) ? COLOR_CARD_FLASH : COLOR_CARD_BG;
    tft.fillRoundRect(xs[i], MEDIA_BTN_Y, MEDIA_BTN_W, MEDIA_BTN_H, 6, bg);

    int16_t cx = xs[i] + MEDIA_BTN_W / 2;
    int16_t cy = MEDIA_BTN_Y + MEDIA_BTN_H / 2;

    if (i == 0) {
      // PREV: |<
      tft.fillRect(cx - 7, cy - 6, 3, 13, COLOR_WHITE);
      tft.fillTriangle(cx + 6, cy - 6, cx + 6, cy + 6, cx - 3, cy, COLOR_WHITE);
    } else if (i == 1) {
      // PLAY / PAUSE
      if (isMediaPlaying) {
        tft.fillRect(cx - 5, cy - 6, 4, 13, COLOR_WHITE);
        tft.fillRect(cx + 2, cy - 6, 4, 13, COLOR_WHITE);
      } else {
        tft.fillTriangle(cx - 4, cy - 7, cx - 4, cy + 7, cx + 7, cy, COLOR_WHITE);
      }
    } else {
      // NEXT: >|
      tft.fillTriangle(cx - 6, cy - 6, cx - 6, cy + 6, cx + 3, cy, COLOR_WHITE);
      tft.fillRect(cx + 4, cy - 6, 3, 13, COLOR_WHITE);
    }
  }
}

void drawTrackBanner() {
  // Subtle single-line track title directly underneath the media controls
  int16_t x = MEDIA_PREV_X;
  int16_t y = MEDIA_BTN_Y + MEDIA_BTN_H + 6;
  int16_t w = SCREEN_W - MEDIA_PREV_X - 10;
  tft.fillRect(x, y, w, 20, COLOR_BG_DARK);
  tft.setTextColor(COLOR_TEXT_MUTED, COLOR_BG_DARK);
  tft.setTextDatum(TL_DATUM);
  tft.drawString(currentTrackTitle, x + 2, y + 2, 2); // Built-in smooth Font 2
}

void drawClock() {
  tft.fillRect(CLOCK_X, CLOCK_Y, CLOCK_W, CLOCK_H, COLOR_BG_DARK);
  tft.setTextColor(COLOR_WHITE, COLOR_BG_DARK);
  tft.setTextDatum(MR_DATUM); // Middle-Right aligned
  // Font 4 is TFT_eSPI's crisp 26px proportional sans-serif font
  tft.drawString(currentClock, CLOCK_X + CLOCK_W - 2, CLOCK_Y + 24, 4);
}

// ============================================================================
// 7. MACRO BUTTONS & VECTOR ICON RENDERERS
// ============================================================================
void drawMacroButton(uint8_t idx, bool flash) {
  if (idx >= BTN_COUNT) return;
  const MacroButton& btn = macroButtons[idx];

  uint16_t fillCol = flash ? COLOR_CARD_FLASH : COLOR_CARD_BG;
  uint16_t borderCol = btn.isActive ? COLOR_ALERT_RED : COLOR_CARD_BORDER;

  tft.fillRoundRect(btn.x, btn.y, btn.w, btn.h, BTN_RADIUS, fillCol);
  tft.drawRoundRect(btn.x, btn.y, btn.w, btn.h, BTN_RADIUS, borderCol);

  int16_t cx = btn.x + btn.w / 2;
  int16_t cy = btn.y + btn.h / 2;

  switch (idx) {
    case 0: drawIconDeafen(cx, cy, btn.isActive);     break; // BTN_1: Deafen
    case 1: drawIconLiveStream(cx, cy, btn.isActive); break; // BTN_2: Live Stream
    case 2: drawIconScreenshot(cx, cy);               break; // BTN_3: Screenshot
    case 3: drawIconSpotify(cx, cy);                  break; // BTN_4: Spotify
    case 4: drawIconSteam(cx, cy);                    break; // BTN_5: Steam
    case 5: drawIconYouTube(cx, cy);                  break; // BTN_6: YouTube
  }
}

/**
 * BTN_1 (Deafen):
 *   - Normal state : White headphones icon (headband arc + left/right ear cups)
 *   - Active state : Bold red diagonal slash across the headphones
 */
void drawIconDeafen(int16_t cx, int16_t cy, bool active) {
  uint16_t bg = COLOR_CARD_BG;
  // Headband outer ring + inner mask
  tft.fillCircle(cx, cy - 1, 14, COLOR_WHITE);
  tft.fillCircle(cx, cy - 1, 11, bg);
  tft.fillRect(cx - 15, cy - 1, 31, 16, bg);

  // Ear cup stems & pads
  tft.fillRoundRect(cx - 15, cy - 2, 6, 13, 2, COLOR_WHITE);
  tft.fillRoundRect(cx + 9,  cy - 2, 6, 13, 2, COLOR_WHITE);

  if (active) {
    // Draw thick 3px red diagonal slash across the headphones
    for (int8_t offset = -1; offset <= 1; offset++) {
      tft.drawLine(cx - 16 + offset, cy - 15, cx + 16 + offset, cy + 15, COLOR_ALERT_RED);
      tft.drawLine(cx - 16, cy - 15 + offset, cx + 16, cy + 15 + offset, COLOR_ALERT_RED);
    }
  }
}

/**
 * BTN_2 (Live Stream):
 *   - Normal state : White Screen / Monitor outline + stand
 *   - Active state : Solid red fill inside the monitor screen
 */
void drawIconLiveStream(int16_t cx, int16_t cy, bool active) {
  // Monitor outer frame
  tft.drawRoundRect(cx - 16, cy - 13, 32, 21, 3, COLOR_WHITE);
  tft.drawRoundRect(cx - 15, cy - 12, 30, 19, 2, COLOR_WHITE);

  // Monitor neck and base stand
  tft.fillRect(cx - 2, cy + 8, 4, 5, COLOR_WHITE);
  tft.fillRect(cx - 9, cy + 12, 18, 2, COLOR_WHITE);

  if (active) {
    // Solid red inner screen fill + white broadcast dot
    tft.fillRect(cx - 13, cy - 10, 26, 15, COLOR_ALERT_RED);
    tft.fillCircle(cx, cy - 3, 3, COLOR_WHITE);
  }
}

/**
 * BTN_3 (Screenshot): Minimalist Camera / Viewfinder icon
 */
void drawIconScreenshot(int16_t cx, int16_t cy) {
  // Top prism hump
  tft.fillRoundRect(cx - 6, cy - 13, 12, 5, 2, COLOR_WHITE);
  // Camera body outline
  tft.drawRoundRect(cx - 15, cy - 9, 30, 21, 4, COLOR_WHITE);
  tft.drawRoundRect(cx - 14, cy - 8, 28, 19, 3, COLOR_WHITE);
  // Center lens ring
  tft.drawCircle(cx, cy + 1, 6, COLOR_WHITE);
  tft.fillCircle(cx, cy + 1, 2, COLOR_WHITE);
}

/**
 * BTN_4 (Spotify): Green disc with 3 curved sound-wave bars
 */
void drawIconSpotify(int16_t cx, int16_t cy) {
  tft.fillCircle(cx, cy, 14, COLOR_SPOTIFY_GREEN);
  // Three sound wave arcs inside the disc
  tft.drawLine(cx - 7, cy - 4, cx + 7, cy - 4, COLOR_BG_DARK);
  tft.drawLine(cx - 8, cy - 3, cx + 8, cy - 3, COLOR_BG_DARK);

  tft.drawLine(cx - 6, cy + 1, cx + 6, cy + 1, COLOR_BG_DARK);
  tft.drawLine(cx - 6, cy + 2, cx + 6, cy + 2, COLOR_BG_DARK);

  tft.drawLine(cx - 4, cy + 6, cx + 4, cy + 6, COLOR_BG_DARK);
}

/**
 * BTN_5 (Steam): Minimalist crank-arm & piston nodes
 */
void drawIconSteam(int16_t cx, int16_t cy) {
  tft.fillCircle(cx, cy, 14, COLOR_STEAM_BLUE);
  // Upper-right large rotor wheel
  tft.fillCircle(cx + 4, cy - 4, 5, COLOR_WHITE);
  tft.fillCircle(cx + 4, cy - 4, 2, COLOR_STEAM_BLUE);
  // Lower-left crank joint
  tft.fillCircle(cx - 5, cy + 5, 4, COLOR_WHITE);
  tft.fillCircle(cx - 5, cy + 5, 2, COLOR_STEAM_BLUE);
  // Connecting rod
  tft.drawLine(cx - 5, cy + 3, cx + 2, cy - 4, COLOR_WHITE);
  tft.drawLine(cx - 4, cy + 5, cx + 4, cy - 2, COLOR_WHITE);
  tft.drawLine(cx - 3, cy + 6, cx + 5, cy - 1, COLOR_WHITE);
}

/**
 * BTN_6 (YouTube): Rounded red play badge with white center triangle
 */
void drawIconYouTube(int16_t cx, int16_t cy) {
  tft.fillRoundRect(cx - 16, cy - 11, 32, 22, 6, COLOR_YOUTUBE_RED);
  tft.fillTriangle(cx - 4, cy - 6, cx - 4, cy + 6, cx + 7, cy, COLOR_WHITE);
}

// ============================================================================
// 8. NON-BLOCKING SERIAL PARSER & CHUNKED RGB565 IMAGE RECEIVER
// ============================================================================
void handleSerialIncoming() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;

    if (c == '\n') {
      serialLineBuffer.trim();
      if (serialLineBuffer.length() > 0) {
        processSerialCommand(serialLineBuffer);
      }
      serialLineBuffer = "";
    } else {
      if (serialLineBuffer.length() < 160) {
        serialLineBuffer += c;
      }
    }
  }
}

void processSerialCommand(const String& line) {
  // 1. Clock Synchronization: "TIME:14:27"
  if (line.startsWith("TIME:")) {
    String newTime = line.substring(5);
    newTime.trim();
    if (newTime.length() > 0 && newTime != currentClock) {
      currentClock = newTime;
      drawClock();
    }
    return;
  }

  // 2. Dynamic Toggle State Feedback for BTN_1 (Deafen)
  if (line.startsWith("UPDATE_BTN_1:")) {
    String state = line.substring(13);
    state.trim();
    macroButtons[0].isActive = state.equalsIgnoreCase("ACTIVE");
    drawMacroButton(0, false);
    return;
  }

  // 3. Dynamic Toggle State Feedback for BTN_2 (Live Stream)
  if (line.startsWith("UPDATE_BTN_2:")) {
    String state = line.substring(13);
    state.trim();
    macroButtons[1].isActive = state.equalsIgnoreCase("ACTIVE");
    drawMacroButton(1, false);
    return;
  }

  // 4. Media Metadata & Playback State: "MEDIA:Title|Artist|Playing"
  if (line.startsWith("MEDIA:")) {
    String payload = line.substring(6);
    int firstSep = payload.indexOf('|');
    int secondSep = payload.indexOf('|', firstSep + 1);
    if (firstSep > 0 && secondSep > firstSep) {
      String title = payload.substring(0, firstSep);
      String status = payload.substring(secondSep + 1);
      status.trim();

      bool playing = status.equalsIgnoreCase("Playing");
      if (playing != isMediaPlaying) {
        isMediaPlaying = playing;
        drawMediaControls(-1);
      }
      if (title != currentTrackTitle) {
        currentTrackTitle = title;
        drawTrackBanner();
      }
    }
    return;
  }

  // 5. Chunked Binary RGB565 Album Art Header: "IMG_START:80:80:12800"
  if (line.startsWith("IMG_START")) {
    uint16_t w = ALBUM_SIZE;
    uint16_t h = ALBUM_SIZE;
    uint32_t expectedBytes = ALBUM_BYTES;

    int p1 = line.indexOf(':');
    int p2 = line.indexOf(':', p1 + 1);
    int p3 = line.indexOf(':', p2 + 1);
    if (p1 > 0 && p2 > p1 && p3 > p2) {
      w = (uint16_t)line.substring(p1 + 1, p2).toInt();
      h = (uint16_t)line.substring(p2 + 1, p3).toInt();
      expectedBytes = (uint32_t)line.substring(p3 + 1).toInt();
    }
    receiveAlbumArtBinary(w, h, expectedBytes);
    return;
  }
}

/**
 * Reads `totalBytes` of raw RGB565 pixel data sent in 512-byte chunks by Python,
 * drains the trailing `IMG_END` footer, and blits the image to the 80x80 album slot.
 */
void receiveAlbumArtBinary(uint16_t width, uint16_t height, uint32_t totalBytes) {
  if (totalBytes == 0 || totalBytes > ALBUM_BYTES) {
    totalBytes = ALBUM_BYTES;
  }

  uint8_t* bytePtr = reinterpret_cast<uint8_t*>(albumArtBuffer);
  uint32_t bytesRead = 0;
  uint32_t lastByteTimestamp = millis();
  const uint32_t STREAM_TIMEOUT_MS = 2500;

  // Tightly drain the UART RX FIFO as 512-byte chunks arrive
  while (bytesRead < totalBytes) {
    int avail = Serial.available();
    if (avail > 0) {
      uint32_t remaining = totalBytes - bytesRead;
      size_t toRead = (avail < (int)remaining) ? (size_t)avail : (size_t)remaining;
      size_t got = Serial.readBytes(bytePtr + bytesRead, toRead);
      bytesRead += got;
      lastByteTimestamp = millis();
    } else if (millis() - lastByteTimestamp > STREAM_TIMEOUT_MS) {
      // Abort cleanly on timeout so the UI never freezes if USB is unplugged mid-transfer
      return;
    }
  }

  // Consume trailing "\nIMG_END\n" footer line (with short 150 ms safety window)
  uint32_t footerWaitStart = millis();
  String footerBuf = "";
  while (millis() - footerWaitStart < 150) {
    while (Serial.available() > 0) {
      char c = (char)Serial.read();
      footerBuf += c;
      if (footerBuf.endsWith("IMG_END\n") || footerBuf.endsWith("IMG_END\r\n")) {
        break;
      }
    }
    if (footerBuf.indexOf("IMG_END") >= 0) {
      break;
    }
  }

  // Push the completed 80x80 RGB565 frame to the top-left album art area
  tft.pushImage(ALBUM_X, ALBUM_Y, width, height, albumArtBuffer);
  tft.drawRoundRect(ALBUM_X, ALBUM_Y, width, height, 8, COLOR_CARD_BORDER);
}

// ============================================================================
// 9. TOUCH INPUT HIT-TESTING & VISUAL FLASH FEEDBACK
// ============================================================================
void handleTouchInput() {
  if (!ts.touched()) {
    return;
  }

  uint32_t now = millis();
  if (now - lastTouchTimeMs < TOUCH_DEBOUNCE_MS) {
    return;
  }

  TS_Point p = ts.getPoint();
  // Map raw XPT2046 ADC values to 320x240 landscape coordinates
  int16_t tx = map(p.x, TS_MINX, TS_MAXX, 0, SCREEN_W);
  int16_t ty = map(p.y, TS_MINY, TS_MAXY, 0, SCREEN_H);
  tx = constrain(tx, 0, SCREEN_W - 1);
  ty = constrain(ty, 0, SCREEN_H - 1);

  // 1. Hit-test 6 Macro Buttons (BTN_1 .. BTN_6)
  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    const MacroButton& btn = macroButtons[i];
    if (tx >= btn.x && tx <= (btn.x + btn.w) &&
        ty >= btn.y && ty <= (btn.y + btn.h)) {
      lastTouchTimeMs = now;

      // Briefly flash lighter gray background before returning to dark gray
      drawMacroButton(i, true);
      Serial.print(btn.cmd);
      Serial.print('\n');
      delay(85);
      drawMacroButton(i, false);
      return;
    }
  }

  // 2. Hit-test Top Center Media Transport Controls (CMD_PREV, CMD_PLAY, CMD_NEXT)
  if (ty >= MEDIA_BTN_Y && ty <= (MEDIA_BTN_Y + MEDIA_BTN_H)) {
    const int16_t xs[3] = { MEDIA_PREV_X, MEDIA_PLAY_X, MEDIA_NEXT_X };
    const char* mediaCmds[3] = { "CMD_PREV\n", "CMD_PLAY\n", "CMD_NEXT\n" };

    for (int8_t m = 0; m < 3; m++) {
      if (tx >= xs[m] && tx <= (xs[m] + MEDIA_BTN_W)) {
        lastTouchTimeMs = now;
        drawMediaControls(m);
        Serial.print(mediaCmds[m]);
        delay(85);
        drawMediaControls(-1);
        return;
      }
    }
  }
}
