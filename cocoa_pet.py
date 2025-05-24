#!/usr/bin/env python3
"""
Transparent desktop-pet for macOS built with Cocoa (PyObjC).
Tested on macOS 14 (Apple M-series).
Place your GIF in assets/doge/3d-doge-spins-like-coin-idle.gif
"""

import os, random, sys, time
import AppKit
from AppKit import (
    NSApplication, NSApp, NSWindow, NSImage, NSImageView,
    NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
    NSScreen, NSColor, NSEvent, NSMenu, NSMenuItem, NSPoint,
)
from Foundation import NSObject, NSTimer, NSOperationQueue
from PyObjCTools import AppHelper
import Quartz
from threading import Lock
import requests, json
import re


# ------------------------------------------------------------------ drag-helper
class DraggableImageView(NSImageView):
    """
    • single-click & drag   → move the window
    • double-click          → ask delegate to show chat
    • right-click           → ask delegate to show menu
    """
    delegate = None        # will be set from PetDelegate

    def acceptsFirstMouse_(self, event):  # so the first click is delivered
        return True

    # -------- left mouse -----------------------------------------------------
    def mouseDown_(self, event):
        # Debug
        # print("mouseDown clickCount:", event.clickCount())

        # If this is a real double-click the recogniser will call
        # delegate.doubleTap_().  Do NOT also call show_chat() here.
        if event.clickCount() == 2:
            return            # ← nothing else, no delegate call

        # single click – may become drag later
        self._drag_origin = event.locationInWindow()
        self._didDrag = False

    def mouseUp_(self, event):
        # Single-click without drag → show price bubble
        if not getattr(self, "_didDrag", False) and event.clickCount() == 1:
            if self.delegate:
                self.delegate.show_price()

    def mouseDragged_(self, event):
        win = self.window()
        current = event.locationInWindow()
        dx = current.x - self._drag_origin.x
        dy = current.y - self._drag_origin.y
        win.setFrameOrigin_(NSPoint(win.frame().origin.x + dx,
                                    win.frame().origin.y + dy))

        # mark as drag so mouseUp won't treat as simple click
        self._didDrag = True

        # inform delegate so bubbles follow
        if self.delegate:
            self.delegate.reposition_bubbles()

    # -------- right mouse ----------------------------------------------------
    def rightMouseDown_(self, event):
        if self.delegate:
            self.delegate.pop_menu_at_event(event, self)


# ---------------------------------------------------------------- chat bubble
class ChatBubble(NSWindow):
    """Simple speech bubble window."""

    def initWithParent_message_(self, parent_win, message):  # noqa: N802
        """Objective-C style initialiser used from both Python and ObjC blocks."""

        w, h = 220, 100
        origin = parent_win.frame().origin
        x = origin.x + (parent_win.frame().size.width - w) / 2
        y = origin.y + parent_win.frame().size.height + 10
        rect = ((x, y), (w, h))

        self = NSWindow.initWithContentRect_styleMask_backing_defer_(
            self,
            rect,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None

        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setLevel_(AppKit.NSFloatingWindowLevel)

        # rounded yellow background using layer
        self.contentView().setWantsLayer_(True)
        bg_layer = self.contentView().layer()
        bg_layer.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 0.75, 0.95).CGColor())
        bg_layer.setCornerRadius_(12.0)

        # label
        tf = AppKit.NSTextField.alloc().initWithFrame_(((10, 30), (w - 20, h - 40)))
        tf.setStringValue_(message)
        tf.setBordered_(False)
        tf.setEditable_(False)
        tf.setSelectable_(False)
        tf.setBackgroundColor_(NSColor.clearColor())
        tf.setAlignment_(AppKit.NSCenterTextAlignment)
        tf.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
        self.contentView().addSubview_(tf)

        self.orderFront_(None)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            4.0, self, "closeBubble:", None, False
        )

        # keep reference to parent window for repositioning
        self.parent_win = parent_win
        self.size = (w, h)
        self.owner = None   # PetDelegate will set this after creation

        # prevent Cocoa from freeing the NSWindow immediately upon close
        self.setReleasedWhenClosed_(False)

        return self

    # Allow a borderless window to accept key events
    def canBecomeKeyWindow(self):  # noqa: N802
        return True

    def closeBubble_(self, _):
        # notify owner (PetDelegate) so it can drop the reference
        if self.owner is not None:
            try:
                self.owner.remove_bubble(self)
            except Exception:
                pass
        self.close()
        # when a chat bubble disappears switch back to idle animation
        if self.owner is not None:
            try:
                self.owner.set_animation("idle")
                # also show live price after chat ends
                self.owner.show_price()
            except Exception:
                pass


