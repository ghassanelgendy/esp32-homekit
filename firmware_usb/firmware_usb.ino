#include <Arduino.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <Preferences.h>

// =========================================================================
//  ESP32 USB Wired Homelab Controller Firmware
//  - Pure USB Serial Communication (Zero Wi-Fi dependencies or drops)
//  - AC IR Control (GPIO 27)
//  - LED Strip IR Control (GPIO 23)
//  - 4-Channel Active-LOW Relays (GPIO 25, 26, 33, 32)
//  - Status LED (GPIO 2)
// =========================================================================

// --- Pin Definitions ---
const uint16_t kIrAcPin  = 27;  // AC IR transmitter
const uint16_t kIrLedPin = 23;  // LED Strip IR transmitter
const int LED_PIN        = 2;   // Onboard indicator LED

IRsend irsendAC(kIrAcPin);
IRsend irsendLED(kIrLedPin);
Preferences prefs;

// --- Relay / Lamp Setup ---
// Relays are active-LOW: LOW = ON, HIGH = OFF
struct Lamp {
  const char* name;
  uint8_t pin;
  bool on;
};

Lamp lamps[] = {
  {"main", 25, false},       // Relay 1 - Main Light
  {"fairy", 26, false},      // Relay 2 - Fairy Light
  {"fan", 33, false},        // Relay 3 - Fan
  {"backlight", 32, false}   // Relay 4 - Backlight
};
const size_t LAMP_COUNT = sizeof(lamps) / sizeof(lamps[0]);

// --- AC IR Remote Codes (NEC Protocol) ---
#define AC_CODE_POWER     0x80FF48B7
#define AC_CODE_TEMP_UP   0x80FF58A7
#define AC_CODE_TEMP_DOWN 0x80FFC837
#define AC_CODE_MODE      0x80FF40BF
#define AC_CODE_FAN       0x80FF708F
#define AC_CODE_SWING     0x80FFF00F
#define AC_CODE_ECO       0x80FF50AF

// --- LED Strip Remote Codes (NEC Protocol) ---
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

#define LED_FLASH    0xF7D02F
#define LED_STROBE   0xF7F00F
#define LED_FADE     0xF7C837
#define LED_SMOOTH   0xF7E817

// --- AC State Variables ---
const float AC_MIN_TEMP = 16.0f;
const float AC_MAX_TEMP = 34.0f;
float targetTemperature = 25.0f;
int targetHeatingCoolingState = 0; // 0=OFF, 1=HEAT, 2=COOL, 3=AUTO
int fan_speed_level = 3;          // 1=Low, 2=Medium, 3=High
int ac_mode_level = 1;            // 1=Cool, 2=Dry, 3=Auto
bool swing_state = false;
bool eco_state = false;

unsigned long last_ac_temp_time = 0;
const unsigned long AC_WAKEUP_TIMEOUT = 20000UL;

// AC Timer variables
bool timer_active = false;
unsigned long timer_duration_ms = 0;
unsigned long timer_start_time = 0;
const unsigned long AC_TIMER_FULL_SCALE_MS = 24UL * 60UL * 60UL * 1000UL; // 24 hours

// --- LED State Variables ---
bool led_power = false;
int led_brightness = 100;
int led_hue = 0;
int led_saturation = 0;
int led_speed_level = 1;
int led_mode_level = 1; // 1=Solid, 2=Flash, 3=Strobe, 4=Fade, 5=Smooth

bool led_timer_active = false;
unsigned long led_timer_duration_ms = 0;
unsigned long led_timer_start_time = 0;
const unsigned long LED_TIMER_FULL_SCALE_MS = 120UL * 60UL * 1000UL; // 120 minutes

unsigned long last_led_command_time = 0;
const unsigned long LED_REFRESH_INTERVAL_MS = 3600000UL; // 1 hour

// --- AC NVS Persistence ---
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
  if (snap.temp == snap.temp && snap.temp >= AC_MIN_TEMP && snap.temp <= AC_MAX_TEMP) {
    targetTemperature = roundf(snap.temp);
  }
  if (snap.state <= 3) targetHeatingCoolingState = snap.state;
  if (snap.fan >= 1 && snap.fan <= 3) fan_speed_level = snap.fan;
  if (snap.mode >= 1 && snap.mode <= 3) ac_mode_level = snap.mode;
  swing_state = (snap.swing != 0);
  eco_state = (snap.eco != 0);
}

