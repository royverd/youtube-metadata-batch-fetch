"""
grab_watchlist.py

Grabs a YouTube playlist (default: Watch Later) using yt-dlp reading your
browser's login cookies, and writes one video ID per line to a text file.

The base step of the backlog pipeline: this produces the file the transcript
fetcher reads. No YouTube API key, no browser automation - yt-dlp borrows the
session you're already logged into.

Deps: yt-dlp on PATH. Config: none (kept deliberately flat).
"""

import subprocess
import sys
import winreg

from . import paths

# WL is YouTube's private Watch Later playlist. It isn't exportable via Takeout
# or the Data API, which is the whole reason we go through browser cookies.
DEFAULT_PLAYLIST = "https://www.youtube.com/playlist?list=WL"

# (menu label, yt-dlp cookie token). yt-dlp names Firefox "firefox", not "mozilla".
BROWSERS = [
    ("Firefox", "firefox"),
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Opera", "opera"),
]


def detect_default_browser():
    """Read Windows' default https handler and map it to a yt-dlp token.
    Returns None if the key is missing or the handler isn't one we know."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        )
        progid = winreg.QueryValueEx(key, "ProgId")[0].lower()
    except OSError:
        return None
    # ProgIds look like ChromeHTML, MSEdgeHTM, FirefoxURL-<hash>, OperaStable, BraveHTML.
    for needle, token in (("chrome", "chrome"), ("edge", "edge"),
                          ("firefox", "firefox"), ("opera", "opera"), ("brave", "brave")):
        if needle in progid:
            return token
    return None


def select_browser():
    """Numbered pick, re-prompting on bad input. 0 = detect the system default."""
    print("Browser to read cookies from:")
    for i, (label, _) in enumerate(BROWSERS, 1):
        print(f"  {i}) {label}")
    print("  0) System default")

    while True:
        choice = input("> ").strip()
        if choice == "0":
            token = detect_default_browser()
            if token:
                print(f"System default: {token}")
                return token
            print("  Couldn't detect the default browser. Pick a number instead.")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(BROWSERS):
            return BROWSERS[int(choice) - 1][1]
        print(f"  Enter 0-{len(BROWSERS)}.")


def grab(browser, playlist_url):
    """Run yt-dlp flat (no per-video network calls) and return a list of IDs."""
    result = subprocess.run(
        [
            "yt-dlp",
            "--cookies-from-browser", browser,
            "--flat-playlist",
            "--print", "%(id)s",
            playlist_url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # yt-dlp puts the useful diagnosis (locked cookie DB, browser not found,
        # not logged in) on stderr - surface it verbatim instead of guessing.
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed with no message")

    # dict.fromkeys dedupes while preserving order; a playlist can list a video
    # twice and we don't want it fetched twice downstream.
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return list(dict.fromkeys(ids))


def main():
    print("ytb-watchlist")
    print("Exports a YouTube playlist to one-ID-per-line via yt-dlp browser cookies.")
    print()

    browser = select_browser()
    # 0 or empty both mean "just give me Watch Later" - saves pasting a URL for
    # the common case, while still accepting any playlist URL when you want one.
    raw_playlist = input("Playlist URL, or 0 for default (Watch Later): ").strip().strip('"').strip("'")
    playlist = DEFAULT_PLAYLIST if raw_playlist in ("", "0") else raw_playlist
    out_path = paths.data_file("watchlist.txt")

    print()
    print(f"Fetching from {browser}...")
    try:
        ids = grab(browser, playlist)
    except FileNotFoundError:
        print("yt-dlp not found on PATH. Install it first: pip install yt-dlp")
        sys.exit(1)
    except RuntimeError as e:
        print(f"Failed: {e}")
        sys.exit(1)

    if not ids:
        print("No IDs returned. Are you logged in on that browser, and is it fully closed?")
        sys.exit(1)

    # utf-8 with a trailing newline, no BOM - the transcript fetcher handles BOMs
    # anyway, but writing clean means the file is portable to anything.
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ids) + "\n")

    print(f"Wrote {len(ids)} unique IDs to {out_path}")


if __name__ == "__main__":
    main()
