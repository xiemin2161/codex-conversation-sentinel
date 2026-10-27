# Setup and runtime

## Settings to collect

Use the connected Codex host's local timezone. Show the recommended schedule in that timezone before creating anything:

| Setting | Recommended value |
|---|---|
| Slow window | 00:00 to 11:30; every hour at :30, including the final scan at 11:30 |
| Fast window | After 11:30 through midnight; every 10 minutes, including the final scan at midnight |
| Actual compaction trigger | 3 events |
| Main task turn trigger | 20 turns |
| Transcript log trigger | 50 decimal MB = 50,000,000 bytes |
| Old title prefix | `OLD·` |
| New title prefix | `NEW·` |
| Create same-project local successor on a trigger | Yes |

The three numeric triggers use OR logic: reaching any one is sufficient. The user can accept the full set or change fields individually. Thresholds must be positive whole numbers; prefixes must be non-empty, single-line strings and must differ. Use exactly the strings the user chooses.

If the user disables automatic successor creation, the monitor may prepare and report a handoff checkpoint, but it must not call `create_thread` itself or tell the source to create one.

## Create or update the paired automations

1. Inspect the current Codex automation records under the configured `CODEX_HOME` (default `~/.codex`) and find tasks named `快速巡检` and `慢速巡检`. Read their IDs, target thread, and current status. Never edit unrelated automations. If a matching task belongs to another thread, stop and ask which thread should own this installation. Update matching tasks instead of creating duplicates. Omit `targetThreadId` when the tool defaults to the current local conversation; never create a cloud task or a separate conversation for setup.
2. Prepare both tasks as `PAUSED`. Use the app's `automation_update` tool for every create or update. Do not write automation TOML files yourself. The slow task is the shared runtime directory because the checker reads its sibling `automation.toml` to identify the monitoring thread.
3. If creating the slow task, create it paused first and use a temporary prompt that cannot run while paused. Read its returned automation ID, locate the corresponding real automation directory, and place the bundled `scripts/check_compactions.py` there. If that mapping cannot be verified, leave both tasks paused and report the blocker.
4. In that same directory write `monitor-config.json` using the values the user selected:

   ```json
   {
     "timezone": "<host local timezone>",
     "splitTime": "11:30",
     "fastEveryMinutes": 10,
     "slowEveryHours": 1,
     "thresholds": {
       "compactions": 3,
       "taskTurns": 20,
       "logBytes": 50000000
     },
     "oldTitlePrefix": "OLD·",
     "newTitlePrefix": "NEW·",
     "autoCreateSuccessor": true
   }
   ```

   Convert a user-entered log size in MB to bytes using decimal MB (1 MB = 1,000,000 bytes). The checker reads `thresholds`; the schedule and title fields are read by the heartbeat instructions.
5. Also create `mode-state.json` in that directory with `userPaused: false`, the selected timezone and split time, `activeMode` set from the current local time (slow through the split; fast after it), and empty `overrideMode` / `expiresAtLocal` fields. Keep an already active matching mode if updating existing tasks.
6. Build both complete prompts from the template below, replacing every `{{...}}` value. Create the fast task paused if it does not already exist. Set each task's recurrence through `automation_update` to match the user's plain-language cadence and the connected Codex scheduler. Keep recurrence syntax internal; explain schedules to the user as clock times and intervals.
7. Activate only the task that owns the current local-time window and leave the other paused. On a later boundary crossing, the active task must pause itself first and then activate its pair. If either operation fails, keep a recoverable state and report which task remains active. Verify both records with the automation tool before reporting success.

For existing tasks, use `automation_update` view/read first and preserve fields unrelated to this skill. If either task is missing and the app tool cannot create it, do not claim installation succeeded. If setup fails partway through, leave created automations paused and explain how to resume setup.

## Heartbeat prompt template

Use the same rules in both prompts. Fill `{{MODE}}`, `{{WINDOW}}`, `{{CADENCE}}`, and `{{SCRIPT_PATH}}`. `{{SLOW_ID}}` and `{{FAST_ID}}` are the exact IDs returned by `automation_update`.

