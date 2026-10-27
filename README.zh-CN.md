# Codex Conversation Sentinel

[English](README.md) · [简体中文](README.zh-CN.md)

一个可安装的 Codex 技能，用于配置两条互斥的本地对话检查心跳任务，并设定它们的交接阈值。

## 项目文件

技能源文件位于仓库根目录。`VERSION` 保存安装包版本号；运行 `./scripts/package.sh` 可生成安装用 ZIP。脚本会将旧的根目录安装包归档到 `dist/history/`，拒绝重复打包已使用过的版本号，并输出新 ZIP 的 SHA-256。

## 技术与目录结构

- `SKILL.md` 定义设置流程和用户可选项。
- `references/setup-and-runtime.md` 说明心跳调度、本地状态和交接验证。
- `scripts/check_compactions.py` 读取本机 Codex 会话日志，并计算配置的阈值。
- `scripts/package.sh` 创建带版本号的安装 ZIP；运行时设置通过已连接的 Codex 自动化和会话工具完成。

## 安装

解压 `dist/codex-conversation-sentinel-<version>.zip`，将其中的 `codex-conversation-sentinel` 目录复制到 Codex 技能目录（通常是 `~/.codex/skills/`），然后重新加载 Codex。运行 `$codex-conversation-sentinel`，即可选择或采用推荐的计划、阈值、标题前缀和后继会话行为。完成设置后，该技能会在已连接的 Codex 中创建两项配对的心跳任务。

## 推荐默认值

- 慢速检查：00:00–11:30，每小时的 :30 执行；11:30 是最后一次慢速检查。
- 快速检查：11:30 后运行至午夜，每 10 分钟执行；午夜是最后一次快速检查。
- 任一条件达到时触发交接：实际压缩 3 次、任务轮次 20 轮，或会话日志达到 50 MB（十进制 MB）。
- 标题前缀：源会话使用 `OLD·`，后继会话使用 `NEW·`。

已连接的 Codex 应用需要提供自动化和会话工具。监控脚本会读取本机 Codex 会话日志，以精确计算各项次数。

如果无法读取本地目标状态数据库，检查器会安全失败，并在本次检查中抑制自动交接；心跳任务应报告此限制，而不应将状态视为安全。
