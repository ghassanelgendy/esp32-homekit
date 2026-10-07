#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include <rom/rtc.h>

// --- Wi-Fi Credentials ---
// Configured for your room AP "Tor" (Open / No Password)
const char* ssid = "Tor";
const char* password = "";

// --- Dual IR Setup ---
const uint16_t kIrAcPin = 27;   // AC IR transmitter on GPIO 27
const uint16_t kIrLedPin = 23;  // LED Strip IR transmitter on GPIO 23 (moved from GPIO 4)

IRsend irsendAC(kIrAcPin);
IRsend irsendLED(kIrLedPin);

const int LED_PIN = 2;  // Onboard status indicator

WebServer server(80);
Preferences prefs;

// --- Relay / Lamp Setup ---
// Relays are active-LOW (LOW = ON, HIGH = OFF).
// relay 1 = GPIO 25 (main lamp)
// relay 2 = GPIO 26 (fairy light)
// relay 3 = GPIO 33 (fan)
// relay 4 = GPIO 32 (backlight)
struct Lamp {
  const char* name;
  uint8_t pin;
  bool on;
};

Lamp lamps[] = {
  {"main", 25, false},       // Lamp 1 - Main
  {"fairy", 26, false},      // Lamp 2 - Fairy Light
  {"fan", 33, false},        // Lamp 3 - Fan
  {"backlight", 32, false},  // Lamp 4 - Backlight
};
const size_t LAMP_COUNT = sizeof(lamps) / sizeof(lamps[0]);

// --- AC state tracking variables ---
const float AC_MIN_TEMP = 16.0;
const float AC_MAX_TEMP = 34.0;
float targetTemperature = 25.0;     // Always a whole number
int targetHeatingCoolingState = 0;  // HomeKit: 0=OFF, 1=HEAT, 2=COOL, 3=AUTO

unsigned long last_ac_temp_time = 0;              // Last time a temp press was transmitted
const unsigned long AC_WAKEUP_TIMEOUT = 20000UL;  // Display sleeps after 20s without a temp press

#define AC_CODE_POWER     0x80FF48B7
#define AC_CODE_TEMP_UP   0x80FF58A7
#define AC_CODE_TEMP_DOWN 0x80FFC837
#define AC_CODE_MODE      0x80FF40BF
#define AC_CODE_FAN       0x80FF708F
#define AC_CODE_SWING     0x80FFF00F
#define AC_CODE_ECO       0x80FF50AF

// --- LED Strip control variables ---
bool led_power = false;
int led_brightness = 100;
int led_hue = 0;
int led_saturation = 0;
int led_speed_level = 1;  // 1 = Low (slow), 2 = Medium, 3 = High (fast)

// Scanned IR code dictionary matching the LED strip remote buttons
#define LED_ON       0xF7C03F
#define LED_OFF      0xF740BF
#define LED_BRI_UP   0xF700FF
#define LED_BRI_DWN  0xF7807F

#define LED_RED      0xF720DF
#define LED_GREEN    0xF7A05F
#define LED_BLUE     0xF7609F
#define LED_WHITE    0xF7E01F

#define LED_ORANGE   0xF710EF
#define LED_LTGREEN  0xF7906F
#define LED_LTBLUE   0xF750AF

#define LED_LTORANGE 0xF730CF
#define LED_CYAN     0xF7B04F
#define LED_PURPLE   0xF7708F

#define LED_YELLOR   0xF708F7
#define LED_DKCYAN   0xF78877
#define LED_MAGENTA  0xF748B7

#define LED_YELLOW   0xF728D7
#define LED_DKBLUE   0xF7A857
#define LED_PINK     0xF76897

// LED Modes IR Codes
#define LED_FLASH    0xF7D02F
#define LED_STROBE   0xF7F00F
#define LED_FADE     0xF7C837
#define LED_SMOOTH   0xF7E817

// --- Virtual state toggles to make switches stateful ---
bool swing_state = false;
bool eco_state = false;
int fan_speed_level = 3;  // 1 = Low, 2 = Medium, 3 = High (default to High matching AC)
int ac_mode_level = 1;    // 1 = Cool, 2 = Dry, 3 = Auto
int led_mode_level = 1;   // 1 = Solid, 2 = Flash, 3 = Strobe, 4 = Fade, 5 = Smooth

// --- Timer variables ---
bool timer_active = false;
unsigned long timer_duration_ms = 0;
unsigned long timer_start_time = 0;
const unsigned long AC_TIMER_FULL_SCALE_MS = 24UL * 60UL * 60UL * 1000UL;  // 100% = 24 hours

