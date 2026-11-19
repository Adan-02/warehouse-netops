# 接手说明

这个项目用仓库温湿度业务验证网络分区、远程运维和故障恢复。当前现场由四台 VMware VM 组成，温湿度来自软件模拟发布器；物理 Wi-Fi、ESP32/DHT22 和 Wokwi 运行要分别验收。先读本页，再按需进入具体手册，不需要聊天记录。

## 先确认运行位置

| 位置 | 承担的职责 | 容易混淆的地方 |
| --- | --- | --- |
| Windows 宿主 | 源码、构建、VM 生命周期、COM1 控制、发布编排 | 宿主还保留旧 Docker 软件演示，不能把它当成虚拟现场 |
| 仓库现场四台 VM | OpenWrt 分区路由；office 查看；iot 发布；service 运行 MQTT、SQLite、看板和现场观测 | 三台 Debian 各只有一块内网网卡，经路由器跨区通信 |
| 广州运维端 | 独立 WireGuard 对端、远程观测、维护中继和通知队列 | Windows 到广州的既有管理连接，与广州到仓库的实验隧道是两条不同路径 |

宿主 `http://127.0.0.1:8080` 是保留的旧软件基线；办公 VM 访问的 `http://192.168.30.10:8080` 才是当前虚拟现场。在已有电脑接手时，先不要执行快速启动的 `init/up` 去更新宿主基线。独立新电脑上的软件演示按 [README](../README.md) 启动，它不等于复现四台 VM。

当前固定拓扑见 [项目设计](../PROJECT.md) 与 `virtualization/lab_topology.py`。机器路径来自 `.local/lab.json` 或 `WAREHOUSE_LAB_PROFILE`，模板为 `lab.example.json`；`runtime.load_paths()` 在执行操作时读取。管理连接别名可配置，但目标身份与主机 pin 仍固定校验，不能借此选择任意服务器。

## 三条链路

**采集与查看：** `iot` 的 `publish_sensor.py` → OpenWrt → service 的 Mosquitto → MQTT SUBACK 后启用订阅 → `TelemetryService.ingest()` → SQLite 提交 → 最新快照与只读 HTTP → office 看板。设备 `cabinet-01` 只发布自己的主题，看板账号只订阅。重复消息不刷新在线时间；读数和设备会话在同一事务提交后，才更新最新快照。

**观测与通知：** service 的现场观测器通过专用 SSH 固定命令读取 OpenWrt；广州观测器检查实验 WireGuard 和服务机 HTTP，再用独立回送 key 送回有界快照。看板只读观测目录，过期显示 `unknown`。广州还通过事件变化游标同步触发与恢复，事务化入通知队列，再签名投递 Webhook。公网外发默认关闭。

**构建与发布：** Windows 从干净 Git 提交构建版本制品 → 既有广州管理 SSH → 绑定实验隧道源地址的服务机维护 SSH → 校验制品、备份数据 → 切换看板镜像和版本指针 → 核对 image ID、API 版本和新采样。只修改工作区源码不会改变已运行镜像。

## 从只读检查开始

在项目根目录的普通 PowerShell 执行：

```powershell
git status --short
python virtualization/doctor.py --quick
python virtualization/control.py status
python virtualization/release.py status
python virtualization/install_observers.py status
```

这些命令不启动 VM、不安装服务、不改网络配置。`doctor --quick` 跳过大镜像摘要，`not_checked` 不代表业务故障，也不构成完整安装验收。后两项会使用已有的广州管理路径；观测状态同时区分 timer 与各流程阶段，不能只看 timer `active`。

`python virtualization/collect_status.py` 汇总历史验收与当前业务健康，会写本机证据 JSON，不修改 VM 或云配置。报告时间、执行版本与当前健康分别读取，不把以前的 `passed` 当成现在在线。

## 命令按副作用选

| 要做的事 | 入口 | 会改变什么 |
| --- | --- | --- |
| 日常启停 | `control.py start --wait` / `stop` | 仅四台既有 VM，等待业务就绪；不重建磁盘，默认预算 600 秒 |
| 访问与身份验收 | `check_matrix.py`、`check_paths.py`、`verify_remote.py` | 发起实际探针并写本机证据；路径验收会建立、清理临时测试监听 |
| 准备维护身份 | `verify_remote.py --prepare-ssh` | 显式准备专用维护公钥授权和主机 pin；默认验证命令不准备身份 |
| 故障演练 | `check_faults.py --phase …` | 实际停 Broker 或断实验 WAN，并在 finally 中恢复和复核；同一时间只运行一个场景 |
| 日常版本更新 | `release.py build/deploy/rollback` | 本机构建制品、传输、备份或切换看板镜像；不倒退数据库 |
| 安装观测器 | `install_observers.py install` | 独立账号、固定命令、公钥限制和定时器；日常查询用 `status` |
| 首次建立环境 | `python -m virtualization.bootstrap.NAME` | 创建或预装环境，不是应用更新入口；已有正式发布或隔离网卡时拒绝预装 |

