"""回放消息路由器规则测试。

载荷只用本地合成的位姿（pose）与触觉（touch）标识，不读任何外部资料。
"""

import json
import tempfile
import unittest
from pathlib import Path

from replay_router import JournalStore, Router


def msg(at, kind, **record):
    return {"at": at, "kind": kind, "record": record}


def kinds(events):
    return [event["kind"] for event in events]


def find(events, kind, **fields):
    for event in events:
        if event["kind"] == kind and all(
            event.get(key) == value for key, value in fields.items()
        ):
            return event
    raise AssertionError(f"找不到 {kind} {fields}")


class RouterTestBase(unittest.TestCase):
    def make_router(self):
        return Router()

    def feed(self, router, *rows):
        output = []
        for row in rows:
            output.extend(router.handle(row))
        return output


class DirectoryTests(RouterTestBase):
    def test_ambiguous_alias_holds_fragment_until_directory_resolves(self):
        router = self.make_router()
        events = self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk", "hold"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=["touch"], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        )
        self.assertNotIn("packet", kinds(events))
        # 无法消歧的分片不挂到任何回放方。
        held = router.trace("r", "hand", 1)
        self.assertEqual(held["state"], "held")
        self.assertEqual(held["reason"], "ambiguous")
        events = self.feed(
            router,
            msg(4, "mapping", run="r", alias="u", revision=2,
                actions=["walk"]),
            msg(5, "receive", request="q", client="view"),
        )
        packet = find(events, "packet")
        self.assertEqual(packet["fragments"][0]["payload"], "p-1")
        self.assertEqual(router.trace("r", "hand", 1)["action"], "walk")

    def test_unreferenced_alias_is_held_then_attributed_on_first_publication(self):
        router = self.make_router()
        self.feed(
            router,
            msg(1, "fragment", run="r", source="glove", source_seq=7,
                alias="late", channel="touch", payload="t-7"),
        )
        held = router.trace("r", "glove", 7)
        self.assertEqual(held["state"], "held")
        self.assertEqual(held["reason"], "unreferenced")
        self.feed(
            router,
            msg(2, "mapping", run="r", alias="late", revision=1,
                actions=["pick"]),
        )
        self.assertEqual(router.trace("r", "glove", 7)["action"], "pick")

    def test_old_or_equal_revision_does_not_change_directory(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=2,
                actions=["walk"]),
            msg(1, "mapping", run="r", alias="u", revision=1,
                actions=["turn"]),
            msg(2, "mapping", run="r", alias="u", revision=2,
                actions=["walk"]),
        )
        status, value = router.directory.resolve("r", "u")
        self.assertEqual((status, value), ("resolved", "walk"))

    def test_run_is_the_namespace_boundary(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="a", alias="brief", revision=1,
                actions=["walk", "hold"]),
            msg(0, "mapping", run="b", alias="brief", revision=1,
                actions=["walk"]),
            msg(1, "fragment", run="a", source="axis", source_seq=5,
                alias="brief", channel="pose", payload="from-a"),
            msg(1, "fragment", run="b", source="axis", source_seq=5,
                alias="brief", channel="pose", payload="from-b"),
        )
        self.assertEqual(router.trace("a", "axis", 5)["state"], "held")
        self.assertEqual(router.trace("b", "axis", 5)["action"], "walk")

    def test_alias_replacement_boundary_never_crosses_runs(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="a", alias="u", revision=1,
                actions=["rise"]),
            msg(0, "mapping", run="b", alias="u", revision=1,
                actions=["turn"]),
            msg(1, "fragment", run="a", source="hand", source_seq=1,
                alias="u", channel="pose", payload="a-p"),
            msg(1, "fragment", run="b", source="hand", source_seq=1,
                alias="u", channel="pose", payload="b-p"),
        )
        self.assertEqual(router.trace("a", "hand", 1)["action"], "rise")
        self.assertEqual(router.trace("b", "hand", 1)["action"], "turn")


