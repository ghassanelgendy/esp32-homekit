#include <WiFi.h>
#include <WebServer.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <ArduinoOTA.h>

#include "secrets.h"
const char* ssid = SECRET_SSID;
const char* password = SECRET_PASSWORD;

// --- Dual IR Setup ---
const uint16_t kIrAcPin = 27;  // AC IR transmitter on GPIO 27
const uint16_t kIrLedPin = 4;  // LED Strip IR transmitter on GPIO 4

IRsend irsendAC(kIrAcPin);
IRsend irsendLED(kIrLedPin);

const int LED_PIN = 2; // Onboard status indicator

WebServer server(80);


// --- AC state tracking variables ---
float targetTemperature = 25.0; // Tracked as float but rounded to whole number
float currentTemperature = 25.0;
int targetHeatingCoolingState = 0; // HomeKit: 0=OFF, 1=HEAT, 2=COOL, 3=AUTO

#define AC_CODE_POWER      0x80FF48B7
#define AC_CODE_TEMP_UP    0x80FF58A7
#define AC_CODE_TEMP_DOWN  0x80FFC837
#define AC_CODE_MODE       0x80FF40BF
#define AC_CODE_FAN        0x80FF708F
#define AC_CODE_SWING      0x80FFF00F
#define AC_CODE_ECO        0x80FF50AF

// --- LED Strip control variables ---
bool led_power = false;
int led_brightness = 100;
int led_hue = 0;
int led_saturation = 0;
int led_speed_level = 1; // 1 = Low (slow), 2 = Medium, 3 = High (fast)

// Scanned IR code dictionary matching your strip remote buttons
#define LED_ON        0xF7C03F
#define LED_OFF       0xF740BF
#define LED_BRI_UP    0xF700FF
#define LED_BRI_DWN   0xF7807F

#define LED_RED       0xF720DF
#define LED_GREEN     0xF7A05F
#define LED_BLUE      0xF7609F
#define LED_WHITE     0xF7E01F

#define LED_ORANGE    0xF710EF
#define LED_LTGREEN   0xF7906F
#define LED_LTBLUE    0xF750AF

#define LED_LTORANGE  0xF730CF
#define LED_CYAN      0xF7B04F
#define LED_PURPLE    0xF7708F

#define LED_YELLOR    0xF708F7
#define LED_DKCYAN    0xF78877
#define LED_MAGENTA   0xF748B7

#define LED_YELLOW    0xF728D7
#define LED_DKBLUE    0xF7A857
#define LED_PINK      0xF76897

// LED Modes IR Codes
#define LED_FLASH     0xF7D02F
#define LED_STROBE    0xF7F00F
#define LED_FADE      0xF7C837
#define LED_SMOOTH    0xF7E817

// --- Virtual state toggles to make switches stateful ---
bool swing_state = false;
bool eco_state = false;
int fan_speed_level = 1; // 1 = Low, 2 = Medium, 3 = High
int ac_mode_level = 1;   // 1 = Cool, 2 = Dry, 3 = Auto
int led_mode_level = 1;  // 1 = Default (Solid/Color control), 2 = Flash, 3 = Strobe, 4 = Fade, 5 = Smooth


// --- Timer variables ---
bool timer_active = false;
unsigned long timer_duration_ms = 0;
unsigned long timer_start_time = 0;

// LED Strip Timer variables
bool led_timer_active = false;
unsigned long led_timer_duration_ms = 0;
unsigned long led_timer_start_time = 0;

// Tracks the last time we interacted with the LED strip
unsigned long last_led_command_time = 0;
const unsigned long LED_REFRESH_INTERVAL_MS = 3600000UL; // 1 hour in milliseconds


// --- Dedicated Transmit Functions ---
void sendAcIR(uint32_t hexCode) {
  digitalWrite(LED_PIN, HIGH);
  irsendAC.sendNEC(hexCode, 32);
  delay(100);
  digitalWrite(LED_PIN, LOW);
}

