# OpenWrt 实验路由器

本目录给独立的课堂实验路由器生成配置。`prepare` 从设备当前的 UCI 配置读取真实 LAN bridge、WAN 端口和 2.4 GHz 无线电，保留物理端口、交换芯片和无线驱动参数。`apply` 会替换网络地址、无线 SSID、DHCP 和防火墙策略，并保存原始配置。

## 接线和地址

| 位置 | 地址与用途 |
|---|---|
| 路由器现有有线 LAN bridge | `192.168.30.1/24`，服务区与有线维护入口 |
| Docker 电脑有线网卡 | 静态 `192.168.30.10/24`，网关 `192.168.30.1` |
| `Warehouse-Office` | `192.168.10.0/24`，网关 `.1`，DHCP `.100` 起 |
| `Warehouse-IoT` | `192.168.20.0/24`，网关 `.1`，DHCP `.100` 起 |
| 路由器 WAN | 静态 `172.31.250.1/24`，连接模拟总部测试机 |
| 总部测试机的有线网卡 | 静态 `172.31.250.2/24`，课堂隔离链路，无需设置默认网关 |
| WireGuard | 路由器 `10.66.66.1/24`，总部测试机 `10.66.66.2/32` |

Docker 电脑插 LAN 口，总部测试机插 WAN 口。WAN 的网线承载课堂模拟广域网。第一版不依赖真实互联网、公网 IP 或云服务器。地址若与电脑其他网络重叠，应先为独立实验调整地址方案，并同步修改服务端、固件、路由脚本和测试目标。

### 访问矩阵

| 来源 | 允许访问 | 其余跨区访问 |
|---|---|---|
| 办公 Wi-Fi | 服务电脑 `192.168.30.10:8080/TCP` | 丢弃 |
| IoT Wi-Fi | 服务电脑 `192.168.30.10:1883/TCP` | 丢弃 |
| WireGuard peer | 服务电脑 `192.168.30.10:8080/TCP`、`:22/TCP` | 丢弃 |
| WAN 测试机 | 路由器 `51820/UDP` | 丢弃 |
| 有线服务区 | 路由器本机维护 | 跨区转发丢弃 |

办公和 IoT 网络额外允许访问路由器本机的 DHCP、DNS。该脚本没有全区 forwarding，也没有用于绕过回程路由的 masquerade。服务电脑必须使用路由器作为实验网卡的回程网关，或显式设置到 `192.168.10.0/24`、`192.168.20.0/24`、`10.66.66.0/24` 经 `192.168.30.1` 的路由。同一有线服务区内的主机属于受信任管理域，其二层流量不经过路由器跨区防火墙。

## 先核对硬件

1. 使用支持的 OpenWrt 23.05 或更新版本、firewall4、现有有线 `network.lan.device` bridge 与独立 `network.wan.device`。脚本对额外逻辑接口会停止并指出名称，避免覆盖已有业务网络。
2. 确认 `uci show wireless` 能识别一个 2.4 GHz radio，且驱动支持同 radio 至少两个 AP 接口。`iw phy` 的 `valid interface combinations` 是检查依据，最终以两个 SSID 能同时关联和领取各自网段地址为准。
3. 在仍可联网时装好与固件版本匹配的 WireGuard、wireguard-tools、firewall4、dnsmasq、iw，以及无线驱动对应的 wpad/hostapd。LuCI 的 WireGuard 页面可选。可使用固件原有包管理器查询和安装匹配包，先不要断开安装网络。

```sh
ubus call system board
uci get network.lan.device
uci get network.wan.device
uci show wireless
iw phy
wg --version
fw4 check
```

`prepare` 会检测关键工具并测试 WireGuard 模块可加载。它保留现有无线电国家码、频段和驱动路径，关闭其他无线电。办公和 IoT SSID 使用独立随机 WPA2 密码及客户端隔离。计划以 IPv4 验收，脚本关闭这些实验网段的 RA/DHCPv6，并只为明确的 IPv4 跨区流量放行。

## 准备与应用

下面命令在路由器上的 SSH 终端执行。先将 `lab-router.sh` 复制至设备 `/root/lab-router.sh`。OpenWrt 的 Dropbear 默认可能不支持现代 scp 的 SFTP 模式，可用 `scp -O`，也可以通过串口或已有文件传输方式复制。

```sh
sh /root/lab-router.sh prepare
# 多个 2.4GHz radio 时，按实际名称显式选择
# sh /root/lab-router.sh prepare --radio radio0
```

生成目录形如 `/root/warehouse-netops-stages/20261117-120000-1234`。下文 `STAGE_DIR` 均替换成实际打印的完整路径。

- `config/` 存放待应用的四份 UCI 配置。
- `backup/` 保存应用前四份原始配置。
- `summary.txt` 记录使用的真实 bridge、WAN 设备和 radio。
- `private/wifi-passwords.txt` 存放两套 Wi-Fi 密码。
- `private/warehouse-admin.conf` 存放可导入 WireGuard 客户端的私密配置。

