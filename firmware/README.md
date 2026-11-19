# ESP32 机柜温湿度采集端

目标板为普通 ESP32 DevKit / ESP32-WROOM-32，PlatformIO 的 `esp32dev` 板定义。ESP32-C3/S3 需要另选板定义和检查引脚。

## 接线

使用 DHT22（AM2302）温湿度采集器。VCC 接 ESP32 的 3V3，GND 接 GND，DATA 接 GPIO4，在 DATA 和 3V3 之间接 4.7 kΩ 上拉电阻。四脚裸器件的 NC 留空。带上拉电阻的模块按模块标注接线，确认 DATA 上拉到 3.3 V。购买后记录准确型号、引脚标识及模块是否已经带上拉。

## 配置与编译

1. 安装 PlatformIO Core 或 VS Code PlatformIO 扩展。
2. 将 `include/warehouse_config.example.h` 复制为 `include/warehouse_config.h`。
3. 填入 Warehouse-IoT 的 Wi-Fi 密码，以及服务端初始化后 `.env` 中的 `MQTT_DEVICE_PASSWORD`。用户名为 `cabinet-01`。配置文件只存本地。
4. 在本目录运行 `pio run -e esp32dev`。
5. 连接 USB 后用 `pio device list` 确认端口，再执行 `pio run -e esp32dev -t upload --upload-port COMx`。
6. 执行 `pio device monitor --port COMx --baud 115200`，观察 Captured 和 Published 记录。

Windows 下若 Xtensa 编译器报 `CreateProcess: No such file or directory`，先确认完整工具链已安装。过长的项目或工具缓存路径可能使编译命令超过 Windows 长度限制，可将项目和 `PLATFORMIO_CORE_DIR` 放到较短路径后重试。

当前工作区已在 `.cache/pio-venv` 准备 PlatformIO Core 6.1.19，工具链缓存位于 `.cache/platformio`。在项目根目录可直接执行以下命令。这些环境和缓存属于本机开发资料，公开源码包可以重新安装依赖。

```powershell
$env:PLATFORMIO_CORE_DIR = "$PWD/.cache/platformio"
& './.cache/pio-venv/Scripts/python.exe' -m platformio run -d firmware -e esp32dev -e esp32dev-simulated -e esp32dev-wokwi
```

没有 `warehouse_config.h` 也可以编译，固件会使用示例配置并在串口提示补全凭据，然后暂停连接。构建和刷写都不会回显凭据。生成的固件包含本地凭据，应按本地敏感文件保管。

依赖固定为 Espressif32 6.10.0、PubSubClient 2.8、DHT sensor library for ESPx（DHTesp）1.19.0。ESP32 的 DHT22 仿真采用 Wokwi 推荐的 DHTesp 驱动。[Wokwi DHT22 文档](https://docs.wokwi.com/parts/wokwi-dht22)

## 数据接口

默认目标为 `192.168.30.10:1883`，从 IoT 分区通过路由访问。主题为 `warehouse/cabinet-01/telemetry`。

```json
{"device_id":"cabinet-01","seq":1,"temperature_c":24.75,"humidity_pct":45.5}
```

每 5 秒读取一次有效的温湿度对。温度范围为 −40 至 80°C，湿度为 0–100 %RH，两个读数都有效才生成一条消息。seq 对每个有效采样递增，重启后重新从 1 开始。网络任务独立运行，连接失败按定时重试，传感器采集可继续。队列只保留最新样本，等待超过 10 秒的旧样本被丢弃，离线期间的完整历史不会补传。消息使用 QoS 0、retain=false。

传感器未连接、读取失败、NaN 或任一读数越界时，只输出串口错误。看板将在无有效消息 20 秒后显示采集离线。重新接好传感器后，下一次成功采样会恢复发布。

## 明确的模拟模式

`pio run -e esp32dev-simulated` 构建台架测试版本，串口启动信息和每条采样均标明 SIMULATED，生成 24°C、45 %RH 左右的测试值。此构建用于没有采集器时验证 Wi-Fi → MQTT → 看板的数据链路。传到看板的主题和协议保持一致，演示时需把服务端运行标签标成模拟数据。实物温湿度验收使用默认 `esp32dev` 构建。

Wokwi 电路仿真入口为 `diagram.json` 和 `wokwi.toml`，构建环境为 `esp32dev-wokwi`，其中运行 DHT22 驱动并使用温度、湿度两个滑块。配置和网关条件见 [WOKWI.md](WOKWI.md)。

## 实物验收

- 室温下每 5 秒收到一条新消息，包含温度和湿度，seq 增长。
- 改变采集器附近环境，观察两个读数，并与参考温湿度计对照记录。
- 拔掉采集器后没有新的虚构读数，20 秒后看板标离线。
- 关闭并恢复 IoT Wi-Fi，串口采样继续，重连后恢复发送最新有效读数。
- 重启 ESP32，seq 归 1，服务端按接收时间接受新样本。
- 使用真实路由器确认 ESP32 可发布而无法以同一账号读取或写入其他设备主题。