# ------------------------------------------------------ chat input bubble
class InputBubble(NSWindow):
    """A non-modal bubble with a text field and a Send button."""

    def initWithParent_sendCallback_(self, parent_win, send_callback):  # noqa: N802
        """Objective-C style designated initializer."""
        self = NSWindow.initWithContentRect_styleMask_backing_defer_(
            self,
            ((0, 0), (260, 120)),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None

        self.send_callback = send_callback

        # size and position relative to parent
        w, h = 260, 120
        origin = parent_win.frame().origin
        x = origin.x + (parent_win.frame().size.width - w) / 2
        y = origin.y + parent_win.frame().size.height + 10
        self.setFrame_display_( ((x, y), (w, h)), False)

        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setLevel_(AppKit.NSFloatingWindowLevel)

        # rounded yellow background via layer properties
        self.contentView().setWantsLayer_(True)
        bg_layer = self.contentView().layer()
        bg_layer.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 0.75, 0.95).CGColor())
        bg_layer.setCornerRadius_(12.0)

        # text field
        self.text_field = AppKit.NSTextField.alloc().initWithFrame_(((15, 60), (w - 30, 24)))
        self.text_field.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        self.text_field.setBezeled_(True)
        self.text_field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        self.text_field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        self.text_field.setTarget_(self)
        self.text_field.setAction_("send:")
        self.contentView().addSubview_(self.text_field)

        # send button
        btn = AppKit.NSButton.alloc().initWithFrame_(((w/2 - 40, 20), (80, 28)))
        btn.setTitle_("Send")
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_("send:")
        self.contentView().addSubview_(btn)

        self.orderFront_(None)
        self.makeKeyAndOrderFront_(None)
        self.text_field.becomeFirstResponder()

        # store for reposition
        self.parent_win = parent_win
        self.size = (w, h)

        # avoid auto-free on close; we manage lifetime from Python side
        self.setReleasedWhenClosed_(False)

        return self

    def send_(self, sender):  # noqa: N802  (ObjC selector)
        text = self.text_field.stringValue().strip()
        if text:
            self.send_callback(text)
        # inform owner to drop reference before window is deallocated
        if hasattr(self, "owner") and self.owner is not None:
            self.owner._input_bubble = None
        self.close()

    # ----- InputBubble class-level methods ----------------------------------
    # allow the border-less window to accept key events/focus
    def canBecomeKeyWindow(self):  # noqa: N802
        return True

    def canBecomeMainWindow(self):  # noqa: N802
        return True