// The AC remote has a single toggle code for power (no discrete on/off), and with
// no feedback path (no IR echo, no current sensor) a dropped blast permanently
// desyncs targetHeatingCoolingState from the physical unit until someone notices.
// Sending one extra NEC repeat frame after the base code improves the odds the AC
// actually receives it, without risking a double-toggle: a NEC repeat frame is a
// distinct, shorter signal that compliant receivers treat as "still holding the
// same button," not as a second independent press.
void sendAcPower(uint32_t hexCode) {
  digitalWrite(LED_PIN, HIGH);
  irsendAC.sendNEC(hexCode, 32, 1);
  delay(100);
  digitalWrite(LED_PIN, LOW);
}

void sendLedIR(uint32_t hexCode) {
  digitalWrite(LED_PIN, HIGH);
  irsendLED.sendNEC(hexCode, 32);
  delay(100); 
  digitalWrite(LED_PIN, LOW);
}

// Endpoint: GET /status for homebridge-web-thermostat compatibility
void handleStatus() {
  String json = "{";
  json += "\"targetTemperature\":" + String(targetTemperature, 1) + ",";
  json += "\"currentTemperature\":" + String(currentTemperature, 1) + ",";
  json += "\"targetHeatingCoolingState\":" + String(targetHeatingCoolingState) + ",";
  json += "\"currentHeatingCoolingState\":" + String(targetHeatingCoolingState);
  json += "}";
  server.send(200, "application/json", json);
}

// Endpoint: GET /targetTemperature?value=X
void handleSetTargetTemp() {
  if (server.hasArg("value")) {
    float rawVal = server.arg("value").toFloat();
    float roundedVal = round(rawVal); // Round to whole number
    
    if (roundedVal >= 16.0 && roundedVal <= 34.0) {
      int steps = (int)(roundedVal - targetTemperature);
      if (steps > 0) {
        for (int i = 0; i < steps; i++) { sendAcIR(AC_CODE_TEMP_UP); delay(250); }
        Serial.printf("IR: Temp UP %d times\n", steps);
      } else if (steps < 0) {
        for (int i = 0; i < abs(steps); i++) { sendAcIR(AC_CODE_TEMP_DOWN); delay(250); }
        Serial.printf("IR: Temp DOWN %d times\n", abs(steps));
      }
      targetTemperature = roundedVal;
      currentTemperature = roundedVal;
    }
  }
  server.send(200, "text/plain", "OK");
}

// Endpoint: GET /targetHeatingCoolingState?value=X
// Values: 0=OFF, 1=HEAT, 2=COOL, 3=AUTO
void handleSetTargetState() {
  if (server.hasArg("value")) {
    int newState = server.arg("value").toInt();
    
    // Only send IR power code if state is actually changing (0 -> on, or on -> 0)
    if ((newState == 0 && targetHeatingCoolingState != 0) || (newState != 0 && targetHeatingCoolingState == 0)) {
      sendAcPower(AC_CODE_POWER);
      Serial.printf("IR: Transmitted AC power command (state: %d -> %d)\n", targetHeatingCoolingState, newState);
    } else {
      Serial.printf("State update: Updated target state to %d without IR power toggle\n", newState);
    }
    targetHeatingCoolingState = newState;
  }
  server.send(200, "text/plain", "OK");
}

// Helper: Safely transition to specified Fan Speed (Low=1, Mid=2, High=3)
void setFanSpeed(int targetLevel) {
  if (targetLevel >= 1 && targetLevel <= 3) {
    int steps = (fan_speed_level - targetLevel + 3) % 3;
    for (int i = 0; i < steps; i++) {
      sendAcIR(AC_CODE_FAN);
      delay(250);
    }
    fan_speed_level = targetLevel;
    Serial.printf("IR: Set Fan Speed to level %d (%d press(es))\n", targetLevel, steps);
  }
}

