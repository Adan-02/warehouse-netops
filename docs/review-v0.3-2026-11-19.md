# v0.3 改进评审与下一阶段范围

评审日期：2026-11-19。评审对象为当前单电脑、四台 VMware VM 与广州独立运维端；本文件是改进建议，不代表这些功能已经实现。

## 当前完成程度

虚拟网络与业务链路已搭建并验收：分区访问、IoT 身份关联、广州 WireGuard/HTTP/SSH、WAN 直入拒绝，以及断 WAN、停 Broker 的故障与恢复。原始结果见 [总验收记录](D:/CodeHomework/warehouse-netops/evidence/virtual-verification.json)。

本次额外只读现场检查确认四台 VM 运行，办公区以 `192.168.10.100` 访问服务机 HTTP 200，Broker 已连接、数据在线、模拟标签开启。本次没有注入真实故障、重启 VM、修改防火墙或云配置，没有访问美国服务器。业务边界复现仅使用内存 SQLite。

当前已有 Broker 状态与数据新鲜度分离、非法消息过滤、retained 回放过滤、设备与订阅账号分权、SQLite 旧表迁移和历史持久化。这些是现成功能，不重复列为待开发。

“虚拟阶段通过”覆盖既定用例；后续代码发布、长期运行、数据灾备和实物无线仍有独立工作要做。实体 Wi-Fi、ESP32/DHT22、Wokwi 实际运行没有被本次审查补验。

## 第一优先：保证改动能够正确发布，状态能够准确解释

### 1. 历史验收与当前健康需要分开

证据：[collect_status.py:11](D:/CodeHomework/warehouse-netops/virtualization/collect_status.py:11) 读取历史报告的 `passed`，[collect_status.py:33](D:/CodeHomework/warehouse-netops/virtualization/collect_status.py:33) 将其合成总体结论。现场另外检查 VM 进程、宿主旧容器及广州保护基线，但没有实时复测服务 VM 的 API、MQTT、采样及实验隧道。

因此，汇总报告的新时间戳不代表全部业务用例在该时刻重新通过。历史验收本身有效；脚本的实时健康含义不够明确。

最小修改：

- 分开显示 `last_acceptance` 与 `live_health`，分别保留观测时间和来源。
- 只读巡检现场 API、Broker/订阅、最新有效采样，以及广州到服务区的实际 HTTP/WG 路径。
- 状态至少区分正常、异常、未知、观测过期；未知不能用绿色“正常”替代。
- 历史报告附带代码、配置或发布版本摘要，避免更新以后沿用旧版结论。

验收：已有历史通过报告时，让一个只读测试桩返回当前 HTTP 失败；界面必须保留“历史通过”，同时显示“当前异常/未知”。

### 2. 二开需要正式发布入口

证据：[deploy_service.py:27](D:/CodeHomework/warehouse-netops/virtualization/deploy_service.py:27) 打包当前源码，但 [deploy_service.py:45](D:/CodeHomework/warehouse-netops/virtualization/deploy_service.py:45) 导入的是此前保存的镜像 tar，并以 `--no-build` 启动。[Dockerfile:6](D:/CodeHomework/warehouse-netops/server/Dockerfile:6) 将业务代码复制进镜像，[compose.yaml:35](D:/CodeHomework/warehouse-netops/server/compose.yaml:35) 没有挂载宿主应用源码。原部署入口还面向预装阶段的临时地址。

后果：修改 D 盘源码、再次复制源码，不保证当前服务 VM 的容器运行新代码。这不影响已经测试的旧镜像，但会阻碍下一轮开发。

最小修改：当前源码构建镜像，生成版本/源码摘要与 image ID 清单，通过已有 VPN/SSH 运维路径发布，读取运行版本确认生效，保留上一版进行定向回滚。沿用现有网络边界，不为发布增加 WAN 暴露。建立本地 Git 版本记录；凭据、数据和 VM 磁盘继续留在忽略清单中。

验收：发布一个可识别的新版本，API 返回版本应与镜像清单一致；再回滚到上一版，历史数据仍可读。

## 最值得新增的功能：只读运维总览与故障事件时间线

当前看板主要回答“当前数值是多少、Broker 是否连接、多久没收到数据”。下一版应进一步回答“哪个环节出现异常、证据是什么、下一步查哪里”。

建议沿用现有页面和数据库，显示以下独立观测：