// Forward declaration
void setLamp(size_t i, bool on);

void saveLampState() {
  uint8_t mask = 0;
  for (size_t i = 0; i < LAMP_COUNT; i++) {
    if (lamps[i].on) mask |= (1 << i);
  }
  prefs.putUChar("lamps", mask);
}

void loadLampState() {
  uint8_t mask = prefs.getUChar("lamps", 0);
  for (size_t i = 0; i < LAMP_COUNT; i++) {
    bool state = ((mask & (1 << i)) != 0);
    setLamp(i, state);
  }
}

// --- Non-blocking IR Transmit Queue ---
const uint8_t IR_TARGET_AC  = 0;
const uint8_t IR_TARGET_LED = 1;

const uint16_t IR_GAP_SINGLE       = 100;
const uint16_t IR_GAP_AC_STEP      = 250;
const uint16_t IR_GAP_LED_STEP     = 150;
const uint16_t IR_GAP_LED_POWER_ON = 300;

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
  if (irCount >= IR_QUEUE_SIZE) {
    irSendNext();
  }
  if (irCount < IR_QUEUE_SIZE) {
    IrJob& slot = irQueue[(irHead + irCount) % IR_QUEUE_SIZE];
    slot.code = code;
    slot.gapMs = gapMs;
    slot.target = target;
    slot.repeat = repeat;
    irCount++;
    if (isAcTempPress(target, code)) irPendingAcTempPresses++;
  }
}