class FragmentIntakeTests(RouterTestBase):
    def test_retransmitted_fragment_is_accepted_once_only(self):
        router = self.make_router()
        rows = [
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=["touch"], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        ]
        first = self.feed(router, *rows)
        self.assertEqual(len([e for e in first if e["kind"] == "packet"]), 1)
        # 旧端重发同一保管事实：不产生第二个包，只回执重复。
        again = self.feed(
            router,
            msg(4, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(5, "receive", request="q", client="view"),
        )
        self.assertNotIn("packet", kinds(again))
        self.assertIn("fragment_duplicate", kinds(again))
        self.assertEqual(len(router.fragments), 1)

    def test_same_source_seq_in_other_run_never_fills_a_gap(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="a", alias="brief", revision=1,
                actions=["walk", "hold"]),
            msg(0, "mapping", run="b", alias="brief", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="a", action="walk", request="a",
                required=["pose"], optional=[], until=10),
            msg(2, "fragment", run="b", source="axis", source_seq=5,
                alias="brief", channel="pose", payload="from-b"),
        )
        fetch = find(
            router.handle(msg(3, "receive", request="a", client="lab")),
            "fetch_empty", request="a",
        )
        self.assertEqual(fetch["missing_required"], ["pose"])

    def test_nearby_timestamps_are_not_used_for_attribution(self):
        # 同一秒到达的两条消息只按行序处理，归属只看目录。
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk", "hold"]),
            msg(0, "fragment", run="r", source="hand", source_seq=2,
                alias="u", channel="pose", payload="p-2"),
        )
        self.assertEqual(router.trace("r", "hand", 2)["state"], "held")


class ReplayLifecycleTests(RouterTestBase):
    def test_packet_waits_for_all_required_channels_and_carries_gaps(self):
        router = self.make_router()
        events = self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose", "touch"], optional=["imu"], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        )
        fetch = find(events, "fetch_empty")
        # pose 已在保管中，真正可查询/重发的欠项只有 touch。
        self.assertEqual(fetch["missing_required"], ["touch"])
        self.assertEqual(
            router.trace("r", "hand", 1)["state"], "attributed")
        events = self.feed(
            router,
            msg(4, "fragment", run="r", source="glove", source_seq=1,
                alias="u", channel="touch", payload="t-1"),
            msg(5, "receive", request="q", client="view"),
        )
        packet = find(events, "packet")
        self.assertEqual(packet["gaps"], ["imu"])
        self.assertEqual(packet["sources"], ["glove", "hand"])

    def test_optional_gap_disappears_once_fragment_arrives(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=["touch"], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        )
        self.feed(
            router,
            msg(4, "fragment", run="r", source="glove", source_seq=1,
                alias="u", channel="touch", payload="t-1"),
            msg(5, "receive", request="q", client="view"),
        )
        packets = router.requests["q"].packets
        self.assertEqual(packets[0]["gaps"], ["touch"])
        self.assertEqual(packets[1]["gaps"], [])

    def test_requires_all_required_acks_before_public_replay(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose", "touch"], optional=[], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(2, "fragment", run="r", source="glove", source_seq=1,
                alias="u", channel="touch", payload="t-1"),
            msg(3, "receive", request="q", client="view"),
            msg(4, "ack", request="q", client="view", packet=1,
                channels=["pose"]),
        )
        self.assertNotEqual(router.status(router.requests["q"]), "ready")
        events = self.feed(
            router,
            msg(5, "ack", request="q", client="view", packet=1,
                channels=["touch"]),
        )
        self.assertIn("replay_ready", kinds(events))
        self.assertEqual(router.status(router.requests["q"]), "ready")

    def test_ack_unknown_channel_or_packet_is_rejected(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=[], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        )
        with self.assertRaisesRegex(ValueError, "不存在分片类型"):
            router.handle(msg(4, "ack", request="q", client="view",
                              packet=1, channels=["touch"]))
        with self.assertRaisesRegex(ValueError, "未知回放包"):
            router.handle(msg(4, "ack", request="q", client="view",
                              packet=2, channels=["pose"]))

    def test_one_action_has_one_valid_admission_only(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q1",
                required=["pose"], optional=[], until=10),
        )
        with self.assertRaisesRegex(ValueError, "已有有效回放请求"):
            router.handle(msg(2, "replay", run="r", action="walk",
                              request="q2", required=["pose"], optional=[],
                              until=10))
        with self.assertRaisesRegex(ValueError, "编号重复"):
            router.handle(msg(2, "replay", run="r", action="other",
                              request="q1", required=["pose"], optional=[],
                              until=10))

    def test_unrelated_action_from_same_device_keeps_delivery(self):
        router = self.make_router()
        events = self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(0, "mapping", run="r", alias="v", revision=1,
                actions=["wave"]),
            msg(1, "replay", run="r", action="walk", request="q1",
                required=["pose"], optional=[], until=10),
            msg(1, "replay", run="r", action="wave", request="q2",
                required=["pose"], optional=[], until=10),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="walk-p"),
            msg(2, "fragment", run="r", source="hand", source_seq=2,
                alias="v", channel="pose", payload="wave-p"),
            msg(3, "receive", request="q1", client="view"),
            msg(3, "receive", request="q2", client="view"),
        )
        packets = [event for event in events if event["kind"] == "packet"]
        self.assertEqual({event["request"] for event in packets}, {"q1", "q2"})
        for event in packets:
            self.assertEqual(len(event["fragments"]), 1)


