# Platform Download Tips

`video-dubber` uses `yt-dlp` for URL input, so it can download from YouTube, Bilibili, Twitter/X, TikTok, Vimeo, Instagram, Twitch, and many other sites supported by yt-dlp.

## General Defaults

Use reproducible downloads by default:

```bash
--ignore-config --no-playlist --download-format "bv*+ba/best"
```

Use browser cookies when the platform needs login, age verification, high quality, private/protected content, or region/session state:

```bash
--cookies-from-browser chrome
```

Supported browser names include `chrome`, `firefox`, `safari`, `edge`, `opera`, `brave`, `chromium`, and `vivaldi`.

List formats before downloading when quality selection fails:

```bash
--list-formats
```

## YouTube

- Use `--cookies-from-browser chrome` for age-restricted, members-only, sign-in-required, or high-quality failures.
- Prefer concrete auto-subtitle tracks such as `en-en-*.srt` over broad `en.srt` when multiple subtitles are available.
- Use `--allow-playlist` only when the user explicitly asks for a playlist.

## Bilibili

- Use cookies for higher quality, logged-in-only content, series, and some subtitle tracks:

```bash
--cookies-from-browser chrome
```

- Use `--playlist-items 1-10 --allow-playlist` for selected P items/series entries.
- If muxing fails, inspect formats with `--list-formats`, then set `--download-format`.

## Twitter/X

- Public posts often work directly.
- Protected tweets, age-gated posts, or login-required media usually need:

```bash
--cookies-from-browser chrome
```

- Both `https://twitter.com/.../status/...` and `https://x.com/.../status/...` are supported by yt-dlp when extractor support is current.

## TikTok

- Normal video URLs usually work through yt-dlp.
- If the default format includes a watermark and a no-watermark format is available, inspect with `--list-formats` and set a specific `--download-format`.
- Use cookies if the URL is region/session restricted.

## Instagram

- Most posts/reels require cookies:

```bash
--cookies-from-browser chrome
```

## Network And Performance

- For slow segmented downloads:

```bash
--concurrent-fragments 3
```

- If `aria2c` is installed:

```bash
--external-downloader aria2c
```

- For regional network issues:

```bash
--proxy socks5://127.0.0.1:1080
```

## Failure Protocol

When URL download fails:

1. Retry with `--cookies-from-browser chrome`.
2. Run `--list-formats`.
3. Try a simpler `--download-format "best"`.
4. For playlists/series, decide whether to add `--allow-playlist` or narrow with `--playlist-items`.
5. Update yt-dlp if the extractor is stale.