首次建立先完成镜像和本机工具前置，再用 `create_router`、`create_guest_disks`、`create_guests` 准备 VM 与磁盘；Linux 客户端的磁盘必须先于配置创建。之后经 `install_router_packages`、`provision_staging_guests`、`stage_service_application` 完成预装，最后 `finalize_isolated_network` 切到独立内网。实际前置条件与步骤见 [虚拟实验说明](virtual-lab.md)；不要把这一组当作日常重启脚本。旧文件入口仅作兼容，帮助与阶段检查先于实际动作。

VMware 中已关机的标签仍可能占用配置；确认关机后关闭对应标签，再重试。不要删除锁、接管文件权限或结束整个 VMware 进程。各类串口检查共享整场操作排他锁，等待另一项检查完成是正常现象。

串口发送业务命令前先确认 shell 就绪；只有幂等确认探针会自动重试，业务命令只发送一次。超时区分角色及 `readiness`／`completion` 阶段。执行结果未知时先检查实际状态，尤其不要直接重发发布、关机或故障命令。

身份路径验收只读取 Broker 最近一小时、最多 2000 行的连接日志。长期连接的认证记录超出窗口时，即使采样正常也可能返回 `inconclusive`；它表示证据不足。受控采集测试恢复发布器、产生新连接后可以复验，不能仅凭 PUBACK 把认证结论改成通过。

## 读代码的顺序

1. `server/app/main.py`：HTTP 路由与 MQTT 回调；`state.py` 的 `TelemetryService`：唯一业务协调器，兼容旧 `TelemetryStore` 名称。
2. `server/app/thresholds.py`：纯阈值决策；`migrations.py` 和 `history.py`：结构兼容与有界读查询。修改阈值先看配置、此评估器及测试，不必追云端脚本。
3. `ops/observer.py`：`probe → reachability → report → event_sync → dispatch → maintenance`；`webhook.py` 的 `NotificationState`：游标、远程事件与持久发送队列。
4. `virtualization/lab_session.py`、`probes.py`、`console.py`：会话生命周期、只读探针、单台 VM 串口 shell。`LabSession` 拥有整场锁；`VmSerialShell` 负责交互协议，`Console` 是旧调用者的兼容入口。
5. `virtualization/release.py`、`release_guest.py`、`release_transport.py`：分别对应主机构建编排、服务机事务和固定 SSH 传输；远端普通源码探针位于 `remote_probes/`。

`broker_connected` 是兼容 JSON 字段，真实含义是 `mqtt_subscription_active`，即订阅已获确认；HTTP 活性、订阅可用、采样新鲜度、存储成功和网络观测是不同状态。阈值持续时间用单调时钟，事件显示用 UTC；缺测不能算作连续越限或恢复。

## 身份与文件边界

| 路径 | 身份与权限 |
| --- | --- |
| Windows → 广州管理连接 | 本机 SSH 配置中的管理 key 与固定主机 pin，既有管理地址 `10.77.0.1` |
| 广州 → service 维护 | 从 `10.89.0.1` 进入，独立 maintenance key，维护登录 `warehouse`；可执行受控发布和备份 |
| service 观测器 → OpenWrt | 独立 router-observer key，只能执行固定只读命令，并校验源地址 |
| 广州观测器 → service 回送 | 独立 reporter key，固定协议与接收命令、禁止 PTY/转发；包装器验证后降权为 `warehouse-observer` |
| MQTT 发布与订阅 | `cabinet-01` 仅发布指定主题；`warehouse-dashboard` 仅读取该主题 |

观测配置的 `private_dir` 放 key、pin、配置和队列数据库，`observations_dir` 放看板只读快照；旧 `state_dir` 由兼容读取处理。不要因改名搬动现有私密文件。源码、镜像制品、活动数据与备份分别管理；SSH 私钥不进入看板容器，私密配置、key 和原始运行证据不进入 Git。应用 JSON 配置可以只读挂载，MQTT 凭据通过本机服务环境提供。

Git 回退改变源码；应用回滚切换 `current/previous` 对应镜像；数据恢复从一致性备份恢复记录。这是三件事。`release.py restore-check` 只在临时副本检查完整性，不改活动数据库；具体操作见 [版本发布与回退](versioning.md)。

本项目只使用广州独立实验接口与规则。**美国服务器及其 v2rayN 转发禁止本项目部署、改路由或代理、重启和故障注入。** 广州既有 wg0、SSH、Docker 与 QQ 业务也必须保留。

## 按任务继续阅读

- [独立接管练习](learning/README.md)：从消息地图、故障判断到自主变更和 Wokwi 联调，按实际掌握情况推进。
- [项目设计与数据契约](../PROJECT.md) · [当前进度](../PROGRESS.md)
- [虚拟实验与访问矩阵](virtual-lab.md) · [演示手册](demo-runbook.md)
- [服务接口与单元测试](../server/README.md) · [观测身份与 Webhook](../ops/README.md)
- [发布、回滚和备份](versioning.md) · [v0.5 验证](verification-v0.5.md) · [v0.5.1 验证](verification-v0.5.1.md)

换电脑前先盘点 VMware、LAN Segment、锁定镜像、私密配置、SSH 主机 pin 与既有云状态。这些前置不都包含在 Git 中，软件演示可以独立启动，完整环境不能仅凭一次 clone 宣称已重建。