// LED Strip Timer variables
bool led_timer_active = false;
unsigned long led_timer_duration_ms = 0;
unsigned long led_timer_start_time = 0;
const unsigned long LED_TIMER_FULL_SCALE_MS = 120UL * 60UL * 1000UL;  // 100% = 120 minutes

// Tracks the last time we interacted with the LED strip
unsigned long last_led_command_time = 0;
const unsigned long LED_REFRESH_INTERVAL_MS = 3600000UL;  // 1 hour in milliseconds

// --- WiFi watchdog variables ---
unsigned long last_wifi_check_time = 0;
const unsigned long WIFI_CHECK_INTERVAL_MS = 5000UL;
const unsigned long WIFI_RECONNECT_GRACE_MS = 25000UL;
const unsigned long WIFI_HARD_REBOOT_TIMEOUT_MS = 180000UL;  // 3 minutes
bool wifi_down = false;
unsigned long wifi_down_since = 0;
unsigned long last_heartbeat_time = 0;

// --- AC state persistence (NVS) ---
struct AcSnapshot {
  float temp;
  uint8_t state;
  uint8_t fan;
  uint8_t mode;
  uint8_t swing;
  uint8_t eco;
};

bool acStateDirty = false;
unsigned long acStateDirtyTime = 0;
const unsigned long AC_STATE_SAVE_DELAY_MS = 2000UL;

void saveAcState() {
  AcSnapshot snap;
  snap.temp = targetTemperature;
  snap.state = (uint8_t)targetHeatingCoolingState;
  snap.fan = (uint8_t)fan_speed_level;
  snap.mode = (uint8_t)ac_mode_level;
  snap.swing = swing_state ? 1 : 0;
  snap.eco = eco_state ? 1 : 0;
  prefs.putBytes("snap", &snap, sizeof(snap));
  acStateDirty = false;
}

void markAcStateDirty() {
  acStateDirty = true;
  acStateDirtyTime = millis();
}

void loadAcState() {
  AcSnapshot snap;
  if (prefs.getBytes("snap", &snap, sizeof(snap)) != sizeof(snap)) return;
  if (snap.temp == snap.temp) targetTemperature = constrain(roundf(snap.temp), AC_MIN_TEMP, AC_MAX_TEMP);
  if (snap.state <= 3) targetHeatingCoolingState = snap.state;
  if (snap.fan >= 1 && snap.fan <= 3) fan_speed_level = snap.fan;
  if (snap.mode >= 1 && snap.mode <= 3) ac_mode_level = snap.mode;
  swing_state = (snap.swing != 0);
  eco_state = (snap.eco != 0);
}

// --- Non-blocking IR transmit queue ---
const uint8_t IR_TARGET_AC  = 0;
const uint8_t IR_TARGET_LED = 1;

const uint16_t IR_GAP_SINGLE        = 100;
const uint16_t IR_GAP_AC_STEP       = 250;
const uint16_t IR_GAP_LED_STEP      = 150;
const uint16_t IR_GAP_LED_POWER_ON  = 300;

struct IrJob {
  uint32_t code;
  uint16_t gapMs;
  uint8_t target;
  uint8_t repeat;
};

const uint8_t IR_QUEUE_SIZE = 48;
IrJob irQueue[IR_QUEUE_SIZE];
uint8_t irHead = 0;
uint8_t irCount = 0;
uint8_t irPendingAcTempPresses = 0;
unsigned long irLastSendTime = 0;
uint16_t irGapMs = 0;

bool isAcTempPress(uint8_t target, uint32_t code) {
  return (target == IR_TARGET_AC) && (code == AC_CODE_TEMP_UP || code == AC_CODE_TEMP_DOWN);
}

bool irSendNext() {
  if (irCount == 0 || millis() - irLastSendTime < irGapMs) return false;

  const IrJob job = irQueue[irHead];
  irHead = (irHead + 1) % IR_QUEUE_SIZE;
  irCount--;

  digitalWrite(LED_PIN, HIGH);
  if (job.target == IR_TARGET_AC) {
    irsendAC.sendNEC(job.code, 32, job.repeat);
  } else {
    irsendLED.sendNEC(job.code, 32, job.repeat);
  }
  digitalWrite(LED_PIN, LOW);

  irLastSendTime = millis();
  irGapMs = job.gapMs;
  if (isAcTempPress(job.target, job.code)) {
    if (irPendingAcTempPresses > 0) irPendingAcTempPresses--;
    last_ac_temp_time = irLastSendTime;
  }
  return true;
}

