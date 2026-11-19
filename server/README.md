# 仓库环境监测服务

使用 Docker Compose 在服务电脑启动 Mosquitto 和 Python 看板。路由器上的服务区地址为 `192.168.30.1/24`，电脑的服务区网卡静态设置为 `192.168.30.10/24`，网关为 `192.168.30.1`。局域网路由、防火墙与 WireGuard 由路由器目录提供。

## 电脑本地演示

在本目录执行，所有发布端口默认只绑定到本机回环地址。

```console
python setup.py --demo
docker compose up -d --build
```

浏览器打开 `http://127.0.0.1:8080`。新建的 `server/.env` 包含两个互不相同的随机密码。固件使用账号 `cabinet-01` 和 `MQTT_DEVICE_PASSWORD`，看板使用 `warehouse-dashboard` 和另一组密码。初始化不会在终端打印密码。软件发布器属于模拟演示，网页上会显示相应提示。

## 接入实物实验网络

先确认服务电脑已经实际拥有 `192.168.30.10` 这个网卡地址，再执行下列命令。Docker 无法绑定本机不存在的地址。

```console
python setup.py --bind-ip 192.168.30.10 --no-demo
docker compose up -d --build
docker compose ps
```

`setup.py` 会保留既有随机密码，仅更新显式传入的绑定地址和演示模式。请把 `.env` 留在本机，不要提交到公开仓库或答辩附件。

ESP32 通过 Wi-Fi 连接 IoT 网络后，将消息发送到 `192.168.30.10:1883`。MQTT 明文传输限定在这个受控的课堂实验网中。远程访问走 WireGuard。若要扩展到跨不可信无线网络的部署，需另行配置 MQTT TLS 与证书验证。

## MQTT 数据约定

| 项目 | 取值 |
|---|---|
| MQTT 版本 | 3.1.1 或 5 |
| 主题 | `warehouse/cabinet-01/telemetry` |
| 发布账号 | `cabinet-01` |
| 订阅账号 | `warehouse-dashboard` |
| 标称采样间隔 | 5 秒 |
| 上报 retain | `false` |

```json
{"device_id":"cabinet-01","seq":1,"temperature_c":26.5,"humidity_pct":45.0}
```

`seq` 是设备每次启动后从 1 递增的采样序号，可以在设备重启后重新开始。`temperature_c` 必须为有限数字，JSON 数字最大长度受 4096 字节总消息长度限制。消息按服务端实际收到的时间入库，设备时间不会影响在线状态。错误 JSON、其它设备编号、布尔数字以及 retained 历史回放不会刷新在线时间。

`humidity_pct` 表示相对湿度，必须是 0–100 的有限数字，边界 0 和 100 均有效。DHT22 每次上报一组有效温湿度。为读取原温度版数据，服务端接受没有湿度字段的旧消息并保存为 NULL。旧消息和历史记录的湿度在看板显示“未上报”，曲线留下空缺。显式传入 null、布尔值、字符串或越界湿度时，整条消息被忽略，最近有效接收时间保持原值。

设备账号只拥有本设备主题的发布权限。订阅账号只拥有同一主题的读取权限。验证其它主题的拒绝行为时使用 MQTT 5、QoS 1，以便客户端读取 Broker 的拒绝原因。

## 状态与接口

| 接口 | 内容 |
|---|---|
| `GET /` | 中文只读看板 |
| `GET /api/status` | 当前消息服务与采集状态 |
| `GET /api/history?limit=120` | 按接收时间顺序返回最近读数，limit 为 1–240 |
| `GET /healthz` | HTTP 进程存活状态，Broker 连接异常时也保留看板供诊断 |

`/api/status` 含有 `device_id`、`status`、`data_status`、`broker_connected`、`seq`、`temperature_c`、`humidity_pct`、`last_seen_at`、`age_seconds`、`offline_after_seconds`、`observed_at` 及 `demo_mode`。`/api/history` 的每条记录也包含可空的 `humidity_pct`。时间为 UTC ISO 8601，网页转换为浏览器当地时间。看板分别绘制温度与湿度曲线，湿度单位为 %RH。

- `data_status=awaiting_data` 表示尚未收到有效数据。
- 距最近接收时间小于 20 秒时为 `online`，到达 20 秒为 `stale`。
- MQTT 连接或主题订阅失败时，`status=broker_disconnected`。`data_status` 同时保留读数新鲜度，便于区分应用服务问题与采集问题。

看板每 2 秒刷新。SQLite 保存在 `dashboard_data` 卷，容器重启会保留历史和原始接收时间。首次启动温湿度版时，服务端在事务内检查旧表并补充可空湿度列；重复启动可安全重用同一数据库。旧行和接收时间保留。重启不会把旧读数当成新上报。课堂实验历史随本地卷保存，可通过 `docker compose down` 停止而保留数据，重新 `up` 后继续使用。删除卷会删除实验数据。

## 故障演示

```console
docker compose stop broker
```

看板应保留旧读数并显示消息服务中断。容器探活正常，故障定位仍可进行。

```console
docker compose start broker
```

看板会自动重连并重新订阅。ESP32 恢复发布后采集状态恢复。测试离线判断可保持 Broker 运行，仅停止 ESP32 或软件发布器，等待至少 20 秒。

## 单元验证

安装 `requirements.txt` 后，在本目录执行。

```console
python -m unittest discover -s tests -v
```

覆盖旧表迁移及重复启动、旧消息缺失湿度、湿度边界与非法值、错误上报不刷新新鲜度、精确 20 秒阈值、Broker 断开状态、历史持久化、设备重启序号归零和只读 HTTP 路由。