class CorrectionTests(RouterTestBase):
    def _accepted_packet(self, action="walk"):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=[action]),
            msg(1, "replay", run="r", action=action, request="q",
                required=["pose"], optional=["touch"], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
            msg(4, "ack", request="q", client="view", packet=1,
                channels=["pose"]),
            msg(5, "played", request="q", client="view", packet=1),
        )
        return router

    def test_accepted_fragment_is_delivered_as_correction_on_rewrite(self):
        router = self._accepted_packet()
        events = self.feed(
            router,
            msg(6, "mapping", run="r", alias="u", revision=2,
                actions=["turn"]),
        )
        invalidated = find(events, "replay_invalidated")
        self.assertEqual(invalidated["missing_required"], ["pose"])
        # 已接受包暂存在 outbox，等回放台下一次拉取时以更正交付。
        events = router.handle(msg(7, "receive", request="q", client="view"))
        correction = find(events, "correction", packet=1)
        self.assertEqual(correction["changes"][0]["from_action"], "walk")
        self.assertEqual(correction["changes"][0]["to_action"], "turn")
        request = router.requests["q"]
        self.assertTrue(request.packets[0]["superseded"])
        # 已播放事实继续可查。
        self.assertIn(1, request.played_packets)

    def test_correction_is_delivered_exactly_once_even_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.jsonl")
            router = Router(store=store)
            self.feed(
                router,
                msg(0, "mapping", run="r", alias="u", revision=1,
                    actions=["walk"]),
                msg(1, "replay", run="r", action="walk", request="q",
                    required=["pose"], optional=[], until=20),
                msg(2, "fragment", run="r", source="hand", source_seq=1,
                    alias="u", channel="pose", payload="p-1"),
                msg(3, "receive", request="q", client="view"),
                msg(4, "ack", request="q", client="view", packet=1,
                    channels=["pose"]),
                msg(5, "mapping", run="r", alias="u", revision=2,
                    actions=["turn"]),
                msg(6, "restart"),
            )
            rebuilt = Router.replay_journal(store.rows())
            events = rebuilt.handle(msg(7, "receive", request="q",
                                        client="view"))
            corrections = [e for e in events if e["kind"] == "correction"]
            self.assertEqual(len(corrections), 1)
            # 再次拉取不会重复更正。
            again = rebuilt.handle(msg(8, "receive", request="q",
                                       client="view"))
            self.assertNotIn("correction", kinds(again))

    def test_never_sent_fragment_is_silently_repackaged(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="turn", request="q",
                required=["pose"], optional=[], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "mapping", run="r", alias="u", revision=2,
                actions=["turn"]),
            msg(4, "receive", request="q", client="view"),
        )
        request = router.requests["q"]
        self.assertEqual(len(request.corrections), 0)
        self.assertEqual(request.packets[0]["packet"], 1)
        self.assertEqual(request.packets[0]["fragments"][0]["payload"], "p-1")

    def test_delivered_but_unacked_fragment_also_gets_correction(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=[], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
            msg(4, "mapping", run="r", alias="u", revision=2,
                actions=["turn"]),
        )
        events = router.handle(msg(5, "receive", request="q", client="view"))
        correction = find(events, "correction")
        self.assertEqual(correction["cno"], 1)
        self.assertEqual(
            correction["changes"][0]["to_action"], "turn")

    def test_directory_resolution_to_same_action_creates_no_correction(self):
        router = self.make_router()
        events = self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk", "hold"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=[], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "mapping", run="r", alias="u", revision=2,
                actions=["walk"]),
            msg(4, "receive", request="q", client="view"),
        )
        self.assertNotIn("correction", kinds(events))

    def test_reassigned_accepted_fragment_is_not_repackaged(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="qw",
                required=["pose"], optional=[], until=30),
            msg(1, "replay", run="r", action="turn", request="qt",
                required=["pose"], optional=[], until=30),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="qw", client="view"),
            msg(4, "ack", request="qw", client="view", packet=1,
                channels=["pose"]),
            msg(5, "mapping", run="r", alias="u", revision=2,
                actions=["turn"]),
        )
        # 改派目标动作的请求拿不到第二份包：保管事实只接纳一次。
        target = router.handle(msg(6, "receive", request="qt",
                                   client="view"))
        self.assertNotIn("packet", kinds(target))
        # 原请求仍以关联原包的更正收到改派事实。
        source = router.handle(msg(7, "receive", request="qw",
                                   client="view"))
        correction = find(source, "correction", packet=1)
        self.assertEqual(correction["changes"][0]["to_action"], "turn")
        trace = router.trace("r", "hand", 1)
        self.assertEqual(trace["action"], "turn")
        self.assertEqual(trace["deliveries"],
                         [{"request": "qw", "packet": 1}])

    def test_corrected_packet_remains_listed_as_played_history(self):
        router = self._accepted_packet()
        self.feed(
            router,
            msg(6, "mapping", run="r", alias="u", revision=2,
                actions=["turn"]),
            msg(7, "receive", request="q", client="view"),
        )
        inspection = router.handle(msg(8, "inspect", request="q"))[0]
        packet = inspection["packets"][0]
        self.assertTrue(packet["superseded"])
        self.assertTrue(packet["played"])
        self.assertEqual(len(inspection["corrections"]), 1)
        self.assertEqual(inspection["status"], "invalidated")


