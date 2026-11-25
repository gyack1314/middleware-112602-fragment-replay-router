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

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
