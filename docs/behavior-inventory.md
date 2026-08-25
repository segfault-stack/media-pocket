# Behavior inventory and modular mapping

This inventory records the supported scenarios retained by the new implementation. Contract tests use synthetic provider payloads and do not access external networks.

| Capability | Modular implementation |
| --- | --- |
| YouTube videos/audio/playlists | YouTube registry capability, yt-dlp resolver, shared worker |
| TikTok video/photo/profile | TikTok registry capability; multi-entry responses become artifacts |
| Instagram reels/posts/albums | Instagram registry capability and multi-entry post artifacts |
| X/Twitter video/photo albums | X registry capability and multi-asset artifacts |
| Pinterest video/photo albums | Pinterest registry capability and multi-asset artifacts |
| Threads photos/videos/carousels | Threads registry capability and multi-asset artifacts |
| SoundCloud tracks | SoundCloud registry capability and audio artifact |
| Spotify track/album/playlist | Authenticated librespot stream preferred; automatic per-track YouTube fallback |
| Generic URL and `!audio` | Generic registry fallback and audio transcoding strategy |
| Direct/business/group replay | `JobKind` plus the same submit/deliver pipeline |
| Inline and chosen-result delivery | Durable inline job binding and inline presenter |
| Batch links | Parent job with independently durable child jobs |
| Audio/document callbacks | Queued-job customization without handler-specific downloaders |
| Settings/admin/stats | Typed preferences, analytics, and shared use cases |

Provider contract fixtures must remain network-free. Live Telegram and provider smoke tests are optional and are not part of automated acceptance.