# ----------------------------------------------------------- main application
class PetDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        gif_path = os.path.join(
            os.path.dirname(__file__),
            "assets",
            "doge",
            "3d-doge-spins-like-coin-idle.gif",
        )
        if not os.path.exists(gif_path):
            AppKit.NSRunAlertPanel(
                "GIF not found",
                f"Could not find GIF at:\n{gif_path}",
                "Quit",
                None,
                None,
            )
            NSApp().terminate_(None)
            return

        # create transparent border-less window
        size = (128, 128)
        screen = NSScreen.mainScreen().frame()
        origin = (screen.size.width / 2 - size[0] / 2, screen.size.height / 2 - size[1] / 2)
        rect = (origin, size)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setIgnoresMouseEvents_(False)
        self.window.setHasShadow_(False)

        # load GIF frames ourselves so we can animate with a timer
        src = Quartz.CGImageSourceCreateWithURL(AppKit.NSURL.fileURLWithPath_(gif_path), None)
        frame_cnt = Quartz.CGImageSourceGetCount(src)
        self.frames = []
        for i in range(frame_cnt):
            cgimg = Quartz.CGImageSourceCreateImageAtIndex(src, i, None)
            if cgimg is None:
                continue  # skip broken frame to avoid crashes
            nsimg = NSImage.alloc().initWithCGImage_size_(cgimg, size)
            if nsimg is not None:
                self.frames.append(nsimg)

        if not self.frames:
            return  # failed to load any frames, bail

        self.frame_idx = 0

        self.img_view = DraggableImageView.alloc().initWithFrame_(((0, 0), size))
        self.img_view.setImage_(self.frames[0])
        self.window.setContentView_(self.img_view)
        self.img_view.delegate = self      # <- give view a back-pointer

        # add system double-click recogniser (more reliable on trackpads)
        recogniser = AppKit.NSClickGestureRecognizer.alloc().initWithTarget_action_(
            self, "doubleTap:"
        )
        recogniser.setNumberOfClicksRequired_(2)
        self.img_view.addGestureRecognizer_(recogniser)

        # drive animation (≈ 8 fps)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.06, self, "nextFrame:", None, True
        )

        # build right-click menu once
        menu = NSMenu.alloc().initWithTitle_("pet_menu")
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Exit", "terminate:", ""
        )
        menu.addItem_(quit_item)
        self.menu = menu

        self.window.makeKeyAndOrderFront_(None)
        print("🐕  Pet started.  Drag = left-click, Chat = double-click, Menu = right-click")

        # state flag: only one GPT request at a time
        self._chat_busy = False

        # one per class – defined at top level
        self._chat_lock = Lock()

        # keep strong refs to open bubbles so they do not dealloc early
        self._bubbles = []

        # --- simple chat history -----------------------------------------
        # stored as list of {"role": "user"|"assistant", "content": str}
        self._chat_history = []
        self._max_history = 12   # keep the last N messages (system prompt not counted)

        # HTTP session for web search
        self._http = requests.Session()

        # cache for CoinGecko price (update at most once per hour)
        self._price_cache = {"ts": 0, "snippet": ""}

        # first price bubble shortly after launch
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, "showPriceTimer:", None, False
        )

        # periodic bubble every 10 minutes while idle
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            600.0, self, "showPriceTimer:", None, True
        )

    # ------------ event handlers ------------------------------------------------
    def show_chat(self):
        """Prompt user, then call GPT in background."""

        # -------- ensure we have an OpenAI key (ask once per launch) -----
        if not os.getenv("OPENAI_API_KEY"):
            if not self._prompt_openai_key():
                return  # user cancelled

        # ---- atomic guard: only continue if we can acquire the lock
        if not self._chat_lock.acquire(blocking=False):
            return         # somebody else is already creating / showing chat

        print("show_chat invoked")

        def respond(user_msg: str):
            self.set_animation("thinking")

            # add user message to history
            self._chat_history.append({"role": "user", "content": user_msg})
            # trim if too long
            if len(self._chat_history) > self._max_history:
                self._chat_history = self._chat_history[-self._max_history:]

            import threading
            threading.Thread(
                target=self.ask_gpt_and_respond,
                args=(user_msg,),
                daemon=True,
            ).start()

        self._input_bubble = InputBubble.alloc(
        ).initWithParent_sendCallback_(self.window, respond)
        print("InputBubble created")

        # track for reposition
        self._input_bubble.owner = self

    def ask_gpt_and_respond(self, user_msg: str):
        """Runs in a background thread."""
        import openai, os, re
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            # fallback: try to read from ~/.openai_api_key (just the key string)
            try:
                key_path = os.path.expanduser("~/.openai_api_key")
                if os.path.exists(key_path):
                    with open(key_path, "r", encoding="utf-8") as fh:
                        openai.api_key = fh.read().strip()
            except Exception:
                pass

        # optional: live web snippet via DuckDuckGo Instant Answer API
        snippet = ""
        try:
            resp = self._http.get(
                "https://api.duckduckgo.com",
                params={"q": user_msg, "format": "json", "no_html": 1, "t": "desktop-doge"},
                timeout=5,
            )
            data = resp.json()
            snippet = data.get("AbstractText") or data.get("Heading") or ""
            if snippet:
                snippet = snippet[:700]  # keep prompt small
        except Exception:
            snippet = ""

        # If the user explicitly asks about dogecoin price, fetch live price
        price_snippet = ""
        price_intent = False
        try:
            # trigger on any mention of dogecoin / doge coin and finance-related words
            if re.search(r"(doge\s*coin|dogecoin)", user_msg, re.I):
                # basic intent detection for price/market questions
                if re.search(r"\b(price|worth|value|market|doing|performance|up|down|trend)\b", user_msg, re.I):
                    price_intent = True
            if price_intent:
                import time
                now = time.time()
                # use cached value if <1h old
                if now - self._price_cache["ts"] < 3600 and self._price_cache["snippet"]:
                    price_snippet = self._price_cache["snippet"]
                else:
                    resp_p = self._http.get(
                        "https://api.coingecko.com/api/v3/coins/markets",
                        params={"vs_currency": "usd", "ids": "dogecoin"},
                        timeout=5,
                    )
                    data_p = resp_p.json()
                    if isinstance(data_p, list) and data_p:
                        coin = data_p[0]
                        price = coin.get("current_price")
                        change = coin.get("price_change_percentage_24h")
                        if price is not None:
                            if change is not None:
                                price_snippet = (
                                    f"Dogecoin live price: ${price:.4f} USD (24h change: {change:+.2f}%). "
                                    "Source: CoinGecko."
                                )
                            else:
                                price_snippet = f"Dogecoin live price: ${price:.4f} USD (CoinGecko)."
                            # update cache
                            self._price_cache = {"ts": now, "snippet": price_snippet}
        except Exception:
            price_snippet = ""

        # prepare message list: system prompt + recent history + optional web context
        system_prompt = {
            "role": "system",
            "content": (
                "You are Desktop Doge. Answer very briefly (≤25 words) and append one mood tag "
                "<mood:HAPPY|LAUGH|WOW|SAD|THINK>."
            ),
        }

        messages = [system_prompt]

        if price_snippet:
            instruction = (
                "You MUST quote that number." if price_intent
                else "Use the number only if the user asked for price/market info."
            )
            messages.append({
                "role": "user",
                "content": f"(live-data) {price_snippet}\n{instruction}",
            })

        if snippet:
            messages.append({
                "role": "user",
                "content": f"(web) {snippet}",
            })

        messages += self._chat_history[-self._max_history:]

        chat = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )
        answer = chat.choices[0].message.content.strip()

        m = re.search(r"<mood:([A-Z]+)>", answer)
        mood = m.group(1) if m else "HAPPY"
        answer = re.sub(r"<mood:[A-Z]+>", "", answer).strip()

        print("answer:", answer, "mood:", mood)

        # append assistant reply to history and trim
        self._chat_history.append({"role": "assistant", "content": answer})
        if len(self._chat_history) > self._max_history:
            self._chat_history = self._chat_history[-self._max_history:]

        # hop back to main thread to update UI safely
        import objc

        def _finish():
            bubble = ChatBubble.alloc().initWithParent_message_(
                self.window, answer
            )
            bubble.owner = self
            self._bubbles.append(bubble)

            self.set_animation(mood.lower())

            # finished – unlock so the next double-click is accepted
            self._chat_lock.release()

        with objc.autorelease_pool():
            NSOperationQueue.mainQueue().addOperationWithBlock_(_finish)

        # input bubble has closed itself; drop reference so we don't
        # attempt to reposition a deallocated window.
        self._input_bubble = None

    # -------- animation timer callback --------------------------------------
    def nextFrame_(self, _):
        self.frame_idx = (self.frame_idx + 1) % len(self.frames)
        self.img_view.setImage_(self.frames[self.frame_idx])

        # keep chat bubbles in sync with pet position if any
        self.reposition_bubbles()

    # --- bubble housekeeping -------------------------------------------------
    def remove_bubble(self, bubble):
        """Called by ChatBubble when it auto-closes"""
        try:
            self._bubbles.remove(bubble)
        finally:
            # Cocoa has already closed the window – avoid touching it again
            bubble.owner = None

    def reposition_bubbles(self):
        """Place all bubbles relative to current pet window position."""
        if not self._bubbles and not getattr(self, "_input_bubble", None):
            return

        pet_frame = self.window.frame()

        # update chat bubbles
        for b in list(self._bubbles):
            try:
                if b is None or not b.isVisible():
                    raise RuntimeError
                w, h = b.size
                x = pet_frame.origin.x + (pet_frame.size.width - w) / 2
                y = pet_frame.origin.y + pet_frame.size.height + 10
                b.setFrameOrigin_((x, y))
            except Exception:
                # underlying window was closed – drop from list safely
                self.remove_bubble(b)

        # update input bubble if alive
        ib = getattr(self, "_input_bubble", None)
        if ib is not None:
            w, h = ib.size
            x = pet_frame.origin.x + (pet_frame.size.width - w) / 2
            y = pet_frame.origin.y + pet_frame.size.height + 10
            ib.setFrameOrigin_((x, y))

    # -------- called from DraggableImageView ---------------------------------
    def pop_menu_at_event(self, event, view):
        AppKit.NSMenu.popUpContextMenu_withEvent_forView_(self.menu, event, view)

    # called by gesture recogniser
    def doubleTap_(self, sender):  # noqa: N802
        self.show_chat()

    # ------------------------ animation helper -----------------------------

    ANIMATIONS = {
        "idle":      "3d-doge-spins-like-coin-idle.gif",
        "happy":     "doge-dances-full-body.gif",
        "laugh":     "doge-laughs.gif",
        "wow":       "doge-appears-disappears.gif",
        "sad":       "doge-shaking-head-in-circles.gif",
        "thinking":  "doge-in-waves-loading-glitching.gif",
    }

    def set_animation(self, key: str):
        """Load a new GIF and restart the timer."""
        fname = self.ANIMATIONS.get(key, self.ANIMATIONS["idle"])
        path = os.path.join(os.path.dirname(__file__), "assets", "doge", fname)
        if not os.path.exists(path):
            return                                # keep current frames

        import Quartz
        src = Quartz.CGImageSourceCreateWithURL(AppKit.NSURL.fileURLWithPath_(path), None)
        frame_cnt = Quartz.CGImageSourceGetCount(src)
        frames = []
        for i in range(frame_cnt):
            cgimg = Quartz.CGImageSourceCreateImageAtIndex(src, i, None)
            if cgimg is None:
                continue  # skip broken frame to avoid crashes
            nsimg = NSImage.alloc().initWithCGImage_size_(cgimg, (128, 128))
            if nsimg is not None:
                frames.append(nsimg)

        if not frames:
            return  # failed to load, keep previous animation

        self.frames = frames
        self.frame_idx = 0
        self.img_view.setImage_(self.frames[0])

        # Schedule automatic return to idle for transient moods
        if key not in ("idle", "thinking"):
            self._revert_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                6.0,  # seconds before switching back
                self,
                "revertToIdle:",
                None,
                False,
            )

    # ObjC selector called by the timer above
    def revertToIdle_(self, _):  # noqa: N802
        self.set_animation("idle")

    # ---------------- quick price popup on single-click ------------------
    def _ensure_price_snippet(self, force_fetch: bool = False):
        """Return cached price snippet or fetch from CoinGecko."""
        import time
        now = time.time()
        if not force_fetch and (now - self._price_cache["ts"] < 3600):
            return self._price_cache["snippet"]

        try:
            resp = self._http.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "ids": "dogecoin"},
                timeout=5,
            )
            data = resp.json()
            if isinstance(data, list) and data:
                coin = data[0]
                price = coin.get("current_price")
                change = coin.get("price_change_percentage_24h")
                if price is not None:
                    snippet = (
                        f"Dogecoin price: ${price:.4f} USD (24h {change:+.2f}%)."
                        if change is not None else f"Dogecoin price: ${price:.4f} USD."
                    )
                    self._price_cache = {"ts": now, "snippet": snippet}
                    return snippet
        except Exception:
            pass
        return ""

    def show_price(self):
        """Display current Dogecoin price in a bubble."""
        # avoid duplicate price bubbles
        for b in self._bubbles:
            if isinstance(b, PriceBubble) and b.isVisible():
                return

        snippet = self._ensure_price_snippet(force_fetch=False) or "Price unavailable."

        price_bubble = PriceBubble.alloc().initWithParent_message_(self.window, snippet)
        price_bubble.owner = self
        self._bubbles.append(price_bubble)

    # Cocoa timer selector
    def showPriceTimer_(self, _):  # noqa: N802
        self.show_price()

    # ------------------------------------------------------------------
    # Ask the user for their OpenAI API key the first time it is needed
    # Stores the key in ~/.openai_api_key for future launches.
    # Returns True if a key is now available, False if the user cancelled.
    def _prompt_openai_key(self) -> bool:
        key_path = os.path.expanduser("~/.openai_api_key")

        # If a key has already been saved on disk, load and export it
        if os.path.exists(key_path):
            try:
                with open(key_path, "r", encoding="utf-8") as fh:
                    api_key = fh.read().strip()
                    if api_key:
                        os.environ["OPENAI_API_KEY"] = api_key
                        return True
            except Exception:
                pass  # fall through to prompt

        # --- Build a secure modal asking for the key ------------------
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("OpenAI API key required")
        alert.setInformativeText_(
            "Enter your OpenAI API key.\n" +
            "It will be saved to ~/.openai_api_key for future launches.")

        # Accessory view: secure text field
        field = AppKit.NSSecureTextField.alloc().initWithFrame_(((0, 0), (300, 24)))
        alert.setAccessoryView_(field)

        alert.addButtonWithTitle_("Save")   # index 1000
        alert.addButtonWithTitle_("Cancel") # index 1001

        resp = alert.runModal()
        if resp != AppKit.NSAlertFirstButtonReturn:  # user cancelled
            return False

        api_key = field.stringValue().strip()
        if not api_key:
            return False

        # Save key to disk with 0600 permissions
        try:
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(api_key + "\n")
            import stat, os as _os
            _os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass  # non-fatal – we still set env var below

        os.environ["OPENAI_API_KEY"] = api_key
        return True


