# 项目进度

更新日期为 2026-11-20。本文件记录网络基线、版本实测结果与实物验收边界。

## 独立接管阶段

已建立 [独立接管路线](docs/learning/README.md)、第一轮消息地图和本机练习记录。下一阶段由用户亲手操作、预测和解释，助教提供分级提示；先掌握现有主链路，再进行自主变更和 Wokwi 集成。当前仅完成学习资料准备，尚未依据用户表现认定掌握，未改动业务环境或注入故障。

用户已确认 Wokwi 许可证搞定；实际仿真、MQTT 联调和相应验收仍按观察结果记录。个人答案和自评存于 Git 忽略的 `.local/learning/RECORD.md`，不会因建立记录自动标记练习完成。

## 接手成本整理

已补充 [接手说明](docs/HANDOFF.md)，标明宿主旧演示、四台现场 VM 与广州运维端的区别，以及业务、观测、发布三条链路。首次创建与预装集中在 `virtualization/bootstrap/`；旧入口兼容，已有发布或隔离网卡会提前拒绝预装。导入与 `--help` 不再读取机器配置，发布状态通过独立只读程序查询。

`LabSession` 明确持有整场操作锁，恢复和清理完成后释放；串口协议由 `VmSerialShell` 处理。拓扑、串口限制、SSH 输出接收与远端探针各有独立来源。业务协调器改名 `TelemetryService`，保留旧导入/API，迁移、历史查询与纯阈值决策分别可读；10 分钟图表使用真实时间窗口，CSV 沿用图表成功读取时的边界。观测器按现场/广州命名，私密目录与只读快照分开，查询同时展示 timer 和流程阶段。

本轮离线验证：虚拟化工具 232 项（217 通过、15 跳过），Debian seed 8 项通过，服务端 44 项（43 通过、容器缺 Node 跳过 1 项），另在宿主 Node 运行页面行为 4 项通过；观测器 44 项（43 通过、Windows symlink 跳过 1 项），发布器 11 项通过。独立 Linux 容器中的广州事务 21 项、路由器事务 10 项均通过。独立 Compose 的 14 项软件集成通过，保留原宿主服务。

新版观测器已实际安装，两侧分阶段状态正常，广州原有保护基线一致，Webhook 仍关闭。修复安装路径遮蔽连接位置参数、只读发布查询创建本地目录两处审查发现的问题，并加入回归。

