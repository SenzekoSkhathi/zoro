"""Vision-based computer-use loop — Zoro's fallback "hands".

Used only when no deterministic tool fits (e.g. clicking inside an arbitrary
app's UI). Drives Anthropic's computer-use tool: screenshot -> Claude picks
an action -> we execute it with pyautogui -> screenshot again -> repeat until
the task is done or a safety limit trips.

Slow by nature (a vision round-trip per step), so the agent prefers the
deterministic desktop tools and only reaches here when it must.
"""

from __future__ import annotations

import base64
import io
import time

import anthropic

import zoro_episodes
import zoro_safety

MODEL = "claude-sonnet-4-5"
BETA = "computer-use-2025-01-24"
MAX_STEPS = 18
# Keep the screen we send to Claude within ~1280px wide — accuracy drops on
# larger images and it costs more tokens.
TARGET_MAX_W = 1280


def _screen_size() -> tuple[int, int]:
    import pyautogui

    w, h = pyautogui.size()
    return int(w), int(h)


def _scaled_dims(nw: int, nh: int) -> tuple[int, int]:
    if nw <= TARGET_MAX_W:
        return nw, nh
    scale = TARGET_MAX_W / float(nw)
    return TARGET_MAX_W, max(1, int(nh * scale))


def _screenshot_b64(tw: int, th: int) -> str:
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.size != (tw, th):
        img = img.resize((tw, th), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# xdotool-style key names (what the computer tool emits) -> pyautogui
_KEYMAP = {
    "return": "enter", "enter": "enter", "escape": "esc", "esc": "esc",
    "tab": "tab", "backspace": "backspace", "delete": "delete",
    "space": "space", "page_down": "pagedown", "page_up": "pageup",
    "next": "pagedown", "prior": "pageup", "home": "home", "end": "end",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "super": "win", "win": "win", "cmd": "win", "meta": "win",
}


def _press_key(combo: str) -> None:
    import pyautogui

    parts = [p.strip().lower() for p in combo.replace(" ", "+").split("+") if p]
    keys = [_KEYMAP.get(p, p) for p in parts]
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


def _type_unicode_safe(text: str) -> None:
    """Type `text`, surviving non-ASCII characters.

    pyautogui.write maps each character to a key press, so anything
    outside ASCII (accented letters, emoji, curly quotes) is silently
    dropped. For ASCII keep the natural per-key typing; otherwise paste
    via the clipboard so the text actually lands intact.
    """
    import pyautogui

    if not text:
        return
    if text.isascii():
        pyautogui.write(text, interval=0.012)
        return
    import zoro_tools

    zoro_tools.clipboard_set(text)
    pyautogui.hotkey("ctrl", "v")


def _do_action(action: str, inp: dict, sx: float, sy: float) -> None:
    """Execute one computer action. sx/sy scale Claude's (target-space)
    coordinates back to real screen pixels."""
    import pyautogui

    def _xy():
        c = inp.get("coordinate") or inp.get("start_coordinate")
        return (int(c[0] * sx), int(c[1] * sy)) if c else (None, None)

    if action in ("mouse_move", "left_click", "right_click", "middle_click",
                  "double_click", "triple_click"):
        x, y = _xy()
        if action == "mouse_move":
            pyautogui.moveTo(x, y)
        elif action == "left_click":
            pyautogui.click(x, y)
        elif action == "right_click":
            pyautogui.click(x, y, button="right")
        elif action == "middle_click":
            pyautogui.click(x, y, button="middle")
        elif action == "double_click":
            pyautogui.doubleClick(x, y)
        elif action == "triple_click":
            pyautogui.click(x, y, clicks=3, interval=0.05)
    elif action == "left_click_drag":
        c = inp.get("start_coordinate")
        d = inp.get("coordinate")
        if c:
            pyautogui.moveTo(int(c[0] * sx), int(c[1] * sy))
        if d:
            pyautogui.dragTo(int(d[0] * sx), int(d[1] * sy), duration=0.3)
    elif action == "left_mouse_down":
        pyautogui.mouseDown()
    elif action == "left_mouse_up":
        pyautogui.mouseUp()
    elif action == "type":
        _type_unicode_safe(inp.get("text", ""))
    elif action == "key":
        _press_key(inp.get("text", ""))
    elif action == "hold_key":
        import pyautogui as pg

        key = _KEYMAP.get(inp.get("text", "").lower(), inp.get("text", ""))
        pg.keyDown(key)
        time.sleep(float(inp.get("duration", 1)))
        pg.keyUp(key)
    elif action == "scroll":
        amt = int(inp.get("scroll_amount", 3)) * 100
        direction = inp.get("scroll_direction", "down")
        if direction in ("up", "down"):
            pyautogui.scroll(amt if direction == "up" else -amt)
        else:
            pyautogui.hscroll(amt if direction == "right" else -amt)
    elif action == "wait":
        time.sleep(min(float(inp.get("duration", 1)), 5))
    # screenshot / cursor_position are handled by the caller


def run_task(task: str, api_key: str) -> str:
    """Drive the computer to accomplish `task`. Returns a short spoken
    summary of the outcome."""
    t0 = time.monotonic()
    outcome = "success"
    steps_used = 0
    final = ""
    err: str | None = None
    try:
        final, steps_used = _run_task_inner(task, api_key)
        outcome, _ = _classify_run(final)
        return final
    except BaseException as e:  # noqa: BLE001 - re-raise after logging
        outcome = "error"
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            zoro_episodes.log_episode(
                kind="computer_use",
                tool="control_computer",
                task=task[:300],
                args={"task": task},
                outcome=outcome,
                latency_ms=latency_ms,
                steps=steps_used or None,
                result_summary=final[:300] if final else None,
                error=err,
            )
        except Exception:  # noqa: BLE001 - telemetry never raises
            pass


def _classify_run(final: str) -> tuple[str, int]:
    """Pick outcome + step-count signal from the run's spoken summary."""
    if not final:
        return "error", 0
    low = final.lower()
    if low.startswith("i stopped"):
        return "aborted", 0
    if low.startswith("i worked on it but hit my step limit"):
        return "aborted", MAX_STEPS
    if low.startswith("my computer") or "failed" in low[:60]:
        return "error", 0
    return "success", 0


def _run_task_inner(task: str, api_key: str) -> tuple[str, int]:
    nw, nh = _screen_size()
    tw, th = _scaled_dims(nw, nh)
    sx, sy = nw / float(tw), nh / float(th)

    client = anthropic.Anthropic(api_key=api_key)
    tools = [{
        "type": "computer_20250124",
        "name": "computer",
        "display_width_px": tw,
        "display_height_px": th,
        "display_number": 1,
    }]
    system = (
        "You are controlling a Windows desktop to accomplish the user's "
        "task. Work in small steps, take a screenshot to verify state when "
        "unsure, and stop as soon as the task is complete. Be efficient — "
        "the user is waiting and each step is slow."
    )
    messages: list[dict] = [{"role": "user", "content": task}]

    step_no = 0
    for step_no in range(1, MAX_STEPS + 1):
        if zoro_safety.is_stood_down():
            return "I stopped — you asked me to stand down.", step_no - 1
        try:
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system,
                tools=tools,
                betas=[BETA],
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001
            return f"My computer-control attempt failed: {e}", step_no

        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            texts = [b.text for b in resp.content if b.type == "text"]
            return (" ".join(texts).strip() or "Done."), step_no

        results = []
        for tu in tool_uses:
            inp = tu.input or {}
            action = inp.get("action", "")
            try:
                if action in ("screenshot", "cursor_position", ""):
                    pass
                else:
                    _do_action(action, inp, sx, sy)
                    time.sleep(0.4)  # let the UI settle
            except Exception as e:  # noqa: BLE001 - report back, let it recover
                results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": f"action error: {e}", "is_error": True,
                })
                continue
            # every step returns a fresh screenshot so Claude sees the result
            shot = _screenshot_b64(tw, th)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": [{
                    "type": "image",
                    "source": {"type": "base64",
                               "media_type": "image/png", "data": shot},
                }],
            })

        messages.append({"role": "user", "content": results})
        # Keep only the 3 most recent screenshots to bound context/cost.
        _trim_old_images(messages, keep=3)

    return ("I worked on it but hit my step limit before finishing — "
            "tell me how you'd like me to continue."), MAX_STEPS


def _trim_old_images(messages: list[dict], keep: int) -> None:
    """Replace all but the last `keep` screenshot images with a short note,
    so the conversation doesn't balloon over a long task."""
    imgs = []
    for mi, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for bi, b in enumerate(c):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                rc = b.get("content")
                if isinstance(rc, list) and rc and isinstance(rc[0], dict) \
                        and rc[0].get("type") == "image":
                    imgs.append((mi, bi))
    for mi, bi in imgs[:-keep] if len(imgs) > keep else []:
        messages[mi]["content"][bi]["content"] = "[earlier screenshot omitted]"
