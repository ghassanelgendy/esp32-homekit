#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <Preferences.h>

// --- Wi-Fi Credentials ---
const char* ssid = "Tor";
const char* password = "33120231t";

const uint16_t kIrAcPin = 27;
const uint16_t kIrLedPin = 23;

struct LampPin {
  uint8_t pin;
  bool state;
};

LampPin lamps[] = {
  {25, false},
  {26, false},
  {33, false},
  {32, false}
};

const size_t LAMP_COUNT = sizeof(lamps) / sizeof(lamps[0]);

const uint32_t AC_CODE_POWER     = 0x807FA05F;
const uint32_t AC_CODE_TEMP_UP   = 0x807F609F;
const uint32_t AC_CODE_TEMP_DOWN = 0x807FE01F;
const uint32_t AC_CODE_MODE      = 0x807F906F;
const uint32_t AC_CODE_FAN       = 0x807FB04F;
const uint32_t AC_CODE_SWING     = 0x807FA857;
const uint32_t AC_CODE_ECO       = 0x807F28D7;

const uint32_t LED_ON            = 0xF7C03F;
const uint32_t LED_OFF           = 0xF740BF;

IRsend irsendAC(kIrAcPin);
IRsend irsendLED(kIrLedPin);
WebServer server(80);
Preferences prefs;

int targetHeatingCoolingState = 0;
float targetTemperature = 24.0;
const float AC_MIN_TEMP = 16.0;
const float AC_MAX_TEMP = 31.0;
int fan_speed_level = 1;
int dry_prev_fan_speed = 1;
int ac_mode_level = 1;
bool swing_state = false;
bool eco_state = false;

unsigned long last_ac_temp_time = 0;
const unsigned long AC_WAKEUP_TIMEOUT = 20000;

bool timer_active = false;
unsigned long timer_start_time = 0;
unsigned long timer_duration_ms = 0;

bool led_power = false;
bool led_timer_active = false;
unsigned long led_timer_start_time = 0;
unsigned long led_timer_duration_ms = 0;
unsigned long last_led_command_time = 0;
const unsigned long LED_REFRESH_INTERVAL_MS = 3600000;

uint32_t last_led_color = 0;
uint32_t last_led_mode = 0;

bool acStateDirty = false;
unsigned long acStateDirtyTime = 0;
const unsigned long AC_STATE_SAVE_DELAY_MS = 2000;

struct AcSnapshot {
  float temp;
  uint8_t state;
  uint8_t fan;
  uint8_t mode;
  uint8_t swing;
  uint8_t eco;
};

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
  return (target == 0) && (code == AC_CODE_TEMP_UP || code == AC_CODE_TEMP_DOWN);
}

bool irSendNext() {
  if (irCount == 0 || millis() - irLastSendTime < irGapMs) return false;

  const IrJob job = irQueue[irHead];
  irHead = (irHead + 1) % IR_QUEUE_SIZE;
  irCount--;

  digitalWrite(2, HIGH);
  if (job.target == 0) {
    irsendAC.sendNEC(job.code, 32, job.repeat);
  } else {
    irsendLED.sendNEC(job.code, 32, job.repeat);
  }
  digitalWrite(2, LOW);

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
  irEnqueue(0, code, 0, gapMs);
}

void ledPress(uint32_t code, uint16_t gapMs = IR_GAP_SINGLE) {
  irEnqueue(1, code, 0, gapMs);
}

void acPowerPress() {
  irEnqueue(0, AC_CODE_POWER, 1, IR_GAP_SINGLE);
}

void queueAcTempSteps(int steps) {
  uint32_t code = (steps > 0) ? AC_CODE_TEMP_UP : AC_CODE_TEMP_DOWN;
  for (int i = abs(steps); i > 0; i--) acPress(code, IR_GAP_AC_STEP);
}

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

void setLamp(size_t i, bool on) {
  if (i >= LAMP_COUNT) return;
  digitalWrite(lamps[i].pin, on ? LOW : HIGH);
  lamps[i].state = on;
}

void handleLampsStatus() {
  char json[72];
  snprintf(json, sizeof(json),
           "{\"main\":%s,\"fairy\":%s,\"fan\":%s,\"backlight\":%s}",
           lamps[0].state ? "true" : "false",
           lamps[1].state ? "true" : "false",
           lamps[2].state ? "true" : "false",
           lamps[3].state ? "true" : "false");
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
    if (wantOn) {
      last_ac_temp_time = millis();
    }
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
}

void setAcMode(int targetMode) {
  if (targetMode < 1 || targetMode > 3) return;
  int steps = (targetMode - ac_mode_level + 3) % 3;
  for (int i = 0; i < steps; i++) acPress(AC_CODE_MODE, IR_GAP_AC_STEP);
  ac_mode_level = targetMode;
  markAcStateDirty();
}

void toggleSwing() {
  acPress(AC_CODE_SWING, IR_GAP_SINGLE);
  swing_state = !swing_state;
  markAcStateDirty();
}

void toggleEco() {
  acPress(AC_CODE_ECO, IR_GAP_SINGLE);
  eco_state = !eco_state;
  markAcStateDirty();
}

void applyLedModeOrColor() {
  if (last_led_mode != 0) {
    ledPress(last_led_mode);
  } else if (last_led_color != 0) {
    ledPress(last_led_color);
  }
}

void setLedPower(bool on) {
  if (on && !led_power) {
    ledPress(LED_ON, IR_GAP_LED_POWER_ON);
    led_power = true;
    applyLedModeOrColor();
  } else if (!on && led_power) {
    ledPress(LED_OFF, IR_GAP_SINGLE);
    led_power = false;
  }
}