class ExpiryTests(RouterTestBase):
    def test_request_expires_at_until_when_required_still_missing(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=[], until=5),
        )
        self.assertEqual(
            self.feed(router, msg(5, "tick")), []
        )
        events = router.handle(msg(6, "tick"))
        expired = find(events, "request_expired")
        self.assertEqual(expired["missing_required"], ["pose"])
        # 过期后不再出新包。
        later = self.feed(
            router,
            msg(7, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(8, "receive", request="q", client="view"),
        )
        self.assertNotIn("packet", kinds(later))

    def test_ready_request_does_not_expire(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=[], until=5),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
            msg(4, "ack", request="q", client="view", packet=1,
                channels=["pose"]),
        )
        self.assertEqual(self.feed(router, msg(9, "tick")), [])
        self.assertEqual(router.status(router.requests["q"]), "ready")


class RestartTests(RouterTestBase):
    def test_in_process_restart_restores_progress(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=["touch"], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
            msg(4, "ack", request="q", client="view", packet=1,
                channels=["pose"]),
            msg(5, "played", request="q", client="view", packet=1),
        )
        events = router.handle(msg(6, "restart"))
        recovered = find(events, "recovered")
        self.assertEqual(recovered["fragments"], 1)
        self.assertEqual(recovered["packets"], 1)
        request = router.requests["q"]
        self.assertIn(1, request.played_packets)
        self.assertEqual(request.acked[1], {"pose"})
        # 包序号继续递增，不重用 1。
        self.feed(
            router,
            msg(7, "fragment", run="r", source="glove", source_seq=1,
                alias="u", channel="touch", payload="t-1"),
            msg(8, "receive", request="q", client="view"),
        )
        self.assertEqual(request.packets[1]["packet"], 2)

    def test_replay_journal_from_disk_matches_live_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.jsonl")
            live = Router(store=store)
            rows = [
                msg(0, "mapping", run="r", alias="u", revision=1,
                    actions=["walk", "hold"]),
                msg(1, "replay", run="r", action="walk", request="q",
                    required=["pose"], optional=[], until=20),
                msg(2, "fragment", run="r", source="hand", source_seq=1,
                    alias="u", channel="pose", payload="p-1"),
                msg(3, "mapping", run="r", alias="u", revision=2,
                    actions=["walk"]),
                msg(4, "receive", request="q", client="view"),
                msg(5, "ack", request="q", client="view", packet=1,
                    channels=["pose"]),
            ]
            self.feed(live, *rows)
            rebuilt = Router.replay_journal(store.rows())
            self.assertEqual(
                rebuilt.trace("r", "hand", 1)["action"], "walk")
            self.assertEqual(
                list(rebuilt.requests["q"].acked), [1])
            self.assertEqual(rebuilt.requests["q"].packets[0]["packet"], 1)


