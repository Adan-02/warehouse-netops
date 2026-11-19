# 观测与事件通知

两个独立的定时观测进程为看板提供网络状态，并将故障事件发送到可配置的 Webhook。

| 位置 | 观测内容 | 通信方式 |
| --- | --- | --- |
| 仓库服务机 | OpenWrt WAN、默认路由、指定 WireGuard peer、分区拒绝计数 | 专用 SSH 身份调用固定只读命令 |
| 广州运维端 | 服务机 HTTP、独立隧道路由与握手、远程不可达事件 | 从 `10.89.0.1` 主动访问现场，经过既有 SSH 22 回送结果 |

每 10 秒采集一次，单次探测超时 3 秒。每个快照带观测来源与时间，接收端设置自己的接收时间；35 秒没有新快照时，看板将该观测视为未知。WAN 接口、握手记录与实际 HTTP 可达性分别显示。

## 安装与检查

在项目根目录运行：

```sh
python virtualization/install_observers.py prepare
python virtualization/install_observers.py install
python virtualization/install_observers.py status
```

`prepare` 只生成本地私密代码包。`install` 使用已有的广州管理连接、服务机维护连接与本地路由器 COM1，安装独立账号、SSH 限制、脚本及定时器。它会核对广州受保护基线，并通过 COM1 固定路由器主机身份，不添加网络端口或修改防火墙规则。

现场快照位于 `/var/lib/warehouse-netops-observer/observations/local.json` 与 `remote.json`，看板只读挂载该目录。观测私钥留在相应观测主机，不进入容器。路由器 SSH 身份只能调用固定诊断命令；回送身份只能向固定接收器提交有界 JSON。

服务镜像的 SSH `AllowUsers` 保持原来的 `warehouse`。广州回送使用一把独立的 reporter key，限定来源为运维隧道地址、禁用转发和 PTY，并强制执行固定接收包装器。包装器核对协议与来源后立即降权到 `warehouse-observer`，由无 sudo／Docker 权限的进程写入快照；它不提供维护账号的交互能力。

项目定时器分别为 `warehouse-observer-site.timer`、`warehouse-observer-cloud.timer`。停止观测可以停用对应定时器，不影响应用服务或已有 WireGuard 接口。

## Webhook

广州端配置位于 `/var/lib/warehouse-netops-observer/private/config.json`，权限为 `0600`。未配置接收地址时，通知外发关闭。

```json
{
  "webhook": {
    "enabled": true,
    "url": "https://hooks.example.net/warehouse",
    "secret": "REPLACE_WITH_A_PRIVATE_SIGNING_SECRET",
    "allow_loopback_http_for_tests": false
  }
}
```

配置文件中的其它字段由安装器维护。接收地址和签名密钥留在私密配置中，命令行及日志不输出它们。发送端保持 HTTPS 证书验证，不跟随重定向。

通知正文包含 `delivery_id`、`source`、`event_id`、`transition`、`occurred_at` 和事件对象。`transition` 区分故障开始、恢复及观测中断。请求头 `Idempotency-Key` 等于稳定的 `delivery_id`；`X-Warehouse-Signature` 为 `sha256=` 加上原始请求体的 HMAC-SHA256 十六进制摘要。

接收端返回 2xx 后确认发送成功。超时、429、5xx 按退避重试，最多 8 次；其它 HTTP 错误进入失败记录。投递采用至少一次语义，接收端应按 `delivery_id` 去重。首次启用从当时的事件游标开始，历史事件仍可查询，不会作为新通知批量发送。

通知游标、发送队列与广州独立观测到的远程故障存放在私有 SQLite 中。事件入队与游标前进在同一事务完成，故障恢复也有独立的变化记录。断 WAN 时现场无法收到广州新快照，广州仍可记录自己的不可达事件；恢复后快照附带最近的远程故障时间。

每日生成一致性备份并保留最近 7 份，已关闭远程事件和已完成发送记录保留 180 天。正在发送、重试和活动故障不因清理而删除。私钥、主机身份记录和 Webhook 配置单独保管，不放入 Git。

## 测试

```sh
python -m unittest discover -s ops/tests -v
```

测试使用临时 SQLite、假观测和本机 HTTP 接收端，覆盖来源限制、过期快照、重复与恢复事件、游标事务、签名、限流、超时和进程重启后的重试。测试用 HTTP 只允许显式开启的 loopback 地址。
