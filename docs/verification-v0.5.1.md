# v0.5.1 验证记录

日期：2026-11-20，北京时间。现场为 VMware 有线虚拟网络，温湿度来自 IoT VM 软件发布器。本轮整理接手入口、运行阶段、探针传输、业务协调器与观测流程；实物无线、ESP32/DHT22 和 Wokwi 运行不在本轮结论中。

## 版本与代码入口

- 应用制品：`v0.5.1-97073b9294fb`，构建提交 `97073b9294fb7c0063445d234bdac2041f4c2f41`。
- 应用源码摘要：`4efa3d8b3c282d9a64e604e6313495e58581f4a0f38bd6c66787c0a4ae263859`。
- 实际镜像：`sha256:752c5f74cb9c9b7a7285cbbf47a7ec176218d088dfdb0b64a5c2bf840270a761`；数据库 schema 仍为 3。
- 后续测试隔离补丁 `c32ae0ecc377092667b3005409db04eda4b9ad73` 的 [GitHub Actions 五个 job 全部通过](https://github.com/Adan-02/warehouse-netops/actions/runs/44567746509)。应用源码与上述制品一致。

首次阅读从 [接手说明](HANDOFF.md) 开始。预装入口集中于 `virtualization/bootstrap/`；正常发布使用 `release.py`。`LabSession` 管理整场锁，`VmSerialShell` 处理串口协议，`remote_probes/` 保存普通源码探针。业务由 `TelemetryService` 协调，历史、迁移与纯阈值决策分别读取。观测器按现场与广州分阶段记录结果，旧接口和已安装数据位置保持兼容。

## 离线与独立容器

| 范围 | 本机结果 |
| --- | --- |
| 虚拟化工具全套 | 232 项，217 通过、15 条件跳过 |
| Debian seed | 8 项全部通过 |
| 服务端 | Linux 容器 44 项，43 通过、缺 Node 跳过 1 项 |
| 页面实际 JavaScript 行为 | 宿主 Node 4 项全部通过，CI 也安装 Node 执行 |
| 观测器 | Windows 44 项，43 通过、symlink 条件跳过 1 项 |
| 模拟发布器 | 11 项全部通过 |
| 广州安装事务 | 独立 Linux 容器 21 项全部通过 |
| OpenWrt 配置事务 | 独立 Linux 容器 10 项全部通过 |
| 软件集成 | 独立 Compose 14 项全部通过，使用单独端口与数据卷 |

不同平台的事务专项包含全套中的条件跳过项，不将这些数字相加当作互不重复的测试总数。源码、模板与测试进入 Git；凭据、私密备份、制品和原始证据保留本机。

新增回归覆盖导入及帮助的副作用、已发布环境拒绝预装、只读发布查询、连接位置与安装目录分离、整场锁与清理、探针编码往返、真实历史时间边界、页面迟到响应及导出、缺测后阈值计时重置，以及观测阶段的独立失败与恢复。

## 真实虚拟环境

| 检查 | 结果 | 本机原始记录 |
| --- | --- | --- |
| 业务 | 10 项通过，模拟发布器恢复 | `evidence/handoff-business-acceptance.local.json` |
| 发布 | 实际镜像、版本与广州端读取一致，发布前备份 4708 行 | `.secrets/handoff-deploy.log` |
| 备份隔离恢复 | 4708 行，`integrity_check=ok`，活动数据库未修改 | `.secrets/handoff-restore-check.log` |
| 身份与双向隔离 | 身份 5 项、隔离及清理 11 项通过 | `evidence/handoff-paths-after-reconnect.local.json` |
| 广州访问 | 13 项通过，复用既有专用 key 与主机 pin，不准备新身份 | `evidence/virtual-remote.json` |
| 断 WAN | 实际保持 28.28 秒，14 项通过，恢复复核通过 | `evidence/handoff-wan-disconnect.local.json` |
| 停 Broker | 实际保持 28.39 秒，13 项通过，恢复复核通过 | `evidence/handoff-broker-stop.local.json` |
| 观测器升级 | 两侧安装成功，timer 与各阶段正常，Webhook 外发仍关闭 | `evidence/observer-installation.local.json` |
| 四台 VM 冷启动 | 6 项断言经只读补核通过，历史 4829→4875 行、最早记录保留、完整性正常、同一版本与七层健康正常 | `evidence/handoff-cold-boot-completed.local.json` |
| Router 首次串口命令 | 单独冷启动 4 项通过，首次业务命令完成，业务自动恢复 | `evidence/handoff-router-startup-barrier.local.json` |

业务验证包含 24 小时及 7 天聚合、CSV、10 分钟原始历史与 CSV 同边界、过期重复消息不刷新新鲜度、持续超温与持续恢复，以及 `raised/recovered` 变化游标。

首次路径复测的认证项返回 `inconclusive`：当前连接建立时间超出 Broker 一小时有界日志窗口。其余采样与隔离检查通过；业务测试恢复发布器产生新连接后，身份链路完整通过。首轮记录保留在 `evidence/handoff-paths.local.json`，没有改写为通过。

冷启动的关机用时 101.75 秒，启动至业务就绪用时 383.94 秒。之后首次串口读取启动 ID 超时，原始失败保留在 `evidence/handoff-cold-boot.local.json`；没有将它改写为整轮一次通过。随后仅补齐只读断言，四台内核启动 ID 均改变，数据库完整且版本一致；没有手动启动 guest 服务。首次读取超时的具体角色和根因未被原脚本记录，后续串口错误需包含角色和执行阶段。

串口随后增加有界 shell 就绪确认：最多三次幂等 `printf`，确认完成才发送业务命令；完成标记丢失时不重发业务命令。就绪和执行共用超时预算，错误包含角色与阶段，避免输出命令和私密内容。新增三项回归覆盖首次确认丢失、确认失败及完成标记丢失；Linux 七项串口测试全部通过，包含真实 PTY 的未闭合引号恢复。

单独 Router 冷启动的首轮专项在 30 秒时首次读取超时，42.28 秒时只读重试成功，保留为 `evidence/handoff-router-startup-barrier-early.local.json`。第二轮在启动后等待 60 秒，不预先读取或激活串口，首次业务命令于 62.19 秒完成；启动 ID 改变，七层健康自动恢复，4 项通过。这些结果验证有界确认与恢复行为，不据此倒推原始瞬态超时的唯一根因。

本轮还修复了观测器安装目录遮蔽连接位置参数、发布状态查询创建本地目录，以及 Python 版本差异导致测试误读个人 SSH 配置的问题。广州既有保护配置核对一致；美国服务器及其 v2rayN 未参与操作。