void irFlush() {
  // Non-blocking flush: try to send the next packet if ready
  irSendNext();
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

float clampAcTemp(float t) {
  return constrain(roundf(t), AC_MIN_TEMP, AC_MAX_TEMP);
}

// --- Relay / Lamp Helpers ---
void setLamp(size_t i, bool on) {
  if (i >= LAMP_COUNT) return;
  digitalWrite(lamps[i].pin, on ? LOW : HIGH);
  lamps[i].on = on;
}

int findLampIndex(const char* name) {
  for (size_t i = 0; i < LAMP_COUNT; i++) {
    if (strcmp(lamps[i].name, name) == 0) return (int)i;
  }
  if (name[0] >= '1' && name[0] <= '4' && name[1] == '\0') {
    return name[0] - '1';
  }
  return -1;
}

// --- AC Control Helpers ---
void setFanSpeed(int targetLevel) {
  if (targetLevel < 1 || targetLevel > 3) return;
  int steps = (fan_speed_level - targetLevel + 3) % 3;
  for (int i = 0; i < steps; i++) acPress(AC_CODE_FAN, IR_GAP_AC_STEP);
  fan_speed_level = targetLevel;
  markAcStateDirty();
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

void setAcTargetTemp(float requested) {
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

int timerRemainingPct(bool active, unsigned long startTime, unsigned long durationMs, unsigned long fullScaleMs) {
  if (!active) return 0;
  unsigned long elapsed = millis() - startTime;
  if (elapsed >= durationMs) return 0;
  int pct = (int)lroundf((float)(durationMs - elapsed) * 100.0f / (float)fullScaleMs);
  return pct < 1 ? 1 : pct;
}

// --- LED Control Helpers ---
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

// =========================================================================
//  SERIAL COMMAND PROCESSOR
// =========================================================================

void handleSerialCommand(char* cmd) {
  // Strip trailing whitespace / cr / lf
  int len = strlen(cmd);
  while (len > 0 && (cmd[len - 1] == '\r' || cmd[len - 1] == '\n' || cmd[len - 1] == ' ')) {
    cmd[--len] = '\0';
  }
  if (len == 0) return;

  // 1. PING -> PONG
  if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
    return;
  }

  // 2. STATUS queries
  if (strcmp(cmd, "STATUS") == 0) {
    Serial.printf("{\"targetTemperature\":%.1f,\"currentTemperature\":%.1f,"
                  "\"targetHeatingCoolingState\":%d,\"currentHeatingCoolingState\":%d}\n",
                  targetTemperature, targetTemperature,
                  targetHeatingCoolingState, targetHeatingCoolingState);
    return;
  }

  if (strcmp(cmd, "LAMPS") == 0 || strcmp(cmd, "LAMPS_STATUS") == 0) {
    Serial.printf("{\"main\":%s,\"fairy\":%s,\"fan\":%s,\"backlight\":%s}\n",
                  lamps[0].on ? "true" : "false",
                  lamps[1].on ? "true" : "false",
                  lamps[2].on ? "true" : "false",
                  lamps[3].on ? "true" : "false");
    return;
  }

  if (strcmp(cmd, "LED") == 0 || strcmp(cmd, "LED_STATUS") == 0) {
    Serial.printf("{\"power\":%s,\"brightness\":%d,\"hue\":%d,\"saturation\":%d,\"mode\":%d,\"speed\":%d}\n",
                  led_power ? "true" : "false", led_brightness, led_hue,
                  led_saturation, led_mode_level, led_speed_level);
    return;
  }

  // GET specific values
  if (strcmp(cmd, "GET ac_swing") == 0) {
    Serial.println(swing_state ? "1" : "0");
    return;
  }
  if (strcmp(cmd, "GET ac_eco") == 0) {
    Serial.println(eco_state ? "1" : "0");
    return;
  }
  if (strcmp(cmd, "GET ac_mode") == 0) {
    Serial.println(ac_mode_level);
    return;
  }
  if (strcmp(cmd, "GET ac_fan") == 0) {
    Serial.println(fan_speed_level);
    return;
  }
  if (strcmp(cmd, "GET ac_fan_speed") == 0) {
    Serial.println(fan_speed_level * 33);
    return;
  }
  if (strcmp(cmd, "GET ac_timer") == 0) {
    Serial.println(timerRemainingPct(timer_active, timer_start_time, timer_duration_ms, AC_TIMER_FULL_SCALE_MS));
    return;
  }
  if (strcmp(cmd, "GET led_mode") == 0) {
    Serial.println(led_mode_level);
    return;
  }
  if (strcmp(cmd, "GET led_speed") == 0) {
    Serial.println(led_speed_level);
    return;
  }
  if (strcmp(cmd, "GET led_timer") == 0) {
    Serial.println(timerRemainingPct(led_timer_active, led_timer_start_time, led_timer_duration_ms, LED_TIMER_FULL_SCALE_MS));
    return;
  }
  if (strncmp(cmd, "GET lamp ", 9) == 0) {
    int idx = findLampIndex(cmd + 9);
    if (idx >= 0) {
      Serial.println(lamps[idx].on ? "1" : "0");
    } else {
      Serial.println("ERR_UNKNOWN_LAMP");
    }
    return;
  }

  // 3. SET AC Commands
  if (strncmp(cmd, "SET ac_temp ", 12) == 0) {
    float val = atof(cmd + 12);
    setAcTargetTemp(val);
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_state ", 13) == 0) {
    char* arg1 = cmd + 13;
    char* space = strchr(arg1, ' ');
    bool force = false;
    if (space) {
      *space = '\0';
      force = (atoi(space + 1) == 1);
    }
    int state = atoi(arg1);
    applyAcState(state, force);
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_swing", 12) == 0) {
    if (cmd[12] == ' ') {
      char* arg = cmd + 13;
      if (strcmp(arg, "1") == 0 || strcmp(arg, "on") == 0) setSwing(true);
      else if (strcmp(arg, "0") == 0 || strcmp(arg, "off") == 0) setSwing(false);
      else setSwing(!swing_state);
    } else {
      setSwing(!swing_state);
    }
    Serial.println(swing_state ? "1" : "0");
    return;
  }

  if (strncmp(cmd, "SET ac_eco", 10) == 0) {
    if (cmd[10] == ' ') {
      char* arg = cmd + 11;
      if (strcmp(arg, "1") == 0 || strcmp(arg, "on") == 0) setEco(true);
      else if (strcmp(arg, "0") == 0 || strcmp(arg, "off") == 0) setEco(false);
      else setEco(!eco_state);
    } else {
      setEco(!eco_state);
    }
    Serial.println(eco_state ? "1" : "0");
    return;
  }

  if (strncmp(cmd, "SET ac_mode", 11) == 0) {
    acPress(AC_CODE_MODE, IR_GAP_SINGLE);
    ac_mode_level = (ac_mode_level % 3) + 1;
    markAcStateDirty();
    Serial.println(ac_mode_level);
    return;
  }

  if (strncmp(cmd, "SET ac_fan", 10) == 0) {
    if (cmd[10] == ' ') {
      int target = atoi(cmd + 11);
      if (target >= 1 && target <= 3) {
        setFanSpeed(target);
      } else {
        acPress(AC_CODE_FAN, IR_GAP_SINGLE);
        fan_speed_level = (fan_speed_level == 1) ? 3 : fan_speed_level - 1;
        markAcStateDirty();
      }
    } else {
      acPress(AC_CODE_FAN, IR_GAP_SINGLE);
      fan_speed_level = (fan_speed_level == 1) ? 3 : fan_speed_level - 1;
      markAcStateDirty();
    }
    Serial.println(fan_speed_level);
    return;
  }

  if (strncmp(cmd, "SET ac_fan_speed ", 17) == 0) {
    float pct = atof(cmd + 17);
    int target = 1;
    if (pct > 66) target = 3;
    else if (pct > 33) target = 2;
    setFanSpeed(target);
    Serial.println(fan_speed_level * 33);
    return;
  }

  if (strcmp(cmd, "SET ac_raw_power") == 0) {
    acPowerPress();
    irFlush();
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_raw_temp_steps ", 22) == 0) {
    float target = clampAcTemp(atof(cmd + 22));
    queueAcTempSteps((int)lroundf(target - targetTemperature));
    targetTemperature = target;
    markAcStateDirty();
    irFlush();
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_state_raw ", 17) == 0) {
    int newState = atoi(cmd + 17);
    applyAcState(newState, true);
    irFlush();
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_state_mem ", 17) == 0) {
    char* arg1 = cmd + 17;
    char* space = strchr(arg1, ' ');
    if (space) {
      *space = '\0';
      targetTemperature = clampAcTemp(atof(arg1));
      targetHeatingCoolingState = constrain(atoi(space + 1), 0, 3);
      markAcStateDirty();
    }
    Serial.println("OK");
    return;
  }

  if (strncmp(cmd, "SET ac_timer ", 13) == 0) {
    float pct = constrain(atof(cmd + 13), 0.0f, 100.0f);
    float hours = roundf(pct * 24.0f / 100.0f * 2.0f) / 2.0f;
    if (hours > 0.0f) {
      timer_duration_ms = (unsigned long)(hours * 3600.0f * 1000.0f);
      timer_start_time = millis();
      timer_active = true;
    } else {
      timer_active = false;
    }
    Serial.println(timerRemainingPct(timer_active, timer_start_time, timer_duration_ms, AC_TIMER_FULL_SCALE_MS));
    return;
  }

  if (strcmp(cmd, "SET ac_timer_on") == 0) {
    timer_duration_ms = 3600UL * 1000UL; // 1 hour
    timer_start_time = millis();
    timer_active = true;
    Serial.println("1");
    return;
  }

  if (strcmp(cmd, "SET ac_timer_off") == 0) {
    timer_active = false;
    Serial.println("0");
    return;
  }

  // 4. SET Lamp Commands: "SET lamp <name|id> <1|0|on|off>"
  if (strncmp(cmd, "SET lamp ", 9) == 0) {
    char* lampName = cmd + 9;
    char* space = strchr(lampName, ' ');
    if (space) {
      *space = '\0';
      char* stateStr = space + 1;
      int idx = findLampIndex(lampName);
      if (idx >= 0) {
        bool turnOn = (strcmp(stateStr, "1") == 0 || strcmp(stateStr, "on") == 0);
        setLamp(idx, turnOn);
        saveLampState();
        Serial.println(turnOn ? "1" : "0");
        return;
      }
    }
    Serial.println("ERR_LAMP");
    return;
  }

  // 5. SET LED Commands
  if (strncmp(cmd, "SET led_power ", 14) == 0) {
    char* arg = cmd + 14;
    bool on = (strcmp(arg, "1") == 0 || strcmp(arg, "on") == 0);
    led_power = on;
    if (on) {
      ledPress(LED_ON, IR_GAP_LED_POWER_ON);
      applyLedModeOrColor();
      Serial.println("1");
    } else {
      last_led_command_time = millis();
      ledPress(LED_OFF, IR_GAP_SINGLE);
      Serial.println("0");
    }
    return;
  }

  if (strncmp(cmd, "SET led_hue ", 12) == 0) {
    led_hue = constrain(atoi(cmd + 12), 0, 360);
    led_mode_level = 1;
    updateStripColorHSV();
    Serial.println(led_hue);
    return;
  }

  if (strncmp(cmd, "SET led_sat ", 12) == 0) {
    led_saturation = constrain(atoi(cmd + 12), 0, 100);
    led_mode_level = 1;
    updateStripColorHSV();
    Serial.println(led_saturation);
    return;
  }

  if (strncmp(cmd, "SET led_bri ", 12) == 0) {
    int targetBri = constrain(atoi(cmd + 12), 0, 100);
    int steps = (targetBri + 5) / 10 - (led_brightness + 5) / 10;
    uint32_t code = (steps > 0) ? LED_BRI_UP : LED_BRI_DWN;
    for (int i = abs(steps); i > 0; i--) ledPress(code, IR_GAP_LED_STEP);
    led_brightness = targetBri;
    last_led_command_time = millis();
    Serial.println(led_brightness);
    return;
  }

  if (strncmp(cmd, "SET led_mode", 12) == 0) {
    if (cmd[12] == ' ') {
      int m = atoi(cmd + 13);
      if (m >= 1 && m <= 5) led_mode_level = m;
      else led_mode_level = (led_mode_level % 5) + 1;
    } else {
      led_mode_level = (led_mode_level % 5) + 1;
    }
    applyLedModeOrColor();
    Serial.println(led_mode_level);
    return;
  }

  if (strncmp(cmd, "SET led_speed", 13) == 0) {
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
    Serial.println(led_speed_level);
    return;
  }

  if (strncmp(cmd, "SET led_timer ", 14) == 0) {
    float pct = constrain(atof(cmd + 14), 0.0f, 100.0f);
    float minutes = pct * 1.2f;
    if (minutes > 0.0f) {
      led_timer_duration_ms = (unsigned long)(minutes * 60.0f * 1000.0f);
      led_timer_start_time = millis();
      led_timer_active = true;
    } else {
      led_timer_active = false;
    }
    Serial.println(timerRemainingPct(led_timer_active, led_timer_start_time, led_timer_duration_ms, LED_TIMER_FULL_SCALE_MS));
    return;
  }

  // Fallback
  Serial.println("ERR_UNKNOWN_CMD");
}

// =========================================================================
//  SETUP & MAIN LOOP
// =========================================================================

void setup() {
  Serial.begin(115200);
  delay(100);

  // Set all relays HIGH (OFF) before setting pinMode to prevent boot clicks
  for (size_t i = 0; i < LAMP_COUNT; i++) {
    digitalWrite(lamps[i].pin, HIGH);
    pinMode(lamps[i].pin, OUTPUT);
  }

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Initialize both IR transmitters
  irsendAC.begin();
  irsendLED.begin();

  // Restore persistent state from NVS flash
  prefs.begin("homekit", false);
  loadAcState();
  loadLampState();

  Serial.println("\n[ESP32] Homelab USB Controller Ready. Baud: 115200");
}

char serialBuffer[128];
uint8_t serialBufferIdx = 0;

void loop() {
  // 1. Process serial commands non-blockingly
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBufferIdx > 0) {
        serialBuffer[serialBufferIdx] = '\0';
        handleSerialCommand(serialBuffer);
        serialBufferIdx = 0;
      }
    } else if (serialBufferIdx < sizeof(serialBuffer) - 1) {
      serialBuffer[serialBufferIdx++] = c;
    }
  }

  // 2. Dispatch non-blocking queued IR signals
  irSendNext();

  // 3. AC Shutoff Timer
  if (timer_active && (millis() - timer_start_time >= timer_duration_ms)) {
    timer_active = false;
    if (targetHeatingCoolingState != 0) {
      acPowerPress();
      targetHeatingCoolingState = 0;
      markAcStateDirty();
      Serial.println("[EVENT] AC timer expired: Power Off");
    }
  }

  // 4. LED Sleep Timer
  if (led_timer_active && (millis() - led_timer_start_time >= led_timer_duration_ms)) {
    led_timer_active = false;
    if (led_power) {
      led_power = false;
      ledPress(LED_OFF, IR_GAP_SINGLE);
      Serial.println("[EVENT] LED timer expired: Power Off");
    }
  }

  // 5. LED Hourly Keep-Alive Refresh
  if (led_power && (millis() - last_led_command_time >= LED_REFRESH_INTERVAL_MS)) {
    last_led_command_time = millis();
    applyLedModeOrColor();
  }

  // 6. Persist AC state to flash once adjustments settle
  if (acStateDirty && millis() - acStateDirtyTime >= AC_STATE_SAVE_DELAY_MS) {
    saveAcState();
  }

  delay(2);
}
