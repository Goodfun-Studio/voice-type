"""Entry point: wires the hook, recorder, HUD, tray and engine into one process.

Flow (identical semantics to the macOS app):
    double-tap right Ctrl  -> start recording (HUD level meter appears)
    double-tap again       -> stop, transcribe, paste into the focused window
    Esc while live         -> throw the take away: no transcribe, no paste
                              (a late result from an abandoned take is discarded)

Run it either as a package  (python -m voicetype_win)  or directly
(python voicetype_win/__main__.py) — the bootstrap below makes both work.
"""
from __future__ import annotations

import os
import sys
import threading
import time

# Make `import voicetype_win...` work even when this file is run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from voicetype_win import __version__, winsys  # noqa: E402
from voicetype_win import config as cfg  # noqa: E402
from voicetype_win.config import Config, log_line  # noqa: E402
from voicetype_win.engine_client import EngineClient  # noqa: E402
from voicetype_win.hotkey import LowLevelKeyboardHook  # noqa: E402
from voicetype_win.hud import HUD  # noqa: E402
from voicetype_win.inserter import insert_text, write_history  # noqa: E402
from voicetype_win.recorder import Recorder  # noqa: E402

MINIMUM_SECONDS = 0.3


def _beep(freq: int, ms: int) -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound
        winsound.Beep(freq, ms)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Controller — the state machine, mirroring AppDelegate in main.swift
# ---------------------------------------------------------------------------
class Controller:
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"

    def __init__(self, config: Config, hud, engine: EngineClient):
        self.config = config
        self.hud = hud
        self.engine = engine
        self.recorder = Recorder()
        self.recorder.on_level = self.hud.push_level

        self.state = self.IDLE
        self.generation = 0
        self.cancelled_generation = -1
        self.target_hwnd: int | None = None
        self.started_at = 0.0
        self.engine_ready = False
        self._lock = threading.RLock()

    # ---- hook-facing predicates --------------------------------------------
    def is_active(self) -> bool:
        """True while a take is live (recording or transcribing). The hook
        swallows Esc only when this is True."""
        return self.state in (self.RECORDING, self.TRANSCRIBING)

    # ---- the one button -----------------------------------------------------
    def toggle(self) -> None:
        with self._lock:
            state = self.state
        if state == self.IDLE:
            self.start_recording()
        elif state == self.RECORDING:
            self.finish_recording()
        else:
            self.cancel()

    def start_recording(self) -> None:
        with self._lock:
            if self.state != self.IDLE:
                return
            if not self.engine_ready:
                self.hud.toast("引擎还没起来")
                return
            self.target_hwnd = winsys.get_foreground_window() if winsys.HAS_WIN32 else None
            self.state = self.RECORDING
            self.generation += 1
            self.started_at = time.monotonic()
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            log_line(f"record failed: {exc}")
            with self._lock:
                self.state = self.IDLE
            self.hud.toast("录音启动失败：麦克风？")
            return
        self.hud.show_listening()
        if self.config.sound:
            _beep(880, 60)
        log_line(f"take {self.generation} started")

    def finish_recording(self) -> None:
        with self._lock:
            if self.state != self.RECORDING:
                return
            self.state = self.TRANSCRIBING
            generation = self.generation
        wav = self.recorder.stop()
        seconds = time.monotonic() - self.started_at
        if self.config.sound:
            _beep(523, 60)

        if wav is None:
            with self._lock:
                self.state = self.IDLE
            return
        if seconds < MINIMUM_SECONDS:
            log_line(f"take too short ({seconds:.2f}s), discarded")
            _discard(wav)
            self.hud.toast("太短了，已丢弃", 0.9)
            with self._lock:
                self.state = self.IDLE
            return

        self.hud.show_transcribing()
        threading.Thread(target=self._transcribe_worker, args=(wav, generation),
                         name="vtw-transcribe", daemon=True).start()

    def _transcribe_worker(self, wav, generation: int) -> None:
        text, elapsed_ms, failed = "", 0, False
        try:
            result = self.engine.transcribe(str(wav), self.config.language)
            text = result.get("text") or ""
            elapsed_ms = int(result.get("elapsedMs") or 0)
            reason = result.get("reason")
            if not text and reason:
                # Normal, not an error: silence / hallucination / no-words /
                # prompt-echo. Never paste an empty string.
                log_line(f"take {generation}: engine returned no text ({reason})")
        except Exception as exc:  # noqa: BLE001
            failed = True
            log_line(f"transcribe failed: {exc}")

        with self._lock:
            cancelled = self.cancelled_generation == generation
            self.state = self.IDLE

        if cancelled:
            log_line(f"take {generation} discarded after cancel")
        elif failed:
            self.hud.toast("转写失败")
        else:
            self.deliver(text, elapsed_ms)
        _discard(wav)

    def cancel(self) -> None:
        with self._lock:
            if self.state == self.RECORDING:
                self.cancelled_generation = self.generation
                self.state = self.IDLE
                stop = True
            elif self.state == self.TRANSCRIBING:
                self.cancelled_generation = self.generation
                self.state = self.IDLE
                stop = False
            else:
                return
        if stop:
            _discard(self.recorder.stop())
        self.hud.toast("已取消")
        if self.config.sound:
            _beep(220, 80)
        log_line("cancelled")

    def deliver(self, text: str, elapsed_ms: int) -> None:
        if not text:
            self.hud.toast("没听到内容")
            return
        write_history(text)
        ok = insert_text(text, self.target_hwnd)
        if ok:
            self.hud.toast(text[:40])
        else:
            self.hud.toast("已复制到剪贴板（粘贴未发出）")
        log_line(f"done {elapsed_ms}ms: {text}")

    def set_language(self, code: str) -> None:
        with self._lock:
            self.config.language = code
            self.config.save()
        self.engine.set_language(code)
        log_line(f"language -> {code}")

    def restart_engine(self) -> None:
        with self._lock:
            self.engine_ready = False
        self.engine.shutdown()
        threading.Thread(target=self._boot_engine, daemon=True).start()

    def _boot_engine(self) -> None:
        ready = self.engine.ensure_running()
        with self._lock:
            self.engine_ready = ready
        self.hud.toast("就绪 · 双击右 Ctrl 说话" if ready else "引擎启动失败，见日志", 2.0)


