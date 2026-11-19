# 5–8 分钟演示手册

主线：仓库现场采集和查看独立运行；远程网管只获得必要权限；故障后能区分网络、消息服务和数据新鲜度，并验证恢复。

开始时明确：温湿度来自 IoT VM 的软件模拟，间隔 5 秒。当前单电脑与广州服务器的虚拟阶段验收全部通过，OpenWrt 分区、跨区连接、WireGuard 和故障注入均在虚拟网络中实际运行；实体无线、ESP32/DHT22 及 Wokwi 实际运行尚未验收。

## 演示前准备

在普通 PowerShell 启动已有实验：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' start
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' status
```

等待四台 VM 就绪，在普通、非管理员 VMware 打开已运行的 Office：

```text
C:\Users\32908\Documents\Codex\warehouse-netops-runtime\vms\office\office.vmx
```

Office Firefox 打开 `http://192.168.30.10:8080`，确认模拟标签、温湿度、序号和新鲜状态。宿主 `localhost` 是另一份旧基线。

提前顺序复测身份、隔离和广州基线：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_paths.py' --phase all --output 'D:\CodeHomework\warehouse-netops\evidence\demo-paths.json'
python 'D:\CodeHomework\warehouse-netops\virtualization\verify_remote.py'
```

准备好已有的 [访问矩阵](D:/CodeHomework/warehouse-netops/evidence/virtual-matrix.json)、[身份与隔离记录](D:/CodeHomework/warehouse-netops/evidence/virtual-paths.json)、[广州记录](D:/CodeHomework/warehouse-netops/evidence/virtual-remote.json) 与两种故障证据。所有验收顺序运行，不同时开多份故障脚本。脚本最终 `restoration.verified` 为 `true` 才能继续下一项。

## 0:00–1:30，权限与看板

说明场景：“仓库有办公电脑、环境采集设备和服务机。设备上传数据，办公人员查看数据，远程网管维护指定服务。”

| 路径 | 权限 |
| --- | --- |
| Office `192.168.10.100` → service `192.168.30.10` | 只允许看板 TCP 8080 |
| IoT `192.168.20.100` → service | 只允许 MQTT TCP 1883 |
| 广州 `10.89.0.1` → service | HTTP 8080、SSH 22；拒绝 MQTT 1883 |
| Office ↔ IoT | 双向隔离，已用实际监听的 TCP 9000 验证 |
| 宿主 VMnet8 → OpenWrt WAN → service | TCP 22、1883、8080 均拒绝，临时路由已清理 |

保持 Office 页面可见，指出两次序号增长与温湿度变化，再执行：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_matrix.py' --phase baseline --require-fresh --output 'D:\CodeHomework\warehouse-netops\evidence\demo-matrix.json'
```

讲解权限怎样对应业务。拒绝项有目标监听和路由器计数支撑，不用失败的 ping 或超时单独作证。

## 1:30–2:15，远程维护

展示广州 13 项通过记录：来源是 `10.89.0.1`，独立接口、唯一 peer 与服务机 `/32` 路由对应；看板数据新鲜，专用公钥已实际 SSH 登录，MQTT 被拒绝。需要现场复测时执行：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\verify_remote.py'
```

讲解时不展示密码或私钥。只说明本项目独立运维路径和保护基线，既有广州业务与美国 v2rayN 不参与故障注入。

## 2:15–4:15，故障一：断 WAN

先记录看板序号，执行带广州观测的已验流程：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_faults.py' --phase wan-disconnect --guangzhou --hold-seconds 27 --output 'D:\CodeHomework\warehouse-netops\evidence\demo-wan.json'
```

脚本先确认正常基线，再关闭实验 OpenWrt WAN，观察至少 25 秒，最后恢复并验证。Office 页面仍可打开、序号继续增加；广州网页访问失败。恢复后本地采样、WAN、广州 HTTP 和有效握手均需通过。

讲解：“采集、存储和现场查看都在仓库内部，外网中断只影响远程管理。”旧握手记录可能仍存在，远程可用性以实际 HTTP 请求为准。

结束时展示 `passed: true` 和 `restoration.verified: true`；不满足时先处理恢复，不继续第二项。

## 4:15–6:15，故障二：停 Broker

执行带广州 HTTP 与实际 SSH 身份核验的已验流程：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_faults.py' --phase broker-stop --guangzhou --hold-seconds 27 --output 'D:\CodeHomework\warehouse-netops\evidence\demo-broker.json'
```

观察 Office 与广州页面仍可打开，广州握手正常，SSH 仍实际登录为 `uid 1000 / warehouse / warehouse-service`。同时 Broker 变为断开，最后读数保留，超过 20 秒后数据过期。恢复 Broker 后，发布器重连，本地与广州都恢复新鲜数据。

讲解：“网页可达不等于采集正常。连接状态和最后有效数据时间分开显示，能缩小故障范围。”同样检查最终 `passed` 与恢复结果。

## 6:15–7:00，解释证据与后续

只挑关键字段：源地址和端口、拒绝计数增量、故障实际保持时间、序号变化、恢复验证。可先展示 [总验收记录](D:/CodeHomework/warehouse-netops/evidence/virtual-verification.json) 的 `status: passed`，再挑对应报告解释；无需逐行翻日志或解释部署内部结构。

宿主 WAN 直入已完成 9 项验收：TCP 22、1883、8080 均拒绝，`forward_wan` 拒绝计数分别增加 3、2、2；临时 /32 路由已删除并复核。展示 [WAN 直入证据](D:/CodeHomework/warehouse-netops/evidence/virtual-wan-policy.json) 即可。该结果验证 VMnet8 宿主路径，不等同于公网来源测试。需要独立复测时，在管理员 PowerShell 执行以下命令；它会临时添加并清理自己的精确路由，不必插入演示时间：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_wan.py' --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-wan-policy.json'
```

下一阶段用实体 OpenWrt 和 ESP32/DHT22 替换对应角色，继续验证无线覆盖、接入隔离与真实采样。模拟数据和已完成的网络实验分别说明。

## 异常处理与结束

- 先看脚本最后的 `restoration`，不要在另一窗口重跑故障或 `prepare_*`。恢复未验证时保留证据、停止演示。
- 确认四台 VM 的 `status`、Office 的目标地址，避免误看宿主旧看板。重跑 baseline 可定位不满足的前置条件。
- 若脚本已结束但恢复仍失败，用本地 VMware 控制台恢复。OpenWrt 执行 `ifup wan`；service 执行 `cd /opt/warehouse-netops/server && sudo docker compose --project-name warehouse-netops start broker`。这些是 Linux 客体命令，不在 Windows PowerShell 执行。随后重新验证基线和广州路径。
- 遇到占用或串口拒绝，不删除锁文件、不覆盖 VMX、不接管运行中 VM。结束检查后正常关闭受影响 VM 及标签页，再由启动器启动。

演示结束可保留运行，或有序关闭本项目四台 VM：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' stop
```
