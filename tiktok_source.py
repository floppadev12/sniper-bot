from dataclasses import dataclass
from datetime import datetime, timezone


ENABLE_TEST_VIDEO = True


@dataclass
class TikTokVideo:
    video_id: str
    creator_username: str
    description: str
    video_url: str
    posted_at: datetime


async def get_latest_videos(username: str) -> list[TikTokVideo]:
    """
    This function is the TikTok data source.

    Right now, it returns a fake test video so you can test the Discord bot.

    Later, replace the test section with your TikTok scraper.

    The bot needs each video to have:
    - video_id
    - creator_username
    - description
    - video_url
    - posted_at
    """

    if ENABLE_TEST_VIDEO:
        return [
            TikTokVideo(
                video_id=f"test-video-{username}-1",
                creator_username=username,
                description="This is a test TikTok description with #challenge in it.",
                video_url=f"https://www.tiktok.com/@{username}/video/test-video-1",
                posted_at=datetime.now(timezone.utc),
            )
        ]

    # TODO: Replace this part with your real TikTok scraper.
    return []
