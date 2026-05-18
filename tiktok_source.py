import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from yt_dlp import YoutubeDL


logger = logging.getLogger("tiktok-source")


@dataclass
class TikTokVideo:
    video_id: str
    creator_username: str
    description: str
    video_url: str
    posted_at: datetime | None


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None


def _clean_username(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    return username.lower()


def _extract_video_id(entry: dict) -> str | None:
    for key in ["id", "display_id"]:
        value = entry.get(key)
        if value:
            return str(value)

    webpage_url = entry.get("webpage_url") or entry.get("url")
    if webpage_url and "/video/" in webpage_url:
        return webpage_url.rstrip("/").split("/video/")[-1].split("?")[0]

    return None


def _extract_description(entry: dict) -> str:
    """
    TikTok captions may appear in different yt-dlp fields depending on the extractor result.
    We try multiple fields to get the best caption/description.
    """
    candidates = [
        entry.get("description"),
        entry.get("title"),
        entry.get("fulltitle"),
    ]

    for value in candidates:
        if value:
            return str(value).strip()

    return ""


def _extract_video_url(username: str, entry: dict, video_id: str) -> str:
    webpage_url = entry.get("webpage_url")

    if webpage_url:
        return str(webpage_url)

    return f"https://www.tiktok.com/@{username}/video/{video_id}"


def _fetch_latest_videos_sync(username: str, max_videos: int = 5) -> list[TikTokVideo]:
    """
    Blocking yt-dlp work happens here.
    The async wrapper below runs this in a thread so the Discord bot does not freeze.
    """
    username = _clean_username(username)
    profile_url = f"https://www.tiktok.com/@{username}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "playlistend": max_videos,
        "ignoreerrors": True,
        "noplaylist": False,
        "socket_timeout": 20,
        "retries": 2,
    }

    videos: list[TikTokVideo] = []

    logger.info("Fetching TikTok profile: %s", profile_url)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not info:
        logger.warning("No TikTok info returned for @%s", username)
        return []

    entries = info.get("entries") or []

    for entry in entries:
        if not entry:
            continue

        video_id = _extract_video_id(entry)
        if not video_id:
            continue

        description = _extract_description(entry)
        video_url = _extract_video_url(username, entry, video_id)
        posted_at = _parse_timestamp(entry.get("timestamp"))

        videos.append(
            TikTokVideo(
                video_id=video_id,
                creator_username=username,
                description=description,
                video_url=video_url,
                posted_at=posted_at,
            )
        )

    logger.info("Fetched %s videos for @%s", len(videos), username)
    return videos


async def get_latest_videos(username: str) -> list[TikTokVideo]:
    """
    Return latest TikTok videos for a creator.

    The bot uses:
    - video_id to prevent duplicate alerts
    - description to check for "challenge"
    - video_url for the Discord alert
    - posted_at for the Discord alert date
    """
    return await asyncio.to_thread(_fetch_latest_videos_sync, username, 5)