在仍通过旧 LAN 地址管理时，把私密文件保存到自己的实验设备。它们由设备本地生成，目录和文件权限限制为 root。包含私钥和密码的文件留在个人实验环境内，不进入公开论文附件或代码仓库。

从直接插在 LAN 口的电脑通过旧地址建立 SSH，检查待应用配置后执行。

脚本检查 SSH 来源是否经 LAN bridge 直连。旧配置里的 Wi-Fi 可能也桥接在同一个 LAN，因此这项检查不能独立证明网线连接，应用前需实际确认使用有线网卡。

```sh
sh /root/lab-router.sh apply STAGE_DIR
```

此时旧 SSH 可能断开。把服务电脑有线网卡改为 `192.168.30.10/24`、网关 `192.168.30.1`，重新 SSH 到 `192.168.30.1`，在 **180 秒内**确认。

```sh
sh /root/lab-router.sh confirm
```

脚本会启动独立于 SSH 的回退计时器。未及时从直接相连的有线服务网段确认，会恢复备份配置。应用前若配置已被其他操作修改，脚本要求重新 `prepare`。若 firewall4 检查失败，会恢复原配置并把错误留在阶段目录。

需要恢复本阶段原始配置时执行。

```sh
sh /root/lab-router.sh rollback STAGE_DIR
```

网络重载会中断当前连接。备份保留用于后续人工恢复。若设备重启发生在计时器期间，内存中的计时器无法跨重启继续运行，应通过有线维护地址或 OpenWrt failsafe 恢复备份。首次应用安排在可物理接触、可恢复的实验设备上。

## WireGuard 客户端

1. 将 `private/warehouse-admin.conf` 导入总部测试机的 WireGuard 客户端。
2. 先把该电脑有线网卡配置成 `172.31.250.2/24`，接至路由器 WAN 口。
3. 先记录未连接 VPN 时访问 `http://192.168.30.10:8080` 的结果，再激活隧道。客户端 `AllowedIPs` 仅安装到服务电脑的 `/32` 路由，服务端防火墙再限制具体端口。
4. `wg show wg_admin` 看到握手只是隧道证据，还需从客户端实际访问看板和实验主机 SSH。

服务电脑的 TCP 22 由主机 SSH 服务提供，Docker 看板项目不附带 SSH 守护进程。先在该电脑配置好本地实验账号和密钥，并用允许/禁止来源测试。Windows Docker Desktop 的虚拟网络与主机防火墙需另外验证，Linux 服务主机更接近最终展示拓扑。

## 逐项验收

```sh
sh /root/lab-router.sh status
fw4 check
wg show wg_admin
ip -4 route
logread -e netifd
```

- 办公、IoT 客户端分别拿到 `.10.0/24` 与 `.20.0/24` 的地址。拿错网段时先查 SSID 所属 network 和 DHCP。
- 办公端打开看板成功，连接 MQTT 与 SSH 失败。
- IoT 端发布 MQTT 成功，访问看板、服务电脑 SSH、办公终端失败。先测服务电脑自身正在监听的已知端口，避免把服务未启动误记为策略生效。
- WAN 端未建 VPN 时，服务电脑的看板和 SSH 失败。激活 VPN 后，仅允许的两项访问成功。
- 拔掉 WAN，保持路由器和服务电脑供电，现场看板的消息序号继续增加。接回后测量握手和远程看板恢复时间。
- MQTT 容器停止时，WireGuard 握手和 SSH 应继续可用，看板应体现 Broker 连接中断或数据过期。

可在 LuCI 的「网络 → 防火墙」和 `nft list ruleset` 查实际规则及命中计数。规则变化后用新的 TCP 连接复测，以免已建立连接状态影响判断。

## 资料

- [OpenWrt guest Wi-Fi 配置](https://openwrt.org/docs/guide-user/network/wifi/guestwifi/configuration_webinterface)
- [OpenWrt 防火墙 UCI](https://openwrt.org/docs/guide-user/firewall/firewall_configuration)
- [OpenWrt WireGuard](https://openwrt.org/docs/guide-user/services/vpn/wireguard/serverclient)
- [WireGuard AllowedIPs](https://www.wireguard.com/#cryptokey-routing)

本目录代码尚需在具体路由器型号上验证无线多 AP、fw4 实际规则与回退流程，不能用本地脚本语法检查代替硬件验收。

`tests/test-rollback.sh` 是可选的隔离回归测试。只在一次性 Linux 容器里以 `WAREHOUSE_DISPOSABLE_CONTAINER=1` 运行，它会在容器自身的 `/etc/config`、`/etc/init.d` 和 `/root` 建立故障桩，检查回退失败的报告、后续服务继续恢复及 firewall4 检查失败时的自动恢复。它不会验证无线或真实转发路径。
