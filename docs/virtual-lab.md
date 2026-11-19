# 单电脑虚拟仓库实验

本阶段已在一台 Windows 电脑上建立四台 VMware 虚拟机，并接通广州独立 WireGuard 运维端。源码在 `D:\CodeHomework\warehouse-netops`，镜像与 VM 文件在 `C:\Users\32908\Documents\Codex\warehouse-netops-runtime`。美国服务器不参与部署，其 v2rayN 转发保持现状。

本阶段虚拟网络验收已完成：分区、路由、防火墙与跨区 MQTT 连接均在真实运行的虚拟网络中验证。温湿度来自软件模拟发布器；实体路由器、无线电、ESP32/DHT22 实物及 Wokwi 实际运行尚未验收。

## 角色与实际网络

| 角色 | 地址与网卡 | 资源与用途 |
| --- | --- | --- |
| OpenWrt | WAN `192.168.200.50/24`；Office `.10.1/24`；IoT `.20.1/24`；Service `.30.1/24`；WG `10.89.0.2/32` | 官方 OpenWrt 25.12.6；四块 e1000；512MB、1 vCPU |
| service | 唯一网卡 `192.168.30.10/24`，网关 `.30.1` | Debian 13；MQTT、SQLite、看板、SSH；2GB、1 vCPU |
| iot | 唯一网卡 DHCP 预约 `192.168.20.100/24`，网关 `.20.1` | 每 5 秒模拟一组温湿度；512MB、1 vCPU |
| office | 唯一网卡 DHCP 预约 `192.168.10.100/24`，网关 `.10.1` | XFCE、Firefox ESR、中文字体；1.5GB、1 vCPU |
| 广州 | 独立 `wg-warehouse`：`10.89.0.1/32`，UDP 51830 | 仅维护指定服务机；保留既有 wg0 与业务 |

三个内网分别使用 `wh-office`、`wh-iot`、`wh-service` LAN Segment，没有宿主机适配器。只有路由器 WAN 使用既有 VMnet8；Windows VMnet8 为 `192.168.200.254/24`，NAT 网关为 `192.168.200.1`。

广州只安装独立 peer 与服务机 `/32` 路由，不承担总部网段互通或默认出口。用户已确认在轻量应用服务器防火墙放行 `0.0.0.0/0 → UDP 51830`，见 [生效后的规则截图](D:/CodeHomework/warehouse-netops/evidence/firewall-rule-applied.jpg)。HTTP、SSH 仍经过身份验证的 WireGuard 隧道。

## 权限与实测结果

| 来源 → service | TCP 22 | TCP 1883 | TCP 8080 | 当前结论 |
| --- | --- | --- | --- | --- |
| Office | 拒绝 | 拒绝 | 允许 | 已通过 |
| IoT | 拒绝 | 允许 | 拒绝 | 已通过 |
| 广州 `10.89.0.1` | 允许，实际公钥登录 | 拒绝 | 允许，数据新鲜 | 已通过 |
| 宿主 VMnet8 → OpenWrt WAN | 拒绝 | 拒绝 | 拒绝 | 已通过，临时路由已清理 |

拒绝结论同时要求正确源地址、唯一网卡、目标实际监听、稳定 fw4 规则和对应拒绝计数增量，超时本身不算通过。Office 与 IoT 还通过双方实际监听的临时 TCP 9000 验证了双向隔离；独立临时 unit 与端口清理已验证。IoT 身份采集另行核对成功发布与看板序号推进，不能用容器内发布代替跨区路径。

截至 2026-11-19：

| 验收 | 结果 | 证据 |
| --- | --- | --- |
| 本地访问矩阵 | 13 项通过 | [virtual-matrix.json](D:/CodeHomework/warehouse-netops/evidence/virtual-matrix.json) |
| IoT 身份发布、Office/IoT 双向隔离与清理 | 通过 | [virtual-paths.json](D:/CodeHomework/warehouse-netops/evidence/virtual-paths.json) |
| 本地停 Broker | 保持 28.27 秒；网页可达，服务断开与数据过期可区分；恢复通过 | [virtual-broker-stop-local.json](D:/CodeHomework/warehouse-netops/evidence/virtual-broker-stop-local.json) |
| 本地断 WAN | 保持 28.22 秒；现场采样继续；恢复通过 | [virtual-wan-disconnect-local.json](D:/CodeHomework/warehouse-netops/evidence/virtual-wan-disconnect-local.json) |
| 广州独立 WG 路由、唯一 peer、HTTP、SSH、MQTT 拒绝及保护基线 | 13 项通过，既有保护基线不变 | [virtual-remote.json](D:/CodeHomework/warehouse-netops/evidence/virtual-remote.json) |
| 带广州观测的断 WAN | 至少 25 秒；本地网页与序号继续、广州网页中断；本地/WAN/广州 HTTP 与握手恢复通过 | [virtual-wan-disconnect.json](D:/CodeHomework/warehouse-netops/evidence/virtual-wan-disconnect.json) |
| 带广州观测的停 Broker | 至少 25 秒；广州 HTTP 与握手保持、实际 SSH 身份核验通过，Broker 断开且数据过期；本地与广州恢复在线 | [virtual-broker-stop.json](D:/CodeHomework/warehouse-netops/evidence/virtual-broker-stop.json) |
| 宿主 WAN 直入 | 9 项通过；22/1883/8080 拒绝计数分别增加 3/2/2，临时 /32 路由已删除并复核 | [virtual-wan-policy.json](D:/CodeHomework/warehouse-netops/evidence/virtual-wan-policy.json) |