def _discard(path) -> None:
    try:
        if path:
            os.remove(path)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Headless HUD fallback — used when tkinter is unavailable
# ---------------------------------------------------------------------------
class NullHUD:
    def __init__(self):
        self._done = threading.Event()

    def show_listening(self) -> None: ...
    def push_level(self, rms) -> None: ...
    def show_transcribing(self) -> None: ...
    def toast(self, text, seconds: float = 1.5) -> None:
        log_line(f"toast: {text}")
    def hide(self) -> None: ...
    def run(self) -> None:
        self._done.wait()
    def destroy(self) -> None:
        self._done.set()


# ---------------------------------------------------------------------------
# Tray icon (pystray + Pillow, both optional)
# ---------------------------------------------------------------------------
class Tray:
    def __init__(self, controller: Controller, on_quit, on_restart):
        self.controller = controller
        self.on_quit = on_quit
        self.on_restart = on_restart
        self._icon = None

    def _image(self):
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((2, 2, size - 2, size - 2), fill=(30, 30, 34, 255))
        draw.rounded_rectangle((24, 12, 40, 38), radius=8, fill=(255, 255, 255, 255))
        draw.line((32, 38, 32, 48), fill=(255, 255, 255, 255), width=4)
        draw.line((22, 48, 42, 48), fill=(255, 255, 255, 255), width=4)
        return img

    def start(self) -> bool:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except Exception as exc:  # noqa: BLE001
            log_line(f"pystray unavailable ({exc}); running without a tray icon")
            return False

        controller = self.controller

        def lang_item(code, label):
            return MenuItem(label, lambda: controller.set_language(code),
                            checked=lambda item, c=code: controller.config.language == c,
                            radio=True)

        menu = Menu(
            MenuItem("开始 / 结束（双击右 Ctrl）", lambda: controller.toggle(), default=True),
            MenuItem("取消当前录音 / 转写（Esc）", lambda: controller.cancel()),
            Menu.SEPARATOR,
            MenuItem("输入语言", Menu(
                lang_item("zh", "中文"),
                lang_item("en", "English"),
                lang_item("auto", "自动识别"),
            )),
            Menu.SEPARATOR,
            MenuItem("重启转写引擎", lambda: self.on_restart()),
            MenuItem("打开日志", lambda: _open(cfg.LOG_PATH)),
            MenuItem("打开转写历史", lambda: _open(cfg.HISTORY_DIR)),
            Menu.SEPARATOR,
            MenuItem("退出", lambda: self.on_quit()),
        )
        try:
            self._icon = pystray.Icon("VoiceType", self._image(), "Voice Type", menu)
            threading.Thread(target=self._icon.run, name="vtw-tray", daemon=True).start()
            return True
        except Exception as exc:  # noqa: BLE001
            log_line(f"tray icon failed: {exc}")
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass


def _open(path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import webbrowser
            webbrowser.open(str(path))
    except Exception as exc:  # noqa: BLE001
        log_line(f"open {path} failed: {exc}")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class App:
    def __init__(self, config: Config, use_tray: bool = True):
        self.config = config
        self.use_tray = use_tray
        self.engine = EngineClient(config)
        self.controller: Controller | None = None
        self.hook: LowLevelKeyboardHook | None = None
        self.tray: Tray | None = None
        self.hud = None
        self._shutdown_done = False

    def _build_hud(self):
        try:
            return HUD()
        except Exception as exc:  # noqa: BLE001
            log_line(f"HUD unavailable ({exc}); running without the level meter")
            return NullHUD()

    def run(self) -> int:
        cfg.ensure_dirs()
        self.hud = self._build_hud()
        self.controller = Controller(self.config, self.hud, self.engine)

        self.hook = LowLevelKeyboardHook(
            on_double_ctrl=self.controller.toggle,
            is_active=self.controller.is_active,
            on_escape=self.controller.cancel,
        )
        try:
            self.hook.start()
        except Exception as exc:  # noqa: BLE001
            log_line(f"keyboard hook failed: {exc}")
            self.hud.toast("全局热键不可用（仅 Windows）")

        threading.Thread(target=self.controller._boot_engine, name="vtw-engine",
                         daemon=True).start()

        if self.use_tray:
            self.tray = Tray(self.controller, on_quit=self.quit,
                             on_restart=self.controller.restart_engine)
            self.tray.start()

        log_line(f"VoiceType (windows) {__version__} started; platform={sys.platform}")
        try:
            self.hud.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    def quit(self) -> None:
        self._shutdown()
        try:
            self.hud.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        log_line("shutting down")
        if self.hook is not None:
            try:
                self.hook.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.tray is not None:
            self.tray.stop()
        self.engine.shutdown()


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    print(f"Voice Type (Windows client) --selftest  v{__version__}")
    print(f"  platform: {sys.platform}   python: {sys.version.split()[0]}")
    print(f"  support dir: {cfg.SUPPORT}")
    print()

    failures = 0

    def check(label: str, ok: bool, detail: str = "", required: bool = True) -> None:
        nonlocal failures
        mark = "✓" if ok else "✗"
        line = f"  {mark} {label}"
        if detail:
            line += f" — {detail}"
        print(line)
        if not ok and required:
            failures += 1

    def skip(label: str, detail: str) -> None:
        print(f"  – {label}: skipped — {detail}")

    # python
    check("python >= 3.9", sys.version_info >= (3, 9),
          f"found {sys.version.split()[0]}")

    # third-party deps
    def try_import(name: str):
        try:
            __import__(name)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    ok, why = try_import("sounddevice")
    check("sounddevice (录音)", ok, why)
    ok, why = try_import("numpy")
    check("numpy (电平 RMS)", ok, why)

    ok, why = try_import("pystray")
    check("pystray (托盘，可选)", ok, why or "未安装 → 退化成无声运行", required=False)
    ok, why = try_import("PIL")
    check("Pillow (托盘图标，可选)", ok, why or "未安装 → 无托盘图标", required=False)

    ok, why = try_import("tkinter")
    check("tkinter (悬浮电平条)", ok, why, required=winsys.IS_WINDOWS)

    # win32 layer
    if winsys.IS_WINDOWS:
        check("win32 (user32/kernel32)", winsys.HAS_WIN32, winsys.IMPORT_ERROR or "ok")
        if winsys.HAS_WIN32:
            size = winsys.input_struct_size()
            expect = 40 if sys.maxsize > 2**32 else 28
            check("SendInput INPUT size", size == expect,
                  f"sizeof(INPUT)={size} (expect {expect})")
    else:
        skip("win32 键盘钩子 / SendInput / WS_EX_NOACTIVATE",
             "仅 Windows 可用（当前 macOS，预期缺失）")

    # engine path
    engine_path = cfg.Config.load(create=False).engine
    check("engine.py 路径", bool(engine_path) and os.path.exists(engine_path),
          engine_path or "未配置")

    # config dir writable
    try:
        cfg.ensure_dirs()
        check("配置/历史目录可写", True, str(cfg.SUPPORT))
    except Exception as exc:  # noqa: BLE001
        check("配置/历史目录可写", False, f"{type(exc).__name__}: {exc}")

    # network stack
    try:
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cli = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        cli.close()
        srv.close()
        check("本地 TCP 网络栈", True, "loopback bind/connect ok")
    except Exception as exc:  # noqa: BLE001
        check("本地 TCP 网络栈", False, f"{type(exc).__name__}: {exc}")

    # engine reachability / port occupancy
    configured = cfg.Config.load(create=False)
    host, port = configured.endpoint()
    client = EngineClient(configured)
    if client.ping(timeout=0.8):
        check(f"引擎 {host}:{port}", True, "ping ok — 引擎已在运行")
        try:
            info = client._request({"cmd": "ping"}, 1.0)
            print(f"      engine: {info.get('model')}  warm={info.get('warm')}")
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            import socket
            probe = socket.create_connection((host, port), timeout=0.5)
            probe.close()
            check(f"引擎 {host}:{port}", False,
                  "端口被占用但 ping 无响应（可能被别的程序占用）", required=False)
        except Exception:
            check(f"引擎 {host}:{port}", False,
                  "未运行（启动客户端时会自动拉起，属正常）", required=False)

    endpoint = cfg.read_endpoint()
    if endpoint:
        check("engine.json 端点发现文件", True, str(endpoint))
    else:
        check("engine.json 端点发现文件", False,
              f"未发现（引擎尚未写 {cfg.ENDPOINT_PATH}），将用配置默认端口",
              required=False)

    print()
    if failures:
        print(f"selftest 结束：{failures} 项必需检查未通过。")
    else:
        print("selftest 结束：必需检查全部通过。")
        if not winsys.IS_WINDOWS:
            print("提醒：当前不是 Windows，键盘钩子/HUD/粘贴 均未在此环境验证。")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="voicetype_win",
                                     description="Windows Voice Type client")
    parser.add_argument("--selftest", action="store_true",
                        help="检查依赖/网络/引擎路径后退出（任何平台都可运行）")
    parser.add_argument("--no-tray", action="store_true", help="不启动托盘图标")
    parser.add_argument("--language", choices=["zh", "en", "auto"],
                        help="启动时覆盖识别语言")
    parser.add_argument("--version", action="version", version=f"voicetype_win {__version__}")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    config = Config.load(create=True)
    if args.language:
        config.language = args.language
        config.save()

    if not winsys.IS_WINDOWS:
        print("注意：此客户端只在 Windows 上可用（当前检测到非 Windows 平台）。",
              file=sys.stderr)
        print("      可以运行 `python -m voicetype_win --selftest` 做依赖自检。",
              file=sys.stderr)
        return 2

    app = App(config, use_tray=not args.no_tray)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
