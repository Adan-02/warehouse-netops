#!/bin/sh
set -eu
umask 077
: "${MQTT_DEVICE_PASSWORD:?missing device password}"
: "${MQTT_VIEWER_PASSWORD:?missing dashboard password}"
test "$MQTT_DEVICE_PASSWORD" != "$MQTT_VIEWER_PASSWORD"
mkdir -p /tmp/warehouse-broker /mosquitto/data
printf 'cabinet-01:%s\nwarehouse-dashboard:%s\n' \
  "$MQTT_DEVICE_PASSWORD" "$MQTT_VIEWER_PASSWORD" > /tmp/warehouse-broker/passwords
mosquitto_passwd -U /tmp/warehouse-broker/passwords
cat > /tmp/warehouse-broker/mosquitto.conf <<'CONF'
user mosquitto
listener 1883 0.0.0.0
allow_anonymous false
password_file /tmp/warehouse-broker/passwords
acl_file /etc/warehouse/acl
persistence true
persistence_location /mosquitto/data/
autosave_interval 30
log_dest stdout
log_type error
log_type warning
log_type notice
connection_messages true
CONF
chmod 755 /tmp/warehouse-broker
chmod 644 /tmp/warehouse-broker/passwords /tmp/warehouse-broker/mosquitto.conf
chown -R mosquitto:mosquitto /mosquitto/data
exec /usr/sbin/mosquitto -c /tmp/warehouse-broker/mosquitto.conf
