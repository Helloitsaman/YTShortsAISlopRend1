# Build Brief: Licensed Still-Image Finder Agent

## Goal

Build a small image-research component for Shorts Studio. For each approved short topic and scene plan, it finds relevant **still images**, checks that their reuse terms allow inclusion in a YouTube video, downloads them locally, and returns a source-and-license manifest. The existing Shorts Studio pipeline then uses those images as the scene backgrounds.

The component's only responsibility is image discovery, license checks, download, and reporting. It does not choose or rewrite the topic, write the script, create voice audio, edit video, upload to YouTube, or create artwork.

## Required video style

- Use one still image per scene, with enough variety to keep the short visually relevant.
- Place large, bold, high-contrast subtitles over the stills. Keep text within mobile-safe areas and away from the Shorts interface overlays.
- Use ElevenLabs for narration through the existing server-side credential configuration.
- Use straightforward cuts between still images. Do not add animation, zoom, pan, parallax, animated backgrounds, GIFs, generated graphics, or video clips.
- Do not build transitions or motion effects into this image-finding component.
- Keep the short engaging through the script, pacing, scene relevance, and readable subtitles. The script should have a strong opening hook and an ending that connects naturally to the opening when replayed. Script generation remains the responsibility of the existing Shorts workflow.

## Image-finding workflow

1. Accept the short's title, topic, approved script, and scene plan. Each scene should provide a concise `visual_query` and a description of what the image needs to communicate.
2. Search for images that illustrate the scene's specific idea. Prefer an exact subject when it is legally reusable; otherwise find an honest thematic match and record that it is illustrative.
3. Download one usable image per scene. Search a small number of results per scene; do not make a separate Claude request for each image. Prefer a source's documented search API or documented download flow. Do not scrape pages or bypass access controls, rate limits, or download restrictions.
4. Validate that each downloaded file is a readable still image, within reasonable file-size and dimension limits, and corresponds to the selected result. Reject broken files, unrelated images, watermarks that obscure the subject, and unsuitable content.
5. Save the accepted images under the current render's private work directory, for example:

   ```text
   work/renders/<short-id>-assets/images/scene-01.jpg
   work/renders/<short-id>-assets/images/scene-02.jpg
   ```

6. Write a `media_manifest.json` beside the images. Include, for every scene: scene number, local filename, source page URL, direct download URL if permitted, creator/uploader, license name, license URL, required attribution text, retrieval date, search query, and a short relevance note. Do not claim a license is verified when the source does not provide enough information to verify it.
7. Pass image paths and attribution data to the renderer. Append required credits to the YouTube description automatically. Keep the manifest with the render so the creator can audit the source later.

## Source and copyright rules

- Use only images that the source explicitly permits to be reused in YouTube videos, including monetized videos where applicable. Favor public-domain/CC0 material and clear Creative Commons Attribution licenses. Preserve attribution where required.
- Avoid licenses that prohibit commercial use or adaptations, and avoid unclear, missing, or conflicting licenses. Do not treat a search result, a “free download” button, or the presence of an image on a website as permission to reuse it.
- Never download frames, screenshots, thumbnails, fan art, GIFs, or clips from YouTube, streaming services, anime/show websites, social media, or search-engine image previews. Do not use copyrighted show characters, promotional art, logos, or anime frames unless a specific source proves that the exact image is licensed for this use.
- A topic mentioning a copyrighted show does not grant rights to its imagery. When no authorized exact-match image is available, use a clearly thematic, legally reusable image and record that it is illustrative. If no suitable image is found, leave that scene unfilled and report the problem; never silently substitute custom animation or an unlicensed image.
- Do not tell the creator that a video is guaranteed copyright-safe. Keep all provenance and license records available for review.

## User review and failures

- Show the selected stills and their source/license information in the dashboard before upload.
- Let the creator replace or remove a selected still before building the final video.
- If a source is unavailable, rate-limited, or returns no suitable licensed image, report which scene failed and why. Keep the rest of the job intact so the creator can retry or provide a local licensed image.
- Do not upload a video from this component. Upload remains behind Shorts Studio's existing creator approval flow.

## Integration boundaries

- Integrate with the existing scene plan and renderer; do not introduce a second script generator or duplicate the Claude draft call.
- The finder may use documented media-provider APIs, but must not call Claude. Image selection and download must not increase Claude token usage.
- Use environment variables or the existing private secrets-file mechanism for provider keys. Never put keys in browser code, `agentbuild.md`, source control, logs, or the media manifest.
- Keep the current local-first behavior. Do not expose the dashboard publicly or add hosting/authentication in this task.
- Preserve the review-before-upload checkpoint. Build a local preview from the downloaded stills, ElevenLabs narration, and subtitles; wait for creator approval before uploading.

## Completion criteria

- A topic and scene plan produce a local image folder and a complete license/provenance manifest.
- The final preview uses only those still images as backgrounds, large bold subtitles, and ElevenLabs narration when configured. It contains no custom animations or video clips.
- Required credits are included in the upload description, and the dashboard shows image previews and source/license information.
- Failed searches are visible and do not cause silent use of unlicensed or unrelated images.
- Provider credentials and OAuth files remain outside public source control.
