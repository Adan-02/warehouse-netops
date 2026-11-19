#pragma once

// Copy this file to warehouse_config.h, then replace the two passwords locally.
#define WAREHOUSE_WIFI_SSID "Warehouse-IoT"
#define WAREHOUSE_WIFI_PASSWORD "REPLACE_WITH_IOT_WIFI_PASSWORD"
#define WAREHOUSE_MQTT_HOST "192.168.30.10"
#define WAREHOUSE_MQTT_PORT 1883
#define WAREHOUSE_MQTT_USERNAME "cabinet-01"
#define WAREHOUSE_MQTT_PASSWORD "REPLACE_WITH_MQTT_DEVICE_PASSWORD"
#define WAREHOUSE_DEVICE_ID "cabinet-01"
#define WAREHOUSE_MQTT_TOPIC "warehouse/cabinet-01/telemetry"
// DHT22 DATA to GPIO4; VCC and data pull-up use 3.3V.
#define WAREHOUSE_SENSOR_PIN 4
