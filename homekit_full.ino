/*
  Reliable ESP32 home controller

  Required libraries:
    - ESP32Async/AsyncTCP
    - ESP32Async/ESPAsyncWebServer
    - crankyoldgit/IRremoteESP8266

  Tested design target: classic dual-core ESP32 using Arduino-ESP32 2.x/3.x.
  Remove old/duplicate me-no-dev AsyncTCP and ESPAsyncWebServer copies before building.

  Reliability design:
    - The recovery AP is started immediately and never intentionally stopped.
    - Only networkTask calls Wi-Fi control APIs.
    - There is one source-level WiFi.begin() and no WiFi.reconnect().
    - HTTP callbacks never transmit IR, wait, or run command sequences.
    - One worker owns both IR transmitters and serializes all IR activity.
    - Queues are bounded; overload receives HTTP 503 instead of silent loss.
    - State access is protected by one short-lived mutex.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <ArduinoOTA.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/event_groups.h>
#include "secrets.h"

// ---------------- Configuration ----------------

static constexpr char WIFI_SSID[] = SECRET_SSID;
static constexpr char WIFI_PASSWORD[] = SECRET_PASSWORD;
static constexpr char HOSTNAME[] = "esp32-ir-controller";
static constexpr char FALLBACK_AP_SSID[] = "esp32-ir-controller";
// The recovery AP intentionally remains open to preserve the original behavior.
// For deployment, pass a strong password as the second argument to WiFi.softAP().

static constexpr uint16_t kIrAcPin = 27;
static constexpr uint16_t kIrLedPin = 23;
static constexpr int LED_PIN = 2;

static constexpr int relayMain = 25;
static constexpr int relayFairy = 26;
static constexpr int relayFan = 33;
static constexpr int relayBack = 32;

static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000UL;
static constexpr uint32_t WIFI_CANCEL_GRACE_MS = 5000UL;
static constexpr uint32_t WIFI_BACKOFF_INITIAL_MS = 10000UL;
static constexpr uint32_t WIFI_BACKOFF_MAX_MS = 300000UL;
static constexpr uint32_t AC_WAKEUP_TIMEOUT_MS = 20000UL;
static constexpr uint32_t LED_REFRESH_INTERVAL_MS = 3600000UL;
static constexpr uint32_t HEALTH_LOG_INTERVAL_MS = 60000UL;

// ---------------- IR codes ----------------

static constexpr uint32_t AC_CODE_POWER = 0x80FF48B7;
static constexpr uint32_t AC_CODE_TEMP_UP = 0x80FF58A7;
static constexpr uint32_t AC_CODE_TEMP_DOWN = 0x80FFC837;
static constexpr uint32_t AC_CODE_MODE = 0x80FF40BF;
static constexpr uint32_t AC_CODE_FAN = 0x80FF708F;
static constexpr uint32_t AC_CODE_SWING = 0x80FFF00F;
static constexpr uint32_t AC_CODE_ECO = 0x80FF50AF;

static constexpr uint32_t LED_ON = 0xF7C03F;
static constexpr uint32_t LED_OFF = 0xF740BF;
static constexpr uint32_t LED_BRI_UP = 0xF700FF;
static constexpr uint32_t LED_BRI_DWN = 0xF7807F;
static constexpr uint32_t LED_RED = 0xF720DF;
static constexpr uint32_t LED_GREEN = 0xF7A05F;
static constexpr uint32_t LED_BLUE = 0xF7609F;
static constexpr uint32_t LED_WHITE = 0xF7E01F;
static constexpr uint32_t LED_ORANGE = 0xF710EF;
static constexpr uint32_t LED_LTGREEN = 0xF7906F;
static constexpr uint32_t LED_LTBLUE = 0xF750AF;
static constexpr uint32_t LED_LTORANGE = 0xF730CF;
static constexpr uint32_t LED_CYAN = 0xF7B04F;
static constexpr uint32_t LED_PURPLE = 0xF7708F;
static constexpr uint32_t LED_YELLOR = 0xF708F7;  // Remote's first yellow button.
static constexpr uint32_t LED_DKCYAN = 0xF78877;
static constexpr uint32_t LED_MAGENTA = 0xF748B7;
static constexpr uint32_t LED_YELLOW = 0xF728D7;
static constexpr uint32_t LED_DKBLUE = 0xF7A857;
static constexpr uint32_t LED_PINK = 0xF76897;
static constexpr uint32_t LED_FLASH = 0xF7D02F;
static constexpr uint32_t LED_STROBE = 0xF7F00F;
static constexpr uint32_t LED_FADE = 0xF7C837;
static constexpr uint32_t LED_SMOOTH = 0xF7E817;

// ---------------- Types and shared state ----------------

enum class StaState : uint8_t {
  IDLE,
  CONNECTING,
  ASSOCIATED,
  CONNECTED,
  CANCELLING,
  BACKOFF
};

enum class IrDevice : uint8_t { AC, LED };

static constexpr uint8_t MAX_IR_FRAMES_PER_JOB = 24;

struct IrJob {
  IrDevice device;
  uint8_t frameCount;
  uint16_t initialGapMs;
  uint16_t gapsAfterMs[MAX_IR_FRAMES_PER_JOB];
  uint32_t codes[MAX_IR_FRAMES_PER_JOB];
};

enum class RelayId : uint8_t { MAIN, FAIRY, FAN, BACKLIGHT };

struct RelayCommand {
  RelayId id;
  bool on;
};

struct ControllerState {
  bool lampMain = false;
  bool lampFairy = false;
  bool lampFan = false;
  bool lampBack = false;

  float targetTemperature = 25.0f;
  float currentTemperature = 25.0f;
  int targetHeatingCoolingState = 0;
  bool swing = false;
  bool eco = false;
  int fanSpeedLevel = 1;
  int acModeLevel = 1;
  uint32_t lastAcTempAt = 0;

  bool ledPower = false;
  int ledBrightness = 100;
  int ledHue = 0;
  int ledSaturation = 0;
  int ledSpeedLevel = 1;
  int ledModeLevel = 1;
  uint32_t lastLedCommandAt = 0;

  bool acTimerActive = false;
  uint32_t acTimerDurationMs = 0;
  uint32_t acTimerStartedAt = 0;
  bool ledTimerActive = false;
  uint32_t ledTimerDurationMs = 0;
  uint32_t ledTimerStartedAt = 0;

  StaState staState = StaState::IDLE;
  uint32_t staAttemptStartedAt = 0;
  uint32_t retryAt = 0;
  uint32_t retryDelayMs = WIFI_BACKOFF_INITIAL_MS;
  uint32_t staAttempts = 0;
  uint8_t lastDisconnectReason = 0;
  uint32_t staIp = 0;
  int32_t rssi = 0;
  uint32_t apIp = 0;
  bool apRunning = false;

  bool otaActive = false;
  uint8_t otaProgress = 0;
  int otaLastError = 0;

  uint32_t irJobsAccepted = 0;
  uint32_t irJobsCompleted = 0;
  uint32_t irJobsRejected = 0;
  uint32_t relayCommandsRejected = 0;
};

ControllerState state;
SemaphoreHandle_t stateMutex = nullptr;
QueueHandle_t irQueue = nullptr;
QueueHandle_t relayQueue = nullptr;
EventGroupHandle_t systemEvents = nullptr;
TaskHandle_t networkTaskHandle = nullptr;

static constexpr EventBits_t NETWORK_READY_BIT = BIT0;
static constexpr uint32_t WIFI_NOTE_STA_CONNECTED = BIT0;
static constexpr uint32_t WIFI_NOTE_GOT_IP = BIT1;
static constexpr uint32_t WIFI_NOTE_DISCONNECTED = BIT2;

volatile uint8_t wifiDisconnectReasonMailbox = 0;
portMUX_TYPE wifiEventMux = portMUX_INITIALIZER_UNLOCKED;

AsyncWebServer server(80);
IRsend irsendAC(kIrAcPin);
IRsend irsendLED(kIrLedPin);

// ---------------- Time and state helpers ----------------

static bool elapsedMs(uint32_t now, uint32_t since, uint32_t interval) {
  return static_cast<uint32_t>(now - since) >= interval;
}

static bool deadlineReached(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

static ControllerState copyState() {
  ControllerState snapshot;
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  snapshot = state;
  xSemaphoreGive(stateMutex);
  return snapshot;
}

static const char* staStateName(StaState value) {
  switch (value) {
    case StaState::IDLE: return "IDLE";
    case StaState::CONNECTING: return "CONNECTING";
    case StaState::ASSOCIATED: return "ASSOCIATED";
    case StaState::CONNECTED: return "CONNECTED";
    case StaState::CANCELLING: return "CANCELLING";
    case StaState::BACKOFF: return "BACKOFF";
  }
  return "UNKNOWN";
}

static String ipString(uint32_t raw) {
  return IPAddress(raw).toString();
}

static bool parseIntParam(AsyncWebServerRequest* request, const char* name, int& value) {
  if (!request->hasParam(name)) return false;
  const String text = request->getParam(name)->value();
  if (text.length() == 0) return false;
  char* end = nullptr;
  const long parsed = strtol(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0') return false;
  value = static_cast<int>(parsed);
  return true;
}

static bool parseFloatParam(AsyncWebServerRequest* request, const char* name, float& value) {
  if (!request->hasParam(name)) return false;
  const String text = request->getParam(name)->value();
  if (text.length() == 0) return false;
  char* end = nullptr;
  const float parsed = strtof(text.c_str(), &end);
  if (end == text.c_str() || *end != '\0' || !isfinite(parsed)) return false;
  value = parsed;
  return true;
}

static void sendBusy(AsyncWebServerRequest* request, const char* service) {
  AsyncWebServerResponse* response = request->beginResponse(503, "text/plain", String(service) + " busy; retry shortly");
  response->addHeader("Retry-After", "1");
  request->send(response);
}

static bool mutationsAllowed() {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  const bool allowed = !state.otaActive;
  xSemaphoreGive(stateMutex);
  return allowed;
}

// ---------------- IR job builders and queue ----------------

static IrJob oneCode(IrDevice device, uint32_t code) {
  IrJob job{};
  job.device = device;
  job.frameCount = 1;
  job.codes[0] = code;
  return job;
}

static IrJob repeatedCode(IrDevice device, uint32_t code, uint8_t count, uint16_t gapMs) {
  IrJob job{};
  job.device = device;
  job.frameCount = min(count, MAX_IR_FRAMES_PER_JOB);
  for (uint8_t i = 0; i < job.frameCount; ++i) {
    job.codes[i] = code;
    job.gapsAfterMs[i] = (i + 1 < job.frameCount) ? gapMs : 0;
  }
  return job;
}

static uint32_t ledColorCodeFor(int hue, int saturation) {
  if (saturation < 12) return LED_WHITE;
  if (hue < 0) hue = 0;
  if (hue > 360) hue %= 360;

  if (hue < 15) return LED_RED;
  if (hue < 30) return LED_ORANGE;
  if (hue < 45) return LED_LTORANGE;
  if (hue < 65) return (hue < 55) ? LED_YELLOR : LED_YELLOW;
  if (hue < 140) return (hue < 100) ? LED_LTGREEN : LED_GREEN;
  if (hue < 200) return (hue < 170) ? LED_DKCYAN : LED_CYAN;
  if (hue < 255) {
    if (hue < 215) return LED_LTBLUE;
    if (hue < 235) return LED_BLUE;
    return LED_DKBLUE;
  }
  if (hue < 330) {
    if (hue < 290) return LED_PURPLE;
    if (hue < 310) return LED_MAGENTA;
    return LED_PINK;
  }
  return LED_RED;
}

static uint32_t ledModeCode(int mode, int hue, int saturation) {
  switch (mode) {
    case 2: return LED_FLASH;
    case 3: return LED_STROBE;
    case 4: return LED_FADE;
    case 5: return LED_SMOOTH;
    default: return ledColorCodeFor(hue, saturation);
  }
}

// Caller must hold stateMutex. State is only changed after this succeeds.
static bool enqueueIrLocked(const IrJob& job) {
  if (state.otaActive || xQueueSend(irQueue, &job, 0) != pdTRUE) {
    state.irJobsRejected++;
    return false;
  }
  state.irJobsAccepted++;
  return true;
}

// ---------------- Worker tasks ----------------

static void irWorkerTask(void*) {
  irsendAC.begin();
  irsendLED.begin();

  IrJob job{};
  for (;;) {
    if (xQueueReceive(irQueue, &job, portMAX_DELAY) != pdTRUE) continue;

    if (job.initialGapMs > 0) {
      vTaskDelay(pdMS_TO_TICKS(job.initialGapMs));
    }

    for (uint8_t i = 0; i < job.frameCount; ++i) {
      digitalWrite(LED_PIN, HIGH);
      if (job.device == IrDevice::AC) {
        irsendAC.sendNEC(job.codes[i], 32);
      } else {
        irsendLED.sendNEC(job.codes[i], 32);
      }
      digitalWrite(LED_PIN, LOW);

      if (job.gapsAfterMs[i] > 0) {
        vTaskDelay(pdMS_TO_TICKS(job.gapsAfterMs[i]));
      }
    }

    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.irJobsCompleted++;
    xSemaphoreGive(stateMutex);
  }
}

static void applyRelayCommand(const RelayCommand& command) {
  int pin = relayMain;
  switch (command.id) {
    case RelayId::MAIN: pin = relayMain; break;
    case RelayId::FAIRY: pin = relayFairy; break;
    case RelayId::FAN: pin = relayFan; break;
    case RelayId::BACKLIGHT: pin = relayBack; break;
  }
  digitalWrite(pin, command.on ? LOW : HIGH);
}

static void controllerTask(void*) {
  RelayCommand relayCommand{};
  uint32_t lastHealthLogAt = millis();

  for (;;) {
    while (xQueueReceive(relayQueue, &relayCommand, 0) == pdTRUE) {
      applyRelayCommand(relayCommand);
    }

    const uint32_t now = millis();
    xSemaphoreTake(stateMutex, portMAX_DELAY);

    if (state.acTimerActive && elapsedMs(now, state.acTimerStartedAt, state.acTimerDurationMs)) {
      if (state.targetHeatingCoolingState == 0) {
        state.acTimerActive = false;
      } else {
        const IrJob job = oneCode(IrDevice::AC, AC_CODE_POWER);
        if (enqueueIrLocked(job)) {
          state.acTimerActive = false;
          state.targetHeatingCoolingState = 0;
          Serial.println("Timer: queued AC power off");
        }
      }
    }

    if (state.ledTimerActive && elapsedMs(now, state.ledTimerStartedAt, state.ledTimerDurationMs)) {
      if (!state.ledPower) {
        state.ledTimerActive = false;
      } else {
        const IrJob job = oneCode(IrDevice::LED, LED_OFF);
        if (enqueueIrLocked(job)) {
          state.ledTimerActive = false;
          state.ledPower = false;
          state.lastLedCommandAt = now;
          Serial.println("Timer: queued LED power off");
        }
      }
    }

    if (state.ledPower && elapsedMs(now, state.lastLedCommandAt, LED_REFRESH_INTERVAL_MS)) {
      const IrJob job = oneCode(IrDevice::LED,
          ledModeCode(state.ledModeLevel, state.ledHue, state.ledSaturation));
      if (enqueueIrLocked(job)) {
        state.lastLedCommandAt = now;
        Serial.println("LED: queued hourly state refresh");
      }
    }

    const bool logHealth = elapsedMs(now, lastHealthLogAt, HEALTH_LOG_INTERVAL_MS);
    if (logHealth) lastHealthLogAt = now;
    const StaState currentStaState = state.staState;
    const uint32_t staIp = state.staIp;
    const uint32_t apIp = state.apIp;
    const int32_t currentRssi = state.rssi;
    xSemaphoreGive(stateMutex);

    if (logHealth) {
      Serial.printf("Health: heap=%u minHeap=%u STA=%s STA_IP=%s RSSI=%ld AP_IP=%s IRq=%u RelayQ=%u\n",
                    ESP.getFreeHeap(), ESP.getMinFreeHeap(), staStateName(currentStaState),
                    ipString(staIp).c_str(), static_cast<long>(currentRssi), ipString(apIp).c_str(),
                    static_cast<unsigned>(uxQueueMessagesWaiting(irQueue)),
                    static_cast<unsigned>(uxQueueMessagesWaiting(relayQueue)));
    }

    // Block briefly for relay work; timer checks remain responsive without spinning.
    if (xQueueReceive(relayQueue, &relayCommand, pdMS_TO_TICKS(20)) == pdTRUE) {
      applyRelayCommand(relayCommand);
    }
  }
}

// ---------------- Wi-Fi state machine ----------------

static void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (networkTaskHandle == nullptr) return;

  BaseType_t shouldYield = pdFALSE;
  if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
    xTaskNotifyFromISR(networkTaskHandle, WIFI_NOTE_STA_CONNECTED, eSetBits, &shouldYield);
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    xTaskNotifyFromISR(networkTaskHandle, WIFI_NOTE_GOT_IP, eSetBits, &shouldYield);
  } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    portENTER_CRITICAL_ISR(&wifiEventMux);
    wifiDisconnectReasonMailbox = info.wifi_sta_disconnected.reason;
    portEXIT_CRITICAL_ISR(&wifiEventMux);
    xTaskNotifyFromISR(networkTaskHandle, WIFI_NOTE_DISCONNECTED, eSetBits, &shouldYield);
  }
  if (shouldYield == pdTRUE) portYIELD_FROM_ISR();
}

// This is the only function allowed to initiate a STA attempt.
static bool beginStaAttempt(uint32_t now) {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  const bool allowed = state.staState == StaState::IDLE || state.staState == StaState::BACKOFF;
  if (allowed) {
    state.staState = StaState::CONNECTING;
    state.staAttemptStartedAt = now;
    state.staAttempts++;
  }
  xSemaphoreGive(stateMutex);
  if (!allowed) return false;

  Serial.printf("WiFi: starting STA attempt %lu\n", static_cast<unsigned long>(copyState().staAttempts));
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  return true;
}

static void scheduleStaBackoff(uint32_t now, uint8_t reason) {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  const uint32_t jitter = esp_random() % 2000UL;
  state.staState = StaState::BACKOFF;
  state.lastDisconnectReason = reason;
  state.staIp = 0;
  state.rssi = 0;
  state.retryAt = now + state.retryDelayMs + jitter;
  state.retryDelayMs = min(state.retryDelayMs * 2UL, WIFI_BACKOFF_MAX_MS);
  const uint32_t retryIn = static_cast<uint32_t>(state.retryAt - now);
  xSemaphoreGive(stateMutex);
  Serial.printf("WiFi: disconnected (reason=%u), retry in %lu ms; recovery AP remains at 192.168.4.1\n",
                reason, static_cast<unsigned long>(retryIn));
}

static void networkTask(void*) {
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.setHostname(HOSTNAME);
  WiFi.mode(WIFI_AP_STA);  // Set exactly once; never switch modes during recovery.
  WiFi.setSleep(false);
  WiFi.onEvent(onWiFiEvent);

  const IPAddress apAddress(192, 168, 4, 1);
  const IPAddress apGateway(192, 168, 4, 1);
  const IPAddress apSubnet(255, 255, 255, 0);
  WiFi.softAPConfig(apAddress, apGateway, apSubnet);
  const bool apStarted = WiFi.softAP(FALLBACK_AP_SSID);

  xSemaphoreTake(stateMutex, portMAX_DELAY);
  state.apRunning = apStarted;
  state.apIp = static_cast<uint32_t>(WiFi.softAPIP());
  state.staState = StaState::IDLE;
  xSemaphoreGive(stateMutex);

  Serial.printf("Recovery AP: %s, IP=%s\n", apStarted ? "started" : "FAILED", WiFi.softAPIP().toString().c_str());
  xEventGroupSetBits(systemEvents, NETWORK_READY_BIT);
  beginStaAttempt(millis());

  for (;;) {
    uint32_t notes = 0;
    xTaskNotifyWait(0, UINT32_MAX, &notes, pdMS_TO_TICKS(250));
    const uint32_t now = millis();

    if (notes & WIFI_NOTE_STA_CONNECTED) {
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      if (state.staState == StaState::CONNECTING) state.staState = StaState::ASSOCIATED;
      xSemaphoreGive(stateMutex);
      Serial.println("WiFi: associated; waiting for DHCP");
    }

    if (notes & WIFI_NOTE_GOT_IP) {
      const uint32_t ip = static_cast<uint32_t>(WiFi.localIP());
      const int32_t rssi = WiFi.RSSI();
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      state.staState = StaState::CONNECTED;
      state.staIp = ip;
      state.rssi = rssi;
      state.retryDelayMs = WIFI_BACKOFF_INITIAL_MS;
      xSemaphoreGive(stateMutex);
      Serial.printf("WiFi: connected, STA IP=%s RSSI=%ld dBm; AP IP=%s\n",
                    ipString(ip).c_str(), static_cast<long>(rssi), WiFi.softAPIP().toString().c_str());
    }

    if (notes & WIFI_NOTE_DISCONNECTED) {
      uint8_t reason;
      portENTER_CRITICAL(&wifiEventMux);
      reason = wifiDisconnectReasonMailbox;
      portEXIT_CRITICAL(&wifiEventMux);
      scheduleStaBackoff(now, reason);
    }

    ControllerState snapshot = copyState();
    if ((snapshot.staState == StaState::CONNECTING || snapshot.staState == StaState::ASSOCIATED) &&
        elapsedMs(now, snapshot.staAttemptStartedAt, WIFI_CONNECT_TIMEOUT_MS)) {
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      if (state.staState == StaState::CONNECTING || state.staState == StaState::ASSOCIATED) {
        state.staState = StaState::CANCELLING;
        state.staAttemptStartedAt = now;  // Reuse as cancellation start time.
      }
      xSemaphoreGive(stateMutex);
      Serial.println("WiFi: STA attempt timed out; cancelling once before retry");
      WiFi.disconnect(false, false);  // Preserve AP and credentials; wait for disconnect event.
    } else if (snapshot.staState == StaState::CANCELLING &&
               elapsedMs(now, snapshot.staAttemptStartedAt, WIFI_CANCEL_GRACE_MS)) {
      // Some core versions omit a disconnect event when no AP was ever found.
      // disconnect() was already issued once; only leave cancellation after the
      // driver reports that it is no longer connected.
      if (WiFi.status() != WL_CONNECTED) {
        scheduleStaBackoff(now, snapshot.lastDisconnectReason);
      }
    } else if (snapshot.staState == StaState::BACKOFF && deadlineReached(now, snapshot.retryAt)) {
      beginStaAttempt(now);
    } else if (snapshot.staState == StaState::CONNECTED) {
      // RSSI refresh is read only and occurs in the network owner task.
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      state.rssi = WiFi.RSSI();
      xSemaphoreGive(stateMutex);
    }
  }
}

// ---------------- OTA task ----------------

static void otaServiceTask(void*) {
  xEventGroupWaitBits(systemEvents, NETWORK_READY_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

  ArduinoOTA.setHostname(HOSTNAME);
  // Add ArduinoOTA.setPasswordHash("...") before production deployment.
  ArduinoOTA.onStart([]() {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.otaActive = true;
    state.otaProgress = 0;
    state.otaLastError = 0;
    xSemaphoreGive(stateMutex);
    Serial.println("OTA: update started; new mutations are temporarily rejected");
  });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    const uint8_t pct = total ? static_cast<uint8_t>((progress * 100U) / total) : 0;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.otaProgress = pct;
    xSemaphoreGive(stateMutex);
  });
  ArduinoOTA.onError([](ota_error_t error) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.otaActive = false;
    state.otaLastError = static_cast<int>(error);
    xSemaphoreGive(stateMutex);
    Serial.printf("OTA: failed with error %u\n", static_cast<unsigned>(error));
  });
  ArduinoOTA.onEnd([]() {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.otaProgress = 100;
    xSemaphoreGive(stateMutex);
    Serial.println("OTA: update complete; rebooting");
  });
  ArduinoOTA.begin();
  Serial.println("OTA: service ready");

  for (;;) {
    ArduinoOTA.handle();
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ---------------- HTTP route helpers ----------------

static bool queueRelayLocked(RelayId id, bool on) {
  RelayCommand command{id, on};
  if (state.otaActive || xQueueSend(relayQueue, &command, 0) != pdTRUE) {
    state.relayCommandsRejected++;
    return false;
  }
  switch (id) {
    case RelayId::MAIN: state.lampMain = on; break;
    case RelayId::FAIRY: state.lampFairy = on; break;
    case RelayId::FAN: state.lampFan = on; break;
    case RelayId::BACKLIGHT: state.lampBack = on; break;
  }
  return true;
}

static void registerRelayRoutes(const char* basePath, const char* statusPath, RelayId id) {
  const String onPath = String(basePath) + "/on";
  const String offPath = String(basePath) + "/off";

  server.on(onPath.c_str(), HTTP_GET, [id](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const bool accepted = queueRelayLocked(id, true);
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "Relay service");
    request->send(200, "text/plain", "1");
  });

  server.on(offPath.c_str(), HTTP_GET, [id](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const bool accepted = queueRelayLocked(id, false);
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "Relay service");
    request->send(200, "text/plain", "0");
  });

  server.on(statusPath, HTTP_GET, [id](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    bool on = false;
    switch (id) {
      case RelayId::MAIN: on = s.lampMain; break;
      case RelayId::FAIRY: on = s.lampFairy; break;
      case RelayId::FAN: on = s.lampFan; break;
      case RelayId::BACKLIGHT: on = s.lampBack; break;
    }
    request->send(200, "text/plain", on ? "1" : "0");
  });
}

static void registerRoutes() {
  server.on("/", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    String msg;
    msg.reserve(220);
    msg += "ESP32 IR controller alive\n";
    msg += "STA state: " + String(staStateName(s.staState)) + "\n";
    msg += "STA IP: " + ipString(s.staIp) + "\n";
    msg += "AP IP: " + ipString(s.apIp) + "\n";
    msg += "Diagnostics: /diagnostics\n";
    request->send(200, "text/plain", msg);
  });

  server.on("/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    String json = "{";
    json += "\"targetTemperature\":" + String(s.targetTemperature, 1) + ",";
    json += "\"currentTemperature\":" + String(s.currentTemperature, 1) + ",";
    json += "\"targetHeatingCoolingState\":" + String(s.targetHeatingCoolingState) + ",";
    json += "\"currentHeatingCoolingState\":" + String(s.targetHeatingCoolingState);
    json += "}";
    request->send(200, "application/json", json);
  });

  server.on("/targetTemperature", HTTP_GET, [](AsyncWebServerRequest* request) {
    float raw;
    if (!parseFloatParam(request, "value", raw)) {
      return request->send(400, "text/plain", "Missing or invalid value");
    }
    const float rounded = roundf(raw);
    if (rounded < 16.0f || rounded > 34.0f) {
      return request->send(422, "text/plain", "Temperature must be 16..34");
    }

    bool accepted = true;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const int steps = static_cast<int>(rounded - state.targetTemperature);
    if (steps != 0) {
      const bool wake = state.lastAcTempAt == 0 || elapsedMs(millis(), state.lastAcTempAt, AC_WAKEUP_TIMEOUT_MS);
      const uint8_t presses = static_cast<uint8_t>(min(abs(steps) + (wake ? 1 : 0),
                                                       static_cast<int>(MAX_IR_FRAMES_PER_JOB)));
      const IrJob job = repeatedCode(IrDevice::AC,
          steps > 0 ? AC_CODE_TEMP_UP : AC_CODE_TEMP_DOWN, presses, 250);
      accepted = enqueueIrLocked(job);
      if (accepted) {
        state.targetTemperature = rounded;
        state.currentTemperature = rounded;
        state.lastAcTempAt = millis();
      }
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", "OK");
  });

  server.on("/targetHeatingCoolingState", HTTP_GET, [](AsyncWebServerRequest* request) {
    int newState;
    if (!parseIntParam(request, "value", newState)) {
      return request->send(400, "text/plain", "Missing or invalid value");
    }
    if (newState < 0 || newState > 3) {
      return request->send(422, "text/plain", "State must be 0..3");
    }

    int force = 0;
    parseIntParam(request, "force", force);

    bool accepted = true;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const bool currentlyOn = state.targetHeatingCoolingState != 0;
    const bool requestingOn = newState != 0;
    if (force != 0 || (currentlyOn != requestingOn)) {
      accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_POWER));
    }
    if (accepted) state.targetHeatingCoolingState = newState;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", "OK");
  });

  auto syncHandler = [](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    if (request->hasParam("temp")) {
      const float t = request->getParam("temp")->value().toFloat();
      if (t >= 16.0f && t <= 34.0f) {
        state.targetTemperature = roundf(t);
        state.currentTemperature = roundf(t);
        state.lastAcTempAt = millis();
      }
    }
    if (request->hasParam("state")) {
      const int s = request->getParam("state")->value().toInt();
      if (s >= 0 && s <= 3) state.targetHeatingCoolingState = s;
    }
    xSemaphoreGive(stateMutex);
    request->send(200, "text/plain", "OK");
  };
  server.on("/ac/sync_state", HTTP_GET, syncHandler);
  server.on("/set_state_memory", HTTP_GET, syncHandler);

  server.on("/send_raw_power", HTTP_GET, [](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const bool accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_POWER));
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", "OK");
  });

  server.on("/ac/swing", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    bool result;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_SWING));
    if (accepted) state.swing = !state.swing;
    result = state.swing;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", result ? "1" : "0");
  });
  server.on("/ac/swing/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", copyState().swing ? "1" : "0");
  });

  server.on("/ac/eco", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    bool result;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_ECO));
    if (accepted) state.eco = !state.eco;
    result = state.eco;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", result ? "1" : "0");
  });
  server.on("/ac/eco/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", copyState().eco ? "1" : "0");
  });

  server.on("/ac/mode", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    int result;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_MODE));
    if (accepted) state.acModeLevel = (state.acModeLevel % 3) + 1;
    result = state.acModeLevel;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(result));
  });

  server.on("/ac/fan", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    int result;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    accepted = enqueueIrLocked(oneCode(IrDevice::AC, AC_CODE_FAN));
    if (accepted) state.fanSpeedLevel = (state.fanSpeedLevel == 1) ? 3 : state.fanSpeedLevel - 1;
    result = state.fanSpeedLevel;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(result));
  });

  server.on("/ac/fan/active", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", "1");
  });

  server.on("/ac/fan/speed", HTTP_GET, [](AsyncWebServerRequest* request) {
    int pct;
    if (!parseIntParam(request, "value", pct)) {
      return request->send(400, "text/plain", "Missing or invalid value");
    }
    pct = constrain(pct, 0, 100);
    int targetLevel = 1;
    if (pct > 33 && pct <= 66) targetLevel = 2;
    else if (pct > 66) targetLevel = 3;

    bool accepted = true;
    int result;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const int presses = (state.fanSpeedLevel - targetLevel + 3) % 3;
    if (presses > 0) {
      accepted = enqueueIrLocked(repeatedCode(IrDevice::AC, AC_CODE_FAN, presses, 250));
    }
    if (accepted) state.fanSpeedLevel = targetLevel;
    result = state.fanSpeedLevel * 33;
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(result));
  });
  server.on("/ac/fan/speed/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", String(copyState().fanSpeedLevel * 33));
  });

  server.on("/ac/timer", HTTP_GET, [](AsyncWebServerRequest* request) {
    int pct;
    if (request->hasParam("value")) {
      if (!parseIntParam(request, "value", pct)) {
        return request->send(400, "text/plain", "Invalid value");
      }
      pct = constrain(pct, 0, 100);
      const float hours = roundf((pct * 24.0f / 100.0f) * 2.0f) / 2.0f;
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      if (hours > 0.0f) {
        state.acTimerDurationMs = static_cast<uint32_t>(hours * 3600000.0f);
        state.acTimerStartedAt = millis();
        state.acTimerActive = true;
      } else {
        state.acTimerActive = false;
      }
      xSemaphoreGive(stateMutex);
    }

    const ControllerState s = copyState();
    int responsePct = 0;
    if (s.acTimerActive) {
      const uint32_t elapsed = millis() - s.acTimerStartedAt;
      if (elapsed < s.acTimerDurationMs) {
        responsePct = static_cast<int>(((s.acTimerDurationMs - elapsed) / 3600000.0f) / 24.0f * 100.0f);
      }
    }
    request->send(200, "text/plain", String(responsePct));
  });

  server.on("/ac/timer/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    int responsePct = 0;
    if (s.acTimerActive) {
      const uint32_t elapsed = millis() - s.acTimerStartedAt;
      if (elapsed < s.acTimerDurationMs) {
        responsePct = static_cast<int>(((s.acTimerDurationMs - elapsed) / 3600000.0f) / 24.0f * 100.0f);
      }
    }
    request->send(200, "text/plain", String(responsePct));
  });

  server.on("/ac/timer/on", HTTP_GET, [](AsyncWebServerRequest* request) {
    if (!mutationsAllowed()) return sendBusy(request, "Controller");
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.acTimerDurationMs = 3600000UL;
    state.acTimerStartedAt = millis();
    state.acTimerActive = true;
    xSemaphoreGive(stateMutex);
    request->send(200, "text/plain", "1");
  });
  server.on("/ac/timer/off", HTTP_GET, [](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    state.acTimerActive = false;
    xSemaphoreGive(stateMutex);
    request->send(200, "text/plain", "0");
  });

  server.on("/led/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    String json = "{";
    json += "\"power\":" + String(s.ledPower ? "true" : "false") + ",";
    json += "\"brightness\":" + String(s.ledBrightness) + ",";
    json += "\"hue\":" + String(s.ledHue) + ",";
    json += "\"saturation\":" + String(s.ledSaturation) + ",";
    json += "\"mode\":" + String(s.ledModeLevel) + ",";
    json += "\"speed\":" + String(s.ledSpeedLevel);
    json += "}";
    request->send(200, "application/json", json);
  });

  server.on("/led/on", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    IrJob job{};
    job.device = IrDevice::LED;
    job.frameCount = 2;
    job.codes[0] = LED_ON;
    job.gapsAfterMs[0] = 250;
    job.codes[1] = ledModeCode(state.ledModeLevel, state.ledHue, state.ledSaturation);
    accepted = enqueueIrLocked(job);
    if (accepted) {
      state.ledPower = true;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", "1");
  });

  server.on("/led/off", HTTP_GET, [](AsyncWebServerRequest* request) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const bool accepted = enqueueIrLocked(oneCode(IrDevice::LED, LED_OFF));
    if (accepted) {
      state.ledPower = false;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", "0");
  });

  server.on("/led/hue", HTTP_GET, [](AsyncWebServerRequest* request) {
    int hue;
    if (!parseIntParam(request, "value", hue)) return request->send(400, "text/plain", "Missing or invalid value");
    hue = constrain(hue, 0, 360);
    bool accepted = true;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    if (state.ledPower) {
      accepted = enqueueIrLocked(oneCode(IrDevice::LED, ledColorCodeFor(hue, state.ledSaturation)));
    }
    if (accepted) {
      state.ledHue = hue;
      state.ledModeLevel = 1;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(hue));
  });

  server.on("/led/saturation", HTTP_GET, [](AsyncWebServerRequest* request) {
    int saturation;
    if (!parseIntParam(request, "value", saturation)) return request->send(400, "text/plain", "Missing or invalid value");
    saturation = constrain(saturation, 0, 100);
    bool accepted = true;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    if (state.ledPower) {
      accepted = enqueueIrLocked(oneCode(IrDevice::LED, ledColorCodeFor(state.ledHue, saturation)));
    }
    if (accepted) {
      state.ledSaturation = saturation;
      state.ledModeLevel = 1;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(saturation));
  });

  server.on("/led/brightness", HTTP_GET, [](AsyncWebServerRequest* request) {
    int target;
    if (!parseIntParam(request, "value", target)) return request->send(400, "text/plain", "Missing or invalid value");
    target = constrain(target, 0, 100);
    bool accepted = true;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    const int steps = (target - state.ledBrightness) / 10;
    if (steps != 0) {
      accepted = enqueueIrLocked(repeatedCode(IrDevice::LED,
          steps > 0 ? LED_BRI_UP : LED_BRI_DWN, abs(steps), 100));
    }
    if (accepted) {
      state.ledBrightness = target;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(target));
  });

  server.on("/led/mode", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted = true;
    int newMode;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    newMode = (state.ledModeLevel % 5) + 1;
    // Preserve legacy behavior: dynamic mode commands transmit even if tracked power is off;
    // returning to solid only transmits while the strip is tracked on.
    if (newMode != 1 || state.ledPower) {
      accepted = enqueueIrLocked(oneCode(IrDevice::LED,
          ledModeCode(newMode, state.ledHue, state.ledSaturation)));
    }
    if (accepted) {
      state.ledModeLevel = newMode;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(newMode));
  });
  server.on("/led/mode/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", String(copyState().ledModeLevel));
  });

  server.on("/led/speed", HTTP_GET, [](AsyncWebServerRequest* request) {
    bool accepted;
    int newLevel;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    newLevel = (state.ledSpeedLevel % 3) + 1;
    IrJob job{};
    job.device = IrDevice::LED;
    if (newLevel == 1) {
      job = repeatedCode(IrDevice::LED, LED_BRI_DWN, 4, 100);
    } else if (newLevel == 2) {
      job.frameCount = 6;
      for (uint8_t i = 0; i < 4; ++i) job.codes[i] = LED_BRI_DWN;
      job.codes[4] = LED_BRI_UP;
      job.codes[5] = LED_BRI_UP;
      for (uint8_t i = 0; i < 5; ++i) job.gapsAfterMs[i] = 100;
    } else {
      job = repeatedCode(IrDevice::LED, LED_BRI_UP, 4, 100);
    }
    accepted = enqueueIrLocked(job);
    if (accepted) {
      state.ledSpeedLevel = newLevel;
      state.lastLedCommandAt = millis();
    }
    xSemaphoreGive(stateMutex);
    if (!accepted) return sendBusy(request, "IR service");
    request->send(200, "text/plain", String(newLevel));
  });
  server.on("/led/speed/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    request->send(200, "text/plain", String(copyState().ledSpeedLevel));
  });

  server.on("/led/timer", HTTP_GET, [](AsyncWebServerRequest* request) {
    int pct;
    if (request->hasParam("value")) {
      if (!parseIntParam(request, "value", pct)) return request->send(400, "text/plain", "Invalid value");
      pct = constrain(pct, 0, 100);
      const float minutes = pct * 1.2f;
      xSemaphoreTake(stateMutex, portMAX_DELAY);
      if (minutes > 0.0f) {
        state.ledTimerDurationMs = static_cast<uint32_t>(minutes * 60000.0f);
        state.ledTimerStartedAt = millis();
        state.ledTimerActive = true;
      } else {
        state.ledTimerActive = false;
      }
      xSemaphoreGive(stateMutex);
    }
    const ControllerState s = copyState();
    int responsePct = 0;
    if (s.ledTimerActive) {
      const uint32_t elapsed = millis() - s.ledTimerStartedAt;
      if (elapsed < s.ledTimerDurationMs) {
        responsePct = static_cast<int>(((s.ledTimerDurationMs - elapsed) / 60000.0f) / 120.0f * 100.0f);
      }
    }
    request->send(200, "text/plain", String(responsePct));
  });
  server.on("/led/timer/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    int responsePct = 0;
    if (s.ledTimerActive) {
      const uint32_t elapsed = millis() - s.ledTimerStartedAt;
      if (elapsed < s.ledTimerDurationMs) {
        responsePct = static_cast<int>(((s.ledTimerDurationMs - elapsed) / 60000.0f) / 120.0f * 100.0f);
      }
    }
    request->send(200, "text/plain", String(responsePct));
  });

  server.on("/lamps/status", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    String json = "{";
    json += "\"main\":" + String(s.lampMain ? "true" : "false") + ",";
    json += "\"fairy\":" + String(s.lampFairy ? "true" : "false") + ",";
    json += "\"fan\":" + String(s.lampFan ? "true" : "false") + ",";
    json += "\"backlight\":" + String(s.lampBack ? "true" : "false");
    json += "}";
    request->send(200, "application/json", json);
  });

  registerRelayRoutes("/lamp/main", "/lamp/main/status", RelayId::MAIN);
  registerRelayRoutes("/lamp/fairy", "/lamp/fairy/status", RelayId::FAIRY);
  registerRelayRoutes("/lamp/fan", "/lamp/fan/status", RelayId::FAN);
  registerRelayRoutes("/lamp/backlight", "/lamp/backlight/status", RelayId::BACKLIGHT);

  server.on("/diagnostics", HTTP_GET, [](AsyncWebServerRequest* request) {
    const ControllerState s = copyState();
    const uint32_t now = millis();
    uint32_t retryIn = 0;
    if (s.staState == StaState::BACKOFF && !deadlineReached(now, s.retryAt)) retryIn = s.retryAt - now;

    String json;
    json.reserve(700);
    json += "{";
    json += "\"uptimeMs\":" + String(now) + ",";
    json += "\"resetReason\":" + String(static_cast<int>(esp_reset_reason())) + ",";
    json += "\"freeHeap\":" + String(ESP.getFreeHeap()) + ",";
    json += "\"minFreeHeap\":" + String(ESP.getMinFreeHeap()) + ",";
    json += "\"maxAllocHeap\":" + String(ESP.getMaxAllocHeap()) + ",";
    json += "\"staState\":\"" + String(staStateName(s.staState)) + "\",";
    json += "\"staIp\":\"" + ipString(s.staIp) + "\",";
    json += "\"rssi\":" + String(s.staState == StaState::CONNECTED ? s.rssi : 0) + ",";
    json += "\"staAttempts\":" + String(s.staAttempts) + ",";
    json += "\"lastDisconnectReason\":" + String(s.lastDisconnectReason) + ",";
    json += "\"retryInMs\":" + String(retryIn) + ",";
    json += "\"apRunning\":" + String(s.apRunning ? "true" : "false") + ",";
    json += "\"apIp\":\"" + ipString(s.apIp) + "\",";
    json += "\"apClients\":" + String(WiFi.softAPgetStationNum()) + ",";
    json += "\"irQueueDepth\":" + String(uxQueueMessagesWaiting(irQueue)) + ",";
    json += "\"irAccepted\":" + String(s.irJobsAccepted) + ",";
    json += "\"irCompleted\":" + String(s.irJobsCompleted) + ",";
    json += "\"irRejected\":" + String(s.irJobsRejected) + ",";
    json += "\"relayQueueDepth\":" + String(uxQueueMessagesWaiting(relayQueue)) + ",";
    json += "\"relayRejected\":" + String(s.relayCommandsRejected) + ",";
    json += "\"otaActive\":" + String(s.otaActive ? "true" : "false") + ",";
    json += "\"otaProgress\":" + String(s.otaProgress) + ",";
    json += "\"otaLastError\":" + String(s.otaLastError);
    json += "}";
    request->send(200, "application/json", json);
  });

  server.onNotFound([](AsyncWebServerRequest* request) {
    request->send(404, "text/plain", "Not found");
  });
}

// ---------------- Arduino entry points ----------------

void setup() {
  Serial.begin(115200);
  Serial.printf("\nBoot: resetReason=%d freeHeap=%u\n", static_cast<int>(esp_reset_reason()), ESP.getFreeHeap());

  setCpuFrequencyMhz(160);

  digitalWrite(LED_PIN, LOW);
  pinMode(LED_PIN, OUTPUT);

  // Apply the inactive relay level before enabling outputs to minimize boot glitches.
  digitalWrite(relayMain, HIGH);
  digitalWrite(relayFairy, HIGH);
  digitalWrite(relayFan, HIGH);
  digitalWrite(relayBack, HIGH);
  pinMode(relayMain, OUTPUT);
  pinMode(relayFairy, OUTPUT);
  pinMode(relayFan, OUTPUT);
  pinMode(relayBack, OUTPUT);

  stateMutex = xSemaphoreCreateMutex();
  irQueue = xQueueCreate(16, sizeof(IrJob));
  relayQueue = xQueueCreate(16, sizeof(RelayCommand));
  systemEvents = xEventGroupCreate();

  if (!stateMutex || !irQueue || !relayQueue || !systemEvents) {
    Serial.println("FATAL: failed to allocate RTOS primitives; halting safely");
    for (;;) vTaskDelay(portMAX_DELAY);
  }

  registerRoutes();
  server.begin();
  Serial.println("HTTP: asynchronous server started");

  if (xTaskCreatePinnedToCore(networkTask, "network", 6144, nullptr, 3, &networkTaskHandle, 0) != pdPASS ||
      xTaskCreatePinnedToCore(irWorkerTask, "ir-worker", 4096, nullptr, 2, nullptr, 1) != pdPASS ||
      xTaskCreatePinnedToCore(controllerTask, "controller", 6144, nullptr, 2, nullptr, 1) != pdPASS ||
      xTaskCreatePinnedToCore(otaServiceTask, "ota", 4096, nullptr, 1, nullptr, 1) != pdPASS) {
    Serial.println("FATAL: failed to create service tasks; restarting");
    ESP.restart();
  }
}

void loop() {
  // All services are event-driven tasks. The Arduino loop task sleeps forever.
  ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
}
