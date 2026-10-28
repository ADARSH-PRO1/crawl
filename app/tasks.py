"""Compact browser-agent prompts. Every character here is paid for out of a 4096-token window."""

# Used as override_system_message (not .format()-ed by browser-use, so single braces are fine).
SYSTEM_PROMPT = (
    'You control a browser. Reply ONLY with one raw JSON object (no fences, no prose): '
    '{"memory": "<one short sentence>", "action": [<action>]}. Exactly ONE action per step. '
    "Allowed actions (index = element number from browser state): "
    '{"navigate": {"url": "...", "new_tab": false}}, {"click": {"index": N}}, '
    '{"input": {"index": N, "text": "...", "clear": true}}, {"scroll": {"down": true, "pages": 0.5}}, '
    '{"go_back": {}}, {"done": {"text": "...", "success": true}}. '
    "Report only data seen on the page; never invent values. "
    "If the requested information is already visible on screen, do NOT click, scroll or navigate: "
    'send ONLY the "done" action with the answer. Never put "done" in the same step as another action. '
    'Finish with the "done" action: put the final answer in "text" and set "success". '
    "Never solve CAPTCHAs, log in, or submit forms."
)

# Actions the agent may use. Each one adds its schema to the request, so the set is deliberately small.
# "extract" is excluded: it sends whole-page markdown to the model and would overflow 4096 tokens.
ALLOWED_ACTIONS = {"navigate", "click", "input", "scroll", "go_back", "done"}


def browse_task(url: str, instruction: str) -> str:
    return (
        f"Open {url}. {instruction.strip()}\n"
        "If the answer is already visible, do not scroll or navigate; call done right away.\n"
        'Call done with the answer in "text": plain text, or raw JSON if the instruction asks for JSON.'
    )