void handleACAction() {
  if (server.hasArg("fan")) {
    setFanSpeed(server.arg("fan").toInt());
  } else if (server.hasArg("mode")) {
    setAcMode(server.arg("mode").toInt());
  } else if (server.hasArg("swing")) {
    toggleSwing();
  } else if (server.hasArg("eco")) {
    toggleEco();
  } else if (server.hasArg("timer")) {
    int minutes = server.arg("timer").toInt();
    if (minutes > 0) {
      timer_duration_ms = (unsigned long)minutes * 60 * 1000;
      timer_start_time = millis();
      timer_active = true;
    } else {
      timer_active = false;
    }
  }
  server.send(200, "text/plain", "OK");
}

void handleLedAction() {
  if (server.hasArg("power")) {
    setLedPower(server.arg("power").toInt() == 1);
  } else if (server.hasArg("code")) {
    uint32_t code = strtoul(server.arg("code").c_str(), NULL, 16);
    if (!led_power) {
      setLedPower(true);
    }
    ledPress(code);
  } else if (server.hasArg("timer")) {
    int minutes = server.arg("timer").toInt();
    if (minutes > 0) {
      led_timer_duration_ms = (unsigned long)minutes * 60 * 1000;
      led_timer_start_time = millis();
      led_timer_active = true;
    } else {
      led_timer_active = false;
    }
  }
  server.send(200, "text/plain", "OK");
}

void handleLedStatus() {
  char json[96];
  snprintf(json, sizeof(json),
           "{\"power\":%s,\"timerActive\":%s,\"timerRemaining\":%lu}",
           led_power ? "true" : "false",
           led_timer_active ? "true" : "false",
           led_timer_active ? (led_timer_duration_ms - (millis() - led_timer_start_time)) / 1000 : 0);
  server.send(200, "application/json", json);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n=========================================");
  Serial.println("    ESP32 HomeKit Controller Booting     ");
  Serial.println("=========================================");

  for (size_t i = 0; i < LAMP_COUNT; i++) {
    digitalWrite(lamps[i].pin, HIGH);
    pinMode(lamps[i].pin, OUTPUT);
  }

  pinMode(2, OUTPUT);
  digitalWrite(2, LOW);

  irsendAC.begin();
  irsendLED.begin();

  prefs.begin("homekit", false);
  loadAcState();

  // Reset WiFi stack & NVS
  WiFi.disconnect(true, true);
  delay(500);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_17dBm);

  Serial.print("ESP32 MAC: ");
  Serial.println(WiFi.macAddress());

  // Event listener for WiFi connection status
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
      Serial.println("\n[WiFi] Associated to AP successfully.");
    } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
      Serial.print("\n[WiFi] Got IP: ");
      Serial.println(IPAddress(info.got_ip.ip_info.ip.addr));
    } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      uint8_t r = info.wifi_sta_disconnected.reason;
      Serial.printf("\n[WiFi] Disconnected! Reason %d: %s\n", r, wifiReasonName(r));
    }
  });

  Serial.printf("[WiFi] Connecting to '%s'...\n", ssid);
  WiFi.begin(ssid, password);

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 40) {
    delay(500);
    Serial.print(".");
    retries++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    Serial.println("\n[WiFi] Connected Successfully!");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Gateway: ");
    Serial.println(WiFi.gatewayIP());
    Serial.print("RSSI: ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");
  } else {
    Serial.println("\n[WiFi] Connection failed!");
  }

  // Start mDNS
  if (MDNS.begin("esp32-homekit")) {
    MDNS.addService("http", "tcp", 80);
    Serial.println("mDNS responder started: esp32-homekit.local");
  }

  server.on("/status", HTTP_GET, handleStatus);
  server.on("/target-temperature", HTTP_GET, handleSetTargetTemp);
  server.on("/target-state", HTTP_GET, handleSetTargetState);
  server.on("/ac", HTTP_GET, handleACAction);
  server.on("/led", HTTP_GET, handleLedAction);
  server.on("/led/status", HTTP_GET, handleLedStatus);
  server.on("/relays", HTTP_GET, handleLampsStatus);

  for (size_t i = 0; i < LAMP_COUNT; i++) {
    server.on(String("/lamp" + String(i + 1)).c_str(), HTTP_GET, [i]() {
      if (server.hasArg("state")) {
        setLamp(i, server.arg("state") == "1");
      }
      handleLampsStatus();
    });
  }

  server.begin();
  Serial.println("HTTP server started. System ready!");
}

unsigned long last_wifi_check = 0;

void loop() {
  server.handleClient();
  irSendNext();

  if (millis() - last_wifi_check > 10000) {
    last_wifi_check = millis();
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[Heartbeat] Uptime: %lu s | WiFi RSSI: %d dBm | Free Heap: %d bytes\n",
                    millis() / 1000, WiFi.RSSI(), ESP.getFreeHeap());
    } else {
      Serial.println("[WiFi] Reconnecting...");
      esp_wifi_connect();
    }
  }

  if (acStateDirty && (millis() - acStateDirtyTime >= AC_STATE_SAVE_DELAY_MS)) {
    saveAcState();
  }

  if (timer_active && (millis() - timer_start_time >= timer_duration_ms)) {
    timer_active = false;
    if (targetHeatingCoolingState != 0) {
      applyAcState(0);
    }
  }

  if (led_timer_active && (millis() - led_timer_start_time >= led_timer_duration_ms)) {
    led_timer_active = false;
    setLedPower(false);
  }

  if (led_power && (millis() - last_led_command_time >= LED_REFRESH_INTERVAL_MS)) {
    last_led_command_time = millis();
    applyLedModeOrColor();
  }
}
