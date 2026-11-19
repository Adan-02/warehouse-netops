#include <Arduino.h>
#include <DHTesp.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <cmath>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#ifndef WAREHOUSE_WOKWI
#define WAREHOUSE_WOKWI 0
#endif

#ifndef WAREHOUSE_ALLOW_OPEN_WIFI
#define WAREHOUSE_ALLOW_OPEN_WIFI 0
#endif

#if WAREHOUSE_WOKWI
#if __has_include("warehouse_wokwi_config.h")
#include "warehouse_wokwi_config.h"
#else
#include "warehouse_wokwi_config.example.h"
#endif
#elif __has_include("warehouse_config.h")
#include "warehouse_config.h"
#else
#include "warehouse_config.example.h"
#endif

#ifndef WAREHOUSE_SIMULATED_SENSOR
#define WAREHOUSE_SIMULATED_SENSOR 0
#endif

namespace {
constexpr uint32_t kSampleIntervalMs = 5000;
constexpr uint32_t kWifiRetryMs = 15000;
constexpr uint32_t kMqttRetryMs = 5000;

struct Reading {
    uint32_t seq;
    float temperature;
    float humidity;
    uint32_t capturedAt;
};

DHTesp sensor;
QueueHandle_t readings = nullptr;
uint32_t sequence = 0;
uint32_t lastSampleStarted = 0;

bool elapsed(uint32_t now, uint32_t since, uint32_t duration) {
    return static_cast<uint32_t>(now - since) >= duration;
}

bool configured() {
    const bool permittedOpenNetwork = WAREHOUSE_WOKWI && WAREHOUSE_ALLOW_OPEN_WIFI &&
                                      std::strcmp(WAREHOUSE_WIFI_SSID, "Wokwi-GUEST") == 0 &&
                                      std::strlen(WAREHOUSE_WIFI_PASSWORD) == 0;
    return std::strstr(WAREHOUSE_WIFI_PASSWORD, "REPLACE_") == nullptr &&
           std::strstr(WAREHOUSE_MQTT_PASSWORD, "REPLACE_") == nullptr &&
           (std::strlen(WAREHOUSE_WIFI_PASSWORD) >= 8 || permittedOpenNetwork) &&
           std::strlen(WAREHOUSE_MQTT_PASSWORD) > 0;
}

const char *sensorMode() {
    if (WAREHOUSE_WOKWI) {
        return "WOKWI-DHT22";
    }
    return WAREHOUSE_SIMULATED_SENSOR ? "SIMULATED" : "DHT22";
}

void queueReading(float temperature, float humidity) {
    if (!std::isfinite(temperature) || !std::isfinite(humidity) ||
        temperature < -40.0f || temperature > 80.0f || humidity < 0.0f || humidity > 100.0f) {
        Serial.println("Invalid DHT22 pair; no telemetry published.");
        return;
    }
    // Sequence numbers count valid captures, including captures while offline.
    ++sequence;
    if (sequence == 0) {
        sequence = 1;
    }
    const Reading reading{sequence, temperature, humidity, millis()};
    xQueueOverwrite(readings, &reading);
    Serial.printf("Captured seq=%lu temperature_c=%.2f humidity_pct=%.2f mode=%s\n",
                  static_cast<unsigned long>(sequence), temperature, humidity,
                  sensorMode());
}

void networkTask(void *) {
    // This task exclusively owns Wi-Fi and MQTT. MQTT's bounded socket waits
    // therefore never stop the sensor acquisition loop.
    WiFiClient socket;
    socket.setTimeout(2000);
    PubSubClient mqtt(socket);
    mqtt.setServer(WAREHOUSE_MQTT_HOST, WAREHOUSE_MQTT_PORT);
    mqtt.setSocketTimeout(2);
    mqtt.setKeepAlive(15);
    mqtt.setBufferSize(256);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.persistent(false);

    uint32_t lastWifiAttempt = millis() - kWifiRetryMs;
    uint32_t lastMqttAttempt = millis() - kMqttRetryMs;
    Reading pending{};
    bool havePending = false;

    for (;;) {
        if (xQueueReceive(readings, &pending, 0) == pdTRUE) {
            havePending = true;
        }
        const uint32_t now = millis();
        if (!configured()) {
            Serial.printf("Set include/%s credentials before connecting.\n",
                          WAREHOUSE_WOKWI ? "warehouse_wokwi_config.h" : "warehouse_config.h");
            vTaskDelay(pdMS_TO_TICKS(10000));
            continue;
        }
        if (WiFi.status() != WL_CONNECTED) {
            if (mqtt.connected()) {
                mqtt.disconnect();
            }
            if (elapsed(now, lastWifiAttempt, kWifiRetryMs)) {
                lastWifiAttempt = now;
                Serial.println("Connecting Wi-Fi...");
                if (WAREHOUSE_WOKWI) {
                    WiFi.begin(WAREHOUSE_WIFI_SSID, WAREHOUSE_WIFI_PASSWORD, 6);
                } else {
                    WiFi.begin(WAREHOUSE_WIFI_SSID, WAREHOUSE_WIFI_PASSWORD);
                }
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (!mqtt.connected() && elapsed(now, lastMqttAttempt, kMqttRetryMs)) {
            lastMqttAttempt = now;
            // Client ID differs from the MQTT username to avoid colliding with
            // a bench publisher that uses the same per-device credentials.
            String clientId = String(WAREHOUSE_DEVICE_ID) + "-" + WiFi.macAddress();
            if (mqtt.connect(clientId.c_str(), WAREHOUSE_MQTT_USERNAME,
                             WAREHOUSE_MQTT_PASSWORD)) {
                Serial.println("MQTT connected.");
            } else {
                Serial.printf("MQTT unavailable; state=%d\n", mqtt.state());
            }
        }
        if (mqtt.connected()) {
            mqtt.loop();
            // A connect attempt may have waited while the acquisition task
            // captured a newer value. Prefer that value on reconnection.
            if (xQueueReceive(readings, &pending, 0) == pdTRUE) {
                havePending = true;
            }
            if (havePending && elapsed(millis(), pending.capturedAt, 2 * kSampleIntervalMs)) {
                havePending = false;
            }
            if (havePending) {
                char payload[192];
                const int length = snprintf(
                    payload, sizeof(payload),
                    "{\"device_id\":\"%s\",\"seq\":%lu,\"temperature_c\":%.2f,\"humidity_pct\":%.2f}",
                    WAREHOUSE_DEVICE_ID, static_cast<unsigned long>(pending.seq),
                    pending.temperature, pending.humidity);
                if (length > 0 && static_cast<size_t>(length) < sizeof(payload) &&
                    mqtt.publish(WAREHOUSE_MQTT_TOPIC, payload, false)) {
                    havePending = false;
                    Serial.printf("Published seq=%lu\n", static_cast<unsigned long>(pending.seq));
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(25));
    }
}
}  // namespace

void setup() {
    Serial.begin(115200);
    readings = xQueueCreate(1, sizeof(Reading));
    if (readings == nullptr) {
        Serial.println("Cannot allocate telemetry queue.");
        abort();
    }
    Serial.printf("Warehouse cabinet-01 firmware; sensor mode=%s\n",
                  sensorMode());
    if (!WAREHOUSE_SIMULATED_SENSOR) {
        sensor.setup(WAREHOUSE_SENSOR_PIN, DHTesp::DHT22);
    }
    if (xTaskCreate(networkTask, "warehouse-network", 6144, nullptr, 1, nullptr) != pdPASS) {
        Serial.println("Cannot create network task.");
        abort();
    }
    lastSampleStarted = millis() - kSampleIntervalMs;
}

void loop() {
    const uint32_t now = millis();
    if (elapsed(now, lastSampleStarted, kSampleIntervalMs)) {
        lastSampleStarted = now;
        if (WAREHOUSE_SIMULATED_SENSOR) {
            const float phase = static_cast<float>(now) / 60000.0f;
            queueReading(24.0f + 0.5f * std::sin(phase), 45.0f + 5.0f * std::sin(phase / 2.0f));
        } else {
            const TempAndHumidity pair = sensor.getTempAndHumidity();
            if (sensor.getStatus() != DHTesp::ERROR_NONE) {
                Serial.printf("DHT22 read failed (%s); no telemetry published.\n", sensor.getStatusString());
            } else {
                queueReading(pair.temperature, pair.humidity);
            }
        }
    }
    delay(5);
}
