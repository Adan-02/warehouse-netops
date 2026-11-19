# 验收与故障实验

## 正常路径

ESP32 通过 IoT Wi-Fi 获得 `192.168.20.x`，向服务主机 `192.168.30.10:1883` 发布自己的 MQTT 主题。看板每次成功接收有效数据时保存服务器接收时间。办公网与已连接 WireGuard 的网管访问 `http://192.168.30.10:8080`。

每条新消息包含设备编号、开机后采样序号、摄氏温度和相对湿度百分比。20 秒无新有效数据，显示采集离线。断开 Broker 时显示服务连接中断，同时保留最后一次读数及其时间。序号在 ESP32 重启时重新计数，判断新鲜度使用服务端时间。固件使用 QoS 0，软件模拟使用 QoS 1；QoS 1 可能重复。采集队列保留最近采样，离线期间的完整历史不补传。

旧温度记录的湿度为 null，看板显示“未上报”。验证新链路时同时核对两个数值、两条独立曲线及历史表格。示例消息为 `{"device_id":"cabinet-01","seq":1,"temperature_c":24.5,"humidity_pct":48.2}`。

## 首次基线

确认服务主机的 22、1883、8080 均真实监听。SSH 必须在主机上独立启用，验证 SSH 实际登录，不把仅端口开放当作登录成功。先从有线服务区验证三个服务，再从每个分区执行探测。

```powershell
python tools/probe_policy.py --role office --source-ip 192.168.10.100 --output evidence/office.json
python tools/probe_policy.py --role iot --source-ip 192.168.20.100 --output evidence/iot.json
python tools/probe_policy.py --role vpn --source-ip 10.66.66.2 --output evidence/vpn.json
```

每次命令要在对应实际终端运行，并替换成终端自己的 IP。IoT 测试时先让电脑连接 IoT Wi-Fi。ESP32 无法充当完整的跨区端口测试端。系统没有该源 IP 时探测会失败，应先修正测试终端配置。

WAN 直接访问测试要关闭 VPN，并确认总部测试机存在到 `192.168.30.10/32`、下一跳 `172.31.250.1` 的临时路由。Linux 可在隔离实验终端手工配置以下路由，测试完删除。

```sh
sudo ip route add 192.168.30.10/32 via 172.31.250.1
python3 tools/probe_policy.py --role wan --source-ip 172.31.250.2 --output evidence/wan.json
sudo ip route del 192.168.30.10/32 via 172.31.250.1
```

拒绝项出现超时或拒绝连接时，还要记录 `ip route get 192.168.30.10`、主机监听状态，以及路由器匹配的防火墙规则计数。错误路由、停服、客户端本机防火墙也能造成连接失败。脚本会先确认源 IP 能在本机绑定，遇到本机路由不可达时标记未确定。WireGuard 成功连接后再验证 `/32` 路由走隧道、网页访问成功、指定服务主机 SSH 登录成功。最后为办公终端配置 `192.168.10.x` 地址，在 IoT 测试端确认不能连接该终端上已知正常监听的服务，反向也测试一次。

WireGuard 默认只安装服务主机路由。若要主动验证 VPN 到办公/IoT 网被防火墙拦截，临时在测试客户端的 AllowedIPs 中加入已知测试终端的 `/32`，确认路由实际走隧道，使用已知开放端口测试并记录路由器计数，测试完移除这些临时路由。只看到客户端缺少路由导致失败，不能作为跨区防火墙验证结果。

## MQTT 主题权限

`tools/check_integration.py` 在 localhost 软件测试中覆盖以下场景。实体网络部署后，从 IoT 终端用自己的测试凭据再次复测。

- 正确凭据发布 `warehouse/cabinet-01/telemetry` 后看板获得新序号。
- 匿名或错误密码无法连接 Broker。
- 设备凭据向 `warehouse/other/telemetry` 发布被拒绝。
- 设备凭据订阅后收不到 telemetry 内容，订阅账号向 telemetry 发布被拒绝。Mosquitto 文件 ACL 可接受订阅请求、在交付阶段阻止读取，因此需观察实际消息交付。
- 非有限温度、错误设备编号、非整数序号，以及已提供却无效的湿度都使整条数据被忽略，不刷新最后有效接收时间。湿度 0 和 100 合法，缺失湿度兼容旧温度消息。

MQTT 5 + QoS 1 的 PUBACK/SUBACK 可验证拒绝。单看客户端成功建立 TCP 连接或命令退出，不足以证明主题授权。

## 实验一，拔掉 WAN

1. 正常发送至少 6 条真实采样，记录最新序号与时间。
2. 从总部 peer 打开看板，并在办公 Wi-Fi 终端同时打开看板。
3. 拔掉路由器 WAN 网线，记录时间。总部访问应中断，办公终端应持续收到新序号。
4. 接回 WAN，记录 WireGuard 恢复后首次成功访问时间和总恢复耗时。
5. 检查事件期间 SQLite 里的现场采样时间线，证明本地采集仍在继续。

本实验检验本地业务与远程管理的依赖关系。网线断开时客户端短暂保留 WireGuard 的旧握手记录，不能仅凭旧握手记录判断隧道可用。

## 实验二，停止 Broker

保持正常外网与 VPN，在服务主机运行以下命令。

```powershell
docker compose --project-name warehouse-netops -f server/compose.yaml stop broker
```

记录网管仍能登录服务主机 SSH、看板仍可打开，但显示服务断开，最后数据逐渐过期。检查 Broker 容器日志和状态，重启服务。

```powershell
docker compose --project-name warehouse-netops -f server/compose.yaml start broker
```

记录 Broker 重新连接时间和首条真实采样恢复时间。现场 ESP32 离线的实验通过断电单独进行，此时 Broker 仍在线，看板应在 20 秒阈值后标记采集离线。

## 一页故障报告

| 项目 | 填写内容 |
| --- | --- |
| 编号与操作人 | 例如 WAN-01、执行人 |
| 业务预期 | 哪个来源应能访问哪个目标服务 |
| 故障时间与影响范围 | 本地采集、现场查看、远程访问分别是否受影响 |
| 假设与证据 | 源 IP、路由、握手、端口、MQTT 鉴权、最后接收时间 |
| 处理动作 | 只记录实际执行的修复 |
| 复测 | 使用同一组允许/拒绝用例 |
| 实测指标 | 中断时间、发现时间、恢复时间和原始日志文件 |
| 结论边界 | 软件模拟或真实设备、观测到的丢失/重复、尚未验证项 |
