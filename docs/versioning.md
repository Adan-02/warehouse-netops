# 版本发布与回退

源码仓库为 [Adan-02/warehouse-netops](https://github.com/Adan-02/warehouse-netops)，默认分支为 `main`。`v0.3.0-baseline` 保存首次入库的虚拟实验源码。运行镜像、数据库、私密配置和源码分别管理。

## 源码

每次改动在功能分支完成，测试通过后提交，再合并到主分支。用 `git log --oneline --decorate` 查看提交；用 `git diff <标签>..HEAD` 比较版本。修改已经推送的代码时，采用新提交或 `git revert`，不重写远程历史。

## 服务版本

以下命令从项目根目录执行。构建要求 Git 工作区干净，镜像记录提交 SHA、应用源码摘要和版本；制品保存在本机运行目录的 `releases/`，不进入 Git。

```sh
python virtualization/release.py status
python virtualization/release.py build --version v0.5.0
python virtualization/release.py deploy --release-id v0.5.0-<提交前12位>
```

发布器复用广州至服务机的既有 WireGuard/SSH 路径，流式传输并校验制品。发布前备份数据库及配置，保存旧镜像；只更新看板容器，Broker 与数据卷沿用。运行版本通过 `/api/version`、实际 image ID 和采样增长核对。

```sh
python virtualization/release.py rollback
python virtualization/release.py status
```

`rollback` 回到 `previous` 指向的应用版本，并把刚退下的版本记为新的 `previous`。它不将数据库恢复成旧快照，因此不会主动删除发布后产生的采样。兼容性预检不通过时停止降级。

## 数据备份与隔离恢复

```sh
python virtualization/release.py backup
python virtualization/release.py restore-check --backup <本机备份路径.sqlite3>
```

备份使用 SQLite 在线备份 API，带文件校验、历史行数及最后一条记录信息。数据库和含凭据的配置包留在私密目录。`restore-check` 恢复到临时隔离副本，再检查完整性与记录，不写活动数据库。

Git 回退、应用回滚和数据恢复是三种不同操作。完整环境重建也不能用覆盖 VMX 或删除 VMware 锁文件代替；运行中的虚拟机与快照差分盘由日常控制工具管理。