已发布 `v0.5.1-97073b9294fb`，实际镜像、广州端版本与 schema 3 核对一致。业务 10 项、身份与双向隔离 16 项、断 WAN 14 项及停 Broker 13 项均通过；两场故障恢复均复核通过。发布前备份 4708 行，隔离恢复完整性正常。代码与测试隔离补丁 `c32ae0e` 的 [GitHub Actions 五个 job 全部通过](https://github.com/Adan-02/warehouse-netops/actions/runs/44567746509)。详见 [v0.5.1 验证记录](docs/verification-v0.5.1.md)。

四台 VM 正常关机耗时 101.75 秒，启动至业务健康耗时 383.94 秒。首次串口读取启动 ID 超时的记录单独保留；随后仅用只读检查补齐 6 项断言，四台启动 ID 均改变，历史 4829→4875 行、最早记录保留且 `quick_check=ok`，同一版本、七层健康正常。没有手动启动 guest 服务，见 `evidence/handoff-cold-boot.local.json` 与 `evidence/handoff-cold-boot-completed.local.json`。

串口增加有界 shell 就绪确认，仅重试幂等探针，业务命令只发送一次；超时包含角色与阶段。新增三项相关回归，Linux 七项串口测试全部通过，含真实 PTY。首次读取超时的具体根因尚未由原记录定位，不将该加固等同于根因已确定。

单独 Router 冷启动补测 4 项通过：等待启动后首次业务命令完成、启动 ID 改变、七层健康自动恢复。首轮过早读取的失败与重试记录保留，第二轮结果见 `evidence/handoff-router-startup-barrier.local.json`。

## 当前里程碑

已在 `D:\CodeHomework\warehouse-netops` 完成四台 VMware 虚拟机的隔离网络：OpenWrt、办公、IoT、服务机。温湿度从独立 IoT 虚拟机经真实 TCP/MQTT 发布，办公虚拟机访问服务区看板。原宿主 localhost 软件环境保留，使用独立数据卷。

虚拟实验已完成业务、应用回滚、数据备份与隔离恢复、断 WAN、停 Broker 及四台 VM 冷启动验证。当前运行 `v0.5.1-97073b9294fb`，版本和镜像已从广州核对，冷启动后采样继续，七层实时健康正常。各轮失败、补核和不同版本的证据分别记录。

新版加入分层运维健康、故障与恢复事件、持续越限告警、时间范围历史与 CSV、启动标识去重、只读网络观测、Webhook 队列，以及不可变发布和数据库备份入口。功能与实测边界见 [v0.5 验证记录](docs/verification-v0.5.md)。原始运行证据和私密备份保留本机。

## v0.5 已完成实测

- 已从干净 Git 提交构建并发布 `v0.5.0-c4a7392e96d5`，服务端版本、源码摘要与 schema 3 核对通过。该提交的 GitHub Actions 5 个 job 全部通过。
- 新版业务验收 9 项全部通过：七层健康正常；24 小时查询返回 1441 个 60 秒桶，7 天查询返回 2017 个 300 秒桶；CSV 导出 2430 行。过期后重发同一消息不刷新新鲜度；连续超温 30 秒触发、持续恢复 30 秒关闭，`raised` 与 `recovered` 变化游标均可读取。正常模拟发布器已恢复。见 `evidence/v05-business-acceptance.local.json`。
- 已实际从新版回滚到 `legacy`，再从 `legacy` 回到 `v0.5.0-c4a7392e96d5`。两次均成功，采样继续，应用回滚没有恢复旧数据库。
- SQLite 一致性备份及隔离恢复通过：2385 行，`integrity_check=ok`，活动数据库未修改。见 `evidence/v05-backup-restore.local.json`。
- 断 WAN 实际保持 45.17 秒，14 项检查全部通过；办公网页和本地采样继续，广州网页中断，结束后 WAN、本地采样与广州 HTTP/WireGuard 均恢复。见 `evidence/v05-wan-disconnect.local.json`。
- 停 Broker 实际保持 31.22 秒，13 项检查全部通过，结束后恢复通过。独立健康快照显示 MQTT 与采样异常，存储、WAN、两侧 WireGuard 和广州 HTTP 仍正常；浏览器实际显示连接中断与采集离线，恢复后事件关闭。见 `evidence/v05-broker-stop.local.json`、`evidence/v05-broker-during-health.local.json`。
- 广州回送身份的任意命令、PTY 与转发通道限制已实际核验，3 项通过。Webhook 外发保持关闭，签名与重试使用本机测试接收端验证。
- `c4a7392e96d5` 的离线回归运行 238 项：224 项通过、14 项跳过，失败和错误均为 0。此后 SSH 大输出接收问题修复完成，发布传输专项 37 项通过，含 2 项新增回归；另实际验证连续 5 次完整接收 300000 字节输出。这是补充专项结果，不是完整 240 项重跑记录。
- 冷启动工具及启动错误诊断修复后，虚拟化工具全套重新运行 198 项：184 项通过、14 项因环境条件跳过，失败和错误均为 0。该批次不与前述跨模块的 238 项相加计数。主机工具提交 `f938ef03148c` 的 [GitHub Actions 5 个 job 全部通过](https://github.com/Adan-02/warehouse-netops/actions/runs/44534283365)。
- 已构建并部署 `v0.5.0-12550d15ec97`，业务源码摘要与前一版一致，纳入发布传输修复。实际 image ID、广州端版本和采样增长核对通过。该提交 [GitHub Actions 5 个 job 全部通过](https://github.com/Adan-02/warehouse-netops/actions/runs/44523141424)。
- 浏览器已检查 10 分钟、24 小时、7 天历史切换与 CSV 按钮下载，文件表头和数据有效；390×844 窄屏下无横向溢出。
- 首轮冷启动发现的关机命令语法与串口续行恢复问题均已修复，并在真实 Office 控制台及 Linux PTY 验证恢复和退出码，见 `evidence/v05-console-recovery.local.json`。修复后的第二轮四台 VM 均正常关机，耗时 105.31 秒；随后 Router 启动被管理员 VMware 标签页占用阻挡，用户关闭已关机标签后释放，未删锁或改权限。各次失败保留独立记录。
- 最终冷启动 6 项检查全部通过：四台内核启动 ID 均改变，历史从 2557 行增至 2571 行，首条记录保留且 `quick_check=ok`，仍运行同一发布版本，七层健康全部正常。未手动启动任何 guest 服务，见 `evidence/v05-cold-boot.local.json`。

## 冷启动观察说明

本机单 vCPU/WHP 的 service 启动曾耗时约 6 分钟，控制器默认业务就绪预算为 600 秒。恢复控制器在北京时间 21:29:40 至 21:34:59 的约 319.5 秒观察窗口内确认健康；三台 Debian 更早开始启动，该窗口不等同于整套冷启动总耗时，也不包含人工释放标签占用的等待。

完整回归、专项补测、首次失败和不同运行版本的实测分别保留，未合并计数。

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

## 日常演示与实物替换

按 `docs/demo-runbook.md` 练习完整演示；办公 VM 的 Firefox 地址是 `http://192.168.30.10:8080/`。重点说明三分区权限、远程运维，以及断 WAN 和停 Broker 时不同的现象与定位证据。硬件到位后按同一数据契约替换模拟发布器并复测。Wokwi 的图与固件已准备，实际运行滑块与串口的验收单独记录；构建命令见 `firmware/README.md`。

旧 C 盘工具环境、调研预览图和旧 `p` 链接的收尾操作由用户自行处理，具体路径和命令在 `docs/manual-migration.md`。该组合操作曾被自动审批拦截，未执行。

## 后续实物验收

- 记录所用 OpenWrt 路由器型号、版本及实际接口。
- 验证两组 SSID、地址分配和跨区访问矩阵。
- 验证 WireGuard 真实 peer、网页访问及指定 SSH 登录。
- 连接 DHT22，验证温湿度变化、拔传感器及重连。
- 执行断 WAN 和停止 Broker 两场故障实验，填写发现与恢复耗时。

Wokwi 运行、MQTTX GUI、GNS3 网络及实体设备的验收状态分别记录。仿真源保持模拟标签，真实接入时切换显示模式。