两种带远程观测的故障均已实际通过，本地版保留为此前的独立记录。2026-11-19 管理员完成最后一项 WAN 直入验收后，单电脑与广州服务器的虚拟阶段验收全部通过；该 WAN 结论限于 VMnet8 宿主经 OpenWrt WAN 的受测路径，不等同于公网来源测试。[总验收记录](D:/CodeHomework/warehouse-netops/evidence/virtual-verification.json) 为 `passed`，六组报告均通过，四台 VM 运行正常，原宿主容器及广州保护基线正常。最新整体状态见 [PROGRESS.md](D:/CodeHomework/warehouse-netops/PROGRESS.md)。

## 日常启动与查看

在普通 PowerShell 执行，使用已有磁盘，不重建虚拟机：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' status
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' start
```

`start` 只启动尚未运行的本项目 VM，使用 `nogui`。等待就绪后，用普通、非管理员 VMware 打开已运行的 Office 配置：

```text
C:\Users\32908\Documents\Codex\warehouse-netops-runtime\vms\office\office.vmx
```

在 Office Firefox 打开 `http://192.168.30.10:8080`。看板标注模拟数据，采样周期 5 秒，20 秒无有效数据会过期。宿主 `http://127.0.0.1:8080` 属于保留的旧软件基线，不用于展示本次分区。

实验结束可有序关闭四台实验 VM：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\control.py' stop
```

启动器不强制断电、不新增镜像，也不管理其他虚拟机。COM1 命名管道用于本机维护，不是额外管理网卡。检查和故障注入应顺序运行。

## 复测命令

以下命令会覆盖指定证据文件；先确认四台 VM 正常运行，再逐条执行。

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_matrix.py' --phase baseline --require-fresh --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-matrix.json'
python 'D:\CodeHomework\warehouse-netops\virtualization\check_paths.py' --phase all --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-paths.json'
python 'D:\CodeHomework\warehouse-netops\virtualization\verify_remote.py'
```

故障入口会实际关闭实验 WAN 或停止 Broker，结束时尝试恢复并验证。两项不要同时运行；确认前一项 `restoration.verified` 为 `true` 后再继续。

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_faults.py' --phase wan-disconnect --guangzhou --hold-seconds 27 --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-wan-disconnect.json'
python 'D:\CodeHomework\warehouse-netops\virtualization\check_faults.py' --phase broker-stop --guangzhou --hold-seconds 27 --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-broker-stop.json'
```

`--guangzhou` 同时观察远程路径，停 Broker 时另验证实际 SSH 登录身份。`check_matrix.py` 的非 baseline 阶段仍返回 `unsupported`，故障实验使用上面的独立入口。

WAN 直入验收需要 Windows 添加临时路由的权限。仅在管理员 PowerShell 执行：

```powershell
python 'D:\CodeHomework\warehouse-netops\virtualization\check_wan.py' --output 'D:\CodeHomework\warehouse-netops\evidence\virtual-wan-policy.json'
```

该入口拒绝覆盖已有目标路由，仅临时增加 ActiveStore `192.168.30.10/32 via 192.168.200.50`，绑定源地址 `.200.254` 探测三个指定端口，最后按自身完整身份删除并复核。权限不足返回 `needs_admin`，不提升权限，也不形成拒绝结论。

讲解流程见 [5–8 分钟演示手册](D:/CodeHomework/warehouse-netops/docs/demo-runbook.md)。

## 维护现有环境

`prepare_router.py`、`prepare_guests.py` 用于首次建立实验，拒绝覆盖既有 VMX、seed 和磁盘；日常不要重跑或绕过保护。三台 Debian 切网前已建立 `warehouse-staging-ready-20261119` 快照；隔离操作只更新网卡字段，保留活动差分盘、UUID 和快照指针。恢复该快照会回到预装阶段，不能当作最终隔离网络状态。

官方镜像地址与校验值在运行目录 `images\official-checksums.json`。首次预装的 `.200.60/.61/.62` 临时连接已去掉。早期 NAT 故障由用户重启原 NAT 服务后恢复，配置未改，原本停止的 DHCP 服务未启动。预装时的临时 SSH 回环代理均不是最终拓扑，OpenWrt 原 feed 已恢复。

主机既有 Hyper-V/WSL、Docker 软件基线与其他虚拟机保持现状。本项目 VM 使用 1 vCPU 作为当前宿主环境的保守配置。服务 VM 应用与数据独立于宿主旧基线，模拟凭据留在私密文件，不进入命令行或演示材料。

避免用管理员 VMware 接管这些 VM。若普通会话遇到配置占用或串口拒绝访问，先结束检查，再正常关闭受影响的实验 VM 及标签页，由启动器重新启动；不要删除运行中 VM 的锁文件或取得所有权。广州仅维护本项目独立接口、规则与端口；美国服务器不执行部署、重启或故障注入。
