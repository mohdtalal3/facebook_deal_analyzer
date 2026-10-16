import json
import os
from urllib.parse import urlparse, parse_qs
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
from dotenv import load_dotenv

load_dotenv()


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    return parsed.path.split("/")[-1]


def fetch_transcript(video_url: str, output_file: str = "transcript.json") -> str:
    video_id = extract_video_id(video_url)

    proxy = os.getenv("YT_PROXY")
    proxy_config = GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None
    ytt = YouTubeTranscriptApi(proxy_config=proxy_config)
    transcript = ytt.fetch(video_id)

    segments = [
        {
            "start": round(s.start, 2),
            "duration": round(s.duration, 2),
            "text": s.text,
        }
        for s in transcript.snippets
    ]

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    print(f"  Saved {len(segments)} transcript segments → {output_file}")
    return output_file


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m youtube.transcript <video_url> [output_file]")
        sys.exit(1)

    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "transcript.json"
    fetch_transcript(url, output_file=out)
