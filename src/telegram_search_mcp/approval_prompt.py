"""Out-of-band owner confirmation for a prepared Telegram document send."""

from __future__ import annotations

import subprocess


_SCRIPT = '''on run argv
    set previewText to item 1 of argv
    try
        set choice to display dialog previewText with title "Unofficial Telegram MCP — approve send" buttons {"Cancel", "Send"} default button "Cancel" cancel button "Cancel" giving up after 120
        if (gave up of choice) is false and (button returned of choice) is "Send" then
            return "APPROVED"
        end if
    on error
        return "DENIED"
    end try
    return "DENIED"
end run'''


def confirm_approved_send(
    *, recipient_title: str, recipient: int, display_name: str,
    size_bytes: int, sha256: str, caption: str,
    kind: str = "document", text: str = "",
    duration_seconds: int | None = None, source_sha256: str | None = None,
    source_display_name: str | None = None, reply_preview: dict | None = None,
    reply_artifact_preview: dict | None = None,
) -> bool:
    """Ask the signed-in macOS user; failure or timeout means no send."""
    if reply_artifact_preview is not None:
        from .reply_artifact_drafts import ReplyArtifactDraftPreview
        try:
            validated=ReplyArtifactDraftPreview.model_validate(reply_artifact_preview)
        except (ValueError,TypeError):return False
        draft=validated.draft;target=validated.reply_target;anchor=target.anchor
        voice=(f"Source: {draft.source_display_name}\nSource SHA-256: {draft.source_sha256}\n"
            f"Converted: {draft.converted}\nDuration: {draft.duration_seconds} seconds\nWaveform (base64): {draft.waveform_base64}\n"
            if draft.kind=='voice_note' else '')
        preview=(f"Send this {draft.kind} reply to {draft.recipient_title} (chat {draft.recipient})?\n"
            f"Account: {draft.account_id}\nReply anchor: chat {anchor.chat_id}, message {anchor.message_id}\n\n"
            f"{target.text}\n\nTarget sanitized: {target.sanitized}; truncated: {target.truncated}\n"
            f"Source SHA-256: {target.source_sha256}\nPreview SHA-256: {validated.preview_sha256}\n\n"
            f"{voice}File: {draft.display_name}\nMIME: {draft.mime_type}\nBytes: {draft.size_bytes}\nSHA-256: {draft.sha256}\n"
            f"Caption:\n{draft.caption}\n\nUnofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.")
    elif reply_preview is not None:
        target=reply_preview['reply_target']; draft=reply_preview['draft']; anchor=target['anchor']
        preview=(f"Send this reply to {recipient_title} (chat {recipient})?\n"
            f"Account: {draft['account_id']}\nReply anchor: chat {anchor['chat_id']}, message {anchor['message_id']}\n\n"
            f"{target['text']}\n\nTarget sanitized: {target['sanitized']}; truncated: {target['truncated']}\n"
            f"Source SHA-256: {target['source_sha256']}\nPreview SHA-256: {reply_preview['preview_sha256']}\n\n"
            f"Reply text:\n{text}\n\nText SHA-256: {sha256}\n\n"
            "Unofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.")
    elif kind == "text":
        preview = (f"Send this text to {recipient_title} (chat {recipient})?\n\n"
                   f"{text}\n\nSHA-256: {sha256}\n\n"
                   "Unofficial Telegram MCP will make one send attempt.")
    else:
        voice_detail = (f"Source: {source_display_name}\nSource SHA-256: {source_sha256}\n"
                        f"Duration: {duration_seconds} seconds\nConverted to OGG/Opus mono\n"
                        if kind == "voice_note" else "")
        preview = (
            f"Send this {kind} to {recipient_title} (chat {recipient})?\n\n"
            f"{voice_detail}"
            f"File: {display_name}\nBytes: {size_bytes}\nSHA-256: {sha256}\n"
            f"Caption: {caption or '(none)'}\n\n"
            "Unofficial Telegram MCP will make one upload/send attempt."
        )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _SCRIPT, "--", preview],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=125, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"APPROVED"
