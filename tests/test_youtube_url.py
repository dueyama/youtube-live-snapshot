import unittest
from urllib.parse import parse_qs, urlparse

from ytlive_snapshot.capture import _normalize_youtube_embed_url


class YouTubeUrlNormalizationTest(unittest.TestCase):
    def assert_normalized_video(self, source_url):
        normalized = _normalize_youtube_embed_url(source_url)
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.youtube-nocookie.com")
        self.assertEqual(parsed.path, "/embed/VIDEO_ID")
        self.assertEqual(query["autoplay"], ["1"])
        self.assertEqual(query["mute"], ["1"])
        self.assertEqual(query["enablejsapi"], ["1"])
        self.assertNotIn("si", query)
        self.assertNotIn("v", query)

    def test_watch_url_is_converted_to_embed_url(self):
        self.assert_normalized_video(
            "https://www.youtube.com/watch?v=VIDEO_ID&si=SHARE_TOKEN"
        )

    def test_short_share_url_is_converted_to_embed_url(self):
        self.assert_normalized_video("https://youtu.be/VIDEO_ID?si=SHARE_TOKEN")

    def test_existing_embed_url_is_normalized(self):
        self.assert_normalized_video(
            "https://www.youtube.com/embed/VIDEO_ID?controls=1"
        )

    def test_live_url_is_converted_to_embed_url(self):
        self.assert_normalized_video("https://www.youtube.com/live/VIDEO_ID")


if __name__ == "__main__":
    unittest.main()