// Helper: Evaluates HSV parameters and sends the closest matches
void updateStripColorHSV() {
  if (!led_power) return;
  last_led_command_time = millis(); // Reset 1-hour timer whenever we update color

  if (led_saturation < 12) {
    sendLedIR(LED_WHITE);
    return;
  }

  if (led_hue >= 0 && led_hue < 15) {
    sendLedIR(LED_RED);
  } else if (led_hue >= 15 && led_hue < 30) {
    sendLedIR(LED_ORANGE);
  } else if (led_hue >= 30 && led_hue < 45) {
    sendLedIR(LED_LTORANGE);
  } else if (led_hue >= 45 && led_hue < 65) {
    if (led_hue < 55) sendLedIR(LED_YELLOR);
    else sendLedIR(LED_YELLOW);
  } else if (led_hue >= 65 && led_hue < 140) {
    if (led_hue < 100) sendLedIR(LED_LTGREEN);
    else sendLedIR(LED_GREEN);
  } else if (led_hue >= 140 && led_hue < 200) {
    if (led_hue < 170) sendLedIR(LED_DKCYAN);
    else sendLedIR(LED_CYAN);
  } else if (led_hue >= 200 && led_hue < 255) {
    if (led_hue < 215) sendLedIR(LED_LTBLUE);
    else if (led_hue < 235) sendLedIR(LED_BLUE);
    else sendLedIR(LED_DKBLUE);
  } else if (led_hue >= 255 && led_hue < 330) {
    if (led_hue < 290) sendLedIR(LED_PURPLE);
    else if (led_hue < 310) sendLedIR(LED_MAGENTA);
    else sendLedIR(LED_PINK);
  } else if (led_hue >= 330 && led_hue <= 360) {
    sendLedIR(LED_RED);
  }
}

