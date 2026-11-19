# 仿真与模拟路线

## 优先验证本地消息链路

```powershell
python tools/lab.py init --demo
python tools/lab.py up
python tools/lab.py demo --count 12
```

发送端每 5 秒产生一组模拟温湿度。接收端使用真实的本地 MQTT Broker、账号权限、SQLite 和看板。停止发送可测 20 秒离线状态，停止 Broker 可测消息服务断开。软件集成结果见 `evidence/software-integration.json`。

## MQTTX Desktop 虚拟设备

MQTTX 支持定时发送和自定义 Payload 脚本，可作为可视化的虚拟传感器。下载入口和脚本行为见官方文档。

- [MQTTX](https://mqttx.app/)
- [定时消息与脚本](https://mqttx.app/docs/advanced)

先运行本地 Compose，在 MQTTX Desktop 中创建设备连接。

| 字段 | 内容 |
| --- | --- |
| Host | `mqtt://127.0.0.1`，或实际实验服务主机 IP |
| Port | `1883` |
| Client ID | `cabinet-01-simulator`，与固件/看板 ID 分开 |
| Username | `cabinet-01` |
| Password | 从本机 `server/.env` 复制 `MQTT_DEVICE_PASSWORD` |
| MQTT Version | `5.0` |
| Topic | `warehouse/cabinet-01/telemetry` |
| QoS | `1` |
| Retain | 关闭 |
| Payload | JSON，初始 `{}` |

将 `examples/mqttx-temperature.js` 复制到 Script → Function，保存并选择用于 Publish。开启 Timed messages，每 5 秒发送一次。脚本用定时发送的 index 生成递增 seq。设备角色只允许发布，观察消息请用另一个只读账号 `warehouse-dashboard` 连接并订阅精确主题。

此脚本按 MQTTX 官方 execute(handlePayload) 接口编写，并验证转换函数输出。此环境没有安装 MQTTX Desktop，GUI 导入和定时按钮操作仍待本机实际运行。命令行模拟已完成相同 Payload 的全链路测试。

## Wokwi 虚拟 ESP32 和温湿度探头

Wokwi 支持 ESP32 Wi-Fi 和 DHT22。运行时可以拖动温度与湿度两个滑块，直接观察固件串口输出。`firmware/` 包含接线图与本地 Wokwi 配置。

- [DHT22 与温湿度滑块](https://docs.wokwi.com/parts/wokwi-dht22)
- [ESP32 Wi-Fi 与网关边界](https://docs.wokwi.com/guides/esp32-wifi)
- [VS Code 项目配置](https://docs.wokwi.com/vscode/project-config)

网页公共网关可以访问互联网，不能访问本地 MQTT。官方私有网关能访问本机/局域网，网页私有网关为付费用户提供。VS Code 版本也有许可要求，使用前确认自己的许可。源码包没有包含许可或购买步骤。

Wokwi 中 ESP32 的 localhost 指向虚拟芯片，要访问电脑需使用 `host.wokwi.internal` 并启用可用的私有/本地网关。即使虚拟芯片能连接 MQTT，也要确认流量是否真正经过 OpenWrt 的 IoT 区，再判断能否用于防火墙验收。在线公开分享工程前移除本地真实凭据。

## 网络分区与 VPN 仿真

GNS3 可把 QEMU 虚拟机、Docker 测试终端组成可抓包、断链的拓扑。OpenWrt x86 虚拟机可用于路由/防火墙/WireGuard 路径实验，办公网、IoT 网和服务区在此阶段对应独立虚拟以太网。

- [官方 OpenWrt 四网卡 appliance](https://github.com/GNS3/gns3-registry/blob/master/appliances/openwrt.gns3a)
- [GNS3 导入 appliance](https://docs.gns3.com/docs/using-gns3/beginners/import-gns3-appliance)
- [GNS3 Docker 测试节点](https://docs.gns3.com/docs/emulators/docker-support-in-gns3)

官方模板可按确认后的网卡顺序映射服务区、WAN、办公网和 IoT 网。Windows 通常用 GNS3 VM 承载 QEMU 节点，具体镜像、虚拟网卡顺序和运行能力仍需本机验证。

当前 `router/lab-router.sh` 面向带实际 2.4 GHz radio 的路由器，不能直接用于无无线硬件的 OpenWrt x86 VM。虚拟机版本需要显式配置两个虚拟 Ethernet 接口，跳过 radio 检查。虚拟网络验证与实体 Wi-Fi 关联、信号、射频干扰分别验收。

## 按项目阶段替换

| 阶段 | 设备数据源 | 网络 | 验证目标 |
| --- | --- | --- | --- |
| 立即运行 | 本地 Python 发布器或 MQTTX | Docker 本机网络 | 消息鉴权、主题权限、离线状态、服务重启 |
| 固件仿真 | Wokwi ESP32 + DHT22 | Wokwi 虚拟 Wi-Fi，局域网访问取决于网关许可 | 固件读取、消息格式、重连逻辑 |
| 网络仿真 | MQTT 测试终端 | GNS3 / OpenWrt x86 + 独立虚拟网段 | 路由、访问矩阵、VPN、断链 |
| 最终实物 | ESP32 + DHT22 | 实体 OpenWrt 两个 SSID | 无线接入、现场持续采集与真实断 WAN |

目前采用本地 Mosquitto 运行 Broker。EMQX 等产品也可充当 Broker，但换 Broker 不会自动产生传感数据。每种软件的角色与测试证据应在论文中说清。
