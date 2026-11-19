"""The fixed, isolated warehouse topology; not a generic target selector."""

ADDRESSES = {"office": "192.168.10.100", "iot": "192.168.20.100", "service": "192.168.30.10"}
GATEWAYS = {"office": "192.168.10.1", "iot": "192.168.20.1", "service": "192.168.30.1"}
PORTS = (22, 1883, 8080)
POLICY = {"office": {22: False, 1883: False, 8080: True}, "iot": {22: False, 1883: True, 8080: False}}
NOT_VERIFIED = ["authenticated MQTT publish from IoT", "WireGuard/Guangzhou", "WAN-disconnect fault", "broker-stop fault", "Office-to-IoT client isolation", "physical Wi-Fi and ESP32"]

OFFICE_IP = ADDRESSES['office']
CLOUD_IP = "10.89.0.1"
SERVICE_IP = ADDRESSES['service']
SERVICE_PORT = 8080
STACK = "cd /opt/warehouse-netops/server && sudo -n docker compose --project-name warehouse-netops "
PUBLIC_FIELDS = (
    "device_id", "broker_connected", "data_status", "demo_mode", "seq",
    "temperature_c", "humidity_pct", "age_seconds", "offline_after_seconds", "last_seen_at",
)

STAGING_ADDRESSES={'service':'192.168.200.60','iot':'192.168.200.61','office':'192.168.200.62'}
ROUTER_WAN_IP='192.168.200.50'
NAT_GATEWAY='192.168.200.1'
ROUTER_VPN_IP='10.89.0.2'
MANAGEMENT_IP='10.77.0.1'
MANAGEMENT_USER='qqops'
MACS={
    'router':['00:50:56:21:10:01','00:50:56:21:10:02','00:50:56:21:10:03','00:50:56:21:10:04'],
    'service':['00:50:56:21:20:01'],
    'iot':['00:50:56:21:20:02'],
    'office':['00:50:56:21:20:03'],
}