class InspectTests(RouterTestBase):
    def test_run_inspection_traces_source_seq_to_owning_action(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk", "hold"]),
            msg(1, "fragment", run="r", source="hand", source_seq=9,
                alias="u", channel="pose", payload="p-9"),
        )
        report = find(router.handle(msg(2, "inspect", run="r")), "inspect")
        self.assertEqual(report["scope"], "run")
        entry = report["fragments"][0]
        self.assertEqual((entry["source"], entry["source_seq"]),
                         ("hand", 9))
        self.assertEqual(entry["state"], "held")
        self.assertEqual(entry["history"][0]["candidates"],
                         ["walk", "hold"])

    def test_request_inspection_lists_gaps_corrections_and_status(self):
        router = self.make_router()
        self.feed(
            router,
            msg(0, "mapping", run="r", alias="u", revision=1,
                actions=["walk"]),
            msg(1, "replay", run="r", action="walk", request="q",
                required=["pose"], optional=["touch"], until=20),
            msg(2, "fragment", run="r", source="hand", source_seq=1,
                alias="u", channel="pose", payload="p-1"),
            msg(3, "receive", request="q", client="view"),
        )
        report = find(router.handle(msg(4, "inspect", request="q")),
                      "inspect")
        self.assertEqual(report["missing_required"], [])
        self.assertEqual(report["packets"][0]["gaps"], ["touch"])
        self.assertEqual(report["status"], "waiting")

    def test_trace_unknown_key_raises(self):
        router = self.make_router()
        with self.assertRaises(KeyError):
            router.trace("r", "hand", 404)


class ScenarioFixtureTests(RouterTestBase):
    """两份随仓库交付的演练必须端到端跑通且行为稳定。"""

    def setUp(self):
        self.examples = Path(__file__).with_name("examples")

    def _run_scenario(self, name):
        router = Router()
        events = []
        for line in (self.examples / name).read_text(
                encoding="utf-8").splitlines():
            events.extend(router.handle(json.loads(line)))
        return router, events

    def test_late_directory_rewrite(self):
        router, events = self._run_scenario("late-directory-rewrite.jsonl")
        self.assertIn("replay_ready", kinds(events))
        correction = find(events, "correction")
        self.assertEqual(correction["changes"][0]["to_action"], "turn")
        self.assertEqual(
            router.requests["r"].played_packets, {1})
        recovered = find(events, "recovered")
        self.assertEqual(recovered["packets"], 1)

    def test_same_sequence_two_runs_keeps_runs_apart(self):
        router, events = self._run_scenario("same-sequence-two-runs.jsonl")
        packets = [event for event in events if event["kind"] == "packet"]
        payloads = {
            event["request"]: event["fragments"][0]["payload"]
            for event in packets
        }
        self.assertEqual(payloads, {"a": "from-a", "b": "from-b"})
        self.assertEqual(
            router.trace("a", "axis", 5)["deliveries"],
            [{"request": "a", "packet": 1}],
        )
        self.assertEqual(
            router.trace("b", "axis", 5)["deliveries"],
            [{"request": "b", "packet": 1}],
        )


if __name__ == "__main__":
    unittest.main()
