# 版本发布与回退

源码仓库为 [Adan-02/warehouse-netops](https://github.com/Adan-02/warehouse-netops)，默认分支为 `main`。`v0.3.0-baseline` 保存首次入库的虚拟实验源码。运行镜像、数据库、私密配置和源码分别管理。

## 源码

每次改动在功能分支完成，测试通过后提交，再合并到主分支。用 `git log --oneline --decorate` 查看提交；用 `git diff <标签>..HEAD` 比较版本。修改已经推送的代码时，采用新提交或 `git revert`，不重写远程历史。

## 服务版本

以下命令从项目根目录执行。构建要求 Git 工作区干净，镜像记录提交 SHA、应用源码摘要和版本；制品保存在本机运行目录的 `releases/`，不进入 Git。

```sh
python virtualization/release.py status
python virtualization/release.py build --version v0.5.1
python virtualization/release.py deploy --release-id v0.5.1-<构建输出的提交前12位>
```

版本号选择本次新版本，`release_id` 使用 `build` 输出的完整值。同一发布目录不可覆盖；已经存在时先用 `status` 核对，不重复构建或盲目重传。以上示例命令中的尖括号部分需要替换为实际值。

发布器复用广州至服务机的既有 WireGuard/SSH 路径，流式传输并校验制品。发布前备份数据库及配置，保存旧镜像；只更新看板容器，Broker 与数据卷沿用。运行版本通过 `/api/version`、实际 image ID、正常入库和采样增长核对，广州端再次读取版本确认所见一致。

`status` 运行独立的只读远端程序，不上传管理器、不创建发布目录。安装和切换只在显式更新操作中执行；首次环境预装入口见[接手说明](HANDOFF.md)。

```sh
python virtualization/release.py rollback
python virtualization/release.py status
```

`rollback` 回到 `previous` 指向的应用版本，并把刚退下的版本记为新的 `previous`。它不将数据库恢复成旧快照，因此不会主动删除发布后产生的采样。兼容性预检不通过时停止降级。

发布事务使用独立版本目录、共享私密配置及 `current`／`previous` 指针。数据库迁移保留旧读数列和旧消息接口；回到旧镜像后，页面及功能也随之回到对应版本。连接中断导致结果未知时，先查询状态再决定重试，不能把 SSH 超时直接当作切换失败。

## 数据备份与隔离恢复

```sh
python virtualization/release.py backup
python virtualization/release.py restore-check --backup <本机备份路径.sqlite3>
```

备份使用 SQLite 在线备份 API，带文件校验、历史行数及最后一条记录信息。命令输出数据库备份路径；将该路径传给 `restore-check`。数据库和含凭据的配置包留在私密目录，恢复前核对对应清单。`restore-check` 恢复到临时隔离副本，再检查完整性与记录，不写活动数据库。

广州的 Webhook 游标及发送队列有独立的一致性备份，见[观测与事件通知](../ops/README.md)。数据库备份不包含 VM 磁盘，也不等同于整台虚拟机快照。

Git 回退、应用回滚和数据恢复是三种不同操作。完整环境重建也不能用覆盖 VMX 或删除 VMware 锁文件代替；运行中的虚拟机与快照差分盘由日常控制工具管理。

冷启动复核使用 `python virtualization/control.py stop` 和 `python virtualization/control.py start --wait --timeout 600`，等待现场与广州业务恢复后再核对版本和历史。默认就绪预算同为 600 秒。若已关机 VM 的标签页仍占用配置，关闭对应标签页后重试，不删除锁文件；具体操作见[虚拟实验日常维护](virtual-lab.md#日常启动与查看)。
