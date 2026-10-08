from __future__ import annotations

import unittest

from telegram_search_mcp.schemas import SearchRequest
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.tdjson import TDLibError
from test_search_service import FakeTDLibClient, text_message


def video_message(message_id: int, media_type: str = "video", *, name: str = "clip.mp4"):
    message = text_message(message_id, "", date=1_750_000_000 + message_id)
    message["content"] = {
        "@type": "messageVideoNote" if media_type == "video_note" else "messageVideo",
        "caption": {"@type": "formattedText", "text": "", "entities": []},
        media_type: {"file_name": name, "mime_type": "video/mp4", "video": {"id": 4, "size": 128}},
    }
    return message


class FilteredMediaClient(FakeTDLibClient):
    def __init__(self):
        super().__init__()
        self.media_pages = {}
        self.media_calls = []
        self.hydrated = []

    def get_chat_history(self, *args, **kwargs):
        raise TDLibError("full history traversal is unavailable")

    def search_chat_media(self, chat_id, media_type, *, from_message_id, limit):
        self.media_calls.append((chat_id, media_type, from_message_id, limit))
        result = self.media_pages[from_message_id]
        if isinstance(result, Exception):
            raise result
        return result

    def get_message(self, chat_id, message_id):
        self.hydrated.append(message_id)
        return super().get_message(chat_id, message_id)


def page(messages, cursor=0):
    return {"@type": "foundChatMessages", "total_count": len(messages),
            "messages": messages, "next_from_message_id": cursor}


class FilteredMediaSearchTests(unittest.TestCase):
    def test_video_types_search_matching_media_without_traversing_chat_history(self):
        for media_type in ("video", "video_note"):
            with self.subTest(media_type=media_type):
                client = FilteredMediaClient()
                hit = video_message(20, media_type)
                client.media_pages[0] = page([hit])
                client.messages[20] = hit
                response = SearchService(client=client).search(SearchRequest(
                    target=-1001, query={"media_type": media_type}))
                self.assertEqual(response.status, "matches")
                self.assertTrue(response.coverage.complete)
                self.assertEqual([m.message_id for m in response.matches], [20])
                self.assertEqual(client.media_calls, [(-1001, media_type, 0, 100)])

    def test_short_filtered_pages_follow_provider_cursor_and_only_enrich_returned_limit(self):
        client = FilteredMediaClient()
        newest, oldest = video_message(20), video_message(10)
        client.media_pages = {0: page([newest], 10), 10: page([oldest])}
        client.messages = {20: newest, 10: oldest}
        response = SearchService(client=client).search(SearchRequest(
            target=-1001, query={"media_type": "video"}, limit=1))
        self.assertTrue(response.coverage.complete)
        self.assertEqual([m.message_id for m in response.matches], [20])
        self.assertEqual(len(client.media_calls), 2)
        self.assertEqual(client.hydrated, [20])

    def test_later_provider_failure_retains_verified_matches_as_incomplete(self):
        client = FilteredMediaClient()
        hit = video_message(20)
        client.media_pages = {0: page([hit], 10), 10: TDLibError("provider timeout")}
        client.messages[20] = hit
        response = SearchService(client=client).search(SearchRequest(
            target=-1001, query={"media_type": "video"}))
        self.assertEqual(response.status, "incomplete")
        self.assertFalse(response.coverage.complete)
        self.assertEqual([m.message_id for m in response.matches], [20])

    def test_filtered_result_still_requires_requested_filename_and_mime(self):
        client = FilteredMediaClient()
        wanted, other = video_message(20, name="wanted.mp4"), video_message(10, name="other.mp4")
        client.media_pages[0] = page([wanted, other])
        client.messages = {20: wanted, 10: other}
        response = SearchService(client=client).search(SearchRequest(target=-1001,
            query={"media_type": "video", "file_name": "wanted", "mime_type": "video/mp4"}))
        self.assertTrue(response.coverage.complete)
        self.assertEqual([m.message_id for m in response.matches], [20])

    def test_malformed_or_cross_chat_page_never_claims_complete(self):
        for bad in (None, {"id": 1, "chat_id": 777, "date": 1_750_000_000},
                    {"id": True, "chat_id": -1001, "date": 1_750_000_000}):
            with self.subTest(bad=bad):
                client = FilteredMediaClient()
                client.media_pages[0] = page([bad])
                response = SearchService(client=client).search(SearchRequest(
                    target=-1001, query={"media_type": "video"}))
                self.assertEqual(response.status, "incomplete")
                self.assertFalse(response.coverage.complete)

    def test_repeated_cursor_stops_with_partial_coverage(self):
        client = FilteredMediaClient()
        hit = video_message(20)
        client.media_pages = {0: page([hit], 10), 10: page([hit], 10)}
        client.messages[20] = hit
        response = SearchService(client=client).search(SearchRequest(
            target=-1001, query={"media_type": "video"}))
        self.assertFalse(response.coverage.complete)
        self.assertEqual(len(client.media_calls), 2)


if __name__ == "__main__":
    unittest.main()
