"""回放消息路由器。

接入目录服务、来源设备与回放台之间实际交换的九类消息，完成：

- 引用目录：按运行代（run）与别名（alias）保存修订，单候选才消歧；
- 分片接入：以 (run, source, source_seq) 为保管事实去重，归属不明时挂起；
- 回放请求与确认：动作内回放包从 1 起编号，必需项齐备才出包，可选项以
  显式缺口继续，分包确认后才允许公开回放；
- 目录更正：未交付的分片静默改派重组，已被回放台接受的分片以关联原包的
  更正交付，已播放事实永久可查；
- 重启恢复：输入日志可重放，分片保管、包序号、确认与播放进度均可恢复。

仅使用标准库，载荷只当作不透明标识保存，不解释其内容。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["Router", "JournalStore", "ROUTER_KINDS"]

# 与 examples/message_types.json 保持一致的输入消息集合。
ROUTER_KINDS = frozenset(
    {"mapping", "fragment", "replay", "tick", "receive", "ack", "played",
     "restart", "inspect"}
)


class Directory:
    """引用目录：记录每个运行代内别名的修订与候选工步。

    运行代是命名空间边界：同名别名在不同 run 内互不相干，目录发布的别名
    只在其所属 run 内有效。修订号单调递增，迟到的旧修订一律忽略。
    """

    def __init__(self):
        # (run, alias) -> {"revision": int|None, "actions": tuple,
        #                  "revisions": [(revision, actions)]}
        self._entries = {}

    def publish(self, run, alias, revision, actions):
        """发布一次目录修订，返回是否引起目录变化。"""
        actions = tuple(actions)
        key = (run, alias)
        entry = self._entries.get(key)
        if entry is None:
            entry = {"revision": None, "actions": None, "revisions": []}
            self._entries[key] = entry
        if entry["revision"] is not None and revision <= entry["revision"]:
            # 重复发布或迟到的旧修订：目录以最高 revision 为准。
            return False
        entry["revisions"].append((revision, actions))
        entry["revision"] = revision
        entry["actions"] = actions
        return True

    def resolve(self, run, alias):
        """解析别名，返回 (状态, 取值)。

        - ("unreferenced", None)：目录从未发布过该引用；
        - ("ambiguous", actions)：多个候选工步，当前无法消歧；
        - ("resolved", action)：唯一候选工步。
        """
        entry = self._entries.get((run, alias))
        if entry is None or entry["actions"] is None:
            return "unreferenced", None
        if len(entry["actions"]) == 1:
            return "resolved", entry["actions"][0]
        return "ambiguous", tuple(entry["actions"])

    def inspect_run(self, run):
        """给出一个运行代内全部别名的修订轨迹。"""
        result = []
        for (entry_run, alias), entry in sorted(self._entries.items()):
            if entry_run != run:
                continue
            result.append({
                "alias": alias,
                "revision": entry["revision"],
                "actions": list(entry["actions"]),
                "history": [
                    {"revision": revision, "actions": list(actions)}
                    for revision, actions in entry["revisions"]
                ],
            })
        return result


class Fragment:
    """一份分片的保管事实。"""

    def __init__(self, run, source, source_seq, alias, channel, payload, at):
        self.run = run
        self.source = source
        self.source_seq = source_seq
        self.alias = alias
        self.channel = channel
        self.payload = payload
        self.first_seen = at
        # held：尚未取得明确归属；attributed：已归到唯一工步。
        self.state = "held"
        # held 时取 unreferenced 或 ambiguous。
        self.reason = None
        self.action = None
        # 已交付记录：(request_id, packet_no)，同一分片只接纳一次。
        self.deliveries = []
        # 归属变迁留痕，供值班追查。
        self.history = []

    @property
    def key(self):
        return self.run, self.source, self.source_seq

    def as_dict(self):
        return {
            "run": self.run,
            "source": self.source,
            "source_seq": self.source_seq,
            "alias": self.alias,
            "channel": self.channel,
            "payload": self.payload,
            "state": self.state,
            "reason": self.reason,
            "action": self.action,
            "deliveries": [
                {"request": request_id, "packet": packet_no}
                for request_id, packet_no in self.deliveries
            ],
            "history": list(self.history),
        }


class RequestState:
    """一个回放请求的全部进度。"""

    def __init__(self, record, at):
        self.request = record["request"]
        self.run = record["run"]
        self.action = record["action"]
        self.required = tuple(record["required"])
        self.optional = tuple(record["optional"])
        self.until = record["until"]
        self.opened_at = at
        # 已交付回放包：{"packet", "at", "gaps", "fragments", "superseded"}
        self.packets = []
        # 待回放台拉取的事件（回放包、更正）。
        self.outbox = []
        # packet_no -> 已确认 channel 集合（包内分片分别确认）。
        self.acked = {}
        self.played_packets = set()
        # 与该请求有关的全部更正（含已交付与待交付）。
        self.corrections = []
        self.expired = False
        self.ready_announced = False


class JournalStore:
    """把输入消息追加保存到本地 JSONL，供进程重启后重放。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row):
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self):
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as source:
            return [json.loads(line) for line in source if line.strip()]


