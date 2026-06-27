"""
Phase 7 — Instagram via instagrapi (unofficial, free, no Cloud project, no credit card).

Scope is deliberately *personal-volume, read-mostly*: see who DM'd, who posted a story,
how a post/story is doing (likes + comments + story viewers), and send the occasional DM —
all the things the boss does by hand, just faster. No cold-DM blasting, no scraping bursts;
a small minimum interval is enforced between calls to stay gentle and keep the (old, trusted)
account safe.

The session is persisted to disk (config.IG_SESSION_PATH) so we log in once and reuse the
device fingerprint + cookies on every later run — this is what avoids repeated login
challenges. If a challenge or 2FA is required, we degrade to a clear, in-character message
rather than crashing.

instagrapi is synchronous (requests under the hood); callers wrap these in asyncio.to_thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from config import IG_USERNAME, IG_PASSWORD, IG_SESSION_PATH

logger = logging.getLogger("jarvis.messaging.instagram")

# Min gap between Instagram calls (anti-burst). Kept modest — enough to look human for
# on-demand personal use, without the long waits the old 2.5s caused on multi-step commands.
_MIN_INTERVAL = 0.8


class InstagramError(RuntimeError):
    pass


@dataclass
class IGThread:
    thread_id: str
    username: str
    full_name: str
    text: str
    ts: float
    unread: bool
    is_group: bool = False
    title: str = ""           # group name (when is_group)
    members: int = 0          # participant count (groups)


class InstagramClient:
    def __init__(self) -> None:
        self.username = IG_USERNAME
        self._password = IG_PASSWORD
        self._session_path = str(IG_SESSION_PATH)
        self._cl = None                 # lazy instagrapi.Client
        self._logged_in = False
        self._lock = threading.RLock()
        self._last_call = 0.0
        self._self_id: str | None = None
        self._uid_cache: dict[str, str] = {}      # username(lower) -> user_id (avoid re-lookups)
        self._resolve_cache: dict[str, str] = {}  # what he said (lower) -> real username
        # username->user_id is STABLE on Instagram, so persist it next to the session: a restart
        # then skips the user_id_from_username network call on the first DM to a known contact.
        import os as _os
        self._uid_cache_path = _os.path.join(
            _os.path.dirname(self._session_path) or ".", "ig_uid_cache.json")
        self._load_uid_cache()
        # Negative cache: after a failed connect we back off rather than hammering Instagram
        # (repeated auth attempts from one IP are exactly what triggers/worsens an IP block).
        self._fail_until = 0.0
        self._fail_msg = ""

    @property
    def enabled(self) -> bool:
        return bool(self.username and self._password)

    # ---- session / login -------------------------------------------------- #
    def _throttle(self) -> None:
        wait = _MIN_INTERVAL - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _load_uid_cache(self) -> None:
        import json, os
        try:
            if os.path.exists(self._uid_cache_path):
                with open(self._uid_cache_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self._uid_cache = {str(k): str(v) for k, v in d.items()}
        except Exception:  # noqa: BLE001
            pass

    def _save_uid_cache(self) -> None:
        import json
        try:
            with open(self._uid_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._uid_cache, f)
        except Exception:  # noqa: BLE001
            pass

    def _user_id(self, cl, username: str) -> str:
        """Cached username -> user_id (each lookup is a network call; cache kills repeats)."""
        key = username.lstrip("@").strip().lower()
        if key in self._uid_cache:
            return self._uid_cache[key]
        uid = str(cl.user_id_from_username(key))
        self._uid_cache[key] = uid
        self._save_uid_cache()
        return uid

    def _fail(self, msg: str, backoff: float = 600.0):
        """Record a connect failure and back off (negative cache) so we don't hammer Instagram
        from a possibly-flagged IP. Raises InstagramError."""
        self._fail_msg = msg
        self._fail_until = time.time() + backoff
        raise InstagramError(msg)

    def _client(self):
        """Return a logged-in instagrapi Client, reusing the session saved by scripts/ig_login.py.

        IMPORTANT: the backend NEVER does a raw password login (that triggers 2FA / challenges
        and, from a flagged IP, an IP blacklist). The one-time interactive login lives only in
        scripts/ig_login.py. Here we just consume its saved session and validate it gently."""
        if self._logged_in and self._cl is not None:
            return self._cl
        if not self.enabled:
            raise InstagramError("Instagram isn't connected.")
        with self._lock:
            if self._logged_in and self._cl is not None:
                return self._cl
            if time.time() < self._fail_until:        # still in backoff — don't touch Instagram
                raise InstagramError(self._fail_msg)

            import os
            if not os.path.exists(self._session_path):
                self._fail("Instagram needs a one-time login. Run:  python scripts/ig_login.py",
                           backoff=60)

            from instagrapi import Client
            cl = Client()
            cl.delay_range = [0.3, 1.0]
            # Validate the SAVED session with a light authenticated call (account_info), NOT
            # get_timeline_feed (Instagram 403s that even on valid sessions).
            try:
                cl.load_settings(self._session_path)
                info = cl.account_info()
            except Exception as e:  # noqa: BLE001
                # The saved login is fine — this is a network/IP rate-limit, not a login problem.
                # Do NOT tell the user to log in again (that's what was confusing him).
                self._fail(
                    "Instagram is temporarily unreachable — Instagram has rate-limited this "
                    "network's IP. Your login is fine and saved; it clears on its own in a few "
                    "hours, or connect through a different network (a phone hotspot) once.")
            self._cl = cl
            self._logged_in = True
            self._self_id = str(getattr(info, "pk", None) or cl.user_id)
            logger.info("instagram connected via saved session as @%s", cl.username)
            return cl

    def status(self) -> dict:
        if not self.enabled:
            return {"connected": False, "reason": "no credentials"}
        try:
            self._client()
            return {"connected": True, "username": self.username}
        except InstagramError as e:
            return {"connected": False, "reason": str(e)}

    # ---- reads ------------------------------------------------------------ #
    def unread_dms(self, limit: int = 10) -> list[IGThread]:
        """Who DM'd me — unread direct threads + pending (message requests)."""
        cl = self._client()
        self._throttle()
        out: list[IGThread] = []
        try:
            threads = cl.direct_threads(amount=limit, selected_filter="unread")
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't read DMs: {e}") from e
        for t in threads:
            out.append(self._thread_summary(t, unread=True))
        # message requests from non-followers
        try:
            for t in cl.direct_pending_inbox(amount=limit):
                out.append(self._thread_summary(t, unread=True))
        except Exception:  # noqa: BLE001
            pass
        return out[:limit]

    def recent_dms(self, limit: int = 12) -> list[IGThread]:
        cl = self._client()
        self._throttle()
        threads = cl.direct_threads(amount=limit)
        return [self._thread_summary(t, unread=getattr(t, "read_state", 0) == 1) for t in threads][:limit]

    @staticmethod
    def _thread_summary(t, unread: bool) -> IGThread:
        users = getattr(t, "users", []) or []
        title = getattr(t, "thread_title", "") or ""
        is_group = getattr(t, "is_group", None)
        if is_group is None:
            is_group = len(users) > 1
        if is_group:
            # A group chat — show the GROUP, not a single member.
            username = title or "group"
            full = title or "Group chat"
        else:
            u = users[0] if users else None
            username = getattr(u, "username", "") if u else ""
            full = getattr(u, "full_name", "") if u else ""
        msgs = getattr(t, "messages", []) or []
        last = msgs[0] if msgs else None
        text = getattr(last, "text", "") or ""
        if not text and last is not None:
            text = f"[{getattr(last, 'item_type', 'media')}]"
        ts = 0.0
        if last is not None and getattr(last, "timestamp", None):
            try:
                ts = last.timestamp.timestamp()
            except Exception:  # noqa: BLE001
                ts = 0.0
        return IGThread(thread_id=str(getattr(t, "id", "") or getattr(t, "pk", "")),
                        username=username, full_name=full, text=text, ts=ts, unread=unread,
                        is_group=bool(is_group), title=title, members=len(users))

    def story_tray(self, limit: int = 15) -> list[str]:
        """Who (among people I follow) currently has a story up."""
        cl = self._client()
        self._throttle()
        names: list[str] = []
        try:
            tray = cl.get_reels_tray_feed()
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't read the story tray: {e}") from e
        items = tray.get("tray", tray) if isinstance(tray, dict) else tray
        for it in (items or []):
            user = it.get("user") if isinstance(it, dict) else getattr(it, "user", None)
            uname = (user.get("username") if isinstance(user, dict)
                     else getattr(user, "username", "")) if user else ""
            if uname and uname != self.username and uname not in names:
                names.append(uname)
            if len(names) >= limit:
                break
        return names

    def my_latest_post_stats(self, with_likers: bool = False) -> dict:
        """Likes + comments on my most recent post (optionally the list of who liked it)."""
        cl = self._client()
        self._throttle()
        medias = cl.user_medias(self._self_id or cl.user_id, amount=1)
        if not medias:
            return {"exists": False}
        m = medias[0]
        comments = []
        try:
            for c in cl.media_comments(m.pk, amount=8):
                comments.append({"user": getattr(c.user, "username", ""), "text": c.text})
        except Exception:  # noqa: BLE001
            pass
        likers = []
        if with_likers:
            try:
                likers = [getattr(u, "username", "") for u in cl.media_likers(m.pk)][:20]
            except Exception:  # noqa: BLE001
                pass
        return {"exists": True, "like_count": int(getattr(m, "like_count", 0) or 0),
                "comment_count": int(getattr(m, "comment_count", 0) or 0),
                "caption": (getattr(m, "caption_text", "") or "")[:120],
                "comments": comments, "likers": likers, "taken_at": _ts(m)}

    def my_latest_story_stats(self) -> dict:
        """Viewers + reactions on my most recent active story."""
        cl = self._client()
        self._throttle()
        try:
            stories = cl.user_stories(self._self_id or cl.user_id)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't read your stories: {e}") from e
        if not stories:
            return {"active": False}
        s = stories[0]
        viewers = []
        viewer_count = 0
        try:
            vs = cl.story_viewers(s.pk)
            viewer_count = len(vs)
            viewers = [getattr(v, "username", "") for v in vs[:10]]
        except Exception:  # noqa: BLE001
            viewer_count = int(getattr(s, "viewer_count", 0) or 0)
        return {"active": True, "viewer_count": viewer_count, "viewers": viewers,
                "taken_at": _ts(s)}

    def account_overview(self) -> dict:
        cl = self._client()
        self._throttle()
        info = cl.account_info()
        return {"username": info.username, "followers": int(getattr(info, "follower_count", 0) or 0),
                "following": int(getattr(info, "following_count", 0) or 0),
                "media": int(getattr(info, "media_count", 0) or 0)}

    # ---- writes ----------------------------------------------------------- #
    _IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
    _VID_EXT = {".mp4", ".mov", ".m4v"}

    def post_media(self, path: str, caption: str = "") -> str:
        """Publish a feed post (photo or video) from a local file. Returns its shortcode/url."""
        cl = self._client()
        import os
        if not os.path.isfile(path):
            raise InstagramError(f"I can't find a file at '{path}'.")
        ext = os.path.splitext(path)[1].lower()
        self._throttle()
        try:
            if ext in self._IMG_EXT:
                media = cl.photo_upload(path, caption)
            elif ext in self._VID_EXT:
                media = cl.video_upload(path, caption)
            else:
                raise InstagramError(f"Unsupported file type '{ext}' — use a jpg/png or mp4.")
        except InstagramError:
            raise
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't publish the post: {e}") from e
        code = getattr(media, "code", "")
        return f"https://instagram.com/p/{code}" if code else "posted"

    def add_story(self, path: str) -> str:
        """Upload a photo/video to my story from a local file."""
        cl = self._client()
        import os
        if not os.path.isfile(path):
            raise InstagramError(f"I can't find a file at '{path}'.")
        ext = os.path.splitext(path)[1].lower()
        self._throttle()
        try:
            if ext in self._IMG_EXT:
                cl.photo_upload_to_story(path)
            elif ext in self._VID_EXT:
                cl.video_upload_to_story(path)
            else:
                raise InstagramError(f"Unsupported file type '{ext}' — use a jpg/png or mp4.")
        except InstagramError:
            raise
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't add the story: {e}") from e
        return "added to your story"

    def send_dm(self, username: str, text: str) -> str:
        cl = self._client()
        username = self.resolve_user(username)      # name OR username -> real username
        self._throttle()
        try:
            uid = self._user_id(cl, username)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"No Instagram user @{username}: {e}") from e
        try:
            cl.direct_send(text, user_ids=[uid])
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't send the DM: {e}") from e
        return username

    def resolve_user(self, query: str) -> str:
        """Turn whatever the boss said into a real @username — works whether he gives the
        username OR the person's display name. Order: exact username -> a person he already
        DMs (match by name/username) -> Instagram search. Raises if nobody matches."""
        cl = self._client()
        q = (query or "").lstrip("@").strip()
        ql = q.lower()
        if not q:
            raise InstagramError("Who on Instagram?")
        if ql in self._resolve_cache:                      # already figured this name out
            return self._resolve_cache[ql]

        def _hit(username: str) -> str:
            self._resolve_cache[ql] = username
            return username

        # 1) already a valid username?
        try:
            self._user_id(cl, q)
            return _hit(q)
        except Exception:  # noqa: BLE001
            pass
        # 2) someone he already has a DM thread with (match full name or username)
        try:
            for t in cl.direct_threads(amount=20):
                for u in (getattr(t, "users", []) or []):
                    uname = (getattr(u, "username", "") or "")
                    fname = (getattr(u, "full_name", "") or "")
                    if uname.lower() == ql or ql in fname.lower():
                        return _hit(uname)
        except Exception:  # noqa: BLE001
            pass
        # 3) search Instagram by name/username
        try:
            results = cl.search_users(q) or []
        except Exception:  # noqa: BLE001
            results = []
        for u in results:                                  # prefer an exact display-name hit
            if (getattr(u, "full_name", "") or "").lower() == ql:
                return _hit(u.username)
        if results:
            return _hit(results[0].username)
        raise InstagramError(f"I couldn't find anyone called '{query}' on Instagram.")

    def _thread_id_for(self, cl, username: str) -> str | None:
        """Find the DM thread id for a 1:1 with this username (resolves name->username first)."""
        username = self.resolve_user(username)
        uid = int(self._user_id(cl, username))
        try:
            res = cl.direct_thread_by_participants([uid])
            if isinstance(res, dict):
                t = res.get("thread") or res
                tid = (t.get("thread_id") or t.get("id")) if isinstance(t, dict) else getattr(t, "id", None)
            else:
                tid = getattr(res, "id", None) or getattr(res, "pk", None)
            if tid:
                return str(tid)
        except Exception:  # noqa: BLE001
            pass
        # fallback: scan recent threads for a 1:1 with this user
        for t in cl.direct_threads(amount=20):
            if getattr(t, "is_group", False):
                continue
            for u in (getattr(t, "users", []) or []):
                if getattr(u, "username", "") == username:
                    return str(getattr(t, "id", "") or getattr(t, "pk", ""))
        return None

    def thread_messages(self, username: str, amount: int = 8) -> list[tuple[str, str]]:
        """Recent messages in the DM with `username`, chronological, as (who, text) where who
        is 'Me' or the username — for drafting a contextual reply."""
        cl = self._client()
        self._throttle()
        username = username.lstrip("@").strip()
        tid = self._thread_id_for(cl, username)
        if not tid:
            raise InstagramError(f"I don't see a DM conversation with @{username}.")
        try:
            msgs = cl.direct_messages(tid, amount=amount)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't read the DM thread: {e}") from e
        self_id = str(self._self_id or cl.user_id)
        out: list[tuple[str, str]] = []
        for m in reversed(msgs):    # direct_messages is newest-first -> chronological
            who = "Me" if str(getattr(m, "user_id", "")) == self_id else username
            text = getattr(m, "text", "") or f"[{getattr(m, 'item_type', 'media')}]"
            out.append((who, text))
        return out

    def delete_last_dm(self, username: str) -> str:
        """Unsend the most recent message I sent in the DM with `username`."""
        cl = self._client()
        self._throttle()
        username = username.lstrip("@").strip()
        tid = self._thread_id_for(cl, username)
        if not tid:
            raise InstagramError(f"I don't see a DM conversation with @{username}.")
        try:
            msgs = cl.direct_messages(tid, amount=15)     # newest first
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't read the DM thread: {e}") from e
        self_id = str(self._self_id or cl.user_id)
        mine = next((m for m in msgs if str(getattr(m, "user_id", "")) == self_id), None)
        if mine is None:
            raise InstagramError("No recent message from you in that DM to delete.")
        item_id = getattr(mine, "id", None) or getattr(mine, "pk", None)
        try:
            cl.direct_message_delete(tid, item_id)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't delete the DM: {e}") from e
        return username

    # ---- engagement (like / comment / follow / profile) ------------------- #
    def _resolve_media_pk(self, target: str):
        """`target` may be a post URL or an @username (their latest post)."""
        cl = self._client()
        target = (target or "").strip()
        if target.startswith("http"):
            return cl.media_pk_from_url(target)
        uname = self.resolve_user(target)          # name OR username -> username
        uid = self._user_id(cl, uname)
        medias = cl.user_medias(uid, amount=1)
        if not medias:
            raise InstagramError(f"@{uname} has no posts to act on.")
        return medias[0].pk

    def like_post(self, target: str, like: bool = True) -> str:
        cl = self._client()
        self._throttle()
        try:
            pk = self._resolve_media_pk(target)
            (cl.media_like if like else cl.media_unlike)(pk)
        except InstagramError:
            raise
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't {'like' if like else 'unlike'} that: {e}") from e
        return target

    def comment_post(self, target: str, text: str) -> str:
        cl = self._client()
        self._throttle()
        try:
            pk = self._resolve_media_pk(target)
            cl.media_comment(pk, text)
        except InstagramError:
            raise
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't comment: {e}") from e
        return target

    def follow(self, username: str, do_follow: bool = True) -> str:
        cl = self._client()
        username = self.resolve_user(username)
        self._throttle()
        try:
            uid = self._user_id(cl, username)
            (cl.user_follow if do_follow else cl.user_unfollow)(uid)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't {'follow' if do_follow else 'unfollow'} @{username}: {e}") from e
        return username

    def profile(self, username: str) -> dict:
        cl = self._client()
        username = self.resolve_user(username)
        self._throttle()
        try:
            u = cl.user_info_by_username(username)
        except Exception as e:  # noqa: BLE001
            raise InstagramError(f"Couldn't find @{username}: {e}") from e
        return {"username": u.username, "full_name": getattr(u, "full_name", ""),
                "followers": int(getattr(u, "follower_count", 0) or 0),
                "following": int(getattr(u, "following_count", 0) or 0),
                "posts": int(getattr(u, "media_count", 0) or 0),
                "bio": (getattr(u, "biography", "") or "")[:160],
                "private": bool(getattr(u, "is_private", False)),
                "verified": bool(getattr(u, "is_verified", False))}


def _ts(obj) -> float:
    t = getattr(obj, "taken_at", None)
    try:
        return t.timestamp() if t else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


_client: InstagramClient | None = None
_client_lock = threading.Lock()


def get_instagram() -> InstagramClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = InstagramClient()
    return _client
