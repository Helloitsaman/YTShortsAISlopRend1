"""Licensed still-image finder agent for Shorts Studio.

Given an approved short's narration and its word-level timing, this agent:
  * divides the narration into 10-15 visual beats, each the exact sentence or
    phrase being spoken plus its start and end time from the alignment data,
  * builds each beat's search query from the beat's own thematic description —
    the scene's idea in openly reusable terms — and never from the show or
    franchise name: a topic that names a copyrighted show grants no rights to
    its imagery, and folding that name into an open-license search only surfaces
    mislabeled fan uploads of copyrighted characters. The result is an honest
    thematic match, recorded as illustrative, not an exact franchise frame.
  * finds one relevant, legally reusable still image per beat,
  * verifies each image's reuse terms, downloads it into the render's private
    assets folder, and records full source-and-license provenance.

It writes `beats.json` (the reviewable sequence), `media_manifest.json` (the
provenance record), and `attribution.txt` (credits for the description).

Scope and boundaries:
  * It only divides beats, discovers images, license-checks, downloads,
    validates, and reports. It never calls Claude (so it adds no Claude token
    usage), writes the script, generates narration, edits video, or uploads.
  * It never scrapes pages or bypasses access controls. It uses documented,
    keyless search APIs only: Openverse and Wikimedia Commons, both of which
    return explicit per-item license metadata.
  * It accepts only CC0, public-domain, CC BY, and CC BY-SA images whose terms
    can be met. NC, ND, GFDL, unknown, and unverifiable licenses are rejected.
  * It never downloads from YouTube, streaming, social media, show/anime sites,
    or image-search previews, and never assumes a screenshot, poster, character
    art, or GIF is reusable. When no licensed match is found, the beat is
    flagged for review rather than filled with an unrelated or unlicensed image.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
DEFAULT_SECRET_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ShortsStudio" / "secrets.env"

USER_AGENT = "ShortsStudioImageFinder/1.0 (local research tool; contact via app operator)"
HTTP_TIMEOUT = 30
RESULTS_PER_SCENE = 12
SEARCH_RETRIES = 2

MIN_BEATS = 10
MAX_BEATS = 15

# Image acceptance limits. Backgrounds are scaled to fill a 1080x1920 frame.
MIN_SHORT_SIDE = 480
MIN_LONG_SIDE = 640
MAX_DIMENSION = 12000
MAX_FILE_BYTES = 25 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
EXT_FOR_FORMAT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

# Creative Commons / public-domain licenses that permit commercial use *and*
# adaptation. NC and ND variants are excluded. share-alike (by-sa) is permitted
# but flagged so the creator knows the derivative inherits the same obligation.
ALLOWED_OPENVERSE_LICENSES = ["cc0", "pdm", "by", "by-sa"]
SHARE_ALIKE_LICENSES = {"by-sa"}
ATTRIBUTION_REQUIRED = {"by", "by-sa"}

LICENSE_DISCLAIMER = (
    "License and provenance recorded from each source's own metadata at retrieval time. "
    "This is a record for your review, not a guarantee that the video is copyright-safe. "
    "Verify terms before monetizing."
)

STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "they", "them", "their", "there", "here",
    "what", "when", "where", "which", "while", "would", "could", "should", "about", "into",
    "your", "you", "yours", "have", "has", "had", "been", "being", "were", "was", "are", "is",
    "but", "for", "not", "only", "just", "than", "then", "that's", "it's", "its", "his", "her",
    "him", "she", "he", "we", "our", "ours", "a", "an", "of", "to", "in", "on", "at", "it",
    "as", "so", "or", "if", "by", "be", "do", "does", "did", "no", "yes", "all", "any", "more",
    "most", "some", "such", "very", "much", "many", "over", "under", "every", "each", "who",
    "whom", "whose", "how", "why", "because", "really", "actually", "something", "someone",
    "things", "thing", "make", "makes", "made", "like", "look", "looks", "watch", "watched",
    "part", "sure", "still", "even", "always", "never", "keep", "keeps", "kept",
}


def load_env_file() -> None:
    candidates = []
    explicit = os.environ.get("SHORTS_SECRET_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(DEFAULT_SECRET_PATH)
    candidates.append(ROOT / "secrets.env")  # project-local location
    seen: set[str] = set()
    for env_path in candidates:
        marker = str(env_path).lower()
        if marker in seen or not env_path.is_file():
            continue
        seen.add(marker)
        # utf-8-sig tolerates a BOM that Notepad adds when saving the file.
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def log(message: str) -> None:
    print(message, flush=True)


def relative_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b"", exc.headers.get("Content-Type", "") if exc.headers else ""


# --------------------------------------------------------------------------- #
# Beat division + timing
# --------------------------------------------------------------------------- #
CLAUSE_SPLIT = re.compile(r",|;|:|—|\s-\s|\s–\s")


def _tokens(text: str) -> list[str]:
    return text.split()


def _split_clause(beat: str) -> tuple[str, str] | None:
    """Split a long beat at the clause boundary nearest its token midpoint."""
    matches = list(CLAUSE_SPLIT.finditer(beat))
    if not matches:
        return None
    mid = len(beat) / 2
    best = min(matches, key=lambda m: abs(m.end() - mid))
    left = beat[:best.end()].strip()
    right = beat[best.end():].strip()
    if len(_tokens(left)) < 2 or len(_tokens(right)) < 2:
        return None
    return left, right


def split_into_beats(script: str) -> list[str]:
    """Divide the script into roughly 10-15 beats on sentence/clause lines."""
    beats = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script.strip()) if s.strip()]
    if not beats:
        return [script.strip()] if script.strip() else []

    # Too few: split the longest splittable beats until we reach the minimum.
    guard = 0
    while len(beats) < MIN_BEATS and guard < 200:
        guard += 1
        order = sorted(range(len(beats)), key=lambda i: len(_tokens(beats[i])), reverse=True)
        split_done = False
        for i in order:
            pieces = _split_clause(beats[i])
            if pieces:
                beats[i:i + 1] = list(pieces)
                split_done = True
                break
        if not split_done:
            break  # unusually short script; accept fewer beats

    # Too many: merge the adjacent pair with the smallest combined length.
    guard = 0
    while len(beats) > MAX_BEATS and guard < 500:
        guard += 1
        pair = min(range(len(beats) - 1),
                   key=lambda i: len(_tokens(beats[i])) + len(_tokens(beats[i + 1])))
        beats[pair:pair + 2] = [beats[pair] + " " + beats[pair + 1]]
    return beats


def time_beats(beat_texts: list[str], words: list[dict], duration: float) -> list[tuple[float, float]]:
    """Assign each beat a (start, end) using word alignment, or a proportional
    estimate when no alignment is available."""
    counts = [max(1, len(_tokens(text))) for text in beat_texts]
    spans: list[tuple[float, float]] = []

    if words:
        pointer = 0
        last_end = 0.0
        for count in counts:
            start_idx = min(pointer, len(words) - 1)
            end_idx = min(pointer + count - 1, len(words) - 1)
            start = float(words[start_idx]["start"])
            end = float(words[end_idx]["end"])
            start = max(start, last_end)
            end = max(end, start + 0.1)
            spans.append((round(start, 3), round(end, 3)))
            last_end = end
            pointer += count
        # Pin the final beat to the narration end so the video covers all audio.
        if spans:
            s, _ = spans[-1]
            spans[-1] = (s, round(max(duration, s + 0.1), 3))
        return spans

    total = sum(counts)
    cursor = 0
    for count in counts:
        start = duration * cursor / total
        cursor += count
        end = duration * cursor / total
        spans.append((round(start, 3), round(max(end, start + 0.1), 3)))
    return spans


# --------------------------------------------------------------------------- #
# Query derivation: the beat's own thematic description, never the franchise.
#
# A topic that names a copyrighted show (an anime, a series, a film) grants no
# rights to its imagery. Searching an open-license library for that name does
# not return the real show — it returns whatever someone mislabeled with it,
# which tends to be fan uploads of copyrighted characters. So queries describe
# the scene's idea in openly reusable, real-world terms (a place, setting,
# object, mood, or concept). An exact subject is preferred only when it is
# itself legally reusable; otherwise the match is an honest thematic one,
# recorded as illustrative. The franchise name is never used as a search anchor.
# --------------------------------------------------------------------------- #
# Words from a topic that describe the *angle* rather than the show/subject;
# dropped when extracting the show name so the recorded subject stays clean.
ANGLE_WORDS = {
    "why", "how", "what", "the", "a", "an", "of", "is", "isn't", "are", "and", "or",
    "explained", "explainer", "analysis", "theory", "breakdown", "really", "actually",
    "truth", "secret", "hidden", "meaning", "ending", "review", "reaction", "story",
}


def subject_phrase(topic: str, title: str) -> str:
    """The show / anime / topic name, kept only for the record and logs.

    It is NOT used to build image searches (see the note above): anchoring an
    open-license search on a franchise name surfaces mislabeled copyrighted
    imagery. It is stored in beats.json so the creator can see the topic the
    plan was built from.

    Keeps short franchise labels split by a colon (for example "Cyberpunk:
    Edgerunners") while discarding explanatory text after the show name."""
    base = str(topic or "").strip() or str(title or "").strip()
    pieces = re.split(r"[:—–\-\|]", base, 1)
    base = pieces[0].strip()
    if len(pieces) > 1 and len(base.split()) <= 2:
        continuation = pieces[1].strip().split()
        if continuation and re.sub(r"[^A-Za-z']", "", continuation[0]).lower() not in ANGLE_WORDS:
            base = f"{base} {continuation[0]}"
    words = [w for w in base.split() if w]
    while words and re.sub(r"[^A-Za-z']", "", words[0]).lower() in ANGLE_WORDS:
        words.pop(0)
    return " ".join(words[:6]).strip()


def content_keywords(text: str, limit: int = 3) -> list[str]:
    """Salient keywords from a beat's text (proper nouns kept — they add relevance)."""
    seen: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z'\-]+", text):
        low = token.lower()
        if len(token) <= 3 or low in STOPWORDS:
            continue
        if low not in seen:
            seen.append(low)
        if len(seen) >= limit:
            break
    return seen


def query_variants(beat_text: str) -> list[str]:
    """Thematic search queries for a beat when Claude supplied no image_query.

    Built only from the beat's own salient keywords — the scene's idea in
    reusable terms — never the franchise name, so the search stays an honest
    thematic match rather than a hunt for copyrighted show imagery. Broader
    keyword subsets follow so the beat can still be filled."""
    keywords = content_keywords(beat_text, 4)
    variants: list[str] = []
    if keywords:
        variants.append(" ".join(keywords[:3]))
        if len(keywords) > 2:
            variants.append(" ".join(keywords[:2]))
    out: list[str] = []
    for variant in variants:
        cleaned = variant.strip()
        if cleaned and cleaned.lower() not in {o.lower() for o in out}:
            out.append(cleaned)
    return out or [beat_text.strip()[:60] or "background"]


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def claude_visual_beats(draft: dict, script: str) -> tuple[list[str], list[str]] | None:
    """Use Claude's per-beat image requirements when they line up with the script.

    Claude (in the single draft-generation call) splits the script into 10-15
    consecutive phrases, each with a precise image_query naming the subject to
    show. We use them only if their words, in order, equal the narrated script,
    so subtitles stay in sync and timing is exact."""
    beats = draft.get("visual_beats")
    if not isinstance(beats, list) or not (8 <= len(beats) <= 16):
        return None
    phrases, queries = [], []
    for beat in beats:
        if not isinstance(beat, dict):
            return None
        phrase = str(beat.get("phrase", "")).strip()
        if not phrase:
            return None
        phrases.append(phrase)
        queries.append(str(beat.get("image_query", "")).strip())
    if _word_tokens(" ".join(phrases)) != _word_tokens(script):
        return None  # the creator edited the script; fall back to heuristic beats
    return phrases, queries


def beat_query_variants(image_query: str) -> list[str]:
    """Search queries from Claude's thematic image requirement for a beat.

    The requirement already describes the subject to show in openly reusable,
    real-world terms. We search it directly, then fall back to its salient
    keywords, and never fold in the show or franchise name — doing so would turn
    an honest thematic search into a hunt for copyrighted character imagery."""
    query = (image_query or "").strip()
    variants: list[str] = []
    if query:
        variants.append(query)
        keywords = content_keywords(query, 4)
        if keywords:
            variants.append(" ".join(keywords[:3]))
            if len(keywords) > 2:
                variants.append(" ".join(keywords[:2]))
    out: list[str] = []
    for variant in variants:
        cleaned = variant.strip()
        if cleaned and cleaned.lower() not in {o.lower() for o in out}:
            out.append(cleaned)
    return out


# --------------------------------------------------------------------------- #
# Candidate model + providers
# --------------------------------------------------------------------------- #
class Candidate:
    def __init__(self, *, provider: str, download_url: str, source_page_url: str, creator: str,
                 license_name: str, license_url: str, attribution: str, title: str,
                 width: int | None, height: int | None, license_verified: bool, obligations: list[str]):
        self.provider = provider
        self.download_url = download_url
        self.source_page_url = source_page_url
        self.creator = creator
        self.license_name = license_name
        self.license_url = license_url
        self.attribution = attribution
        self.title = title
        self.width = width
        self.height = height
        self.license_verified = license_verified
        self.obligations = obligations


def search_openverse(query: str) -> list[Candidate]:
    params = {
        "q": query, "license": ",".join(ALLOWED_OPENVERSE_LICENSES),
        "license_type": "commercial,modification", "page_size": RESULTS_PER_SCENE, "mature": "false",
    }
    headers = {"Accept": "application/json"}
    token = os.environ.get("OPENVERSE_ACCESS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    for attempt in range(SEARCH_RETRIES + 1):
        status, body, _ = http_get(url, headers)
        if status in (429, 502, 503, 504) and attempt < SEARCH_RETRIES:
            time.sleep(2 * (attempt + 1))
            continue
        if status != 200:
            raise RuntimeError(f"Openverse search returned HTTP {status}")
        break
    else:  # pragma: no cover
        raise RuntimeError("Openverse search was unavailable")

    data = json.loads(body.decode("utf-8", "replace"))
    candidates: list[Candidate] = []
    for item in data.get("results", []) or []:
        code = str(item.get("license", "")).lower().strip()
        if code not in ALLOWED_OPENVERSE_LICENSES:
            continue
        version = str(item.get("license_version", "")).strip()
        pretty = "CC0" if code == "cc0" else ("Public Domain Mark" if code == "pdm"
                                              else f"CC {code.upper()} {version}".strip())
        obligations: list[str] = []
        if code in ATTRIBUTION_REQUIRED:
            obligations.append("attribution required")
        if code in SHARE_ALIKE_LICENSES:
            obligations.append("share-alike: a derivative work must use the same license")
        candidates.append(Candidate(
            provider="Openverse", download_url=str(item.get("url", "")).strip(),
            source_page_url=str(item.get("foreign_landing_url") or item.get("url") or "").strip(),
            creator=str(item.get("creator", "") or "").strip(), license_name=pretty,
            license_url=str(item.get("license_url", "") or "").strip(),
            attribution=str(item.get("attribution", "") or "").strip(),
            title=str(item.get("title", "") or "").strip(),
            width=item.get("width"), height=item.get("height"),
            license_verified=True, obligations=obligations))
    return candidates


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _commons_license(code: str) -> tuple[bool, bool, bool]:
    """Classify a Wikimedia license code -> (allowed, attribution_required, share_alike)."""
    value = (code or "").lower().strip()
    if not value:
        return False, False, False
    if "nc" in value or "nd" in value or "fair" in value or "nonfree" in value or "gfdl" in value:
        return False, False, False
    if value in ("cc0", "cc-zero") or value.startswith(("pd", "publicdomain")):
        return True, False, False
    if value.startswith(("cc-by-sa", "cc_by_sa")):
        return True, True, True
    if value.startswith(("cc-by", "cc_by")):
        return True, True, False
    return False, False, False


def search_wikimedia(query: str) -> list[Candidate]:
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": "6",
        "gsrlimit": RESULTS_PER_SCENE, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1600",
    }
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    status, body, _ = http_get(url, {"Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"Wikimedia Commons search returned HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    pages = (data.get("query", {}) or {}).get("pages", {}) or {}
    candidates: list[Candidate] = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 1_000_000)):
        info = (page.get("imageinfo") or [{}])[0]
        if str(info.get("mime", "")).lower() not in ("image/jpeg", "image/png", "image/webp"):
            continue
        meta = info.get("extmetadata", {}) or {}

        def field(name: str) -> str:
            value = meta.get(name)
            return str(value.get("value", "")) if isinstance(value, dict) else ""

        allowed, attribution_required, share_alike = _commons_license(field("License") or field("LicenseShortName"))
        if not allowed:
            continue
        short_name = _strip_html(field("LicenseShortName"))
        if not short_name:
            continue
        license_url = field("LicenseUrl").strip()
        artist = _strip_html(field("Artist")) or _strip_html(field("Credit")) or "Unknown"
        obligations: list[str] = []
        if attribution_required:
            obligations.append("attribution required")
        if share_alike:
            obligations.append("share-alike: a derivative work must use the same license")
        download = str(info.get("thumburl") or info.get("url") or "").strip()
        if not download:
            continue
        candidates.append(Candidate(
            provider="Wikimedia Commons", download_url=download,
            source_page_url=str(info.get("descriptionurl") or info.get("url") or "").strip(),
            creator=artist, license_name=short_name, license_url=license_url,
            attribution=f"{_strip_html(field('Credit')) or _strip_html(page.get('title', ''))} by {artist} — {short_name}",
            title=_strip_html(page.get("title", "")),
            width=info.get("thumbwidth") or info.get("width"),
            height=info.get("thumbheight") or info.get("height"),
            license_verified=bool(license_url), obligations=obligations))
    return candidates


# Keyless, documented sources with explicit per-item license metadata.
PROVIDERS = [("Openverse", search_openverse), ("Wikimedia Commons", search_wikimedia)]


# --------------------------------------------------------------------------- #
# Download + validation
# --------------------------------------------------------------------------- #
def download_and_validate(url: str, target_base: Path) -> tuple[Path, int, int] | None:
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        status, body, content_type = http_get(url, {"Accept": "image/*"})
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if status != 200 or not body or len(body) > MAX_FILE_BYTES:
        return None
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in ("image/gif", "image/svg+xml") or ctype.startswith(("text/", "application/")):
        return None
    try:
        with Image.open(io.BytesIO(body)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(body)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ALLOWED_FORMATS:
                return None
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                return None
            width, height = img.size
    except Exception:
        return None
    short_side, long_side = sorted((width, height))
    if short_side < MIN_SHORT_SIDE or long_side < MIN_LONG_SIDE or long_side > MAX_DIMENSION:
        return None
    for stale in target_base.parent.glob(target_base.name + ".*"):
        stale.unlink(missing_ok=True)
    saved = target_base.with_suffix("." + EXT_FOR_FORMAT.get(fmt, "jpg"))
    saved.write_bytes(body)
    return saved, width, height


def candidate_to_source(candidate: Candidate, width: int, height: int, query: str) -> dict:
    return {
        "provider": candidate.provider,
        "source_page_url": candidate.source_page_url,
        "download_url": candidate.download_url,
        "creator": candidate.creator or "Unknown",
        "title": candidate.title,
        "license_name": candidate.license_name,
        "license_url": candidate.license_url,
        "license_verified": candidate.license_verified,
        "attribution_text": candidate.attribution,
        "license_obligations": candidate.obligations,
        "width": width,
        "height": height,
        "retrieval_date": dt.date.today().isoformat(),
        "search_query": query,
    }


def search_and_download(queries: list[str] | str, target_base: Path, exclude_urls: set[str] | None = None
                        ) -> tuple[dict, Path] | tuple[None, None]:
    """Try each query in order across the providers; return the first validated,
    licensed image as (source_dict, saved_path). Earlier queries are the beat's
    full thematic description, later ones broader keyword subsets, so the result
    is as on-topic as the libraries allow while still filling the beat."""
    if isinstance(queries, str):
        queries = [queries]
    exclude = exclude_urls or set()
    errors: list[str] = []
    for query in queries:
        for name, search in PROVIDERS:
            try:
                candidates = search(query)
            except Exception as exc:
                errors.append(f"{name} ('{query}'): {exc}")
                continue
            for candidate in candidates:
                if candidate.download_url in exclude:
                    continue
                result = download_and_validate(candidate.download_url, target_base)
                if result:
                    saved, width, height = result
                    return candidate_to_source(candidate, width, height, query), saved
    if errors:
        raise RuntimeError("; ".join(errors[:4]))
    return None, None


# --------------------------------------------------------------------------- #
# Manifest + attribution (derived from beats.json)
# --------------------------------------------------------------------------- #
def build_attribution_block(beats: list[dict]) -> str:
    lines = ["Image credits:"]
    any_credit = False
    for beat in beats:
        source = beat.get("source") or {}
        credit = str(source.get("attribution_text", "")).strip()
        if not credit:
            continue
        any_credit = True
        extra = [x for x in (source.get("license_url"), source.get("source_page_url")) if x]
        suffix = (" — " + " — ".join(extra)) if extra else ""
        # Keep the public description focused on creator/license attribution.
        # The provider name remains recorded in media_manifest.json.
        lines.append(f"- Beat {beat['beat']}: {credit}{suffix}")
    return "\n".join(lines) if any_credit else ""


def merge_attribution(assets_dir: Path, block: str) -> None:
    path = assets_dir / "attribution.txt"
    existing = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    kept: list[str] = []
    skipping = False
    for line in existing.splitlines():
        if line.strip() == "Image credits:":
            skipping = True
            continue
        if skipping:
            if line.startswith("- Beat "):
                continue
            skipping = False
        kept.append(line)
    rest = "\n".join(x for x in kept if x.strip())
    merged = "\n".join(x for x in (block, rest) if x.strip())
    path.write_text((merged.strip() + "\n") if merged.strip() else "", encoding="utf-8")


def finalize(assets_dir: Path) -> dict:
    """Rewrite media_manifest.json + attribution.txt from the current beats.json."""
    data = load_beats(assets_dir)
    beats = data.get("beats", [])
    scenes = []
    failures = []
    for beat in beats:
        source = beat.get("source") or {}
        if beat.get("status") in ("ok", "manual") and beat.get("image_path"):
            scenes.append({
                "beat": beat["beat"],
                "script_excerpt": beat.get("text", ""),
                "start": beat.get("start"),
                "end": beat.get("end"),
                "filename": Path(beat["image_path"]).name,
                "local_path": beat["image_path"],
                "match_type": "creator-supplied" if beat.get("status") == "manual" else "illustrative",
                "image_requirement": beat.get("image_query", ""),
                "source_page_url": source.get("source_page_url", ""),
                "download_url": source.get("download_url", ""),
                "provider": source.get("provider", ""),
                "creator": source.get("creator", ""),
                "title": source.get("title", ""),
                "license_name": source.get("license_name", ""),
                "license_url": source.get("license_url", ""),
                "license_verified": source.get("license_verified", False),
                "attribution_text": source.get("attribution_text", ""),
                "license_obligations": source.get("license_obligations", []),
                "width": source.get("width"),
                "height": source.get("height"),
                "retrieval_date": source.get("retrieval_date", ""),
                "search_query": beat.get("search_query", ""),
                "relevance_note": beat.get("relevance_note", ""),
            })
        else:
            failures.append({"beat": beat["beat"], "script_excerpt": beat.get("text", ""),
                             "reason": beat.get("failure_reason") or "No licensed image selected."})
    manifest = {
        "short_topic": data.get("topic", ""),
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "disclaimer": LICENSE_DISCLAIMER,
        "timing_source": data.get("timing_source", ""),
        "beat_count": len(beats),
        "filled": len(scenes),
        "scenes": scenes,
        "failures": failures,
    }
    write_json(assets_dir / "media_manifest.json", manifest)
    merge_attribution(assets_dir, build_attribution_block(beats))
    return manifest


# --------------------------------------------------------------------------- #
# beats.json load/save
# --------------------------------------------------------------------------- #
def load_beats(assets_dir: Path) -> dict:
    return json.loads((assets_dir / "beats.json").read_text(encoding="utf-8"))


def save_beats(assets_dir: Path, data: dict) -> None:
    write_json(assets_dir / "beats.json", data)


def relevance_for(query: str) -> str:
    return (f"Illustrative thematic match for \"{query}\". Sourced from a public-domain or openly licensed "
            "library and selected for the scene's theme rather than the exact franchise — an openly licensed "
            "image, never a copyrighted show frame, character render, or promotional art.")


# --------------------------------------------------------------------------- #
# Planning (full run) and single-beat operations
# --------------------------------------------------------------------------- #
def plan(assets_dir: Path, draft: dict) -> dict:
    load_env_file()
    alignment = json.loads((assets_dir / "alignment.json").read_text(encoding="utf-8"))
    script = str(alignment.get("script") or draft.get("script", "")).strip()
    words = alignment.get("words") or []
    duration = float(alignment.get("duration") or 0.0)

    images_dir = assets_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    subject = subject_phrase(str(draft.get("topic", "")), str(draft.get("title", "")))
    claude = claude_visual_beats(draft, script)
    if claude:
        beat_texts, image_queries = claude
        source_note = "Claude per-beat image requirements"
    else:
        beat_texts = split_into_beats(script)
        image_queries = [""] * len(beat_texts)
        source_note = "heuristic (thematic beat keywords; no usable Claude beats)"
    spans = time_beats(beat_texts, words, duration)

    # Preserve creator-supplied (manual) images across a re-plan: keep any prior
    # beat with the same spoken text whose image file still exists.
    preserved: dict[str, dict] = {}
    if (assets_dir / "beats.json").is_file():
        try:
            for prior in load_beats(assets_dir).get("beats", []):
                if prior.get("status") == "manual" and prior.get("image_path") and (ROOT / prior["image_path"]).is_file():
                    preserved[str(prior.get("text", "")).strip()] = prior
        except Exception:
            preserved = {}

    log(f"[1/1] Dividing narration into {len(beat_texts)} beats "
        f"(timing: {alignment.get('timing_source')}; subject: \"{subject or '—'}\"; queries: {source_note}).")

    beats: list[dict] = []
    for index, (text, (start, end)) in enumerate(zip(beat_texts, spans)):
        number = index + 1
        requirement = image_queries[index] if index < len(image_queries) else ""
        variants = beat_query_variants(requirement) if requirement else query_variants(text)
        query = variants[0] if variants else text
        keep = preserved.get(text.strip())
        if keep:
            beat = dict(keep)
            beat.update({"beat": number, "text": text, "start": start, "end": end,
                         "image_query": requirement})
            beats.append(beat)
            log(f"[1/1] Beat {number}: kept your replacement image.")
            continue
        beat = {"beat": number, "text": text, "start": start, "end": end,
                "image_query": requirement, "image_path": "", "status": "failed",
                "search_query": query, "relevance_note": relevance_for(query),
                "source": None, "failure_reason": ""}
        target_base = images_dir / f"beat-{number:02d}"
        try:
            source, saved = search_and_download(variants, target_base)
        except Exception as exc:
            source, saved = None, None
            beat["failure_reason"] = f"Search error: {exc}"
        if source and saved:
            beat["image_path"] = relative_to_root(saved)
            beat["status"] = "ok"
            beat["source"] = source
            beat["search_query"] = source.get("search_query", query)
            beat["relevance_note"] = (f"Requirement: {requirement}. Illustrative thematic match \"{beat['search_query']}\" "
                                      "from an openly licensed library — never a copyrighted frame, character render, or poster." if requirement
                                      else relevance_for(beat["search_query"]))
            log(f"[1/1] Beat {number}: {source['license_name']} from {source['provider']} "
                f"for \"{beat['search_query']}\".")
        else:
            if not beat["failure_reason"]:
                want = f"\"{requirement}\"" if requirement else f"\"{query}\""
                beat["failure_reason"] = (f"No licensed image found for {want}. Retry with different terms "
                                          "or upload your own licensed image.")
            log(f"[1/1] Beat {number}: FLAGGED — {beat['failure_reason']}")
        beats.append(beat)

    data = {
        "short_topic": str(draft.get("topic", "")),
        "topic": str(draft.get("topic", "")),
        "subject": subject,
        "timing_source": alignment.get("timing_source", ""),
        "audio_path": alignment.get("audio_path", ""),
        "duration": duration,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "approved": False,
        "beats": beats,
    }
    save_beats(assets_dir, data)
    finalize(assets_dir)
    filled = sum(1 for b in beats if b["status"] in ("ok", "manual") and b["image_path"])
    log(f"[1/1] Beat plan ready: {filled} of {len(beats)} beats have a licensed image.")
    return data


def _reset_image_files(assets_dir: Path, number: int) -> None:
    for stale in (assets_dir / "images").glob(f"beat-{number:02d}.*"):
        stale.unlink(missing_ok=True)


def retry_beat(assets_dir: Path, number: int, query_override: str | None = None) -> dict:
    load_env_file()
    data = load_beats(assets_dir)
    beats = data["beats"]
    beat = next((b for b in beats if b["beat"] == number), None)
    if beat is None:
        raise ValueError(f"Beat {number} does not exist.")
    override = (query_override or "").strip()
    requirement = str(beat.get("image_query", "")).strip()
    if override:
        # Honour the creator's exact search terms as given, with a keyword
        # fallback. The franchise name is never prepended (see beat_query_variants).
        keywords = content_keywords(override, 3)
        queries = [override] + ([" ".join(keywords[:2])] if keywords else [])
        queries = list(dict.fromkeys(q for q in queries if q))
    elif requirement:
        queries = beat_query_variants(requirement)
    else:
        queries = query_variants(beat.get("text", ""))
    primary = queries[0] if queries else beat.get("search_query", "")
    exclude = set()
    current = beat.get("source") or {}
    if current.get("download_url"):
        exclude.add(current["download_url"])
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)
    target_base = assets_dir / "images" / f"beat-{number:02d}"
    _reset_image_files(assets_dir, number)
    try:
        source, saved = search_and_download(queries, target_base, exclude_urls=exclude)
    except Exception as exc:
        source, saved = None, None
        failure = f"Search error: {exc}"
    else:
        failure = f"No new licensed image passed checks for \"{primary}\"."
    beat["search_query"] = (source.get("search_query") if source else primary) or primary
    beat["relevance_note"] = relevance_for(beat["search_query"])
    if source and saved:
        beat["image_path"] = relative_to_root(saved)
        beat["status"] = "ok"
        beat["source"] = source
        beat["failure_reason"] = ""
    else:
        beat["image_path"] = ""
        beat["status"] = "failed"
        beat["source"] = None
        beat["failure_reason"] = failure
    save_beats(assets_dir, data)
    finalize(assets_dir)
    return beat


def replace_beat_image(assets_dir: Path, number: int, image_bytes: bytes, original_name: str) -> dict:
    data = load_beats(assets_dir)
    beats = data["beats"]
    beat = next((b for b in beats if b["beat"] == number), None)
    if beat is None:
        raise ValueError(f"Beat {number} does not exist.")
    if len(image_bytes) > MAX_FILE_BYTES:
        raise ValueError("Image is too large.")
    try:
        with Image.open(io.BytesIO(image_bytes)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(image_bytes)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ALLOWED_FORMATS:
                raise ValueError("Use a JPG, PNG, or WEBP still image.")
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                raise ValueError("Animated images are not allowed; use a still image.")
            width, height = img.size
    except ValueError:
        raise
    except Exception:
        raise ValueError("That file is not a readable still image.")
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)
    _reset_image_files(assets_dir, number)
    saved = assets_dir / "images" / f"beat-{number:02d}.{EXT_FOR_FORMAT.get(fmt, 'jpg')}"
    saved.write_bytes(image_bytes)
    beat["image_path"] = relative_to_root(saved)
    beat["status"] = "manual"
    beat["failure_reason"] = ""
    beat["relevance_note"] = "Creator-supplied image; its license was not verified by the agent."
    beat["source"] = {
        "provider": "creator-supplied (local upload)",
        "source_page_url": "", "download_url": "", "creator": "", "title": original_name,
        "license_name": "Not verified by the image agent", "license_url": "",
        "license_verified": False, "attribution_text": "",
        "license_obligations": ["License not checked by the agent; confirm reuse rights yourself."],
        "width": width, "height": height, "retrieval_date": dt.date.today().isoformat(),
        "search_query": "",
    }
    save_beats(assets_dir, data)
    finalize(assets_dir)
    return beat


def remove_beat(assets_dir: Path, number: int) -> dict:
    data = load_beats(assets_dir)
    beats = data["beats"]
    index = next((i for i, b in enumerate(beats) if b["beat"] == number), None)
    if index is None:
        raise ValueError(f"Beat {number} does not exist.")
    if len(beats) <= 1:
        raise ValueError("A Short needs at least one beat; cannot remove the last one.")
    removed = beats.pop(index)
    _reset_image_files(assets_dir, number)
    # Absorb the removed beat's time into a neighbour so the audio stays covered.
    start, end = removed.get("start", 0.0), removed.get("end", 0.0)
    if index < len(beats):  # extend the next beat backwards to the removed start
        beats[index]["start"] = min(beats[index].get("start", start), start)
    else:  # removed the last beat: extend the new last beat to the old end
        beats[-1]["end"] = max(beats[-1].get("end", end), end)
    # Renumber beats and their image files so they stay contiguous.
    images_dir = assets_dir / "images"
    renamed: list[tuple[Path, Path]] = []
    for new_index, beat in enumerate(beats, start=1):
        old_number = beat["beat"]
        if old_number != new_index and beat.get("image_path"):
            old_path = ROOT / beat["image_path"]
            if old_path.is_file():
                new_path = images_dir / f"beat-{new_index:02d}{old_path.suffix}"
                renamed.append((old_path, new_path))
                beat["image_path"] = relative_to_root(new_path)
        beat["beat"] = new_index
    # Apply renames via a temp pass to avoid clobbering.
    for old_path, new_path in renamed:
        tmp = old_path.with_name("tmp-" + old_path.name)
        old_path.replace(tmp)
        renamed_tmp = tmp
        new_path.unlink(missing_ok=True)
        renamed_tmp.replace(new_path)
    save_beats(assets_dir, data)
    finalize(assets_dir)
    return data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Find licensed still images for narration beats.")
    parser.add_argument("--assets-dir", required=True, type=Path)
    parser.add_argument("--mode", default="plan", choices=["plan", "retry", "finalize"])
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--beat", type=int)
    parser.add_argument("--query")
    args = parser.parse_args()
    assets_dir = args.assets_dir.resolve()
    try:
        if args.mode == "plan":
            if not args.draft:
                raise ValueError("--draft is required for plan mode.")
            plan(assets_dir, json.loads(args.draft.read_text(encoding="utf-8")))
        elif args.mode == "retry":
            if args.beat is None:
                raise ValueError("--beat is required for retry mode.")
            retry_beat(assets_dir, args.beat, args.query)
        elif args.mode == "finalize":
            finalize(assets_dir)
        return 0
    except Exception as exc:
        log(f"[1/1] Image finder could not run: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