// Endpoint: LED JSON status
void handleLEDStatus() {
  String json = "{";
  json += "\"power\":" + String(led_power ? "true" : "false") + ",";
  json += "\"brightness\":" + String(led_brightness) + ",";
  json += "\"hue\":" + String(led_hue) + ",";
  json += "\"saturation\":" + String(led_saturation) + ",";
  json += "\"mode\":" + String(led_mode_level) + ",";
  json += "\"speed\":" + String(led_speed_level);
  json += "}";
  server.send(200, "application/json", json);
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Initialize both IR transmitters
  irsendAC.begin();
  irsendLED.begin();

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
  Serial.println("WiFi Connected!");

  // --- AC THERMOSTAT COMPATIBLE ROUTES ---
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/targetTemperature", HTTP_GET, handleSetTargetTemp);
  server.on("/targetHeatingCoolingState", HTTP_GET, handleSetTargetState);

  // Raw helper endpoints for proxy calibration & swap control (does NOT trigger unwanted IR)
  server.on("/set_state_memory", HTTP_GET, []() {
    if (server.hasArg("temp")) {
      targetTemperature = round(server.arg("temp").toFloat());
      currentTemperature = targetTemperature;
    }
    if (server.hasArg("state")) {
      targetHeatingCoolingState = server.arg("state").toInt();
    }
    server.send(200, "text/plain", "OK");
  });

  server.on("/send_raw_power", HTTP_GET, []() {
    sendAcPower(AC_CODE_POWER);
    server.send(200, "text/plain", "OK");
  });

  server.on("/raw_temp_steps", HTTP_GET, []() {
    if (server.hasArg("target")) {
      float roundedVal = round(server.arg("target").toFloat());
      if (roundedVal >= 16.0 && roundedVal <= 34.0) {
        int steps = (int)(roundedVal - targetTemperature);
        if (steps > 0) {
          for (int i = 0; i < steps; i++) { sendAcIR(AC_CODE_TEMP_UP); delay(250); }
        } else if (steps < 0) {
          for (int i = 0; i < abs(steps); i++) { sendAcIR(AC_CODE_TEMP_DOWN); delay(250); }
        }
        targetTemperature = roundedVal;
        currentTemperature = roundedVal;
      }
    }
    server.send(200, "text/plain", "OK");
  });

  server.on("/set_state_raw", HTTP_GET, []() {
    if (server.hasArg("state")) {
      int newState = server.arg("state").toInt();
      if ((newState == 0 && targetHeatingCoolingState != 0) || (newState != 0 && targetHeatingCoolingState == 0)) {
        sendAcPower(AC_CODE_POWER);
      }
      targetHeatingCoolingState = newState;
    }
    server.send(200, "text/plain", "OK");
  });

  // --- VIRTUAL STATEFUL ROUTING FOR SWING, MODE, FAN, ECO ---
  server.on("/ac/swing", HTTP_GET, []() {
    sendAcIR(AC_CODE_SWING);
    swing_state = !swing_state; // Toggle tracked swing state
    server.send(200, "text/plain", swing_state ? "1" : "0");
  });
  
  server.on("/ac/swing/status", HTTP_GET, []() {
    server.send(200, "text/plain", swing_state ? "1" : "0");
  });

  server.on("/ac/eco", HTTP_GET, []() {
    sendAcIR(AC_CODE_ECO);
    eco_state = !eco_state; // Toggle tracked eco state
    server.send(200, "text/plain", eco_state ? "1" : "0");
  });
  
  server.on("/ac/eco/status", HTTP_GET, []() {
    server.send(200, "text/plain", eco_state ? "1" : "0");
  });

  // Cycle endpoints
  server.on("/ac/mode", HTTP_GET, []() {
    sendAcIR(AC_CODE_MODE);
    ac_mode_level = (ac_mode_level % 3) + 1; // cycle 1 -> 2 -> 3
    server.send(200, "text/plain", String(ac_mode_level));
  });

  server.on("/ac/fan", HTTP_GET, []() {
    sendAcIR(AC_CODE_FAN);
    // Cycle downward: 3 -> 2 -> 1 -> 3
    fan_speed_level = (fan_speed_level == 1) ? 3 : fan_speed_level - 1; 
    server.send(200, "text/plain", String(fan_speed_level));
  });

  // Fan active validation status (forces plugin responsiveness)
  server.on("/ac/fan/active", HTTP_GET, []() {
    server.send(200, "text/plain", "1");
  });

  // Native Fan Speed Percentages (0-100%) mapped to Low, Mid, High
  server.on("/ac/fan/speed", HTTP_GET, []() {
    if (server.hasArg("value")) {
      int pct = server.arg("value").toInt();
      int targetLevel = 1;
      if (pct > 33 && pct <= 66) targetLevel = 2;
      else if (pct > 66) targetLevel = 3;
      
      setFanSpeed(targetLevel);
    }
    // Report back standard levels: 33%, 66%, or 99%
    server.send(200, "text/plain", String(fan_speed_level * 33));
  });

  server.on("/ac/fan/speed/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(fan_speed_level * 33));
  });

  // --- AC Timer Control Endpoints ---
  // Maps 0-100% slider to 0-24 hours in 0.5 hour increments
  server.on("/ac/timer", HTTP_GET, []() {
    if (server.hasArg("value")) {
      int pct = server.arg("value").toInt();
      float hours = round((pct * 24.0 / 100.0) * 2.0) / 2.0; // round to 0.5hr steps
      
      if (hours > 0.0) {
        timer_duration_ms = (unsigned long)(hours * 3600.0 * 1000.0);
        timer_start_time = millis();
        timer_active = true;
        Serial.printf("Timer: Set AC to shutoff in %.1f hour(s)\n", hours);
      } else {
        timer_active = false;
        Serial.println("Timer: Disabled");
      }
    }
    
    // Return current remaining percentage
    int responsePct = 0;
    if (timer_active) {
      unsigned long elapsed = millis() - timer_start_time;
      if (timer_duration_ms > elapsed) {
        float remaining_hours = (float)(timer_duration_ms - elapsed) / (3600.0 * 1000.0);
        responsePct = (int)((remaining_hours / 24.0) * 100.0);
      } else {
        timer_active = false; // clean up expired timer state on query
      }
    }
    server.send(200, "text/plain", String(responsePct));
  });

  server.on("/ac/timer/status", HTTP_GET, []() {
    int responsePct = 0;
    if (timer_active) {
      unsigned long elapsed = millis() - timer_start_time;
      if (timer_duration_ms > elapsed) {
        float remaining_hours = (float)(timer_duration_ms - elapsed) / (3600.0 * 1000.0);
        responsePct = (int)((remaining_hours / 24.0) * 100.0);
      } else {
        timer_active = false; // clean up expired timer state on query
      }
    }
    server.send(200, "text/plain", String(responsePct));
  });

  server.on("/ac/timer/on", HTTP_GET, []() {
    // Turning switch on defaults to a 1.0 hour timer
    timer_duration_ms = (unsigned long)(1.0 * 3600.0 * 1000.0);
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
    last_led_command_time = millis();
    sendLedIR(LED_ON);
    
    // Idea 2: Wait 250ms and push color sync to ensure LED starts in last chosen state
    delay(250); 
    if (led_mode_level == 1) {
      updateStripColorHSV();
    } else {
      // Re-trigger dynamic mode
      if (led_mode_level == 2) sendLedIR(LED_FLASH);
      else if (led_mode_level == 3) sendLedIR(LED_STROBE);
      else if (led_mode_level == 4) sendLedIR(LED_FADE);
      else if (led_mode_level == 5) sendLedIR(LED_SMOOTH);
    }
    server.send(200, "text/plain", "1");
  });

  server.on("/led/off", HTTP_GET, []() {
    led_power = false;
    last_led_command_time = millis();
    sendLedIR(LED_OFF);
    server.send(200, "text/plain", "0");
  });

  server.on("/led/hue", HTTP_GET, []() {
    if (server.hasArg("value")) {
      led_hue = server.arg("value").toInt();
      led_mode_level = 1; // Change back to Solid mode upon color select
      updateStripColorHSV();
    }
    server.send(200, "text/plain", String(led_hue));
  });

  server.on("/led/saturation", HTTP_GET, []() {
    if (server.hasArg("value")) {
      led_saturation = server.arg("value").toInt();
      led_mode_level = 1; // Change back to Solid mode
      updateStripColorHSV();
    }
    server.send(200, "text/plain", String(led_saturation));
  });

  server.on("/led/brightness", HTTP_GET, []() {
    if (server.hasArg("value")) {
      int targetBri = server.arg("value").toInt();
      int steps = (targetBri - led_brightness) / 10;
      if (steps > 0) {
        for (int i = 0; i < steps; i++) { sendLedIR(LED_BRI_UP); delay(100); }
      } else if (steps < 0) {
        for (int i = 0; i < abs(steps); i++) { sendLedIR(LED_BRI_DWN); delay(100); }
      }
      led_brightness = targetBri;
      last_led_command_time = millis(); // Reset timer for brightness changes too
    }
    server.send(200, "text/plain", String(led_brightness));
  });

  // Cycle LED Modes: 1=Solid, 2=Flash, 3=Strobe, 4=Fade, 5=Smooth
  server.on("/led/mode", HTTP_GET, []() {
    led_mode_level = (led_mode_level % 5) + 1; // cycle 1 -> 2 -> 3 -> 4 -> 5 -> 1
    if (led_mode_level == 1) {
      updateStripColorHSV(); // revert to last selected solid color
    } else if (led_mode_level == 2) {
      sendLedIR(LED_FLASH);
    } else if (led_mode_level == 3) {
      sendLedIR(LED_STROBE);
    } else if (led_mode_level == 4) {
      sendLedIR(LED_FADE);
    } else if (led_mode_level == 5) {
      sendLedIR(LED_SMOOTH);
    }
    server.send(200, "text/plain", String(led_mode_level));
  });

  server.on("/led/mode/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(led_mode_level));
  });

  // Idea 1: Speed cycle endpoint
  server.on("/led/speed", HTTP_GET, []() {
    led_speed_level = (led_speed_level % 3) + 1; // Cycle: 1 (Slow) -> 2 (Med) -> 3 (Fast)
    if (led_speed_level == 1) {
      // Send speed down a few times to guarantee slowest speed
      for (int i = 0; i < 4; i++) { sendLedIR(LED_BRI_DWN); delay(100); }
    } else if (led_speed_level == 2) {
      // Speed up to medium
      for (int i = 0; i < 4; i++) { sendLedIR(LED_BRI_DWN); delay(100); }
      sendLedIR(LED_BRI_UP); delay(100);
      sendLedIR(LED_BRI_UP);
    } else if (led_speed_level == 3) {
      // Speed up to maximum
      for (int i = 0; i < 4; i++) { sendLedIR(LED_BRI_UP); delay(100); }
    }
    server.send(200, "text/plain", String(led_speed_level));
  });

  server.on("/led/speed/status", HTTP_GET, []() {
    server.send(200, "text/plain", String(led_speed_level));
  });

  // Idea 3: Soft Turn-Off Sleep Timer Endpoint (0-100% maps to 0-120 minutes)
  server.on("/led/timer", HTTP_GET, []() {
    if (server.hasArg("value")) {
      int pct = server.arg("value").toInt();
      float minutes = pct * 1.2; // 0-100% -> 0-120 minutes
      if (minutes > 0.0) {
        led_timer_duration_ms = (unsigned long)(minutes * 60.0 * 1000.0);
        led_timer_start_time = millis();
        led_timer_active = true;
        Serial.printf("LED Timer: Set lights to shutoff in %.1f minute(s)\n", minutes);
      } else {
        led_timer_active = false;
      }
    }
    int responsePct = 0;
    if (led_timer_active) {
      unsigned long elapsed = millis() - led_timer_start_time;
      if (led_timer_duration_ms > elapsed) {
        float remaining = (float)(led_timer_duration_ms - elapsed) / (60.0 * 1000.0);
        responsePct = (int)((remaining / 120.0) * 100.0);
      }
    }
    server.send(200, "text/plain", String(responsePct));
  });

  server.on("/led/timer/status", HTTP_GET, []() {
    int responsePct = 0;
    if (led_timer_active) {
      unsigned long elapsed = millis() - led_timer_start_time;
      if (led_timer_duration_ms > elapsed) {
        float remaining = (float)(led_timer_duration_ms - elapsed) / (60.0 * 1000.0);
        responsePct = (int)((remaining / 120.0) * 100.0);
      } else {
        led_timer_active = false;
      }
    }
    server.send(200, "text/plain", String(responsePct));
  });

  // --- Arduino OTA Config ---
  ArduinoOTA.setHostname("esp32-ir-controller");
  ArduinoOTA.begin();

  server.begin();
}

void loop() {
  server.handleClient();
  ArduinoOTA.handle();

  // --- AC Background Timer ---
  if (timer_active && (millis() - timer_start_time >= timer_duration_ms)) {
    timer_active = false;
    
    // Automatically power off the AC if it is currently tracked as ON
    if (targetHeatingCoolingState != 0) {
      sendAcPower(AC_CODE_POWER);
      targetHeatingCoolingState = 0;
      Serial.println("Timer Expired: Sent IR AC Power Off command");
    }
  }

  // --- LED Sleep Timer ---
  if (led_timer_active && (millis() - led_timer_start_time >= led_timer_duration_ms)) {
    led_timer_active = false;
    if (led_power) {
      led_power = false;
      sendLedIR(LED_OFF);
      Serial.println("LED Timer Expired: Sent IR LED Power Off command");
    }
  }

  // --- LED Hourly Refresh ---
  if (led_power && (millis() - last_led_command_time >= LED_REFRESH_INTERVAL_MS)) {
    last_led_command_time = millis();
    updateStripColorHSV(); // Re-emit the current color code to the LED strip
    Serial.println("LED: Hourly refresh command sent.");
  }
}
