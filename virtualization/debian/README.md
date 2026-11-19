# Debian 13 虚拟机 NoCloud seed

此目录仅生成新建 VMware 实验虚拟机的初始化数据，不连接云服务器、不修改宿主机网络、不启动 VM。

```powershell
python virtualization/debian/generate_seed.py --role service --output-dir .cache/seeds/service --ssh-public-key-file .secrets/virtual-lab/id_ed25519.pub --instance-id warehouse-service-v1 --mac 00:50:56:30:00:10
```

`--role` 为 `service`、`iot` 或 `office`。MAC 必须与该 VM 唯一的 e1000 网卡一致。服务机、IoT、办公机 staging 地址分别为 `192.168.200.60`、`.61`、`.62`，网关和 DNS 都是 `.1`，无需启用 VMware DHCP。

生成 `user-data`、`meta-data`、`network-config`，将这三个文件放进卷标为 `cidata` 的 ISO 后挂到 Debian 13 cloud image VM。其余 `.sh`、`.network` 文件供审查；完整内容已经嵌入 user-data，无需额外挂载。数据采用 JSON 格式，是 cloud-init 可读取的合法 YAML，不依赖 PyYAML。

用户 `warehouse` 只允许 SSH 公钥登录，提供本实验 VM 内的免密 sudo。密码与 root SSH 禁用。仅 office VM 配置本地 LightDM 自动登录；这不开放 SSH 密码登录。Shell 初始化命令有 VMware DMI 与实验标记双重保护。

三种角色都启用 `serial-getty@ttyS0`，通过 VMware COM1 本地 named pipe 自动登录 `warehouse`；无额外 TCP 监听或管理网卡。GRUB 配置保留原参数并增加 `console=ttyS0,115200n8`，便于启动诊断；脚本会运行 `update-grub`。串口入口是仅供本地实验的控制台，宿主必须将 named pipe 保持为本机访问。

通过 SSH 读取：

```sh
sudo cloud-init status --wait --long
cat /var/lib/warehouse/bootstrap.json
```

必须同时满足 cloud-init 成功、JSON 的 `status` 为 `ready`，再核对所需软件。安装失败写 `failed` 和退出码；不会生成假成功。`ready` 只代表系统软件准备好，不代表项目 Docker 镜像或应用已部署。服务机需要继续复制项目、生成实验专用 `.env`、构建并启动现有服务；IoT 机需要复制外部发布器和实验设备凭据。

准备工作完成后：

```sh
sudo warehouse-switch-network final --poweroff
```

脚本准备下一次启动的 systemd-networkd 配置，禁用 cloud-init 后续网络覆盖，不重启当前接口；写入 `network-pending.json` 后关机。关机后把唯一网卡从 NAT 切到角色对应的 LAN Segment，再开机。服务机固定 `192.168.30.10/24`、网关 `192.168.30.1`；office/iot 从 OpenWrt DHCP 获取地址。不要给角色 VM 再加第二块管理/NAT 网卡。

需要回到 staging 时，在 VMware 控制台执行 `sudo warehouse-switch-network staging --poweroff`，关机后把唯一网卡改回 VMnet8 NAT。该模式不是正式验收状态。

切网脚本会保存其覆盖文件的备份到 `/var/lib/warehouse/network-backups/`，最终网络验证须以实际 IP、路由和跨区探测为准；`network-pending.json` 仅表示已准备，不表示切网已成功。

已核实的 Debian trixie 包名与依据：

- `docker.io`、`docker-cli`、`docker-compose`：Debian 的 `docker-compose` 已是 v2，不能假设其包名为 `docker-compose-v2`。[包说明](https://packages.debian.org/trixie/docker-compose)
- IoT 使用发行版 `python3-paho-mqtt` 2.1.0，可避免 PEP 668 下往系统 Python 执行 pip 安装。[包说明](https://packages.debian.org/trixie/python3-paho-mqtt)
- systemd-networkd 属于 `systemd` 包；`systemd-resolved` 单独安装。初始解析器配置保持 staging DNS，切网后才使用各区网关。[systemd 文件列表](https://packages.debian.org/trixie/amd64/systemd/filelist)、[resolved 包说明](https://packages.debian.org/trixie/systemd-resolved)

本目录的生成与结构检查不能代替 VMware 实际引导、软件安装和分区验收。

本地结构检查：`python -m unittest discover -s virtualization/debian -p 'test_*.py' -v`。输出文件强制 LF 换行；在 Windows 将脚本通过 subprocess stdin 传给 Linux 工具时，应发送 UTF-8 bytes，避免 text 模式把 LF 转成 CRLF。