class Router:
    """回放消息路由器：逐条处理消息，返回给中间件/回放台的输出事件。"""

    def __init__(self, store=None):
        self.store = store
        self.now = 0
        self.directory = Directory()
        self.fragments = {}
        self.requests = {}
        # (run, action) -> request_id，保证一个动作只有一个有效接纳结果。
        self.action_owner = {}
        self._journal = []

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def handle(self, row):
        """处理一条输入消息，返回由此产生的输出事件列表。"""
        kind = row["kind"]
        if kind not in ROUTER_KINDS:
            raise ValueError(f"未登记的消息名称：{kind}")
        self.now = row["at"]
        self._journal.append(row)
        if self.store is not None:
            self.store.append(row)
        if kind == "restart":
            return self._on_restart()
        events = self._dispatch(kind, row["record"])
        events.extend(self._sweep_expiry())
        return events

    def _dispatch(self, kind, record):
        handler = getattr(self, f"_on_{kind}")
        return handler(record)

    # ------------------------------------------------------------------
    # 目录发布
    # ------------------------------------------------------------------

    def _on_mapping(self, record):
        run = record["run"]
        alias = record["alias"]
        changed = self.directory.publish(
            run, alias, record["revision"], tuple(record["actions"])
        )
        if not changed:
            return []
        # request_id -> packet_no -> 变更明细，把同包分片合并成一条更正。
        buckets = {}
        for fragment in self._fragments_of_run(run):
            if fragment.alias != alias:
                continue
            self._reevaluate(fragment, buckets)
        return self._materialize_corrections(buckets)

    def _reevaluate(self, fragment, buckets):
        """目录变化后重新判定一个分片的归属。"""
        status, value = self.directory.resolve(fragment.run, fragment.alias)
        if status == "resolved":
            new_action = value
            if fragment.state == "held":
                fragment.state = "attributed"
                fragment.reason = None
                fragment.action = new_action
                fragment.history.append({
                    "at": self.now, "event": "attributed", "action": new_action,
                })
            elif fragment.action != new_action:
                old_action = fragment.action
                fragment.action = new_action
                fragment.history.append({
                    "at": self.now, "event": "reassigned",
                    "from_action": old_action, "to_action": new_action,
                })
                self._collect_reassignment(fragment, buckets, old_action, new_action)
            return
        # 目录回到不可解析状态（理论上修订只会减少歧义，仍做防御）。
        if fragment.state == "attributed":
            old_action = fragment.action
            fragment.state = "held"
            fragment.reason = status
            fragment.action = None
            fragment.history.append({
                "at": self.now, "event": "reassigned",
                "from_action": old_action, "to_action": None,
            })
            self._collect_reassignment(fragment, buckets, old_action, None)
        else:
            fragment.reason = status

    def _collect_reassignment(self, fragment, buckets, old_action, new_action):
        for request_id, packet_no in fragment.deliveries:
            buckets.setdefault(request_id, {}).setdefault(packet_no, []).append({
                "source": fragment.source,
                "source_seq": fragment.source_seq,
                "channel": fragment.channel,
                "payload": fragment.payload,
                "from_action": old_action,
                "to_action": new_action,
            })

    def _materialize_corrections(self, buckets):
        events = []
        for request_id, per_packet in buckets.items():
            request = self.requests[request_id]
            for packet_no, changes in sorted(per_packet.items()):
                correction = {
                    "kind": "correction",
                    "at": self.now,
                    "request": request_id,
                    "cno": len(request.corrections) + 1,
                    "packet": packet_no,
                    "run": request.run,
                    "changes": changes,
                }
                request.corrections.append(correction)
                request.outbox.append(correction)
                request.packets[packet_no - 1]["superseded"] = True
            events.extend(self._refresh_ready(request))
        return events

    # ------------------------------------------------------------------
    # 分片接入
    # ------------------------------------------------------------------

    def _on_fragment(self, record):
        key = (record["run"], record["source"], record["source_seq"])
        if key in self.fragments:
            # 旧端重发：保管事实已存在，不产生第二份接纳结果。
            return [{
                "kind": "fragment_duplicate",
                "at": self.now,
                "run": key[0],
                "source": key[1],
                "source_seq": key[2],
            }]
        fragment = Fragment(
            run=record["run"], source=record["source"],
            source_seq=record["source_seq"], alias=record["alias"],
            channel=record["channel"], payload=record["payload"], at=self.now,
        )
        status, value = self.directory.resolve(fragment.run, fragment.alias)
        if status == "resolved":
            fragment.state = "attributed"
            fragment.action = value
            fragment.history.append({
                "at": self.now, "event": "attributed", "action": value,
            })
        else:
            fragment.reason = status
            fragment.history.append({
                "at": self.now, "event": "held", "reason": status,
                "candidates": None if status == "unreferenced" else list(value),
            })
        self.fragments[key] = fragment
        # 时间接近不构成归属依据：无法消歧时不挂到任何回放方。
        return []

    def _fragments_of_run(self, run):
        return [f for f in self.fragments.values() if f.run == run]

    # ------------------------------------------------------------------
    # 回放请求、拉取、确认
    # ------------------------------------------------------------------

    def _on_replay(self, record):
        request_id = record["request"]
        if request_id in self.requests:
            raise ValueError(f"回放请求编号重复：{request_id}")
        owner_key = (record["run"], record["action"])
        if owner_key in self.action_owner:
            raise ValueError(
                f"动作 {record['run']}/{record['action']} 已有有效回放请求 "
                f"{self.action_owner[owner_key]}"
            )
        self.requests[request_id] = RequestState(record, self.now)
        self.action_owner[owner_key] = request_id
        return [{
            "kind": "request_opened",
            "at": self.now,
            "request": request_id,
            "run": record["run"],
            "action": record["action"],
            "required": list(record["required"]),
            "optional": list(record["optional"]),
            "until": record["until"],
        }]

    def _on_receive(self, record):
        request = self._require_request(record["request"])
        events = []
        if not request.expired:
            packet = self._build_packet(request)
            if packet is not None:
                request.outbox.append(packet)
        # 只投递尚未交付的事件；重连或重启后的重复拉取不会多出包。
        while request.outbox:
            event = request.outbox.pop(0)
            event = dict(event)
            event["delivered_at"] = self.now
            event["client"] = record["client"]
            if event["kind"] == "packet":
                request.packets[event["packet"] - 1]["delivered_at"] = self.now
            events.append(event)
        if not events:
            events.append({
                "kind": "fetch_empty",
                "at": self.now,
                "request": request.request,
                "client": record["client"],
                "missing_required": self._missing_required(request),
            })
        return events

    def _build_packet(self, request):
        fresh = [
            fragment for fragment in self.fragments.values()
            if fragment.run == request.run
            and fragment.state == "attributed"
            and fragment.action == request.action
            and not fragment.deliveries
        ]
        if not fresh:
            return None
        covered = set()
        for packet in request.packets:
            covered.update(self._live_channels(request, packet))
        covered.update(fragment.channel for fragment in fresh)
        missing = [channel for channel in request.required if channel not in covered]
        if missing:
            # 必需项未齐：不能出包，分片继续保管，等待迟到序号或目录更正。
            return None
        packet_no = len(request.packets) + 1
        gaps = [
            channel for channel in request.optional
            if not self._has_attributed_channel(request, channel)
        ]
        event = {
            "kind": "packet",
            "at": self.now,
            "request": request.request,
            "packet": packet_no,
            "run": request.run,
            "action": request.action,
            "sources": sorted({fragment.source for fragment in fresh}),
            "gaps": gaps,
            "fragments": [
                {
                    "source": fragment.source,
                    "source_seq": fragment.source_seq,
                    "alias": fragment.alias,
                    "channel": fragment.channel,
                    "payload": fragment.payload,
                }
                for fragment in sorted(fresh, key=lambda item: item.key)
            ],
        }
        for fragment in fresh:
            fragment.deliveries.append((request.request, packet_no))
        request.packets.append({
            "packet": packet_no,
            "at": self.now,
            "delivered_at": None,
            "gaps": list(gaps),
            "superseded": False,
            "fragments": event["fragments"],
        })
        return event

    def _on_ack(self, record):
        request = self._require_request(record["request"])
        packet_no = record["packet"]
        if not 1 <= packet_no <= len(request.packets):
            raise ValueError(f"未知回放包：{request.request} 包 {packet_no}")
        packet = request.packets[packet_no - 1]
        known_channels = {item["channel"] for item in packet["fragments"]}
        unknown = set(record["channels"]) - known_channels
        if unknown:
            raise ValueError(f"包 {packet_no} 内不存在分片类型：{sorted(unknown)}")
        acked = request.acked.setdefault(packet_no, set())
        acked.update(record["channels"])
        events = [{
            "kind": "ack_recorded",
            "at": self.now,
            "request": request.request,
            "client": record["client"],
            "packet": packet_no,
            "channels": sorted(acked),
        }]
        events.extend(self._refresh_ready(request))
        return events

    def _on_played(self, record):
        request = self._require_request(record["request"])
        packet_no = record["packet"]
        if not 1 <= packet_no <= len(request.packets):
            raise ValueError(f"未知回放包：{request.request} 包 {packet_no}")
        request.played_packets.add(packet_no)
        # 播放事实只追加不清除，即使旧包事后被更正判定为不再适用。
        return [{
            "kind": "played_recorded",
            "at": self.now,
            "request": request.request,
            "client": record["client"],
            "packet": packet_no,
        }]

    def _on_tick(self, record):
        return self._sweep_expiry()

    def _sweep_expiry(self):
        events = []
        for request in self.requests.values():
            if request.expired or self.now <= request.until:
                continue
            if self._is_ready(request):
                continue
            request.expired = True
            events.append({
                "kind": "request_expired",
                "at": self.now,
                "request": request.request,
                "missing_required": self._missing_required(request),
            })
        return events

    # ------------------------------------------------------------------
    # 公开判定与查询
    # ------------------------------------------------------------------

    def _is_ready(self, request):
        """必需项在当前归属下齐备且均已确认接收，才允许公开回放。"""
        for channel in request.required:
            confirmed = False
            for packet in request.packets:
                if channel not in request.acked.get(packet["packet"], set()):
                    continue
                if channel in self._live_channels(request, packet):
                    confirmed = True
                    break
            if not confirmed:
                return False
        return True

    def _live_channels(self, request, packet):
        """包内当前仍归属于本动作的分片类型（被改派的会被剔除）。"""
        channels = set()
        for item in packet["fragments"]:
            fragment = self.fragments[
                (request.run, item["source"], item["source_seq"])
            ]
            if fragment.state == "attributed" and fragment.action == request.action:
                channels.add(item["channel"])
        return channels

    def _has_attributed_channel(self, request, channel):
        return any(
            fragment.run == request.run
            and fragment.state == "attributed"
            and fragment.action == request.action
            and fragment.channel == channel
            for fragment in self.fragments.values()
        )

    def _missing_required(self, request):
        return [
            channel for channel in request.required
            if not self._has_attributed_channel(request, channel)
        ]

    def _refresh_ready(self, request):
        ready = self._is_ready(request)
        if ready and not request.ready_announced:
            request.ready_announced = True
            return [{
                "kind": "replay_ready",
                "at": self.now,
                "request": request.request,
                "run": request.run,
                "action": request.action,
            }]
        if not ready and request.ready_announced:
            request.ready_announced = False
            return [{
                "kind": "replay_invalidated",
                "at": self.now,
                "request": request.request,
                "missing_required": self._missing_required(request),
            }]
        return []

    def status(self, request):
        if self._is_ready(request):
            return "ready"
        if request.expired:
            return "expired"
        if any(packet["superseded"] for packet in request.packets):
            return "invalidated"
        return "waiting"

    def _on_inspect(self, record):
        if "request" in record:
            return [self._inspect_request(record["request"])]
        return [self._inspect_run(record["run"])]

    def _inspect_request(self, request_id):
        request = self._require_request(request_id)
        return {
            "kind": "inspect",
            "at": self.now,
            "scope": "request",
            "request": request.request,
            "run": request.run,
            "action": request.action,
            "status": self.status(request),
            "until": request.until,
            "required": list(request.required),
            "optional": list(request.optional),
            "missing_required": self._missing_required(request),
            "pending_delivery": len(request.outbox),
            "packets": [
                {
                    "packet": packet["packet"],
                    "at": packet["at"],
                    "delivered_at": packet["delivered_at"],
                    "sources": sorted({
                        item["source"] for item in packet["fragments"]
                    }),
                    "gaps": list(packet["gaps"]),
                    "superseded": packet["superseded"],
                    "played": packet["packet"] in request.played_packets,
                    "acked_channels": sorted(
                        request.acked.get(packet["packet"], set())
                    ),
                    "fragments": packet["fragments"],
                }
                for packet in request.packets
            ],
            "corrections": list(request.corrections),
        }

    def _inspect_run(self, run):
        """按运行代列出保管事实：值班人可凭源序号追查所归工步。"""
        fragments = [
            fragment.as_dict()
            for fragment in self.fragments.values()
            if fragment.run == run
        ]
        fragments.sort(key=lambda item: (item["source"], item["source_seq"]))
        return {
            "kind": "inspect",
            "at": self.now,
            "scope": "run",
            "run": run,
            "directory": self.directory.inspect_run(run),
            "fragments": fragments,
        }

    def trace(self, run, source, source_seq):
        """按 (run, source, source_seq) 直接追查分片归属。"""
        fragment = self.fragments.get((run, source, source_seq))
        if fragment is None:
            raise KeyError((run, source, source_seq))
        return fragment.as_dict()

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------

    def _on_restart(self):
        restart_at = self.now
        rebuilt = self.replay_journal(
            [row for row in self._journal if row["kind"] != "restart"]
        )
        self.now = restart_at
        self.directory = rebuilt.directory
        self.fragments = rebuilt.fragments
        self.requests = rebuilt.requests
        self.action_owner = rebuilt.action_owner
        accepted_packets = sum(
            len(request.packets) for request in self.requests.values()
        )
        return [{
            "kind": "recovered",
            "at": restart_at,
            "requests": len(self.requests),
            "fragments": len(self.fragments),
            "packets": accepted_packets,
        }]

    @classmethod
    def replay_journal(cls, rows, store=None):
        """从一段输入日志重建路由器（不重复产生历史输出事件）。"""
        router = cls(store=store)
        for row in rows:
            if row["kind"] == "restart":
                continue
            router.now = row["at"]
            router._journal.append(row)
            router._dispatch(row["kind"], row["record"])
            router._sweep_expiry()
        return router

    def _require_request(self, request_id):
        request = self.requests.get(request_id)
        if request is None:
            raise ValueError(f"未知回放请求：{request_id}")
        return request
