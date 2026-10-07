#include <WiFi.h>
#include <esp_wifi.h>
#include <nvs_flash.h>

const char* ssid = "Gold";

// Candidate passwords to test
const char* candidates[] = {
  "017265G!!",
  "017265g!!",
  "017265G!",
  "017265g!",
  "017265G",
  "017265g"
};
const size_t NUM_CANDIDATES = sizeof(candidates) / sizeof(candidates[0]);

const char* getAuthModeName(wifi_auth_mode_t auth) {
  switch (auth) {
    case WIFI_AUTH_OPEN: return "OPEN";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA_PSK";
    case WIFI_AUTH_WPA2_PSK: return "WPA2_PSK";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA_WPA2_PSK";
    case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2_ENTERPRISE";
    case WIFI_AUTH_WPA3_PSK: return "WPA3_PSK";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2_WPA3_PSK";
    case WIFI_AUTH_WAPI_PSK: return "WAPI_PSK";
    case WIFI_AUTH_OWE: return "OWE";
    default: return "OTHER";
  }
}

volatile bool connected = false;
volatile int lastDisconnectReason = 0;

void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_START:
      Serial.println("[WiFi Event] Station Mode Started");
      break;
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      Serial.println("[WiFi Event] >>> Connected to AP! Negotiating IP... <<<");
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      connected = true;
      Serial.println("\n========================================");
      Serial.println("  >>> WI-FI SUCCESS! GOT IP ADDRESS! <<<");
      Serial.printf("  IP Address: %s\n", WiFi.localIP().toString().c_str());
      Serial.printf("  Subnet:     %s\n", WiFi.subnetMask().toString().c_str());
      Serial.printf("  Gateway:    %s\n", WiFi.gatewayIP().toString().c_str());
      Serial.printf("  DNS:        %s\n", WiFi.dnsIP().toString().c_str());
      Serial.printf("  RSSI:       %d dBm\n", WiFi.RSSI());
      Serial.println("========================================\n");
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      lastDisconnectReason = info.wifi_sta_disconnected.reason;
      Serial.printf("[WiFi Event] Disconnected! Reason %d: %s\n",
                    lastDisconnectReason,
                    WiFi.disconnectReasonName((wifi_err_reason_t)lastDisconnectReason));
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n\n========================================");
  Serial.println("       ESP32 WI-FI CONNECTION TEST");
  Serial.println("========================================");

  nvs_flash_erase();
  nvs_flash_init();

  WiFi.persistent(false);
  WiFi.disconnect(true, true);
  delay(500);

  WiFi.onEvent(onWiFiEvent);
  WiFi.mode(WIFI_STA);
  delay(200);

  Serial.printf("ESP32 Station MAC: %s\n", WiFi.macAddress().c_str());

  // Force HT20 and 802.11 b/g/n
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  WiFi.setSleep(false);

  // Scan visible networks
  Serial.println("\n[Scan] Scanning 2.4GHz WiFi networks...");
  int n = WiFi.scanNetworks();
  Serial.printf("[Scan] Found %d networks:\n", n);
  for (int i = 0; i < n; ++i) {
    Serial.printf("  %2d: %-18s | Ch: %2d | RSSI: %3d dBm | Auth: %s\n",
                  i + 1,
                  WiFi.SSID(i).c_str(),
                  WiFi.channel(i),
                  WiFi.RSSI(i),
                  getAuthModeName(WiFi.encryptionType(i)));
  }
  WiFi.scanDelete();

  // Test passwords and configurations
  for (size_t c = 0; c < NUM_CANDIDATES; c++) {
    const char* pwd = candidates[c];
    Serial.printf("\n---> [TEST %d/%d] Trying SSID: '%s' with Password: '%s' <---\n",
                  (int)(c + 1), (int)NUM_CANDIDATES, ssid, pwd);

    // Try normal power first, then lower power
    wifi_power_t powers[] = { WIFI_POWER_19_5dBm, WIFI_POWER_8_5dBm };
    for (size_t p = 0; p < 2; p++) {
      if (p == 1) {
        Serial.println("  (Retrying with reduced TX power 8.5 dBm to avoid AP receiver saturation)");
      }
      WiFi.setTxPower(powers[p]);
      WiFi.disconnect(true, false);
      delay(300);
      lastDisconnectReason = 0;

      WiFi.begin(ssid, pwd);

      unsigned long start = millis();
      while (!connected && (millis() - start < 8000UL)) {
        delay(500);
        Serial.print(".");
      }
      Serial.println();

      if (connected) {
        Serial.printf("\n>>> MATCH FOUND! Connected with password: '%s' (Power: %s) <<<\n",
                      pwd, p == 0 ? "19.5dBm" : "8.5dBm");
        return;
      }

      Serial.printf("  Attempt finished. Status: %d, Last Disconnect Reason: %d (%s)\n",
                    WiFi.status(),
                    lastDisconnectReason,
                    WiFi.disconnectReasonName((wifi_err_reason_t)lastDisconnectReason));

      // If disconnect reason is NOT auth expire (e.g. timeout / no ap), don't waste time on power retry
      if (lastDisconnectReason != 2 && lastDisconnectReason != 0) {
        break;
      }
    }
  }

  Serial.println("\n[SUMMARY] All candidate attempts exhausted.");
  Serial.println("If all failed with AUTH_EXPIRE (Reason 2), the AP router is explicitly rejecting this MAC");
  Serial.println("or has MAC filtering enabled, or the password differs from all tested variants.");
}

void loop() {
  if (connected) {
    Serial.printf("[ALIVE] IP: %s | RSSI: %d dBm | Heap: %u\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI(), ESP.getFreeHeap());
  } else {
    Serial.println("[IDLE] Not connected to WiFi.");
  }
  delay(3000);
}
