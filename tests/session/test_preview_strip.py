from core.session.preview_strip import strip_persisted_output_previews


def test_strip_persisted_output_previews_rewrites_only_persisted_outputs() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "toolu_1", "content": "<persisted-output>\nOutput too large. Full output saved to: /tmp/toolu_1.txt\n\nPreview (first 20 bytes):\nabcdef\n</persisted-output>"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "keep me"},
    ]
    replacements = {"toolu_1": messages[0]["content"]}

    rewritten, changed = strip_persisted_output_previews(messages, replacements)

    assert changed is True
    assert rewritten[0]["content"] == "[Tool result offloaded to: /tmp/toolu_1.txt]"
    assert replacements["toolu_1"] == "[Tool result offloaded to: /tmp/toolu_1.txt]"
    assert rewritten[1]["content"] == "keep me"
