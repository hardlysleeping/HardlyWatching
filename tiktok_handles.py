"""
Turn a pasted TikTok "Following accounts" page dump into handles.

The pasted text is Ctrl+A / Ctrl+C from the page, so it is full of chrome
(nav, counts, footer). The reliable signal is the avatar lines:

    Avatar for exampleuser

    Example Display Name

    exampleuser

Each account produces exactly one "Avatar for <handle>" line, and the handle
is the account's real username. Everything else in the dump is noise.

Also accepts plain handles, @handles, and profile URLs, so a hand-typed list
or an installolder-style paste works too.
"""

import re

# TikTok handles may contain letters, digits, dot, underscore and hyphen.
# Hyphens were missing at first, so any 'first-last' handle was dropped from
# the dump without a word.
AVATAR_RE = re.compile(r'^\s*Avatar for ([A-Za-z0-9._-]{1,30})\s*$')
HANDLE_RE = re.compile(r'^[A-Za-z0-9._-]{1,30}$')
URL_RE = re.compile(
    r'^(?:https?://)?(?:www\.|m\.)?(?:tiktok|instagram)\.com/@?([A-Za-z0-9._-]{1,30})/?',
    re.I,
)

# Words that look like handles but are page chrome.
#
# Deliberately does NOT include 'tiktok' or 'instagram': both are real accounts
# that people follow, and an explicit @handle or a profile URL is unambiguous
# enough that filtering them out would silently drop them from an import.
# They are only rejected when they look like a bare word in a page dump, which
# the avatar path already avoids by starting below the "Following accounts"
# heading.
STOP = {
    'following', 'followers', 'friends', 'for', 'you',
    'shop', 'explore', 'live', 'videos', 'favorites', 'liked', 'latest',
    'popular', 'oldest', 'messages', 'activity', 'upload', 'profile', 'more',
    'avatar', 'search', 'home', 'about', 'help', 'terms', 'privacy', 'login',
}


def _clean(h: str) -> str:
    h = h.strip()
    if h.startswith('@'):
        h = h[1:]
    return h.rstrip('/')


def parse(text: str):
    """Return (handles, source, dropped).

    `source` says which path found the handles; `dropped` counts rows that
    looked like an account but could not be read, so a malformed paste is
    reported rather than quietly producing a shorter list.
    """
    handles = []
    seen = set()

    def add(h):
        h = _clean(h)
        if not h or h.lower() in STOP:
            return
        if not HANDLE_RE.match(h):
            return
        if h.lower() in seen:
            return
        seen.add(h.lower())
        handles.append(h)

    lines = text.splitlines()

    # Path 1: the avatar lines. Most reliable, present in a Ctrl+A dump.
    # Only look below the "Following accounts" heading: the nav also has
    # "Avatar for <self>" above it, which is the logged-in user, not a follow.
    start = 0
    for i, ln in enumerate(lines):
        if 'following accounts' in ln.lower():
            start = i + 1
            break

    avatar_hits = 0
    for ln in lines[start:]:
        m = AVATAR_RE.match(ln)
        if m:
            add(m.group(1))
            avatar_hits += 1
    if avatar_hits:
        # Count rows that announced an avatar but whose handle did not fit the
        # expected shape. A silent drop here would look like a smaller list
        # rather than an error, so the number is carried back for reporting.
        declared = sum(1 for ln in lines[start:] if ln.strip().lower().startswith('avatar for'))
        return handles, 'avatar', max(0, declared - avatar_hits)

    # Path 2: profile URLs AND bare handles mixed together.
    url_hits = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = URL_RE.match(s)
        if m:
            add(m.group(1))
            url_hits += 1
            continue
        # A line that is just a handle or @handle.
        tok = s.lstrip('@')
        if HANDLE_RE.match(tok):
            add(tok)
            url_hits += 1
    if url_hits:
        return handles, 'url', 0

    # Path 3: comma / space separated on one line.
    for ln in lines:
        for tok in re.split(r'[\s,]+', ln):
            if tok:
                add(tok)
    return handles, 'plain', 0


def to_urls(handles, platform='tiktok'):
    base = 'https://www.tiktok.com/@' if platform == 'tiktok' else 'https://www.instagram.com/'
    return [base + h for h in handles]


if __name__ == '__main__':
    import sys
    txt = sys.stdin.read()
    h, src = parse(txt)
    print(f'{len(h)} handles via {src}')
    print('\n'.join(to_urls(h)))

# The page itself states how many accounts are followed ("388" above
# "Following"), while the list only holds the rows that have been scrolled into
# view. Comparing the two catches a paste taken before scrolling to the bottom,
# which would otherwise look like a complete import of a much shorter list.
_TOTAL_RE = re.compile(
    r'^\s*(\d{1,6})\s*$', re.MULTILINE)


def stated_total(text: str) -> int:
    """The Following count printed on the page, or 0 if it is not there.

    Looks for a bare number on its own line followed, within a couple of lines,
    by the word "Following". Deliberately conservative: a wrong guess here would
    nag the user about a list that is actually fine.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        m = _TOTAL_RE.match(ln)
        if not m:
            continue
        window = " ".join(lines[i + 1:i + 4]).lower()
        if "following" in window:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return 0

