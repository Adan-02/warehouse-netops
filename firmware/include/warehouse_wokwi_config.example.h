#pragma once

// This config is only used by the explicit esp32dev-wokwi environment.
// Copy to warehouse_wokwi_config.h for a LOCAL licensed private-gateway test.
// Do not upload a file or firmware containing local broker credentials.
#define WAREHOUSE_WIFI_SSID "Wokwi-GUEST"
#define WAREHOUSE_WIFI_PASSWORD ""
#define WAREHOUSE_MQTT_HOST "host.wokwi.internal"
#define WAREHOUSE_MQTT_PORT 1883
#define WAREHOUSE_MQTT_USERNAME "cabinet-01"
#define WAREHOUSE_MQTT_PASSWORD "REPLACE_WITH_LOCAL_TEST_MQTT_PASSWORD"
#define WAREHOUSE_DEVICE_ID "cabinet-01"
#define WAREHOUSE_MQTT_TOPIC "warehouse/cabinet-01/telemetry"
// Matches environment:SDA in diagram.json.
#define WAREHOUSE_SENSOR_PIN 4