void irEnqueue(uint8_t target, uint32_t code, uint8_t repeat, uint16_t gapMs) {
  while (irCount >= IR_QUEUE_SIZE) {
    if (!irSendNext()) delay(1);
  }
  IrJob& slot = irQueue[(irHead + irCount) % IR_QUEUE_SIZE];
  slot.code = code;
  slot.gapMs = gapMs;
  slot.target = target;
  slot.repeat = repeat;
  irCount++;
  if (isAcTempPress(target, code)) irPendingAcTempPresses++;
}

void irFlush() {
  while (irCount > 0 || millis() - irLastSendTime < irGapMs) {
    if (!irSendNext()) delay(1);
  }
}

void acPress(uint32_t code, uint16_t gapMs = IR_GAP_AC_STEP) {
  irEnqueue(IR_TARGET_AC, code, 0, gapMs);
}

void ledPress(uint32_t code, uint16_t gapMs = IR_GAP_SINGLE) {
  irEnqueue(IR_TARGET_LED, code, 0, gapMs);
}

void acPowerPress() {
  irEnqueue(IR_TARGET_AC, AC_CODE_POWER, 1, IR_GAP_SINGLE);
}

void queueAcTempSteps(int steps) {
  uint32_t code = (steps > 0) ? AC_CODE_TEMP_UP : AC_CODE_TEMP_DOWN;
  for (int i = abs(steps); i > 0; i--) acPress(code, IR_GAP_AC_STEP);
}

// --- Request argument parsing ---
bool readNumberArg(const char* name, float& out) {
  if (!server.hasArg(name)) return false;
  String raw = server.arg(name);
  char* end;
  float v = strtof(raw.c_str(), &end);
  if (end == raw.c_str() || !(v > -1e6f && v < 1e6f)) return false;
  out = v;
  return true;
}

bool readStateArg(const char* name, int& out) {
  float v;
  if (!readNumberArg(name, v)) return false;
  int s = (int)v;
  if (s < 0 || s > 3) return false;
  out = s;
  return true;
}

float clampAcTemp(float t) {
  return constrain(roundf(t), AC_MIN_TEMP, AC_MAX_TEMP);
}

// --- Relay / Lamp helpers ---
void setLamp(size_t i, bool on) {
  if (i >= LAMP_COUNT) return;
  digitalWrite(lamps[i].pin, on ? LOW : HIGH);
  lamps[i].on = on;
}

void handleLampsStatus() {
  char json[72];
  snprintf(json, sizeof(json),
           "{\"main\":%s,\"fairy\":%s,\"fan\":%s,\"backlight\":%s}",
           lamps[0].on ? "true" : "false",
           lamps[1].on ? "true" : "false",
           lamps[2].on ? "true" : "false",
           lamps[3].on ? "true" : "false");
  server.send(200, "application/json", json);
}

void handleStatus() {
  char json[128];
  snprintf(json, sizeof(json),
           "{\"targetTemperature\":%.1f,\"currentTemperature\":%.1f,"
           "\"targetHeatingCoolingState\":%d,\"currentHeatingCoolingState\":%d}",
           targetTemperature, targetTemperature,
           targetHeatingCoolingState, targetHeatingCoolingState);
  server.send(200, "application/json", json);
}

void handleSetTargetTemp() {
  float requested;
  if (readNumberArg("value", requested)) {
    float target = clampAcTemp(requested);
    int steps = (int)lroundf(target - targetTemperature);

    if (steps != 0) {
      bool needsWakeup = (irPendingAcTempPresses == 0) &&
                         (last_ac_temp_time == 0 || (millis() - last_ac_temp_time > AC_WAKEUP_TIMEOUT));
      if (needsWakeup) {
        acPress(steps > 0 ? AC_CODE_TEMP_UP : AC_CODE_TEMP_DOWN, IR_GAP_AC_STEP);
      }
      queueAcTempSteps(steps);
      Serial.printf("IR: Temp %s %d steps%s -> %.0fC\n",
                    steps > 0 ? "UP" : "DOWN", abs(steps),
                    needsWakeup ? " (+1 wakeup press)" : "", target);

      targetTemperature = target;
      markAcStateDirty();
    }
  }
  server.send(200, "text/plain", "OK");
}