| 层次 | 要显示的事实 | 解释边界 |
| --- | --- | --- |
| WAN | 接口状态、默认路由、端点实际探测结果 | 接口 up 不等于公网正常 |
| WireGuard | 预期 peer、路由、握手时间、广州 HTTP 结果 | 旧握手不等于当前可达 |
| 服务 | HTTP/SSH 的实际探测结果、容器健康 | 进程存在不等于业务就绪 |
| MQTT | Broker/订阅状态、认证发布证据 | PUBACK 不等于数据库提交成功 |
| 数据 | 最后有效采样、最后成功入库、入库错误 | 数据过期不能单独证明传感器故障 |

每项保留观测位置、时间和新鲜度。自动建议只能指向下一项检查，不应将一次超时直接归结成唯一根因。

路由器自身当前还没有广州远程诊断入口：[virtual-router.sh:299](D:/CodeHomework/warehouse-netops/router/virtual-router.sh:299) 的 VPN 本机输入为 DROP，已有授权主要是到服务机的转发。如果加入路由器观测，应新增来源受限、只读的诊断身份，仅查看接口、路由、DHCP、WG 和防火墙；配置修改另行设计授权与回滚。原矩阵并没有承诺路由器远程管理，这属于扩展需求。

### 入库健康是应补的状态缺口

[main.py:74](D:/CodeHomework/warehouse-netops/server/app/main.py:74) 在 SQLite 写入失败后只记日志；[state.py:83](D:/CodeHomework/warehouse-netops/server/app/state.py:83) 没有独立的持久化失败状态。内存 SQLite 置为只读后，写入抛出异常，而最近 20 秒窗口内仍可显示数据在线、Broker 已连接。

建议增加最后入库成功时间、最后失败类型及失败计数；正确 rollback 后允许后续恢复。保留 `/healthz` 的 HTTP 存活用途，另加业务健康字段。存储写入故障本身不能只依赖同一个失效数据库记录，应同时保留内存状态与结构化日志。

### 故障事件应在服务端记录

[state.py:62](D:/CodeHomework/warehouse-netops/server/app/state.py:62) 当前只有读数表。先记录 Broker/订阅不可用、采样过期、存储写入失败以及对应恢复事件，包含开始、结束、持续时间和观测事实。事件评估由服务端定时执行，不依赖是否有人打开浏览器。

温湿度越限可以作为后续业务增强。演示阈值必须明确是人为配置；实际阈值取决于仓库存放物，不能把任意温湿度数值说成通用企业标准。加入持续时间与恢复回差，防止边界抖动重复告警。首版保持只读事件查看，告警确认或远程操作再配身份授权。

验收：复用现有两场故障。WAN 中断时本地采样继续，远程路径异常；Broker 停止时远程 HTTP/SSH 可达、Broker 与采样状态异常。恢复后事件仍可查，开始/结束时间完整。

## 可靠交付与业务追溯

### 3. 数据备份和恢复需要单独验收

服务 VM 当前没有新的数据备份/恢复入口。预装快照和停 Broker 后恢复不能替代数据库灾备。[state.py:57](D:/CodeHomework/warehouse-netops/server/app/state.py:57) 已启用 SQLite WAL；备份应采用一致性方法。

