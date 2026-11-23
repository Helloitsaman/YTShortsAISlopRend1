# Shorts Studio

A phone-friendly, local-first dashboard for generating daily short ideas, reviewing scripts, building a preview, and uploading only after a second approval.

## Run it

Open PowerShell in this folder and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\app.py
```

The dashboard opens at `http://127.0.0.1:8765`. Keep the PowerShell window open. If the app is off at the chosen daily time, it generates that day's draft the next time the app starts. `Ctrl+C` stops the dashboard and its daily scheduler.

## Daily ideas

Save a weekly list of anime, shows, and themes in the dashboard. With daily generation enabled, the app asks Claude Code for one new idea at the chosen local time, avoids the recent topic list, and leaves it for review. The app does not publish on a schedule. A pending idea blocks later automatic drafts until you approve or reject it.

Draft and revision requests use the signed-in Claude Code CLI, one request per new script or revision. Editing and rendering do not use Claude. The prompt asks for a sharp opening, factual support, and an ending that flows back into the opening on replay; generated claims and sources still need your review.

## Build and upload flow

1. Review and edit the draft.
2. Approve the script to build a preview. Activity reports the current stage, log line, elapsed time, and a rough remaining-time range.
3. Cancel during the build if needed.
4. Watch the finished video in the dashboard, select visibility and the made-for-kids declaration, then press **Upload reviewed short**.

Approving the script does **not** upload or even render anything. It runs a two-checkpoint build:

1. **Narration & timing** — `narration.py` generates the ElevenLabs narration and its word-level timing (via the text-to-speech "with-timestamps" endpoint, falling back to ElevenLabs forced alignment; with no key it uses Windows speech synthesis with clearly-flagged *estimated* timing).
2. **Find stills** — the licensed image-finder agent (`image_finder.py`) divides the narration into 10–15 timed beats (each the exact phrase spoken, with start/end times), builds each beat's search query from the show/topic name plus that beat's keywords (so results stay on-subject), and finds one reusable, licensed still for each from **Openverse** and **Wikimedia Commons** (keyless, documented APIs that return explicit per-item license metadata). It accepts only CC0, public-domain, CC BY, and CC BY-SA images and rejects NC, ND, GFDL, unknown, or unverifiable licenses. It never downloads from YouTube, streaming, social media, show/anime sites, or image-search previews, and never assumes a screenshot, poster, character art, or GIF is reusable.
3. **Review stills** — the dashboard shows all beats in order with the exact phrase, its timestamps, the chosen image, and its source/license. You can **retry** a single beat's search (with an editable query), **replace** a beat with your own licensed image, or **remove** a beat — without regenerating the script or the other images. A beat with no suitable licensed image is flagged with its reason; it is never silently filled. Every beat needs an image before you can approve.
4. **Build & preview** — only after you approve the stills does the renderer build the video: hard cuts at the beat timestamps, each still shown only while its phrase is spoken, large phone-safe subtitles, and the already-generated narration. No clips, GIFs, zooms, pans, parallax, or crossfades.
5. **Upload** — you review the finished preview and explicitly approve the upload. Nothing uploads automatically after script or still approval.

Full source/license provenance for every beat is saved in `media_manifest.json` beside the render, and required credits are appended to the YouTube description automatically. The agent never calls Claude, so finding images uses no Claude tokens.

Put licensed background tracks in `work/media/music` and effects in `work/media/sfx`; music is mixed quietly under narration and matching effects are used by scene cue. If a track needs credit, put its required attribution in a neighboring `.txt` file with the same basename. Publicly available footage and GIFs may still be copyrighted; only use assets you own or have permission to use.

## Optional visual and voice credentials

For ElevenLabs narration, get an API key from the ElevenLabs dashboard and put it in a `secrets.env` file using the names in `secrets.env.example`. The file is read from any of these, most-specific first: the path in `SHORTS_SECRET_FILE`, then `%LOCALAPPDATA%\ShortsStudio\secrets.env`, then `secrets.env` in this project folder (which is `.gitignored`, so it stays out of source control). **Restart the app after editing the secrets file** — it is read once at startup. The project never needs a Claude API key for local Claude Code CLI use.

The image-finder agent's sources (Openverse and Wikimedia Commons) are keyless. An optional `OPENVERSE_ACCESS_TOKEN` only raises Openverse's anonymous rate limit. Any provider keys stay in the secrets file only — never in browser code, the media manifest, logs, or source control.

If no ElevenLabs key is configured, narration falls back to Windows speech synthesis and beat timings are estimated (and flagged as such in the dashboard). A beat with no suitable licensed image is flagged for review; the build cannot proceed to render until every beat has an image, which you supply by retrying the search or uploading your own licensed image.

## Hosting notes

This local app binds only to `127.0.0.1` and has no login system, so it is **not safe to expose directly to the public internet**. Hosting for phone access needs authentication, HTTPS, protected secret storage, persistent storage for YouTube OAuth refresh tokens and renders, and a hosted Claude API integration. A Claude Pro subscription does not include Anthropic API usage; API billing is separate. Do not put provider keys in a public repository or a frontend page.

OAuth credentials, refresh tokens, drafts, settings, and generated videos are stored under `work/` and ignored by Git.