void applyAcState(int newState, bool force = false) {
  bool currentlyOn = (targetHeatingCoolingState != 0);
  bool wantOn = (newState != 0);

  if (force || (currentlyOn != wantOn)) {
    acPowerPress();
    Serial.printf("IR: Transmitted AC power command (state: %d -> %d, force: %d)\n",
                  targetHeatingCoolingState, newState, force);
    if (wantOn) {
      last_ac_temp_time = millis();
    }
  } else {
    Serial.printf("State update: Re-affirmed state %d without duplicate power toggle\n", newState);
  }
  targetHeatingCoolingState = newState;
  markAcStateDirty();
}

void handleSetTargetState() {
  int newState;
  bool force = server.hasArg("force") && (server.arg("force") == "1");
  if (readStateArg("value", newState)) {
    applyAcState(newState, force);
  }
  server.send(200, "text/plain", "OK");
}

void setFanSpeed(int targetLevel) {
  if (targetLevel < 1 || targetLevel > 3) return;
  int steps = (fan_speed_level - targetLevel + 3) % 3;
  for (int i = 0; i < steps; i++) acPress(AC_CODE_FAN, IR_GAP_AC_STEP);
  fan_speed_level = targetLevel;
  markAcStateDirty();
  Serial.printf("IR: Set Fan Speed to level %d (%d press(es))\n", targetLevel, steps);
}

void setSwing(bool on) {
  if (swing_state == on) return;
  acPress(AC_CODE_SWING, IR_GAP_SINGLE);
  swing_state = on;
  markAcStateDirty();
}

void setEco(bool on) {
  if (eco_state == on) return;
  acPress(AC_CODE_ECO, IR_GAP_SINGLE);
  eco_state = on;
  markAcStateDirty();
}

int timerRemainingPct(bool active, unsigned long startTime, unsigned long durationMs, unsigned long fullScaleMs) {
  if (!active) return 0;
  unsigned long elapsed = millis() - startTime;
  if (elapsed >= durationMs) return 0;
  int pct = (int)lroundf((float)(durationMs - elapsed) * 100.0f / (float)fullScaleMs);
  return pct < 1 ? 1 : pct;
}

uint32_t ledColorCode(int hue, int sat) {
  if (sat < 12) return LED_WHITE;
  if (hue < 15) return LED_RED;
  if (hue < 30) return LED_ORANGE;
  if (hue < 45) return LED_LTORANGE;
  if (hue < 55) return LED_YELLOR;
  if (hue < 65) return LED_YELLOW;
  if (hue < 100) return LED_LTGREEN;
  if (hue < 140) return LED_GREEN;
  if (hue < 170) return LED_DKCYAN;
  if (hue < 200) return LED_CYAN;
  if (hue < 215) return LED_LTBLUE;
  if (hue < 235) return LED_BLUE;
  if (hue < 255) return LED_DKBLUE;
  if (hue < 290) return LED_PURPLE;
  if (hue < 310) return LED_MAGENTA;
  if (hue < 330) return LED_PINK;
  return LED_RED;
}

void updateStripColorHSV() {
  if (!led_power) return;
  last_led_command_time = millis();
  ledPress(ledColorCode(led_hue, led_saturation), IR_GAP_SINGLE);
}

void applyLedModeOrColor() {
  last_led_command_time = millis();
  switch (led_mode_level) {
    case 2: ledPress(LED_FLASH, IR_GAP_SINGLE); break;
    case 3: ledPress(LED_STROBE, IR_GAP_SINGLE); break;
    case 4: ledPress(LED_FADE, IR_GAP_SINGLE); break;
    case 5: ledPress(LED_SMOOTH, IR_GAP_SINGLE); break;
    default: updateStripColorHSV(); break;
  }
}

void handleLEDStatus() {
  char json[96];
  snprintf(json, sizeof(json),
           "{\"power\":%s,\"brightness\":%d,\"hue\":%d,\"saturation\":%d,\"mode\":%d,\"speed\":%d}",
           led_power ? "true" : "false", led_brightness, led_hue,
           led_saturation, led_mode_level, led_speed_level);
  server.send(200, "application/json", json);
}

// --- Wi-Fi helpers ---
const char* wifiReasonName(uint8_t r) {
  switch (r) {
    case 2:   return "AUTH_EXPIRE (AP dropped auth: MAC filter, too close/too strong, or power dip)";
    case 3:   return "AUTH_LEAVE";
    case 4:   return "ASSOC_EXPIRE";
    case 5:   return "ASSOC_TOOMANY (AP client limit reached)";
    case 6:   return "NOT_AUTHED";
    case 7:   return "NOT_ASSOCED";
    case 8:   return "ASSOC_LEAVE";
    case 15:  return "4WAY_HANDSHAKE_TIMEOUT (wrong password)";
    case 200: return "BEACON_TIMEOUT (signal lost)";
    case 201: return "NO_AP_FOUND (SSID not visible)";
    case 202: return "AUTH_FAIL (wrong password / security mode)";
    case 203: return "ASSOC_FAIL";
    case 204: return "HANDSHAKE_TIMEOUT (wrong password)";
    default:  return "OTHER";
  }
}

