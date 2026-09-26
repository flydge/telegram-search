from __future__ import annotations

import concurrent.futures
import threading
import unittest

from telegram_search_mcp.discovery_state import (
    CatalogPosition,
    DiscoveryCursorExpired,
    DiscoveryRegistry,
    DiscoveryWorkBusy,
    GlobalLaneKey,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def populated_registry(
    *,
    hypothesis_count: int = 2,
    scope: str = "both",
    clock: FakeClock | None = None,
    capacity: int = 4,
) -> tuple[DiscoveryRegistry, str]:
    registry = DiscoveryRegistry(
        clock=clock or FakeClock(), ttl_seconds=300, capacity=capacity
    )
    cursor = registry.start(
        hypothesis_digest="a" * 64,
        hypothesis_count=hypothesis_count,
        scope=scope,
    )
    return registry, cursor


class DiscoveryRegistryTests(unittest.TestCase):
    def test_registry_builds_every_hypothesis_list_lane_without_raw_strings(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=3, scope="both")

        state = registry.bind(
            cursor,
            hypothesis_digest="a" * 64,
            hypothesis_count=3,
            scope="both",
        )

        self.assertEqual(set(state.catalog), {"main", "archive"})
        self.assertEqual(
            set(state.global_lanes),
            {
                GlobalLaneKey(index, chat_list)
                for index in range(3)
                for chat_list in ("main", "archive")
            },
        )
        retained_fields = set(vars(state))
        self.assertFalse(
            retained_fields
            & {"hypotheses", "queries", "snippets", "titles", "messages", "evidence"}
        )

    def test_round_robin_advances_one_catalog_and_one_global_lane_per_turn(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="both")

        first = registry.next_work(cursor)
        self.assertIsNotNone(first)
        registry.release_work(cursor, first.lease_token)
        second = registry.next_work(cursor)
        self.assertIsNotNone(second)

        self.assertEqual((first.catalog_list, second.catalog_list), ("main", "archive"))
        self.assertEqual(
            (first.global_lane, second.global_lane),
            (GlobalLaneKey(0, "main"), GlobalLaneKey(0, "archive")),
        )
        registry.release_work(cursor, second.lease_token)

    def test_round_robin_pointers_are_independent_and_skip_terminal_lanes(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="both")
        registry.mark_catalog_end(cursor, "main")
        registry.record_global_page(
            cursor, GlobalLaneKey(0, "main"), next_offset="", hits=0
        )

        work = registry.next_work(cursor)

        self.assertEqual(work.catalog_list, "archive")
        self.assertEqual(work.global_lane, GlobalLaneKey(0, "archive"))

    def test_concurrent_callers_cannot_reserve_the_same_cursor_work(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        registry.mark_catalog_end(cursor, "main")
        registry.record_global_page(
            cursor, GlobalLaneKey(1, "main"), next_offset="", hits=0
        )
        start = threading.Barrier(3)
        selected = threading.Barrier(3)

        def reserve() -> object:
            start.wait()
            try:
                work: object = registry.next_work(cursor)
            except DiscoveryWorkBusy:
                work = "busy"
            selected.wait()
            return work

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve) for _ in range(2)]
            start.wait()
            selected.wait()
            results = [future.result() for future in futures]

        reserved = [work for work in results if work != "busy"]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(results.count("busy"), 1)
        self.assertEqual(reserved[0].global_lane, GlobalLaneKey(0, "main"))
        self.assertTrue(reserved[0].lease_token.startswith("work_"))
        registry.release_work(cursor, reserved[0].lease_token)

    def test_releasing_failed_work_makes_the_cursor_schedulable_again(self) -> None:
        registry, cursor = populated_registry(scope="main")

        first = registry.next_work(cursor)
        self.assertIsNotNone(first)
        with self.assertRaises(DiscoveryWorkBusy):
            registry.next_work(cursor)
        registry.release_work(cursor, first.lease_token)

        second = registry.next_work(cursor)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.lease_token, second.lease_token)
        registry.release_work(cursor, second.lease_token)

    def test_catalog_hydration_retry_requeues_only_known_numeric_ids(self) -> None:
        registry, cursor = populated_registry(scope="main")
        registry.record_catalog_positions(
            cursor,
            "main",
            [CatalogPosition(-1001, 20), CatalogPosition(-1002, 10)],
        )
        self.assertEqual(
            registry.catalog_page(cursor, "main", limit=2),
            [-1001, -1002],
        )

        registry.record_catalog_hydration_retry(
            cursor,
            "main",
            [-1001],
        )

        snapshot = registry.bind(
            cursor,
            hypothesis_digest="a" * 64,
            hypothesis_count=2,
            scope="main",
        )
        self.assertEqual(snapshot.catalog["main"].status, "error")
        self.assertEqual(snapshot.catalog["main"].hydration_retryable_ids, {-1001})
        self.assertEqual(registry.catalog_page(cursor, "main", limit=2), [-1001])
        registry.record_catalog_hydrated(cursor, "main", [-1001])
        registry.mark_catalog_end(cursor, "main")
        self.assertEqual(
            registry.bind(
                cursor,
                hypothesis_digest="a" * 64,
                hypothesis_count=2,
                scope="main",
            ).catalog["main"].status,
            "complete",
        )
        for invalid in ([-9999], [True], [0]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                registry.record_catalog_hydration_retry(cursor, "main", invalid)

    def test_reserved_work_requires_the_matching_lease_for_commit(self) -> None:
        registry, cursor = populated_registry(scope="main")
        key = GlobalLaneKey(0, "main")
        work = registry.next_work(cursor)
        self.assertIsNotNone(work)

        with self.assertRaisesRegex(RuntimeError, "^discovery work is reserved$"):
            registry.record_global_page(cursor, key, next_offset="next", hits=1)
        with self.assertRaisesRegex(RuntimeError, "^discovery work is reserved$"):
            registry.record_global_page(
                cursor,
                key,
                next_offset="next",
                hits=1,
                lease_token="work_wrong",
            )
        registry.record_global_page(
            cursor,
            key,
            next_offset="next",
            hits=1,
            lease_token=work.lease_token,
        )
        registry.release_work(cursor, work.lease_token)

        self.assertEqual(registry.global_lane(cursor, key).offset, "next")

    def test_bind_and_lane_reads_return_detached_snapshots(self) -> None:
        registry, cursor = populated_registry(scope="main")
        key = GlobalLaneKey(0, "main")
        snapshot = registry.bind(
            cursor,
            hypothesis_digest="a" * 64,
            hypothesis_count=2,
            scope="main",
        )
        lane_snapshot = registry.global_lane(cursor, key)

        snapshot.hypothesis_digest = "b" * 64
        snapshot.catalog.clear()
        snapshot.global_lanes[key].offset = "tampered-in-snapshot"
        snapshot.returned_candidate_ids.add(999)
        lane_snapshot.offset = "tampered-lane"
        rebound = registry.bind(
            cursor,
            hypothesis_digest="a" * 64,
            hypothesis_count=2,
            scope="main",
        )

        self.assertEqual(set(rebound.catalog), {"main"})
        self.assertEqual(rebound.global_lanes[key].offset, "")
        self.assertNotIn(999, rebound.returned_candidate_ids)

    def test_global_lane_completes_only_on_empty_next_offset(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")

        registry.record_global_page(cursor, key, next_offset="opaque-next", hits=3)
        scanning = registry.global_lane(cursor, key)
        self.assertEqual(scanning.status, "scanning")
        self.assertEqual(scanning.pages_scanned, 1)
        self.assertEqual(scanning.hits_seen, 3)

        registry.record_global_page(cursor, key, next_offset="", hits=0)
        complete = registry.global_lane(cursor, key)
        self.assertEqual(complete.status, "complete")
        self.assertEqual(complete.pages_scanned, 2)

    def test_provider_error_keeps_offset_and_marks_retryable_partial(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")
        registry.record_global_page(cursor, key, next_offset="opaque-next", hits=1)

        registry.record_retryable_error(cursor, key)

        lane = registry.global_lane(cursor, key)
        self.assertEqual(lane.offset, "opaque-next")
        self.assertEqual(lane.status, "error")
        self.assertEqual(registry.next_work(cursor).global_lane, key)

    def test_successful_retry_clears_retryable_error_without_resetting_counts(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")
        registry.record_retryable_error(cursor, key)

        registry.record_global_page(cursor, key, next_offset="next", hits=2)

        lane = registry.global_lane(cursor, key)
        self.assertEqual(lane.status, "scanning")
        self.assertEqual((lane.pages_scanned, lane.hits_seen), (1, 2))

    def test_repeated_nonempty_offset_is_terminal_integrity_partial(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")
        registry.record_global_page(cursor, key, next_offset="same", hits=1)

        registry.record_global_page(cursor, key, next_offset="same", hits=1)

        lane = registry.global_lane(cursor, key)
        self.assertEqual(lane.offset, "same")
        self.assertEqual(lane.status, "partial")
        self.assertEqual(
            registry.next_work(cursor).global_lane, GlobalLaneKey(1, "main")
        )
        self.assertFalse(registry.is_complete(cursor))

    def test_two_state_offset_cycle_is_terminal_integrity_partial(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")

        for offset in ("cycle-a", "cycle-b", "cycle-a"):
            registry.record_global_page(cursor, key, next_offset=offset, hits=1)

        lane = registry.global_lane(cursor, key)
        self.assertEqual(lane.offset, "cycle-b")
        self.assertEqual(lane.status, "partial")
        self.assertNotIn("cycle-a", repr(lane.seen_offset_digests))
        self.assertTrue(
            all(isinstance(digest, bytes) for digest in lane.seen_offset_digests)
        )

    def test_longer_offset_cycle_is_terminal_integrity_partial(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")

        for offset in ("cycle-a", "cycle-b", "cycle-c", "cycle-a"):
            registry.record_global_page(cursor, key, next_offset=offset, hits=0)

        self.assertEqual(registry.global_lane(cursor, key).status, "partial")

    def test_offset_digest_budget_exhaustion_fails_closed_as_partial(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")

        for index in range(65):
            registry.record_global_page(
                cursor,
                key,
                next_offset=f"unique-offset-{index}",
                hits=0,
            )

        lane = registry.global_lane(cursor, key)
        self.assertEqual(lane.status, "partial")
        self.assertLessEqual(len(lane.seen_offset_digests), 64)

    def test_permanent_integrity_partial_survives_an_empty_terminal_offset(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        key = GlobalLaneKey(0, "main")
        registry.record_permanent_partial(cursor, key)

        registry.record_global_page(cursor, key, next_offset="", hits=0)

        self.assertEqual(registry.global_lane(cursor, key).status, "partial")
        self.assertFalse(registry.is_complete(cursor))

    def test_catalog_pages_are_deterministic_unique_and_not_complete_before_end(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="main")
        registry.record_catalog_positions(
            cursor,
            "main",
            [
                CatalogPosition(-3, 30),
                CatalogPosition(-1, 50),
                CatalogPosition(-2, 40),
                CatalogPosition(-1, 45),
            ],
        )

        self.assertEqual(registry.catalog_page(cursor, "main", limit=2), [-1, -2])
        self.assertEqual(registry.catalog_page(cursor, "main", limit=2), [-3])
        self.assertFalse(registry.is_complete(cursor))
        registry.mark_catalog_end(cursor, "main")
        self.assertFalse(registry.is_complete(cursor))

        for index in (0, 1):
            registry.record_global_page(
                cursor, GlobalLaneKey(index, "main"), next_offset="", hits=0
            )
        self.assertTrue(registry.is_complete(cursor))

    def test_catalog_and_global_completion_are_independent(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="both")
        registry.mark_catalog_end(cursor, "main")
        registry.mark_catalog_end(cursor, "archive")
        self.assertFalse(registry.is_complete(cursor))

        for index in (0, 1):
            for chat_list in ("main", "archive"):
                registry.record_global_page(
                    cursor,
                    GlobalLaneKey(index, chat_list),
                    next_offset="",
                    hits=0,
                )
        self.assertTrue(registry.is_complete(cursor))

    def test_returned_candidate_ids_are_exact_bounded_mechanics(self) -> None:
        registry, cursor = populated_registry()

        registry.record_returned_candidates(cursor, [-1001, 42, -1001])

        self.assertTrue(registry.was_candidate_returned(cursor, -1001))
        self.assertTrue(registry.was_candidate_returned(cursor, 42))
        self.assertFalse(registry.was_candidate_returned(cursor, 7))
        with self.assertRaises(ValueError):
            registry.record_returned_candidates(cursor, [True])
        with self.assertRaises(ValueError):
            registry.record_returned_candidates(cursor, [0])

    def test_cursor_binding_rejects_digest_count_and_scope_mismatch_redacted(self) -> None:
        registry, cursor = populated_registry(hypothesis_count=2, scope="both")
        cases = (
            ("b" * 64, 2, "both"),
            ("a" * 64, 3, "both"),
            ("a" * 64, 2, "main"),
        )
        for digest, count, scope in cases:
            with self.subTest(digest=digest[:1], count=count, scope=scope):
                with self.assertRaisesRegex(DiscoveryCursorExpired, "^discovery cursor expired$"):
                    registry.bind(
                        cursor,
                        hypothesis_digest=digest,
                        hypothesis_count=count,
                        scope=scope,
                    )

    def test_mismatched_binding_does_not_extend_cursor_ttl(self) -> None:
        clock = FakeClock()
        registry, cursor = populated_registry(clock=clock, scope="main")
        clock.advance(299)
        with self.assertRaises(DiscoveryCursorExpired):
            registry.bind(
                cursor,
                hypothesis_digest="b" * 64,
                hypothesis_count=2,
                scope="main",
            )
        clock.advance(1)

        with self.assertRaises(DiscoveryCursorExpired):
            registry.bind(
                cursor,
                hypothesis_digest="a" * 64,
                hypothesis_count=2,
                scope="main",
            )

    def test_cursor_expires_after_inactivity_and_capacity_evicts_oldest(self) -> None:
        clock = FakeClock()
        registry = DiscoveryRegistry(clock=clock, ttl_seconds=300, capacity=1)
        old = registry.start(
            hypothesis_digest="a" * 64, hypothesis_count=2, scope="main"
        )
        newer = registry.start(
            hypothesis_digest="b" * 64, hypothesis_count=2, scope="archive"
        )
        with self.assertRaises(DiscoveryCursorExpired):
            registry.bind(
                old,
                hypothesis_digest="a" * 64,
                hypothesis_count=2,
                scope="main",
            )

        clock.advance(300)
        with self.assertRaises(DiscoveryCursorExpired):
            registry.bind(
                newer,
                hypothesis_digest="b" * 64,
                hypothesis_count=2,
                scope="archive",
            )

    def test_start_rejects_invalid_scan_shape_without_allocating_state(self) -> None:
        registry = DiscoveryRegistry(clock=FakeClock(), ttl_seconds=300, capacity=4)

        for digest, count, scope in (
            ("not-a-digest", 2, "main"),
            ("a" * 64, 1, "main"),
            ("a" * 64, 6, "main"),
            ("a" * 64, 2, "all"),
        ):
            with self.subTest(count=count, scope=scope):
                with self.assertRaises(ValueError):
                    registry.start(
                        hypothesis_digest=digest,
                        hypothesis_count=count,
                        scope=scope,
                    )


if __name__ == "__main__":
    unittest.main()