```text
你是本地 Codex 全局对话巡检 heartbeat，模式={{MODE}}，负责窗口={{WINDOW}}，运行频率={{CADENCE}}。配对任务 ID：slow={{SLOW_ID}}，fast={{FAST_ID}}。巡检脚本：{{SCRIPT_PATH}}。只做巡检、交接协调和短状态报告，不继续被监控项目的业务工作。

每次运行：先读取与脚本同目录的 monitor-config.json、mode-state.json 和两个 heartbeat 的当前状态。userPaused=true 时不做巡检、不自动恢复。按配置时区判断窗口；11:30 的慢速 tick 与午夜的快速 tick 是各自模式的最后一轮巡检，完成本轮后先暂停自身，再启用配对任务。其他超出本模式窗口的 tick 则先暂停自身、启用配对任务并结束本轮。跨时段切换必须保持至多一个 ACTIVE。用户明确要求暂停/恢复/临时切换时，更新 mode-state 并按用户要求更新任务状态；临时覆盖在下一个窗口边界过期。

在正常窗口内调用 list_threads(limit=50)，将返回的原始 JSON（保留外层包装，不要只传数组）交给脚本 scan，并读取 scanned、eligible、pending、warnings。脚本只统计 hostId=local、kind=codex、active 且没有活动工具/等待用户标记的线程。压缩次数只数真实 compacted 事件；任务轮数按唯一 task_started turn_id 计；日志大小按实际字节数计。任一配置阈值命中即符合条件。不得用轮数推测压缩数，也不得凭标题、估值或摘要编造指标。

处理 eligible 时，对每个源线程先执行脚本 claim <源threadId>。只有返回 claimed=true 才能调用一次 send_message_to_thread，并使用脚本返回的 handoffId。通知必须要求源线程停止后续业务工作，等当前单个工具/原子操作安全返回后立即保存交接，不继续做下一步业务；保留现有修改与运行状态，不回滚、不删除。checkpoint 写到当前项目 `.codex/work/<任务标识>/handoff/continuation-state.json` 和配套说明；projectless 对话写到自己的 `work/<任务标识>/handoff/`。checkpoint 必须包含原始目标、最新要求和授权边界、已完成工作/文件/产物/验证结果、全部未完成项及下一步、运行状态、触发阈值、源 ID、handoffId 和 successor ID。autoCreateSuccessor=true 时，由源线程在同一项目、local 环境创建唯一 successor；projectless 协调线程创建 projectless successor。新线程先确认收到完整 handoff，并等源线程 idle 后再继续业务。不得创建云端线程。若用户未授权自动创建，则只准备 checkpoint 并报告待用户操作。

发送指令后立即执行脚本 sent <源threadId>。工具受理不等于送达。后续每轮必须按 pending.nextAction 使用脚本要求的核验、重试或催办动作；不能重复发送，除非脚本明确允许。只有从目标本机日志核验到完整 handoffId 和来源后，才报告目标日志已收到。源线程 idle 后核对 checkpoint、唯一 successor ID、接续线程已启动、完整 handoff 已读，以及标题实际生效。新旧标题使用配置的 oldTitlePrefix/newTitlePrefix 加同一个基名；先剥掉旧的重复前缀再加新前缀。标题修改后用 list_threads 核实。交接证据不全时保留 pending 并准确报告卡点，不说交接已完成。

达到阈值的线程若对应未完成 Codex goal，或 goal 状态无法安全读取，遵循脚本保护结果，不自动交接。扫描被截断、日志缺失/损坏、保护状态未知或工具失败时，不伪造指标/交接结果，简短报告阻塞原因。

巡检协调线程自身达到阈值时，不向自己发送消息；按 coordinator continuation 状态创建唯一 successor、传递完整协调规则，并在确认 successor 已启动和收到交接后，将两条 heartbeat 绑定到新协调线程，保持当前快慢模式。不得创建重复 successor。

每轮简短报告活跃本地对话的标题、压缩次数、任务轮数、日志 MB、是否触发及通知/交接的已核实状态。使用脚本提供的 notificationNote；不要输出完整对话正文、日志原文、隐私内容或 thread ID。
```

After creating both prompts, verify that neither contains unresolved `{{...}}` placeholders and that each references the same slow-directory checker.

