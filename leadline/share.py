"""Share a story: a plain-text message on the clipboard, laid out for
iMessage and readable anywhere else plain text goes (spec §3 attribution:
the publisher's headline and link travel with the summary)."""
import os
import shutil
import subprocess
import sys


def _line(text):
    """One line of text: feed and model output can carry stray newlines."""
    return " ".join((text or "").split())


def share_text(card):
    """The straight headline and its BLUF bullets, then the publisher's
    original headline with its source, then the link.

    Plain text only, because Messages shows Markdown as literal asterisks. The
    link sits alone on the last line: Messages sends a trailing link as its
    own bubble with a preview card, and Slack, Mail and most chat apps unfurl
    or linkify it the same way. A story with no summary yet shares just the
    original headline and link."""
    parts = []
    headline = _line(card.get("straight_headline"))
    if card.get("processed") and headline:
        parts.append(headline)
        bullets = [_line(b) for b in card.get("bluf_bullets") or []]
        if any(bullets):
            parts.append("\n".join(f"• {b}" for b in bullets if b))
    source = _line(card.get("source_name"))
    original = _line(card.get("original_headline"))
    footer = []
    if original:
        label = f"Original headline ({source})" if source else "Original headline"
        footer.append(f"{label}: “{original}”")
    elif source:
        footer.append(source)
    url = (card.get("canonical_url") or "").strip()
    if url:
        footer.append(url)
    if footer:
        parts.append("\n".join(footer))
    return "\n\n".join(parts)


def copy_to_clipboard(text):
    """Put plain text on the system clipboard. True if it got there.

    Done here rather than in the page: the web view's clipboard API isn't
    reliably allowed in an app window, while the system's own tools are."""
    if sys.platform == "darwin":
        # pbcopy decodes its input with the locale, which an app launched from
        # Finder may not have; without UTF-8 the bullets and quotes garble
        cmd, data = ["/usr/bin/pbcopy"], text.encode("utf-8")
        env = {**os.environ, "LC_ALL": "en_US.UTF-8"}
    elif sys.platform == "win32":
        # clip.exe reads UTF-16 when the input starts with its byte-order mark;
        # named in full so a clip.exe beside the app is never the one run
        cmd = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "clip.exe")]
        env = None
        data = b"\xff\xfe" + text.replace("\n", "\r\n").encode("utf-16-le")
    else:
        cmd = next((c for c in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                                ["xsel", "--clipboard", "--input"])
                    if shutil.which(c[0])), None)
        if not cmd:
            return False
        data, env = text.encode("utf-8"), None
    try:
        # output to DEVNULL: xclip and wl-copy stay behind to serve the
        # clipboard, and a pipe to them would never close
        subprocess.run(cmd, input=data, env=env, check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError):
        return False
