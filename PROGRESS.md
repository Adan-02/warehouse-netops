# 项目进度

更新日期为 2026-11-19。

## 当前里程碑

已在 `D:\CodeHomework\warehouse-netops` 完成四台 VMware 虚拟机的隔离网络：OpenWrt、办公、IoT、服务机。温湿度从独立 IoT 虚拟机经真实 TCP/MQTT 发布，办公虚拟机访问服务区看板。原宿主 localhost 软件环境保留，使用独立数据卷。

**单电脑 + 广州服务器的虚拟实验阶段已完成验收。** 总记录 `evidence/virtual-verification.json` 为 `passed`；四台 VM 正在运行，广州受保护基线核对一致，原宿主容器健康且仍仅绑定 localhost。

## 虚拟仓库 v0.3：真实虚拟网络实测

- 三台 Debian 都只有一块物理虚拟网卡，分别是办公 `.10.100`、IoT `.20.100`、服务 `.30.10`，经三个独立 LAN Segment 接 OpenWrt。
- 13 项本地矩阵通过：办公只允许看板 8080，IoT 只允许 MQTT 1883；其余受测端口拒绝有真实 fw4 计数佐证。见 `evidence/virtual-matrix.json`。
- 身份链路通过：当前 IoT 发布进程、TCP 源端口、Broker 认证账号与源地址、MQTT PUBACK 和看板温湿度载荷关联。见 `evidence/virtual-paths.json`。
- 办公到 IoT、IoT 到办公的 TCP 9000 均拒绝；目标监听及自连接正常，拒绝计数增长，测试监听器已清理。
- 停 Broker 至少 25 秒：网页继续可用、Broker 断开、温湿度过期，恢复后采集在线。见 `evidence/virtual-broker-stop-local.json`。
- 断 OpenWrt WAN 至少 25 秒：办公网页和现场采集继续，序号增长，WAN 恢复后再次核验。见 `evidence/virtual-wan-disconnect-local.json`。
- 广州独立 `wg-warehouse` 已安装，UDP 51830，旧 wg0/SSH/Docker/QQ 状态和受保护配置核对一致。经用户确认，在轻量服务器防火墙仅新增 UDP 51830 规则，旧规则保留。见 `evidence/guangzhou-deployment.json` 和 `evidence/virtual-cloud-firewall.json`。
- 广州远程 13 项验收通过：独立隧道的唯一 peer、/32 路由及握手正确，远程看板和独立密钥 SSH 可用，MQTT 1883 拒绝且计数增长。见 `evidence/virtual-remote.json`。
- 带广州端的断 WAN 故障通过：现场网页与采集继续、远程网页失败，恢复后两端在线及握手正常。见 `evidence/virtual-wan-disconnect.json`。
- 带广州端的 Broker 故障通过：网页、WireGuard、指定 SSH 仍正常，Broker 断开且数据过期，恢复后两端采集在线。见 `evidence/virtual-broker-stop.json`。

- 用户已在管理员 PowerShell 完成宿主 WAN 直入验收，9 项检查全部通过：到服务机 TCP 22/1883/8080 的访问均被拒绝，实际 `forward_wan` 拒绝计数分别增长 3/2/2；目标监听正常。临时 /32 路由已删除，脚本及随后宿主只读查询均确认不存在。见 `evidence/virtual-wan-policy.json`。

当前温湿度仍为软件模拟；实体 Wi-Fi、ESP32/DHT22 与 Wokwi 实际运行尚未验证。

本阶段运行命令见 `docs/virtual-lab.md`。`python virtualization/control.py status/start/stop` 只管理四台本项目 VM；不要对已运行环境重新执行 prepare 入口。美国服务器及其 v2rayN 转发未参与部署或故障注入。

## 已有基线

- v0.1 为 DS18B20 温度版，37 个交付文件与开发副本一致。
- 温度版曾通过 11 项软件集成、4 项单元测试、3 项路由器恢复脚本测试及三种固件编译。
- 以上结果是旧温度版基线，保存在 archive 中供追溯。
- 迁移前备份两个 Docker 数据卷，迁移后已从 D 盘重启原服务，确认旧温度记录和接收时间可读。

## 温湿度版 v0.2 已完成

- 已升级 DHT22 温湿度采集、Wokwi 图及固件文档，三种固件环境编译通过。
- 数据库、API、看板、Python 和 MQTTX 模拟器均支持湿度，旧记录的湿度显示“未上报”。
- 7 项单元测试、14 项软件集成检查通过，包括旧库迁移、湿度边界、非法值、离线与重连。
- 浏览器已检查温湿度数值、独立曲线、历史表格及模拟标签。
- 当前服务从 D 盘运行。PlatformIO 的工具链缓存与新 Python 环境均位于 `.cache/`。

## 接下来从这里开始

按 `docs/demo-runbook.md` 练习完整演示；办公 VM 的 Firefox 地址是 `http://192.168.30.10:8080/`。重点说明三分区权限、远程运维，以及断 WAN 和停 Broker 时不同的现象与定位证据。硬件到位后按同一数据契约替换模拟发布器并复测。Wokwi 的图与固件已准备，实际运行滑块与串口的验收单独记录；构建命令见 `firmware/README.md`。

旧 C 盘工具环境、调研预览图和旧 `p` 链接的收尾操作由用户自行处理，具体路径和命令在 `docs/manual-migration.md`。该组合操作曾被自动审批拦截，未执行。

## 后续实物验收

- 记录所用 OpenWrt 路由器型号、版本及实际接口。
- 验证两组 SSID、地址分配和跨区访问矩阵。
- 验证 WireGuard 真实 peer、网页访问及指定 SSH 登录。
- 连接 DHT22，验证温湿度变化、拔传感器及重连。
- 执行断 WAN 和停止 Broker 两场故障实验，填写发现与恢复耗时。

Wokwi 运行、MQTTX GUI、GNS3 网络及实体设备的验收状态分别记录。仿真源保持模拟标签，真实接入时切换显示模式。