// Scans the air, prints every network for diagnostic logging
void printAvailableNetworks() {
  Serial.println("[WiFi] Quick scan for diagnostics...");
  int n = WiFi.scanNetworks(false, true);
  for (int i = 0; i < n; i++) {
    Serial.printf("  [%d] %-20s %s ch%2ld %4ld dBm\n",
                  i + 1, WiFi.SSID(i).c_str(), WiFi.BSSIDstr(i).c_str(),
                  (long)WiFi.channel(i), (long)WiFi.RSSI(i));
  }
  WiFi.scanDelete();
}

void setup() {
  Serial.begin(115200);
  delay(1000);  // Allow serial interface and power rail to stabilize
  Serial.println("\n\n=========================================");
  Serial.println("    ESP32 HomeKit Controller Booting     ");
  Serial.println("=========================================");

  // Set all relays HIGH (OFF) before setting pinMode to prevent boot clicks/glitches
  for (size_t i = 0; i < LAMP_COUNT; i++) {
    digitalWrite(lamps[i].pin, HIGH);
    pinMode(lamps[i].pin, OUTPUT);
  }

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Initialize both IR transmitters
  irsendAC.begin();
  irsendLED.begin();

  // Restore persistent AC state
  prefs.begin("homekit", false);
  loadAcState();

  // Wi-Fi Setup
  WiFi.persistent(false);
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.mode(WIFI_STA);
  delay(100);

  // Set lower TX power to prevent receiver saturation with close AP
  WiFi.setTxPower(WIFI_POWER_15dBm);
  // Ensure standard 802.11b/g/n protocols
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  Serial.print("ESP32 MAC: ");
  Serial.println(WiFi.macAddress());

  // Event listener for WiFi connection status
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
      Serial.println("\n[WiFi] Associated to AP successfully.");
    } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
      Serial.print("\n[WiFi] Got IP: ");
      Serial.println(IPAddress(info.got_ip.ip_info.ip.addr));
      WiFi.setSleep(false);
    } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      uint8_t r = info.wifi_sta_disconnected.reason;
      Serial.printf("\n[WiFi] Disconnected! Reason %d: %s\n", r, wifiReasonName(r));
    }
  });

  // Attempt 1: Static IP 192.168.1.112 on Channel 1
  IPAddress local_IP(192, 168, 1, 112);
  IPAddress gateway(192, 168, 1, 1);
  IPAddress subnet(255, 255, 255, 0);
  IPAddress dns(192, 168, 1, 1);
  WiFi.config(local_IP, gateway, subnet, dns);

  // Connect to open network or with password if set
  Serial.printf("[WiFi] Connecting to '%s' (CH1) with static IP 192.168.1.112...\n", ssid);
  if (strlen(password) > 0) {
    WiFi.begin(ssid, password, 1);
  } else {
    WiFi.begin(ssid);
  }

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 25) {
    delay(500);
    Serial.print(".");
    retries++;
  }

  // Attempt 2: If static IP failed to connect, fall back to pure DHCP
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[WiFi] Static IP attempt timed out. Falling back to DHCP...");
    WiFi.disconnect(true, false);
    delay(500);
    WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE);
    if (strlen(password) > 0) {
      WiFi.begin(ssid, password, 1);
    } else {
      WiFi.begin(ssid);
    }

    retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 30) {
      delay(500);
      Serial.print(".");
      retries++;
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    Serial.println("\nWiFi Connected Successfully!");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Gateway: ");
    Serial.println(WiFi.gatewayIP());
    Serial.print("AP BSSID: ");
    Serial.println(WiFi.BSSIDstr());
    Serial.print("RSSI: ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");
  } else {
    Serial.println("\n[WiFi] Connection to Tor failed!");
    printAvailableNetworks();
  }

  // Start mDNS
  if (MDNS.begin("esp32-homekit")) {
    MDNS.addService("http", "tcp", 80);
    Serial.println("mDNS responder started: esp32-homekit.local");
  }

  // --- AC THERMOSTAT COMPATIBLE ROUTES ---
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/targetTemperature", HTTP_GET, handleSetTargetTemp);
  server.on("/targetHeatingCoolingState", HTTP_GET, handleSetTargetState);

  server.on("/set_state_memory", HTTP_GET, []() {
    irFlush();
    float temp;
    int state;
    if (readNumberArg("temp", temp)) targetTemperature = clampAcTemp(temp);
    if (readStateArg("state", state)) targetHeatingCoolingState = state;
    markAcStateDirty();
    server.send(200, "text/plain", "OK");
  });

  server.on("/send_raw_power", HTTP_GET, []() {
    acPowerPress();
    irFlush();
    server.send(200, "text/plain", "OK");
  });

  server.on("/raw_temp_steps", HTTP_GET, []() {
    float requested;
    if (readNumberArg("target", requested)) {
      float target = clampAcTemp(requested);
      queueAcTempSteps((int)lroundf(target - targetTemperature));
      targetTemperature = target;
      markAcStateDirty();
    }
    irFlush();
    server.send(200, "text/plain", "OK");
  });

  server.on("/set_state_raw", HTTP_GET, []() {
    int newState;
    if (readStateArg("state", newState)) applyAcState(newState, true);
    irFlush();
    server.send(200, "text/plain", "OK");
  });

  // --- AC SWING, ECO, MODE, FAN ROUTES ---
  server.on("/ac/swing", HTTP_GET, []() {
    setSwing(!swing_state);
    server.send(200, "text/plain", swing_state ? "1" : "0");
  });
  server.on("/ac/swing/on", HTTP_GET, []() {
    setSwing(true);
    server.send(200, "text/plain", "1");
  });
  server.on("/ac/swing/off", HTTP_GET, []() {
    setSwing(false);
    server.send(200, "text/plain", "0");
  });
  server.on("/ac/swing/status", HTTP_GET, []() {
    server.send(200, "text/plain", swing_state ? "1" : "0");
  });

  server.on("/ac/eco", HTTP_GET, []() {
    setEco(!eco_state);
    server.send(200, "text/plain", eco_state ? "1" : "0");
  });
  server.on("/ac/eco/on", HTTP_GET, []() {
    setEco(true);
    server.send(200, "text/plain", "1");
  });
  server.on("/ac/eco/off", HTTP_GET, []() {
    setEco(false);
    server.send(200, "text/plain", "0");
  });
  server.on("/ac/eco/status", HTTP_GET, []() {
    server.send(200, "text/plain", eco_state ? "1" : "0");
  });

  server.on("/ac/mode", HTTP_GET, []() {
    acPress(AC_CODE_MODE, IR_GAP_SINGLE);
    ac_mode_level = (ac_mode_level % 3) + 1;
    markAcStateDirty();
    server.send(200, "text/plain", String(ac_mode_level));
  });
  server.on("/ac/mode/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(ac_mode_level));
  });

  server.on("/ac/fan", HTTP_GET, []() {
    acPress(AC_CODE_FAN, IR_GAP_SINGLE);
    fan_speed_level = (fan_speed_level == 1) ? 3 : fan_speed_level - 1;
    markAcStateDirty();
    server.send(200, "text/plain", String(fan_speed_level));
  });
  server.on("/ac/fan/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(fan_speed_level));
  });

  server.on("/ac/fan/active", HTTP_GET, []() {
    server.send(200, "text/plain", "1");
  });

  server.on("/ac/fan/speed", HTTP_GET, []() {
    float pct;
    if (readNumberArg("value", pct)) {
      int targetLevel = 1;
      if (pct > 66) targetLevel = 3;
      else if (pct > 33) targetLevel = 2;
      setFanSpeed(targetLevel);
    }
    server.send(200, "text/plain", String(fan_speed_level * 33));
  });
  server.on("/ac/fan/speed/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(fan_speed_level * 33));
  });

  // --- AC Timer Control Endpoints ---
  server.on("/ac/timer", HTTP_GET, []() {
    float pct;
    if (readNumberArg("value", pct)) {
      pct = constrain(pct, 0.0f, 100.0f);
      float hours = roundf(pct * 24.0f / 100.0f * 2.0f) / 2.0f;

      if (hours > 0.0f) {
        timer_duration_ms = (unsigned long)(hours * 3600.0f * 1000.0f);
        timer_start_time = millis();
        timer_active = true;
        Serial.printf("Timer: Set AC shutoff in %.1f hour(s)\n", hours);
      } else {
        timer_active = false;
        Serial.println("Timer: Disabled");
      }
    }
    server.send(200, "text/plain",
                String(timerRemainingPct(timer_active, timer_start_time, timer_duration_ms, AC_TIMER_FULL_SCALE_MS)));
  });
  server.on("/ac/timer/status", HTTP_GET, []() {
    server.send(200, "text/plain",
                String(timerRemainingPct(timer_active, timer_start_time, timer_duration_ms, AC_TIMER_FULL_SCALE_MS)));
  });
  server.on("/ac/timer/on", HTTP_GET, []() {
    timer_duration_ms = 3600UL * 1000UL;  // Default 1 hour
    timer_start_time = millis();
    timer_active = true;
    server.send(200, "text/plain", "1");
  });
  server.on("/ac/timer/off", HTTP_GET, []() {
    timer_active = false;
    server.send(200, "text/plain", "0");
  });

  // --- LED Strip Endpoints ---
  server.on("/led/status", HTTP_GET, handleLEDStatus);

  server.on("/led/on", HTTP_GET, []() {
    led_power = true;
    ledPress(LED_ON, IR_GAP_LED_POWER_ON);
    applyLedModeOrColor();
    server.send(200, "text/plain", "1");
  });

  server.on("/led/off", HTTP_GET, []() {
    led_power = false;
    last_led_command_time = millis();
    ledPress(LED_OFF, IR_GAP_SINGLE);
    server.send(200, "text/plain", "0");
  });

  server.on("/led/hue", HTTP_GET, []() {
    float v;
    if (readNumberArg("value", v)) {
      led_hue = constrain((int)lroundf(v), 0, 360);
      led_mode_level = 1;
      updateStripColorHSV();
    }
    server.send(200, "text/plain", String(led_hue));
  });

  server.on("/led/saturation", HTTP_GET, []() {
    float v;
    if (readNumberArg("value", v)) {
      led_saturation = constrain((int)lroundf(v), 0, 100);
      led_mode_level = 1;
      updateStripColorHSV();
    }
    server.send(200, "text/plain", String(led_saturation));
  });

  server.on("/led/brightness", HTTP_GET, []() {
    float v;
    if (readNumberArg("value", v)) {
      int targetBri = constrain((int)lroundf(v), 0, 100);
      int steps = (targetBri + 5) / 10 - (led_brightness + 5) / 10;
      uint32_t code = (steps > 0) ? LED_BRI_UP : LED_BRI_DWN;
      for (int i = abs(steps); i > 0; i--) ledPress(code, IR_GAP_LED_STEP);
      led_brightness = targetBri;
      last_led_command_time = millis();
    }
    server.send(200, "text/plain", String(led_brightness));
  });

  server.on("/led/mode", HTTP_GET, []() {
    led_mode_level = (led_mode_level % 5) + 1;
    applyLedModeOrColor();
    server.send(200, "text/plain", String(led_mode_level));
  });
  server.on("/led/mode/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(led_mode_level));
  });

  server.on("/led/speed", HTTP_GET, []() {
    led_speed_level = (led_speed_level % 3) + 1;
    if (led_speed_level == 3) {
      for (int i = 0; i < 4; i++) ledPress(LED_BRI_UP, IR_GAP_LED_STEP);
    } else {
      for (int i = 0; i < 4; i++) ledPress(LED_BRI_DWN, IR_GAP_LED_STEP);
      if (led_speed_level == 2) {
        ledPress(LED_BRI_UP, IR_GAP_LED_STEP);
        ledPress(LED_BRI_UP, IR_GAP_LED_STEP);
      }
    }
    server.send(200, "text/plain", String(led_speed_level));
  });
  server.on("/led/speed/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(led_speed_level));
  });

  server.on("/led/timer", HTTP_GET, []() {
    float pct;
    if (readNumberArg("value", pct)) {
      float minutes = constrain(pct, 0.0f, 100.0f) * 1.2f;
      if (minutes > 0.0f) {
        led_timer_duration_ms = (unsigned long)(minutes * 60.0f * 1000.0f);
        led_timer_start_time = millis();
        led_timer_active = true;
        Serial.printf("LED Timer: Set lights shutoff in %.1f minute(s)\n", minutes);
      } else {
        led_timer_active = false;
      }
    }
    server.send(200, "text/plain",
                String(timerRemainingPct(led_timer_active, led_timer_start_time, led_timer_duration_ms, LED_TIMER_FULL_SCALE_MS)));
  });
  server.on("/led/timer/status", HTTP_GET, []() {
    server.send(200, "text/plain",
                String(timerRemainingPct(led_timer_active, led_timer_start_time, led_timer_duration_ms, LED_TIMER_FULL_SCALE_MS)));
  });

  // --- Lamp / Relay Endpoints (Named & Numbered) ---
  server.on("/lamps/status", HTTP_GET, handleLampsStatus);

  for (size_t i = 0; i < LAMP_COUNT; i++) {
    // Named: /lamp/main, /lamp/fairy, /lamp/fan, /lamp/backlight
    String baseNamed = String("/lamp/") + lamps[i].name;
    server.on(baseNamed + "/on", HTTP_GET, [i]() {
      setLamp(i, true);
      server.send(200, "text/plain", "1");
    });
    server.on(baseNamed + "/off", HTTP_GET, [i]() {
      setLamp(i, false);
      server.send(200, "text/plain", "0");
    });
    server.on(baseNamed + "/status", HTTP_GET, [i]() {
      server.send(200, "text/plain", lamps[i].on ? "1" : "0");
    });

    // Numbered: /lamp/1, /lamp/2, /lamp/3, /lamp/4
    String baseNum = String("/lamp/") + String(i + 1);
    server.on(baseNum + "/on", HTTP_GET, [i]() {
      setLamp(i, true);
      server.send(200, "text/plain", "1");
    });
    server.on(baseNum + "/off", HTTP_GET, [i]() {
      setLamp(i, false);
      server.send(200, "text/plain", "0");
    });
    server.on(baseNum + "/status", HTTP_GET, [i]() {
      server.send(200, "text/plain", lamps[i].on ? "1" : "0");
    });
  }

  // --- Arduino OTA Config ---
  ArduinoOTA.setHostname("esp32-homekit");
  ArduinoOTA.begin();

  server.begin();
  Serial.println("HTTP server started. System ready!");
}

