# 剩余迁移的人工操作

项目源码、旧版交付副本、原始 ZIP、MQTT 本地配置和数据卷备份已迁入 `D:\CodeHomework\warehouse-netops`。运行中的容器挂载已指向新工作区。

自动审批拦截了包含旧工具环境迁移和旧快捷路径清理的组合命令，仅返回 `blocked by policy`，未提供具体原因。用户决定自行处理以下剩余步骤，本说明没有自动执行这些操作。

## 归档旧工具环境

新的 PlatformIO 环境为 `.cache\pio-venv`，缓存为 `.cache\platformio`。旧虚拟环境可作为归档保留，其内部启动脚本可能包含旧绝对路径，当前编译使用新环境。

在 PowerShell 中自行运行以下明确路径的迁移命令。

```powershell
New-Item -ItemType Directory -Path 'D:\CodeHomework\warehouse-netops\archive\v0.1-tool-environments' -Force
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\warehouse-pio-venv' -Destination 'D:\CodeHomework\warehouse-netops\archive\v0.1-tool-environments\warehouse-pio-venv'
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\warehouse-service-test-env' -Destination 'D:\CodeHomework\warehouse-netops\archive\v0.1-tool-environments\warehouse-service-test-env'
```

如果目标目录已存在，先查看两边内容。避免覆盖同名目录。

## 调研预览图和迁移过程脚本

```powershell
New-Item -ItemType Directory -Path 'D:\CodeHomework\warehouse-netops\archive\research-previews' -Force
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\selection-review-05.png' -Destination 'D:\CodeHomework\warehouse-netops\archive\research-previews\selection-review-05.png'
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\selection-review-06.png' -Destination 'D:\CodeHomework\warehouse-netops\archive\research-previews\selection-review-06.png'
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\selection-review-07.png' -Destination 'D:\CodeHomework\warehouse-netops\archive\research-previews\selection-review-07.png'
Move-Item -LiteralPath 'C:\Users\32908\Documents\Codex\2026-11-17\wo\work\migrate-warehouse.ps1' -Destination 'D:\CodeHomework\warehouse-netops\archive\migrate-baseline.ps1'
```

## 旧 p 快捷路径

旧路径 `C:\Users\32908\Documents\Codex\2026-11-17\wo\work\p` 是用于缩短编译命令的 Junction，原目标为同一目录下的 `warehouse-pio-core`。实际缓存已迁到新工作区 `.cache\platformio`。可以在文件管理器中确认 `p` 为旧链接后移除该链接项，当前编译已直接使用 D 盘缓存。

以上操作只涉及这次生成的工具环境和中间文件。当前源码在新工作区，运行数据在既有 Docker 命名卷。
