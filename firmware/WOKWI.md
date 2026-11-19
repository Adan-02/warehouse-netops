# Wokwi ESP32 与 DHT22 温湿度仿真

`diagram.json` 包含 ESP32 DevKit v1、GPIO4 上的 DHT22，以及 DATA 到 3V3 的 4.7 kΩ 上拉电阻。Wokwi 运行实际 DHT22 驱动分支，温度和湿度由仿真器中的两个滑块改变。它与生成测试值的 `esp32dev-simulated` 环境独立。

## 先运行采集与串口

1. 在有自己有效 Wokwi VS Code 许可证的环境中，以本目录为工作区打开项目。扩展和许可证的申请流程见[官方入门文档](https://docs.wokwi.com/vscode/getting-started)。
2. 运行 `pio run -e esp32dev-wokwi`。`wokwi.toml` 已指向此环境的 `.bin` 和 `.elf`。
3. 执行命令面板的 `Wokwi: Start Simulator`。
4. 点击 DHT22，调整温度到 24°C、35°C，湿度到 45 %RH、75 %RH 等值。串口每 5 秒输出 `Captured`，同时显示 `temperature_c`、`humidity_pct` 和 `mode=WOKWI-DHT22`。滑块行为见[DHT22 文档](https://docs.wokwi.com/parts/wokwi-dht22)。

默认 MQTT 密码仍为占位符，固件继续读取传感器并提示配置本地凭据。公开或分享项目时保持这个状态。

只验证传感器与串口时，也可以手动打开[官方网页版自定义固件模板](https://wokwi.com/projects/305457271083631168)，把本目录 `diagram.json` 内容复制到同名页签，在代码编辑器按 F1，选择加载固件并启动仿真的命令，选取刚构建的 `.pio/build/esp32dev-wokwi/firmware.bin`。操作前保持本地 MQTT 密码为示例占位符。这个路径使用公共网关时只用于传感器仿真，连接本机 Docker 的步骤见下文。

## 连接本机 Docker MQTT

这个步骤需要能访问本机网络的 Wokwi 网关。浏览器版默认 Public Gateway 只能访问公网，无法访问本机的 `127.0.0.1` 或 `192.168.30.10`。官方将浏览器版 Private Gateway 列为付费用户功能。Wokwi for VS Code 也需要用户自己的有效许可证，网络能力以所用扩展和许可证为准。[官方 Wi-Fi 与网关说明](https://docs.wokwi.com/guides/esp32-wifi)

在本地许可环境中操作以下步骤。

1. 运行仓库服务端，让 MQTT 绑定 `127.0.0.1:1883`，确认 Docker 服务正常。
2. 把 `include/warehouse_wokwi_config.example.h` 复制成 `include/warehouse_wokwi_config.h`。只在这个本地文件中填入服务端 `.env` 的 `MQTT_DEVICE_PASSWORD`。主机名保持 `host.wokwi.internal`，用户名保持 `cabinet-01`。
3. 确认 Wokwi 使用可连接本机网络的网关，然后重新构建 `esp32dev-wokwi` 并启动仿真。`Wokwi-GUEST` 是仿真器的开放 Wi-Fi，密码为空。此许可只在 Wokwi 编译环境中启用，实际设备默认环境仍检查非空 WPA 密码。
4. 观察串口 `MQTT connected` 和 `Published`，再打开看板确认消息序号增长。将演示数据标记为 Wokwi 仿真，分别调整两个滑块并核对温度、湿度变化。
5. 暂停仿真超过 20 秒，验证看板离线提示。重新运行后核对恢复，注意复位会使 seq 从 1 重计。

本地配置文件和包含凭据的固件只在本地保存，勿粘贴到公开网页项目或上传到共享项目。Wokwi 的网关将仿真器网络映射到本机，具体网络隔离与 WireGuard 的验收继续使用 OpenWrt 实验网。

## 验证状态

工程模板和 Wokwi 环境可在无账号条件下构建。本文中的仿真器运行、许可证和网关联通需在用户的许可环境中验证。本工程没有自动注册账号、购买许可或上传本地凭据。
