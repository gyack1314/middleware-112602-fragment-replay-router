# 训练回放代理的来源消歧与分片确认

动作属于 run，来源的 source_seq 在同一 run 内唯一。旧别名的多个映射可以先挂起，后来由目录发布更高 revision 减少歧义。回放包在同一动作下从 1 起编号。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **mapping**　run、alias 的 revision 与候选 actions。
- **fragment**　run、source 与 source_seq、alias、channel 和 payload。
- **replay**　为 run/action 发出申请 request，required 与 optional 为分片类型列表。 until 为最后等待秒点，必需项未齐时可查询欠项，不能发布回放。
- **tick**　推进逻辑整数秒点。
- **receive**　消费者 client 按 request 取消息。
- **ack**　消费者按 request 和包序号确认 channels。
- **played**　消费者确认指定包实际播放。
- **restart**　中间件重启。
- **inspect**　按 request 或 run 查询。

## 示例

- late-directory-rewrite.jsonl
- same-sequence-two-runs.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 -I scenario_reader.py examples
    python3 -I -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。

## 回放消息路由器

`replay_router.py` 在九类输入消息之上提供可复用的 Python 路由层，`run_router.py`
是把一段演练跑成审计输出的命令行入口。所有判断只依据消息中的明确归属字段，
时间接近不构成归属依据；载荷（如 `marker-p`、`from-a`）只作不透明标识保存。

### 目录与归属规则

- 目录按 `(run, alias)` 保存 revision 与候选 actions，revision 单调递增；
  迟到的旧修订与重复发布被忽略，目录历史完整留痕。
- run 是工步命名空间与运行代边界：同名 alias、同号 source_seq 跨 run 互不相干，
  不能用另一运行代的同号数据填补缺口。
- 别名只有唯一候选时才消歧；多候选为 `ambiguous`、目录未发布为 `unreferenced`，
  两种状态下分片一律挂起，不派给任何回放方。目录发布更高 revision 后自动重评。

### 分片接入

- 保管事实键为 `(run, source, source_seq)`，同键重发只回执 `fragment_duplicate`，
  一个动作只允许一个有效接纳结果。
- 目录更正后的旧分片：从未交付给回放台的静默改派并重新组包；已交付（含已确认、
  已播放）的通过关联原包的 `correction` 交付，旧包标记 `superseded`，
  `played` 事实永久保留可查。
- 已被某回放包接受的分片不会在改派目标动作下重新打包，避免同一保管事实产出两份。

### 回放请求与确认

- `replay` 建立 request 与 `(run, action)` 的唯一归属，重复请求或重复动作均拒绝。
- `receive` 时组包：必需分片类型齐备才出包，动作内包号从 1 起递增；
  可选类型未到时在 `gaps` 中给出显式缺口，可带缺口继续交付。
- 包信封携带 `run`、`action`、`sources`（来源范围）与每片的 source/source_seq，
  保证包可解释。
- `ack` 按包内分片类型分别确认；必需项在当前归属下全部确认后才发出
  `replay_ready`。更正使旧包失效时发出 `replay_invalidated` 并列欠项。
- 超过 `until` 且仍未就绪的请求转为 `expired`，不再出新包；已就绪请求不过期。

### 重启与值班查询

- 指定 `--state` 后输入消息追加到本地 JSONL 日志；`restart` 或新进程用
  `Router.replay_journal` 重放，恢复目录、分片保管、包序号、确认、播放与待交付更正。
- `inspect`（request）给出状态、欠项、每包缺口/确认/播放/更正；
  `inspect`（run）与 `Router.trace(run, source, source_seq)` 支持按源序号
  追查所归工步及完整变迁历史。

### 运行方式

    # 内存处理，输出每条路由事件与汇总
    python3 -I run_router.py examples/late-directory-rewrite.jsonl

    # 持久化输入日志，之后从日志重放恢复
    python3 -I run_router.py examples/same-sequence-two-runs.jsonl --state .router-state
    python3 -I run_router.py ignored --state .router-state --recover

路由器测试同样只使用本地合成的位姿（pose）与触觉（touch）标识：

    python3 -I -m unittest test_replay_router -v

### 主要接口

| 接口 | 作用 |
| --- | --- |
| `Router.handle(row)` | 处理一条输入消息，返回产生的输出事件 |
| `Router.replay_journal(rows)` | 从输入日志重建路由器状态 |
| `Router.trace(run, source, source_seq)` | 按源序号追查分片归属与变迁 |
| `Router.status(request)` | `waiting` / `ready` / `expired` / `invalidated` |
| `JournalStore(path)` | 输入消息的本地 JSONL 追加日志 |
