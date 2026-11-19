# Warehouse Netops 项目协作约定

## 项目目标

本项目实现小型仓库网络分区与远程运维。OpenWrt 负责办公、IoT、服务区及 WireGuard，电脑运行 MQTT、SQLite 与看板，ESP32 使用 DHT22 提供每 5 秒一组温湿度。网络运维验收覆盖访问权限、现场持续采集和故障定位。

当前先使用一台电脑上的 VMware 虚拟分区和广州服务器推进。美国服务器的 v2rayN 转发必须保持现状：禁止本项目在美国部署、安装软件、改代理/路由/防火墙/端口转发、重启服务或注入故障。广州已有 wg0、10.77/10.78 网段和长期业务，新增实验必须使用独立配置和定向回滚，不能替换既有规则。

## 目录与命令

- `firmware/` 为 PlatformIO ESP32 固件与 Wokwi 电路。
- `server/` 为 Docker Compose、MQTT 和只读看板。
- `router/` 为实体 OpenWrt 配置与恢复脚本。
- `tools/` 为本地启动、模拟和验收脚本，`examples/` 为 MQTTX 示例。
- `docs/` 为方案与验收，`evidence/` 为实测记录，`archive/` 为旧版基线和数据备份。

从项目根目录运行 `python tools/lab.py init --demo`、`python tools/lab.py up`、`python tools/lab.py demo --count 12`。软件集成入口为 `python tools/check_integration.py`。固件入口为 `pio run -d firmware`，仿真环境为 `esp32dev-simulated`、`esp32dev-wokwi`。

## 接口与数据

MQTT 主题为 `warehouse/cabinet-01/telemetry`。新增湿度字段 `humidity_pct`，有效范围 0–100。保留旧温度消息及 SQLite 历史的读取能力，未知湿度显示“未上报”。提供的字段无效时忽略整条消息，不更新接收时间。

## 验证与记录

修改数据契约时同步更新固件、API、看板、模拟器、示例与相关测试。运行受影响的验证并更新 `PROGRESS.md`。实测记录写清软件模拟、固件编译和实物验证的范围。未经实测的 Wi-Fi、WireGuard、防火墙和断 WAN 行为列为待验。

## 本地配置

`server/.env`、固件本地配置、`.cache/`、数据卷备份保留在本机。公开发布包仅包含配置模板和脱敏证据。模拟发布需开启 DEMO_MODE。操作实体路由器前读取实际接口和型号，并使用既有备份恢复流程。

## 自主接管练习

用户已选择自己操作、预测和解释，助教按“观察位置 → 相关概念 → 局部命令”的顺序提供帮助。练习入口为 `docs/learning/README.md`；若本机存在 `.local/learning/RECORD.md`，先读当前轮次和已用提示，不重问已经确认的偏好。

准备资料和检查文件可以直接完成；不要把用户要亲手完成的练习自动代做，不在用户作答前填写预测或标记掌握。每轮保留初始解释、实际观察、提示和修正；个人答案写本机记录，不默认提交 Git。用户明确要求示范、遇到配置风险或恢复失败时可直接协助，记录介入范围。

故障练习只在已进入的练习轮次中进行，先落实观察窗口与恢复手段，不因建立路线就启动故障。自动全套验收用于收尾复核，普通练习只检查当次假设和受影响行为。Wokwi 留作后期自主集成，沿用同一业务实现、按需启动独立联调实例，不用其结果替代 IoT 分区的系统级结论。