建议使用 [SQLite 在线备份 API](https://www.sqlite.org/backup.html) 生成数据库快照，另行保护私密配置并记录清单与校验值。恢复到新隔离卷，核对历史记录、最后序号及应用可读性。验证后再决定是否切换，避免覆盖现有唯一数据。

### 4. 冷启动与业务就绪

[control.py:26](D:/CodeHomework/warehouse-netops/virtualization/control.py:26) 发出 VM 启动指令，[control.py:47](D:/CodeHomework/warehouse-netops/virtualization/control.py:47) 显示进程运行；尚无最终网络整体冷启动的独立验收记录。

增加 `start --wait` 或独立 readiness 检查：等待地址、Docker、MQTT、有效采样和远程路径就绪。安排一次正常关机后的冷启动回归，记录就绪时间，并确认原历史数据保留。此项会中断实验，应在执行任务中单独安排，本次评审没有运行。

### 5. 历史曲线和时间范围

- [index.html:166](D:/CodeHomework/warehouse-netops/server/app/templates/index.html:166) 不按相邻样本的时间间隙断线；长时间无采样后会跨越缺测区间连线。建议超过离线阈值时断线并标注缺测。
- [main.py:37](D:/CodeHomework/warehouse-netops/server/app/main.py:37) 的历史 API 最多 240 条，页面固定取 120 条。5 秒间隔下约为 20 分钟和 10 分钟，而数据库持续追加。建议增加受限的时间范围查询、按时间桶聚合、CSV 导出和保留策略。
- 优先利用现有 SQLite；当前单设备规模不要求立即更换数据库。

### 6. 数据标识与重复投递

[state.py:90](D:/CodeHomework/warehouse-netops/server/app/state.py:90) 对每条有效消息直接 INSERT。内存复现中，同一载荷隔 25 秒再投递会产生两行，并将数据重新标为在线。

不能简单按 `seq` 去重：现有协议明确允许设备重启后序号从 1 开始。若增强数据质量，应增加启动标识 `boot_id`，用 `(device_id, boot_id, seq)` 识别重复，同时保留旧协议读取能力。重复消息不应更新有效采样新鲜度。

固件目前有意采用“只保留最新样本、过期丢弃、离线完整历史不补传”，见 [firmware/README.md:39](D:/CodeHomework/warehouse-netops/firmware/README.md:39)。这不是本次发现的实现违约。若以后承诺历史完整性，需另行设计持久队列、原始采样时间、去重与补传标记，补传不能冒充实时在线。Paho 官方列出的[客户端会话持久化限制](https://github.com/eclipse-paho/paho.mqtt.python#known-limitations)与 Mosquitto 的[Broker 持久化配置](https://mosquitto.org/man/mosquitto-conf-5.html)分别作用于不同环节，不能用开一个 persistence 选项替代端到端设计。

## 补充验收与企业场景边界

1. **真实 IoT 区的 MQTT 负向权限。** [acceptance.md:37](D:/CodeHomework/warehouse-netops/docs/acceptance.md:37) 仍把匿名、错误密码、越主题发布及设备订阅读取的反向测试列为 localhost 软件测试；真实 IoT 已验的是正向认证与载荷关联。补齐这些反向用例，并确认正常采集不受干扰。
2. **日常诊断与实验管理员分权。** [generate_seed.py:242](D:/CodeHomework/warehouse-netops/virtualization/debian/generate_seed.py:242) 的 warehouse 当前拥有无密码全 sudo，服务角色还加入 Docker 组。日常只读诊断可用独立受限身份；保留现有管理身份用于受控维护，权限调整需兼容现有恢复流程。
3. **办公出网场景。** 当前办公终端仅被允许访问现场看板，未实现普通企业办公的通用上网。若设定为仓库离线查看终端，现方案成立；若要访问云 ERP，应先列出明确业务目标及 DNS/NTP/维护需求，再定向扩展规则。
4. **拒绝计数的关联粒度。** [check_matrix.py:264](D:/CodeHomework/warehouse-netops/virtualization/check_matrix.py:264) 当前每个来源的多个端口共享一次计数增量。受控实验有证据支持，但背景流量增加时关联不够精确。可逐端口采集，并使用更精确的计数或受控 trace；本次没有运行并发假阳性复现。
5. **交付可复现。** 提供本机 profile、依赖清单与 doctor 预检；固定 Debian 日期镜像及校验清单，补齐磁盘生成步骤。继续保留不覆盖已有 VMX、快照差分盘和凭据的保护。

## 推荐实施顺序

| 批次 | 具体交付 | 完成判据 |
| --- | --- | --- |
| v0.3.1 工程修正 | 历史验收与实时健康分开；版本构建/发布/回滚入口；本地版本记录 | 新源码确实进入运行镜像；当前故障不沿用历史绿色状态；回滚保留数据 |
| v0.4 运维能力 | 只读分层总览、入库健康、故障事件时间线、缺测断线；IoT MQTT 反向权限 | 两场故障能分别定位到不同观测层，恢复事件可追溯，原访问矩阵继续通过 |
| v0.4 交付验收 | 数据备份恢复、冷启动就绪、发现/恢复耗时报告 | 在隔离副本中恢复成功；正常冷启动后历史与业务均可用；指标注明观测频率与误差 |
| 后续按需求 | 温湿度阈值告警、较长历史查询、启动标识去重、实体替换 | 先明确货物、历史完整性及硬件目标，再增加对应契约与测试 |

最适合当前毕设的下一步，是把已经跑通的网络做成能够持续观察、解释异常和复核恢复的运维系统。现有 OpenWrt、WireGuard、Mosquitto、SQLite 与看板均可继续复用。