void loop() {
  server.handleClient();
  ArduinoOTA.handle();

  irSendNext();

  // Heartbeat logging every 10 seconds
  if (millis() - last_heartbeat_time >= 10000UL) {
    last_heartbeat_time = millis();
    Serial.printf("[Heartbeat] Uptime: %lu s | WiFi RSSI: %d dBm | Free Heap: %u bytes\n",
                  millis() / 1000, WiFi.RSSI(), (unsigned)ESP.getFreeHeap());
  }

  // --- Wi-Fi Reconnect Watchdog ---
  if (millis() - last_wifi_check_time >= WIFI_CHECK_INTERVAL_MS) {
    last_wifi_check_time = millis();
    if (WiFi.status() == WL_CONNECTED) {
      wifi_down = false;
    } else if (!wifi_down) {
      wifi_down = true;
      wifi_down_since = millis();
      Serial.println("WiFi link down -- auto-reconnect active in background.");
    } else if (millis() - wifi_down_since >= WIFI_HARD_REBOOT_TIMEOUT_MS) {
      Serial.println("WiFi down > 3 minutes -- triggering hardware restart!");
      ESP.restart();
    } else if (millis() - wifi_down_since >= WIFI_RECONNECT_GRACE_MS) {
      Serial.println("WiFi still down after grace period -- reconnecting...");
      WiFi.disconnect();
      delay(100);
      if (strlen(password) > 0) {
        WiFi.begin(ssid, password);
      } else {
        WiFi.begin(ssid);
      }
      wifi_down_since = millis();
    }
  }

  // --- AC Shutoff Timer ---
  if (timer_active && (millis() - timer_start_time >= timer_duration_ms)) {
    timer_active = false;
    if (targetHeatingCoolingState != 0) {
      acPowerPress();
      targetHeatingCoolingState = 0;
      markAcStateDirty();
      Serial.println("Timer Expired: Sent IR AC Power Off command");
    }
  }

  // --- LED Sleep Timer ---
  if (led_timer_active && (millis() - led_timer_start_time >= led_timer_duration_ms)) {
    led_timer_active = false;
    if (led_power) {
      led_power = false;
      ledPress(LED_OFF, IR_GAP_SINGLE);
      Serial.println("LED Timer Expired: Sent IR LED Power Off command");
    }
  }

  // --- LED Hourly Keep-Alive Refresh ---
  if (led_power && (millis() - last_led_command_time >= LED_REFRESH_INTERVAL_MS)) {
    last_led_command_time = millis();
    applyLedModeOrColor();
    Serial.println("LED: Hourly refresh command sent.");
  }

  // --- Persist AC state to flash once adjustments settle ---
  if (acStateDirty && millis() - acStateDirtyTime >= AC_STATE_SAVE_DELAY_MS) {
    saveAcState();
  }

  // Yield to FreeRTOS idle task so CPU can enter wait-for-interrupt sleep
  delay(2);
}