# ------------------------------------------------------ price bubble (smaller)
class PriceBubble(NSWindow):
    """Compact info bubble shown on single-click for price."""

    def initWithParent_message_(self, parent_win, message):  # noqa: N802
        w, h = 200, 60
        origin = parent_win.frame().origin
        # place to the right of the pet
        x = origin.x + parent_win.frame().size.width + 10
        y = origin.y + (parent_win.frame().size.height - h) / 2
        rect = ((x, y), (w, h))

        self = NSWindow.initWithContentRect_styleMask_backing_defer_(
            self,
            rect,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None

        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setLevel_(AppKit.NSFloatingWindowLevel)

        # bluish rounded background
        self.contentView().setWantsLayer_(True)
        bg_layer = self.contentView().layer()
        bg_layer.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.8, 0.9, 1.0, 0.95).CGColor())
        bg_layer.setCornerRadius_(10.0)

        # label (smaller font)
        tf = AppKit.NSTextField.alloc().initWithFrame_(((8, 18), (w - 16, h - 30)))
        tf.setStringValue_(message)
        tf.setBordered_(False)
        tf.setEditable_(False)
        tf.setSelectable_(False)
        tf.setBackgroundColor_(NSColor.clearColor())
        tf.setAlignment_(AppKit.NSCenterTextAlignment)
        tf.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        self.contentView().addSubview_(tf)

        self.orderFront_(None)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.5, self, "closeBubble:", None, False
        )

        self.size = (w, h)
        self.owner = None
        self.setReleasedWhenClosed_(False)

        return self

    def canBecomeKeyWindow(self):  # noqa: N802
        return True

    def closeBubble_(self, _):
        if self.owner is not None:
            try:
                self.owner.remove_bubble(self)
            except Exception:
                pass
        self.close()


# -------------------------------------------------------------- run the app
if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    delegate = PetDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()