"""Clicking automation of the manual checklist ("> appium <flow> [KEY=VALUE …]" in checklists/mobile.md), Appium with
the UiAutomator2 driver on every API level (Maestro cannot drive Android 6: its adb client needs shell protocol v2).

Native screens (connect screen, system file picker, player) through UiAutomator, jellyfin-web through the WebView
context (chromedriver, DOM selectors). A flow raises FlowFailed with a short reason, progress lines go to log().

Needs the UiAutomator2 driver 5.x: its server (8.x) is the last one with minSdk 21, 6.x+ needs API 26
  appium driver install uiautomator2@5.0.7

Standalone, against a running emulator and Appium server (manual-test.py starts both):
  python3 appium_flows.py emulator-5590 setup
  python3 appium_flows.py emulator-5590 play CARD=1
"""
import contextlib
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

from appium import webdriver
from appium.options.common import AppiumOptions
from appium.webdriver.common.appiumby import AppiumBy as By
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, WebDriverException

APP_ID = "org.jellyfin.mobile.debug"
# 8922 for a second manual-test.py in parallel, see MANUAL_SERVER_PORT there
SERVER = f"https://10.0.2.2:{os.environ.get('MANUAL_SERVER_PORT', '8920')}"
# The worker's CA-only port of the test Caddy: custom CA, no client certificate (the CA-only steps)
CA_ONLY_SERVER = f"https://10.0.2.2:{os.environ.get('MANUAL_CA_ONLY_PORT', '8921')}"
ADB = str(Path(os.environ.get("ANDROID_HOME", Path.home() / "Android/Sdk")) / "platform-tools/adb")
WEBVIEW = f"WEBVIEW_{APP_ID}"
# web_wait attaches the WebView again after this many seconds without a truthy value
REATTACH_AFTER = 15
CHROMEDRIVER_DIR = Path(os.environ.get("MANUAL_CHROMEDRIVER_DIR", Path.home() / ".cache/appium-chromedriver"))
# Chrome 115+: Chrome for Testing, older: the legacy storage. Not Appium's chromedriver_autodownload, it renames from
# /tmp into the cache and fails across filesystems (EXDEV)
CHROMEDRIVER_CFT = ("https://googlechromelabs.github.io/chrome-for-testing/"
                    "latest-versions-per-milestone-with-downloads.json")
CHROMEDRIVER_LEGACY = "https://chromedriver.storage.googleapis.com"


class FlowFailed(Exception):
    """A flow step did not work, the message says which."""


def ensure_chromedriver(webview_version, log=print):
    """Chromedriver for the major version of the WebView (API 23: com.android.webview 88) in CHROMEDRIVER_DIR,
    Appium picks the matching one from there."""
    major = int(webview_version.split(".")[0])
    target = CHROMEDRIVER_DIR / f"chromedriver_{major}"
    if target.exists():
        return
    CHROMEDRIVER_DIR.mkdir(parents=True, exist_ok=True)
    if major >= 115:
        milestone = json.load(urllib.request.urlopen(CHROMEDRIVER_CFT, timeout=30))["milestones"][str(major)]
        version = milestone["version"]
        url = next(d["url"] for d in milestone["downloads"]["chromedriver"] if d["platform"] == "linux64")
        member = "chromedriver-linux64/chromedriver"
    else:
        version = urllib.request.urlopen(f"{CHROMEDRIVER_LEGACY}/LATEST_RELEASE_{major}", timeout=30).read().decode()
        url = f"{CHROMEDRIVER_LEGACY}/{version}/chromedriver_linux64.zip"
        member = "chromedriver"
    log(f"downloading chromedriver {version}")
    data = urllib.request.urlopen(url, timeout=120).read()
    target.write_bytes(zipfile.ZipFile(BytesIO(data)).read(member))
    target.chmod(0o755)


# Texts are compared case-insensitively everywhere: button texts are upper case on some Android versions ("TRUST",
# "CANCEL"), titles and labels differ in case between images. nocase() rewrites the comparisons of an XPath query,
# CaseInsensitiveRemote applies it to every XPath lookup (driver, elements, d.find, d.visible), ui_text() is the
# counterpart for UiSelector, Python-side checks compare lower().
TEXT_ATTRIBUTE = r"@text|@content-desc"
STRING_LITERAL = r"""'[^']*'|"[^"]*["]"""
TEXT_COMPARISON = re.compile(rf"(contains|starts-with)\(\s*({TEXT_ATTRIBUTE})\s*,\s*({STRING_LITERAL})\s*\)"
                             rf"|({TEXT_ATTRIBUTE})\s*(!?=)\s*({STRING_LITERAL})")
LOWER = "translate({}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"


def ascii_lower(text):
    """Lower case like LOWER (XPath translate() only maps A - Z)."""
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def nocase(xpath):
    """The XPath with @text / @content-desc compared case-insensitively to string literals (=, !=, contains(),
    starts-with())."""
    def rewrite(match):
        function, attribute, literal, attribute2, operator, literal2 = match.groups()
        if function:
            return f"{function}({LOWER.format(attribute)}, {ascii_lower(literal)})"
        return f"{LOWER.format(attribute2)}{operator}{ascii_lower(literal2)}"
    return TEXT_COMPARISON.sub(rewrite, xpath)


def ui_text(text, prefix=False):
    """UiSelector for an element with this text (or starting with it), case-insensitive. Punctuation is matched as a
    character class: the UiSelector string takes no backslash escapes."""
    assert not any(c in text for c in '"\\]^'), text
    pattern = "".join(c if c.isalnum() or c == " " else f"[{c}]" for c in text)
    return f'new UiSelector().textMatches("(?is){pattern}{".*" if prefix else ""}")'


class CaseInsensitiveRemote(webdriver.Remote):
    """Every XPath lookup goes through execute() (also those of elements): compares texts case-insensitively."""

    def execute(self, driver_command, params=None):
        if params and params.get("using") == By.XPATH:
            params = {**params, "value": nocase(params["value"])}
        return super().execute(driver_command, params)


class Device:
    """One Appium session on the emulator, created on first use. close() after anything that kills the app or the
    device (force-stop, reboot): the WebView connection of the session does not survive it."""

    def __init__(self, serial, appium_url, log=print):
        self.serial = serial
        self.appium_url = appium_url
        self.log = log
        self.driver = None
        self.notes = []  # for the report of the step, taken by the caller

    def open(self):
        if self.driver:
            return self.driver
        options = AppiumOptions()
        options.load_capabilities({
            "platformName": "Android",
            "appium:automationName": "UiAutomator2",
            "appium:udid": self.serial,
            # The app is installed and launched by manual-test.py, the session only attaches
            "appium:noReset": True,
            "appium:autoLaunch": False,
            # Skips the Appium Settings helper: on Android 6 the driver cannot parse "ps" (unlabelled state column)
            # and waits for it in vain
            "appium:skipDeviceInitialization": True,
            # No timeout: a by-eye run pauses for minutes between flows, after 600 s Appium ended the session and every
            # later command failed with "A session is either terminated or not started" (manual-test.py stops its
            # Appium server at the end, no session is left behind)
            "appium:newCommandTimeout": 0,
            "appium:uiautomator2ServerInstallTimeout": 120000,
            "appium:chromedriverExecutableDir": str(CHROMEDRIVER_DIR),
            # Own host ports per emulator: two Appium servers in parallel picked the same ones
            "appium:systemPort": 8200 + self.emulator_offset(),
            "appium:chromedriverPort": 9515 + self.emulator_offset(),
        })
        self.driver = CaseInsensitiveRemote(self.appium_url, options=options)
        return self.driver

    def reopen_if_lost(self):
        """Opens a new session when the current one is gone (ended by Appium, its server restarted)."""
        if not self.driver:
            return
        try:
            self.driver.current_context  # noqa: B018 cheap call that fails on a dead session
        except WebDriverException as error:
            self.log(f"Appium session lost ({type(error).__name__}), opening a new one")
            self.driver = None

    def emulator_offset(self):
        """0 for emulator-5554, 1 for 5556, …"""
        port = self.serial.rpartition("-")[2]
        return (int(port) - 5554) // 2 if port.isdigit() else 0

    @contextlib.contextmanager
    def no_idle_wait(self):
        """For the player: UiAutomator waits up to 10 s for an idle UI before every lookup, a playing video never gets
        idle, and the player controls are hidden again by then (seen on Android 6)."""
        self.native().update_settings({"waitForIdleTimeout": 0})
        try:
            yield
        finally:
            if self.driver:
                self.driver.update_settings({"waitForIdleTimeout": 10000})

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None

    def shell(self, cmd, timeout=30):
        """adb shell directly, Appium's mobile: shell needs --relaxed-security."""
        return subprocess.run([ADB, "-s", self.serial, "shell", cmd], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL).stdout.replace("\r", "")

    def screencap(self, timeout=15):
        """PNG of the screen. FlowFailed when it hangs: adb does not answer or the emulator's graphics are stuck."""
        try:
            return subprocess.run([ADB, "-s", self.serial, "exec-out", "screencap", "-p"], capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL).stdout
        except subprocess.TimeoutExpired:
            raise FlowFailed(f"screen capture hangs after {timeout}s, adb does not answer or the emulator's graphics "
                             "are stuck") from None

    # --- Native (UiAutomator) ----------------------------------------------------------------------------------------

    def native(self):
        driver = self.open()
        if driver.context != "NATIVE_APP":
            driver.switch_to.context("NATIVE_APP")
        return driver

    def find(self, xpath, timeout=10, what=None):
        deadline = time.time() + timeout
        while True:
            elements = self.native().find_elements(By.XPATH, xpath)
            if elements:
                return elements
            if dismiss_got_it(self):
                continue  # a notice covered the screen
            if time.time() > deadline:
                raise FlowFailed(f"not found after {timeout}s: {what or xpath}")
            time.sleep(0.5)

    def text(self, text, timeout=10, index=0, contains=False):
        xpath = (f"//*[contains(@text, '{text}')]" if contains
                 else f"//*[@text='{text}' or @content-desc='{text}']")
        elements = self.find(xpath, timeout, what=f'"{text}"')
        if len(elements) <= index:
            raise FlowFailed(f'"{text}" #{index} not found ({len(elements)} found)')
        return elements[index]

    def by_id(self, resource_id, timeout=10):
        return self.find(f"//*[@resource-id='{resource_id}']", timeout, what=resource_id)[0]

    def visible(self, xpath):
        return bool(self.native().find_elements(By.XPATH, xpath))

    def tap_point(self, x_fraction, y_fraction, double=False, within=None):
        """Taps at a fraction of the screen, or of the element found by the XPath "within"."""
        driver = self.native()
        area = self.find(within, what=within).pop(0).rect if within else dict(x=0, y=0, **driver.get_window_size())
        point = dict(x=int(area["x"] + area["width"] * x_fraction), y=int(area["y"] + area["height"] * y_fraction))
        driver.execute_script("mobile: doubleClickGesture" if double else "mobile: clickGesture", point)

    # --- jellyfin-web (WebView context) ------------------------------------------------------------------------------

    def web(self, timeout=30):
        driver = self.open()
        # Listing the contexts while attached to the WebView takes 20 s and more (chromedriver holds its DevTools
        # socket)
        if driver.context == WEBVIEW:
            return driver
        deadline = time.time() + timeout
        while WEBVIEW not in driver.contexts:
            if time.time() > deadline:
                raise FlowFailed(f"no WebView after {timeout:.0f}s (contexts: {', '.join(driver.contexts)})")
            time.sleep(1)
        try:
            driver.switch_to.context(WEBVIEW)
        except WebDriverException as error:
            # Appium names the WebView version it needs a chromedriver for: fetch it, attach again
            needed = re.search(r"automate Chrome '([\d.]+)'", error.msg or "")
            if not needed:
                raise
            ensure_chromedriver(needed.group(1), log=self.log)
            driver.switch_to.context(WEBVIEW)
        return driver

    def web_other_window(self, script):
        """The WebView can hold several pages (window handles) after the app replaced its page, chromedriver stays on
        the old one. Switches to the page where the JavaScript expression is truthy, returns its value, else None (and
        the page it was on stays current)."""
        try:
            current = self.driver.current_window_handle
            handles = self.driver.window_handles
            self.log(f"DIAG web_wait windows: {len(handles)}, stuck on {current} {self.driver.current_url!r} "
                     f"{self.driver.title!r}")  # TEMP
            for handle in handles:
                if handle == current:
                    continue
                self.driver.switch_to.window(handle)
                value = self.driver.execute_script(f"return {script}")
                self.log(f"DIAG web_wait other window {handle}: {self.driver.current_url!r} {self.driver.title!r} "
                         f"value={value!r}")  # TEMP
                if value:
                    return value
            self.driver.switch_to.window(current)
        except WebDriverException as e:
            self.log(f"DIAG web_wait other window: {type(e).__name__}: {str(e).splitlines()[0][:150]}")  # TEMP
        return None

    def web_wait(self, script, timeout, what):
        """Waits until the JavaScript expression is truthy, returns its value. Not truthy for REATTACH_AFTER seconds:
        the session is closed, the next web() call opens a new one and attaches to the WebView again. The chromedriver
        stays on the page of the WebView before the app replaced it: TLS_API_24 reimport, 54 checks of 60 s all null
        while the screen showed the home page. Only switching to NATIVE_APP and
        back did not help: Appium keeps its chromedriver of that WebView."""
        deadline = time.time() + timeout
        falsy_since = time.time()
        while True:
            try:
                value = self.web(timeout=max(1, deadline - time.time())).execute_script(f"return {script}")
            except WebDriverException as e:
                value = None  # page navigating
                self.log(f"DIAG web_wait {what}: {type(e).__name__}: {str(e).splitlines()[0][:150]}")  # TEMP
            else:
                self.log(f"DIAG web_wait {what}: value={value!r}")  # TEMP
            if value:
                return value
            if time.time() > deadline:
                raise FlowFailed(f"{what} not shown after {timeout}s")
            if time.time() - falsy_since >= REATTACH_AFTER:
                falsy_since = time.time()
                value = self.web_other_window(script)
                if value:
                    return value
                self.close()  # the next web() call opens a new session (the app keeps running)
            time.sleep(1)

    def web_click(self, selector, index=0, timeout=20):
        """Clicks the index-th visible element matching the CSS selector (from 0). A JavaScript click: the card
        buttons only show on hover, a WebDriver click would find them not interactable."""
        self.web_wait(f"[...document.querySelectorAll({selector!r})].filter(e => e.offsetParent).length > {index}",
                      timeout, f"{selector} #{index}")
        self.web().execute_script(
            f"[...document.querySelectorAll({selector!r})].filter(e => e.offsetParent)[{index}].click()")


# --- Shared steps ----------------------------------------------------------------------------------------------------

PLAYER = f"//*[@resource-id='{APP_ID}:id/player_view']"
POSITION = f"//*[@resource-id='{APP_ID}:id/exo_position']"
# The video itself: on Android 6 the player stays in portrait, the video is a band in the middle of the screen
VIDEO = f"//*[@resource-id='{APP_ID}:id/exo_content_frame']"
# Device storage root in the file picker, by its free space line ("651 MB free", "5.16 GB free")
STORAGE_ROOT = "//*[contains(@text, ' free')]"
CONNECT_ERROR = "//*[contains(@text, 'Connection cannot be established')]"
# Download location, created by push-certs.sh
DOWNLOAD_DIR = "/sdcard/Download/jellyfin-tls/downloads"
# Confirms the folder in the folder picker: "SELECT" (Android 6 - 9), "ALLOW ACCESS TO …" (10), "USE THIS FOLDER" (11+)
# Any class: not an android.widget.Button on Android 8 - 9
FOLDER_CONFIRM = "//*[@text='Select' or starts-with(@text, 'Allow access') or @text='Use this folder']"
PLAYING, PAUSED = 3, 2  # PlaybackState.STATE_*
KEYCODE_HOME = 3
# Seconds in the background before the app is killed, see kill_app
HOME_WAIT = 5
MY_MEDIA = ("[...document.querySelectorAll('.section0 .sectionTitle')]"
            ".some(e => e.offsetParent && e.textContent.trim().toLowerCase() === 'my media')")


# System document picker (com.android.documentsui, com.google.android.documentsui on Android 11+): the toolbar title is
# the current folder or root; the roots drawer is out while its list is shown; entries of the file list ("list" or
# "grid" on Android 6, "dir_list" later) apart from the title and the roots
PICKER = "//*[contains(@package, 'documentsui')]"
PICKER_TITLE = "//*[contains(@resource-id, ':id/toolbar')]//android.widget.TextView[@text!='']"
ROOTS_DRAWER = "//*[contains(@resource-id, ':id/roots_list')]"
# Folders of Download/jellyfin-tls/ (push-certs.sh)
TLS_FOLDERS = ("jellyfin-tls", "proxy", "fixtures", "client", "downloads")


def picker_entry(name):
    return (f"//*[contains(@resource-id, ':id/dir_list') or contains(@resource-id, ':id/list') or "
            f"contains(@resource-id, ':id/grid')]//*[@text='{name}']")


def picker_title(d):
    titles = d.native().find_elements(By.XPATH, PICKER_TITLE)
    return titles[0].get_attribute("text") if titles else ""


def close_roots_drawer(d):
    """A tap beside the drawer: Back closes the whole picker on Android 8 (TLS_API_26)."""
    d.tap_point(0.95, 0.5)
    time.sleep(1)


def open_storage_root(d, api, title):
    """The roots drawer: the device storage root ("… free" below its name), on Android 10+ the "Downloads" root (the
    Download folder, no storage root there). Android 6 - 9 hide the storage root until "Show internal storage" /
    "Show SD card" in the menu (remembered by the picker)."""
    d.log(f"file picker: in {title or '?'}, opening the roots")
    d.text("Show roots").click()
    # The drawer slides in: a tap at the position read during the animation hit "Recent" instead
    time.sleep(1.5)
    if d.visible(STORAGE_ROOT):
        d.find(STORAGE_ROOT)[0].click()
    elif api >= 29:
        # The roots list is redrawn while the drawer opens: the element goes stale between lookup and click
        for attempt in range(3):
            try:
                d.find(f"{ROOTS_DRAWER}//*[@text='Downloads']", what='"Downloads" root')[0].click()
                return
            except StaleElementReferenceException:
                if attempt == 2:
                    raise
                time.sleep(1)
    else:
        close_roots_drawer(d)
        if not show_internal_storage(d):
            raise FlowFailed("file picker: no device storage root, and none to show in the menu")
        d.log("file picker: internal storage was hidden, shown again")


def open_picker_folder(d, path):
    """Shows Download/<path…> in the system document picker (files or folder), from wherever it opened: "Recent", a
    root, the last folder (also one below jellyfin-tls/, Download/jellyfin-tls/downloads after the download step),
    with or without the roots drawer out. One step per round, decided from the toolbar title (the current folder):
    - in jellyfin-tls/ or below: down along path, up from a folder off it: Back on Android 8+, the roots on Android 6
      - 7 (the picker reopens in the last folder without its parents there, Back closes it)
    - in Download/ (title "Download", or "Downloads" on Android 10+, where that root is the folder): into path[0]
    - in the device storage root ("Download" listed): into Download/
    - elsewhere: the roots drawer, the device storage root there ("… free" below its name). Android 6 - 9 hide it
      until "Show internal storage" / "Show SD card" in the menu (remembered by the picker), their "Downloads" root
      lists only DownloadManager files or does not open folders (API 26). Android 10+ has no storage root there,
      its "Downloads" root is the Download folder.
    Only names in the toolbar title count as the current folder: "Recent" lists the folder names as well."""
    api = api_level(d)
    # The picker takes seconds to come up after the app's button (API 24 under load)
    d.find(PICKER, timeout=20, what="file picker")
    time.sleep(1)
    title = ""
    for _ in range(12):
        if not d.visible(PICKER):
            raise FlowFailed(f"the file picker closed (last folder: {title or '?'})")
        if d.visible(ROOTS_DRAWER):
            close_roots_drawer(d)
        title = picker_title(d)
        following = path[path.index(title) + 1] if title in path[:-1] else path[0]
        if title == path[-1]:
            return
        if title in TLS_FOLDERS and title in path and d.visible(picker_entry(following)):
            d.find(picker_entry(following), what=following)[0].click()
        elif title in TLS_FOLDERS and api >= 26:
            d.log(f"file picker: in {title}/, up with Back")
            d.native().back()
        elif title in ("Download", "Downloads" if api >= 29 else "Download") and d.visible(picker_entry(path[0])):
            d.find(picker_entry(path[0]), what=path[0])[0].click()
        elif title not in TLS_FOLDERS and d.visible(picker_entry("Download")):
            d.find(picker_entry("Download"), what="Download")[0].click()
        else:
            open_storage_root(d, api, title)
        time.sleep(1)
    raise FlowFailed(f"file picker: Download/{'/'.join(path)} not reached, still in {title or '?'}")


def pick_file(d, name, folder=("proxy",)):
    """Picks Download/jellyfin-tls/<folder…>/<name> in the system file picker."""
    open_picker_folder(d, ("jellyfin-tls", *folder))
    if not d.visible(picker_entry(name)):
        # fixtures/client has more files than fit on the screen. The file list by its id: the folder spinner above it
        # is scrollable as well
        d.native().find_element(By.ANDROID_UIAUTOMATOR,
                                'new UiScrollable(new UiSelector().resourceIdMatches(".*:id/(dir_list|list|grid)"))'
                                f'.scrollIntoView({ui_text(name)})')
    for attempt in range(2):
        entry = d.find(picker_entry(name), what=name)[0]
        size = d.native().get_window_size()
        if entry.rect["y"] + entry.rect["height"] > size["height"] * 0.88:
            # scrollIntoView stops with the name at the lower edge of the list, which reaches behind the navigation
            # bar: the tap went to the bar, three times, the picker stayed open (API 29)
            d.log(f"file picker: {name} is behind the navigation bar, scrolling on")
            # Slowly: the fling of a swipe moves the list by several rows
            d.native().execute_script("mobile: scrollGesture", dict(
                left=100, top=int(size["height"] * 0.4), width=size["width"] - 200, height=int(size["height"] * 0.3),
                direction="down", percent=0.75, speed=800))
            time.sleep(1)
            entry = d.find(picker_entry(name), what=name)[0]
        entry.click()
        # A tap right after scrolling was taken as the end of the fling, the picker stayed open (API 29)
        for _ in range(5):
            time.sleep(1)
            if not d.visible(PICKER):
                return
        if not d.visible(picker_entry(name)):
            return
        d.log(f"file picker: still open after tapping {name}, again")


def show_internal_storage(d):
    """"Show SD card" / "Show internal storage" in the picker menu, True when it was hidden. The drawer must be closed."""
    d.text("More options").click()
    # Wait for the menu itself: pressing Back without it open closes the picker
    d.find("//*[@resource-id='android:id/title']", what="file picker menu")
    item = d.native().find_elements(By.XPATH, "//*[@text='Show SD card' or @text='Show internal storage']")
    if item:
        item[0].click()
    else:
        # Already shown, or no such entry: the certificate installer opens the picker with the device storage
        # shown, its menu only has "Show file size" (API 24)
        d.native().back()
    time.sleep(1)
    return bool(item)


def kill_app(d, app_id=None):
    """Stops the app from the background: Home first, HOME_WAIT seconds for Android to release its windows and
    surfaces (player, WebView). Killed in the foreground, the emulator's host GPU lost its contexts ("error null ctx")
    and the emulator exited (TLS_API_30 / TLS_TV_34). Playback must be stopped before (stop_player).
    Also before "pm clear", which kills the app as well: 4 of the 5 emulators lost in one run died
    within seconds of a pm clear with the app in front (the 5th after a kill with 3 s in the background, API 29)."""
    d.shell("input keyevent KEYCODE_HOME")
    time.sleep(HOME_WAIT / 2)
    # The app gives up its graphics memory first, see run_action of manual-test.py
    d.shell(f"am send-trim-memory {app_id or APP_ID} COMPLETE 2>&1")
    time.sleep(HOME_WAIT / 2)
    d.native().terminate_app(app_id or APP_ID)


def stop_player(d):
    """Pauses playback, then leaves the player (back to jellyfin-web). Killing the app during playback crashes the
    emulator with the host GPU and overloads it with software rendering, both unrelated to the app. After the
    background step the video plays again with the player shown, Back alone left the session playing at times."""
    if player_state(d) == PLAYING:
        d.log("pausing playback")
        d.shell("input keyevent KEYCODE_MEDIA_PAUSE")  # to the active media session
        try:
            wait_player_state(d, PAUSED, 10, "after the pause key")
        except FlowFailed as error:
            d.log(str(error))
    for attempt in range(2):  # the first Back can only hide the controls
        if not d.visible(PLAYER):
            break
        if not attempt:
            d.log("leaving the player")
        d.native().back()
        time.sleep(3)
    if player_state(d) == PLAYING:
        raise FlowFailed("playback still running after leaving the player")


def seconds(position_text):
    """ "01:13" or "1:02:03" -> seconds."""
    total = 0
    for part in position_text.split(":"):
        total = total * 60 + int(part)
    return total


def position(d):
    """Playback position of the player controls. They fade out quickly, under load before the text is read: a tap
    on the upper part of the video shows them (the center is play/pause, a repeated tap hit the audio track menu
    once). After a tap they take a while to appear on a slow emulator: tapping again too early hides them again."""
    for _ in range(3):
        dismiss_got_it(d)
        if not d.visible(POSITION):
            d.tap_point(0.5, 0.25, within=VIDEO)
        deadline = time.time() + 3
        while time.time() < deadline:
            elements = d.native().find_elements(By.XPATH, POSITION)
            if elements:
                try:
                    return elements[0].text
                except WebDriverException:
                    pass  # hidden again in between
            time.sleep(0.3)
    raise FlowFailed("player position not readable (controls did not show)")


# --- Flows -----------------------------------------------------------------------------------------------------------

def hide_keyboard(driver):
    """Hides the software keyboard when it is up. hide_keyboard() raises "The software keyboard cannot be hidden" when
    the keyboard went away between the check and the call (fresh TLS_API_36 AOSP): then it is gone."""
    try:
        if driver.is_keyboard_shown():
            driver.hide_keyboard()
    except WebDriverException:
        pass


def enter_address(d, server=SERVER):
    host = d.find("//android.widget.EditText", what="address field")[0]
    host.click()
    host.send_keys(server)
    # Only when it is up: hide_keyboard() raises "The software keyboard cannot be hidden" when it is not (fresh
    # TLS_API_36)
    driver = d.native()
    hide_keyboard(driver)


def type_into_dialog(d, text, what):
    """Types into the last text field, the one of the dialog on top. Looked up again when it went stale: a field
    found while the dialog is still drawn is replaced (seen on API 28 - 29)."""
    for attempt in range(3):
        try:
            d.find("//android.widget.EditText", what=what)[-1].send_keys(text)
            return
        except StaleElementReferenceException:
            if attempt == 2:
                raise
            time.sleep(1)


def choose_button(d, index):
    """ "Choose…" of the CA (0) or client certificate (1) row of the connect screen: the first one below the row's
    label. A long error message above pushes the rows below the screen (API 26), the screen is scrolled a bit at a
    time then (scrolled to the end, the CA row was above the screen)."""
    label = ("CA certificate", "Client certificate")[index]
    driver = d.native()
    # The address field has the focus after the app restart of missing: the keyboard covers the rows (API 26)
    if driver.is_keyboard_shown():
        hide_keyboard(driver)
        time.sleep(1)
    for attempt in range(4):
        labels = driver.find_elements(By.XPATH, f"//*[@text='{label}']")
        if labels:
            top = labels[0].rect["y"]
            below = [e for e in driver.find_elements(By.XPATH, "//*[@text='Choose…']") if e.rect["y"] > top]
            if below:
                return min(below, key=lambda e: e.rect["y"])
        screen = driver.find_element(By.XPATH, "//android.widget.ScrollView").rect
        driver.execute_script("mobile: swipeGesture", dict(
            left=screen["x"], top=screen["y"] + screen["height"] // 3, width=screen["width"],
            height=screen["height"] // 3, direction="up", percent=0.5))
        time.sleep(1)
    raise FlowFailed(f'"Choose…" of "{label}" not found')


CONTAINED_CA_TITLE = "//*[@resource-id='android:id/alertTitle' and @text='CA certificate in the file']"
CLIENT_CA = "Jellyfin Test Client CA"  # in client-ec.p12 besides the client certificate (generate.sh -certfile)


def contained_ca_offer(d, name):
    """After a client certificate from name: the app offers to trust the CA certificate contained in the file. The
    offer must name the client CA. Returns the message."""
    d.find(CONTAINED_CA_TITLE, timeout=20, what=f'"CA certificate in the file" offer for {name}')
    message = d.find("//*[@resource-id='android:id/message']", what="offer message")[0].text
    if CLIENT_CA.lower() not in message.lower():
        raise FlowFailed(f"the offer does not name {CLIENT_CA}: {message!r}")
    d.log("offered: " + message.splitlines()[0])
    return message


def decline_contained_ca(d, name):
    """The client CA did not issue the server certificate (the proxy has its own CA): "Don't trust"."""
    contained_ca_offer(d, name)
    d.by_id("android:id/button2").click()


def stage_file_certificate(d, name):
    """Connect screen with Advanced open: picks Download/jellyfin-tls/proxy/<name> in the system file picker, ca.pem
    for the CA row, a .p12 (password jellyfin) for the client certificate row."""
    is_client = name.endswith(".p12")
    choose_button(d, 1 if is_client else 0).click()
    pick_file(d, name)
    if is_client:
        # Wait for the dialog: an EditText looked up while the picker closes is the address field, stale once the
        # dialog is drawn (seen on API 29)
        d.text("Enter the password", contains=True)
        type_into_dialog(d, "jellyfin", "password field")
        d.by_id("android:id/button1").click()
        decline_contained_ca(d, name)
    d.text(f"{name} (new)")
    d.log(f"{'client certificate' if is_client else 'CA'}: {name}")


def wait_login_page(d, timeout=30):
    d.web_wait("document.querySelector('#txtManualName')", timeout, "jellyfin-web login page")
    d.log("login page")


def open_url_import(d):
    """Connect screen: Advanced open, "Load certificates from URL" checked. The address field has the focus after the
    app restart of missing: the keyboard coming up moved the screen and the tap on Advanced was lost (API 26), so the keyboard is hidden first and Advanced tapped again when the section did not open."""
    driver = d.native()
    if driver.is_keyboard_shown():
        hide_keyboard(driver)
        time.sleep(1)
    for attempt in range(3):
        if d.visible("//*[@text='Load certificates from URL (network)']"):
            break
        d.text("Advanced", contains=True).click()
        time.sleep(2)
    for attempt in range(3):
        try:
            checkbox = d.find("//android.widget.CheckBox", what="Load certificates from URL checkbox")[0]
            checked = checkbox.get_attribute("checked") == "true"
            break
        except StaleElementReferenceException:  # redrawn after Advanced opened
            if attempt == 2:
                raise
            time.sleep(1)
    if not checked:
        d.text("Load certificates from URL (network)").click()


def stage_url_certificate(d, name):
    """Connect screen with the URL import open: <IMPORT_URL>/<name> for the CA row (ca.pem) or the client certificate
    row (a .p12, password jellyfin)."""
    is_client = name.endswith(".p12")
    choose_button(d, 1 if is_client else 0).click()
    d.text("Import from URL", contains=True, timeout=20)  # the dialog
    type_into_dialog(d, f"{IMPORT_URL}/{name}", "URL field")
    d.by_id("android:id/button1").click()
    if is_client:
        d.text("Enter the password", contains=True, timeout=20)
        type_into_dialog(d, "jellyfin", "password field")
        d.by_id("android:id/button1").click()
        decline_contained_ca(d, name)
    d.find(f"//*[contains(@text, '{name}') and contains(@text, '(new)')]", timeout=20, what=f"{name} staged")
    d.log(f"{'client certificate' if is_client else 'CA'} from the URL: {name}")


def flow_setup(d, ca_only="0"):
    """Connect screen: address, Advanced, CA and client certificate from the system file picker, connect. CA_ONLY=1:
    the worker's CA-only port (no client certificate required), only the CA, from the URL: the most common setup, a
    server with its own CA without mutual TLS."""
    if ca_only == "1":
        enter_address(d, CA_ONLY_SERVER)
        open_url_import(d)
        stage_url_certificate(d, "ca.pem")
    else:
        enter_address(d)
        d.text("Advanced", contains=True).click()
        stage_file_certificate(d, "ca.pem")
        stage_file_certificate(d, "client-ec.p12")
    d.text("Connect").click()
    wait_login_page(d)


def flow_login(d):
    """Manual login form of jellyfin-web, credentials from E2E_USER / E2E_PASSWORD."""
    user, password = os.environ.get("E2E_USER"), os.environ.get("E2E_PASSWORD")
    if not (user and password):
        raise FlowFailed("E2E_USER / E2E_PASSWORD not set")
    d.web_wait("document.querySelector('#txtManualName')", 30, "login form")
    web = d.web()
    web.find_element(By.CSS_SELECTOR, "#txtManualName").send_keys(user)
    web.find_element(By.CSS_SELECTOR, "#txtManualPassword").send_keys(password)
    web.find_element(By.CSS_SELECTOR, ".manualLoginForm button[type=submit]").click()
    d.web_wait(MY_MEDIA, 30, '"My Media"')
    d.log(f"logged in as {user}")


def flow_browse(d):
    """Opens the first library from the home screen, posters checked by the request evidence."""
    d.web_wait(MY_MEDIA, 30, '"My Media"')
    d.web_click(".section0 a.textActionButton")
    page = d.web_wait("[...document.querySelectorAll('#moviesPage, #tvshowsPage')].filter(e => e.offsetParent)"
                      ".map(e => e.id)[0]", 20, "library page")
    d.log(f"library: {page}")


def flow_play(d, card="0", quality=""):
    """Plays the video of card CARD (from 0) of the library page, whatever its title, for 10 seconds in the native
    player, the position must advance. Leaves a running player first. QUALITY=low picks the lowest quality of the
    player's quality menu: the test videos are H264 the emulator plays directly, the low bitrate makes the server
    transcode (HLS)."""
    stop_player(d)
    if not d.web().execute_script("return !!document.querySelector('.page:not(.hide) .card button[data-action=play]')"):
        # Not on the library page the step before left: after a recovery of the emulator or a restart of the app it
        # starts on the home screen
        d.log("no video cards on this page, opening the library first")
        flow_browse(d)
    d.web_click(".page:not(.hide) .card button[data-action=play]", index=int(card))
    with d.no_idle_wait():
        d.find(PLAYER, timeout=30, what="player")
        if quality == "low":
            choose_lowest_quality(d)
        start = position(d)
        time.sleep(10)
        end = position(d)
    d.log(f"position {start} -> {end}")
    if start == end:
        raise FlowFailed(f"position did not advance ({start})")


def choose_lowest_quality(d):
    """Quality menu of the player ("1080p - 10 Mbps" … "360p - 420 kbps", "Auto"): the lowest bitrate. The controls
    can fade out before the button is tapped, shown again for every attempt."""
    button = f"//*[@resource-id='{APP_ID}:id/quality_button']"
    entries = "//*[@resource-id='android:id/title']"
    for _ in range(3):
        position(d)  # controls visible
        buttons = d.native().find_elements(By.XPATH, button)
        if not buttons:
            continue
        try:
            buttons[0].click()
        except WebDriverException:
            continue  # hidden in between
        d.find(entries, timeout=10, what="quality menu")
        qualities = [e for e in d.native().find_elements(By.XPATH, entries) if re.match(r"\d+p - ", e.text, re.IGNORECASE)]
        if not qualities:
            raise FlowFailed("quality menu has no bitrate entries")
        lowest = qualities[-1]
        label = lowest.text
        lowest.click()
        d.log(f"quality: {label}")
        # The player restarts the stream with the new bitrate
        time.sleep(3)
        d.find(PLAYER, timeout=30, what="player after switching the quality")
        return
    raise FlowFailed("quality button not reachable (controls hid again 3 times)")


# Seeking in the seek steps (mobile and TV): past ExoPlayer's buffer (~50 s), only then a new range request goes out and
# the open direct stream ends, which puts it into the proxy log. Key presses and double taps get lost under load: seek
# step by step until SEEK_AHEAD seconds further, never past SEEK_LIMIT (the test clip is 120 s, playback would end).
SEEK_AHEAD = 55
SEEK_LIMIT = 95


def seek_far_enough(before, after):
    return seconds(after) - seconds(before) >= SEEK_AHEAD or seconds(after) >= SEEK_LIMIT


def check_seek(before, after):
    """FlowFailed when seeking did not get SEEK_AHEAD seconds further (or to SEEK_LIMIT)."""
    if not seek_far_enough(before, after):
        raise FlowFailed(f"seeking moved the position only from {before} to {after}")


def ensure_player(d):
    """A running player for the flows that act on it (seek, background). After a recovery of the emulator the app is on
    its home screen: the first video is played again first."""
    if not d.visible(PLAYER):
        d.log("no player, playing the first video again")
        flow_play(d)


def flow_seek(d):
    """Seeks forward with double taps, switches the subtitles, playback continues."""
    ensure_player(d)
    with d.no_idle_wait():
        dismiss_battery_snackbar(d)
        before = position(d)
        # The clip is 120 s: started late (a slow emulator, the steps before took a minute: 01:22 on TLS_API_34 with
        # software rendering) the seeks ahead end the playback and the player closes before the subtitles button is
        # tapped. Back first, with double taps on the left side, until SEEK_AHEAD still fits below SEEK_LIMIT.
        for _ in range(8):
            if seconds(before) <= SEEK_LIMIT - SEEK_AHEAD:
                break
            d.tap_point(0.2, 0.5, double=True, within=VIDEO)
            before = position(d)
            d.log(f"seeked back to {before}")
        after = before
        for _ in range(8):
            d.tap_point(0.8, 0.5, double=True, within=VIDEO)
            after = position(d)
            if seek_far_enough(before, after):
                break
        d.log(f"position {before} -> {after}")
        check_seek(before, after)
        # The controls can fade out again before the button is tapped (seen on API 30): show them again
        subtitles = f"//*[@resource-id='{APP_ID}:id/subtitles_button']"
        for attempt in range(3):
            dismiss_battery_snackbar(d)  # it covers the control row
            position(d)  # controls visible
            buttons = d.native().find_elements(By.XPATH, subtitles)
            if buttons:
                try:
                    buttons[0].click()
                    break
                except WebDriverException:
                    pass  # hidden in between
        else:
            raise FlowFailed("subtitles button not reachable (controls hid again 3 times)")
        d.log("subtitles switched")
        time.sleep(5)
        d.find(PLAYER, what="player after switching subtitles")


def player_state(d):
    """PlaybackState.state of the native player's MediaSession (tag PlayerViewModel): 3 playing, 2 paused, None
    without the session. Read from dumpsys: the notification shade covers the player controls."""
    lines = d.shell("dumpsys media_session").splitlines()
    # Android 14+: a session id after the tag
    header = re.compile(rf"PlayerViewModel {re.escape(APP_ID)}/PlayerViewModel(/\d+)?( \(userId=\d+\))?$")
    for index, line in enumerate(lines):
        # The session header "PlayerViewModel <package>/PlayerViewModel (userId=0)" (Android 6: without the user,
        # 14+: "…/PlayerViewModel/8 (userId=0)"), not "Media button session is …"
        if not header.match(line.strip()):
            continue
        indent = len(line) - len(line.lstrip())
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break  # next session
            # "state=3", Android 14+: "state=PLAYING(3)"
            match = re.search(r"state=PlaybackState \{state=(?:[A-Z_]+\()?(\d+)", following)
            if match:
                return int(match.group(1))
    return None


def wait_player_state(d, state, timeout, what):
    deadline = time.time() + timeout
    while (current := player_state(d)) != state:
        if time.time() > deadline:
            raise FlowFailed(f"{what}: player state {current}, expected {state} (3 playing, 2 paused)")
        time.sleep(0.5)


def notification_button(d, label, timeout=10):
    """Action button of the media notification (or of the media controls in the shade, Android 11+)."""
    return d.find(f"//*[@content-desc='{label}']", timeout, what=f'"{label}" in the media notification')[0]


def flow_background(d, back="1"):
    """Media notification: Home during playback, pause and resume with the notification controls, back to the app,
    the position advances (BACK=0: stops with the app in the background). Below Android 10 the notification carries
    the poster as large icon (it crashed PlayerService.start on Android 6 with TransactionTooLargeException)."""
    ensure_player(d)
    with d.no_idle_wait():
        driver = d.native()
        d.find(PLAYER, what="player")
        wait_player_state(d, PLAYING, 10, "before Home")
        driver.press_keycode(KEYCODE_HOME)
        time.sleep(2)
        state = player_state(d)
        d.log(f"after Home: player state {state}")
        driver.open_notifications()
        time.sleep(1)
        if state == PLAYING:
            notification_button(d, "Pause").click()
            wait_player_state(d, PAUSED, 10, "after Pause in the notification")
            d.log("paused from the notification")
        notification_button(d, "Play").click()
        wait_player_state(d, PLAYING, 10, "after Play in the notification")
        d.log("resumed from the notification")
        driver.back()  # closes the shade
        if back == "0":
            return
        driver.activate_app(APP_ID)
        d.find(PLAYER, timeout=15, what="player after returning to the app")
        start = position(d)
        time.sleep(5)
        end = position(d)
        d.log(f"back in the app: position {start} -> {end}")
        if start == end:
            raise FlowFailed(f"position did not advance after returning to the app ({start})")
        # The position advances with the audio alone: the picture must change too (API 30 with the host GPU froze it
        # after leaving picture-in-picture, dequeueBuffer timeouts, the sound went on)
        frames = []
        for _ in range(3):
            frames.append(d.screencap())
            time.sleep(1)
        if len(set(frames)) == 1:
            raise FlowFailed("video picture frozen after returning to the app, the position (audio) advances")
        d.log("video picture moves")


def folder_sizes(d):
    """Sizes of the files under DOWNLOAD_DIR except README.txt, by name. ls -lR: the size is the largest number in a
    line on every ls variant (toolbox on Android 6, toybox later)."""
    sizes = {}
    for line in d.shell(f"ls -lR {DOWNLOAD_DIR}").splitlines():
        parts = line.split()
        if line.startswith("-") and not line.endswith("README.txt"):
            name = re.split(r" \d\d:\d\d ", line, maxsplit=1)[-1]
            sizes[name] = max(int(part) for part in parts if part.isdigit())
    return sizes


def allow_mobile_data_downloads(d):
    """Downloads use WiFi only by default and wait for it, the Android 6 - 7 images have mobile data only. "Mobile
    Data" allows both, on every image."""
    d.web().execute_script("NativeInterface.openClientSettings()")
    driver = d.native()
    driver.find_element(By.ANDROID_UIAUTOMATOR,
                        f'{SETTINGS_LIST}.scrollIntoView({ui_text("Allowed Network Types")})').click()
    d.text("Mobile Data").click()
    d.by_id("android:id/button1").click()  # OK: the choice is only saved with it, Back cancels
    d.native().back()  # leaves the settings
    d.log("downloads allowed over mobile data")


def choose_download_folder(d):
    """First download: the app asks for the download location, picked in the system folder picker."""
    d.text("Select folder").click()
    # Wait for the picker: it can take seconds to list the folder (seen on API 33)
    d.find(PICKER, timeout=15, what="folder picker")
    time.sleep(1)
    open_picker_folder(d, ("jellyfin-tls", "downloads"))
    d.find(FOLDER_CONFIRM, what="folder picker confirm button")[0].click()
    # Android 11+: "Allow Jellyfin to access files in downloads?"
    time.sleep(1)
    if d.visible("//*[@resource-id='android:id/button1']"):
        d.by_id("android:id/button1").click()
    d.log("download location: Download/jellyfin-tls/downloads")


def flow_download(d, card="0"):
    """Downloads movie CARD (from 0, by name like the library) with "Download" on its details page: jellyfin-web
    hands it to the app, which downloads with its own OkHttp client and the client certificate. Passes when the file
    in the download folder has the size of the media source on the server."""
    with d.no_idle_wait():
        stop_player(d)
    # Files of an earlier download step: the app resumed the complete file (request answered 416, nothing loaded) and
    # the size check below found the old file (ca-only-download)
    if folder_sizes(d):
        d.shell(f"find {DOWNLOAD_DIR} -type f ! -name README.txt -exec rm {{}} \\;")
        left = folder_sizes(d)
        d.log(f"earlier downloads removed{f', still there: {left}' if left else ''}")
    allow_mobile_data_downloads(d)
    # Whatever page is shown: the CARD-th movie by name (the library's default order), from the server through
    # jellyfin-web's ApiClient. A promise: chromedriver returns its value
    d.web_wait("window.ApiClient && ApiClient.getCurrentUserId()", 30, "jellyfin-web logged in")
    item_id, expected = d.web().execute_script(
        "return ApiClient.getItems(ApiClient.getCurrentUserId(), {IncludeItemTypes: 'Movie', Recursive: true,"
        " SortBy: 'SortName', Fields: 'MediaSources', StartIndex: arguments[0], Limit: 1})"
        ".then(result => [result.Items[0].Id, result.Items[0].MediaSources[0].Size])", int(card))
    # The details page of the item: the cards of the mobile layout show no item menu button
    d.web().execute_script("location.hash = '#/details?id=' + arguments[0] + '&serverId=' + ApiClient.serverId()",
                           item_id)
    download_button = ("[...document.querySelectorAll('.page:not(.hide) .btnDownload')].find(e => e.offsetParent)")
    more_button = ("[...document.querySelectorAll('.page:not(.hide) .btnMoreCommands')].find(e => e.offsetParent)")
    d.web_wait(f"{download_button} || {more_button}", 20, "details page")
    if d.web().execute_script(f"return !!{download_button}"):
        d.web().execute_script(f"{download_button}.click()")
    else:
        d.web().execute_script(f"{more_button}.click()")
        menu_item = ("[...document.querySelectorAll('.actionSheetMenuItem')]"
                     ".find(e => e.offsetParent && e.textContent.trim().toLowerCase() === 'download')")
        d.web_wait(menu_item, 10, '"Download" in the item menu')
        d.web().execute_script(f"{menu_item}.click()")
    deadline = time.time() + 10
    while not (asked := d.visible("//*[@text='Select folder']")) and time.time() < deadline:
        time.sleep(1)
    if asked:
        choose_download_folder(d)
    else:
        d.log("no download location dialog, chosen before")
    d.log(f"downloading item {item_id}, {expected} bytes")
    deadline = time.time() + 180
    sizes = {}
    while time.time() < deadline:
        sizes = folder_sizes(d)
        if expected in sizes.values():
            name = next(name for name, size in sizes.items() if size == expected)
            d.log(f"downloaded: {name} ({expected} bytes)")
            return
        time.sleep(2)
    raise FlowFailed(f"download not complete after 180s, {expected} bytes expected, files: {sizes or 'none'}")


# --- Certificates screen and error messages ----------------------------------------------------------------------------

ENDPOINT = SERVER.removeprefix("https://")
# The settings list of the app by its id: the WebView below it is scrollable as well once jellyfin-web shows the home
# screen, "the first scrollable" then searched the WebView and the swipes moved the list (entry not found)
SETTINGS_LIST = f'new UiScrollable(new UiSelector().resourceId("{APP_ID}:id/recycler_view"))'
IMPORT_URL = "http://10.0.2.2:8930"  # plain HTTP file server of the test Caddy


def dismiss_got_it(d):
    """Clicks the "Got it" button of an Android notice over the app when it is shown, the first time on every AVD: the
    immersive mode confirmation ("Viewing full screen", Android 6 - 7 at the first playback) and other first-use hints. It
    keeps the focus and covers the screen until it is dismissed. The text match ignores case ("GOT IT"). True when
    there was one."""
    found = False
    try:
        for button in d.native().find_elements(By.XPATH, "//*[@text='Got it']"):
            d.log('"Got it" clicked')
            button.click()
            time.sleep(1)
            found = True
    except WebDriverException:
        pass  # gone in between
    return found


def dismiss_battery_snackbar(d):
    """Swipes the battery optimization snackbar of the app away when it is shown (its OK would open the system battery
    settings). It stays until dismissed and covers the bottom of the screen: the settings list (see
    open_certificates_screen) and the player's control row, the subtitles button of the seek step on TLS_API_34.
    True when there was one."""
    found = False
    for snackbar in d.native().find_elements(By.XPATH, f"//*[@resource-id='{APP_ID}:id/snackbar_text']/.."):
        # Within the middle of it: a swipe from the screen edge is the system back gesture (closed the settings on
        # API 36)
        rect = snackbar.rect
        d.native().execute_script("mobile: swipeGesture", dict(
            left=rect["x"] + rect["width"] // 4, top=rect["y"], width=rect["width"] // 2, height=rect["height"],
            direction="right", percent=0.9))
        d.log("battery optimization snackbar swiped away")
        time.sleep(1)
        found = True
    return found


def open_certificates_screen(d):
    """App settings (from jellyfin-web through the JS bridge) > Certificates of the server."""
    d.web().execute_script("NativeInterface.openClientSettings()")
    # The battery optimization snackbar of the WebView stays over the bottom of the settings list, the entry scrolled
    # into view ended up below it and the tap went to the snackbar (settings step failed after the full checklist):
    # swipe it away (its OK would open the system battery settings)
    dismiss_battery_snackbar(d)
    # The entry is the last one, its summary is the endpoint, the category header above it is also called
    # "Certificates". scrollIntoView / scrollToEnd stopped with the entry cut off at the bottom edge of the list, its
    # summary hidden (API 24, 28, 29): one more short swipe, then the title of the entry. Once more if the screen does
    # not open.
    for attempt in range(2):
        try:
            d.native().find_element(By.ANDROID_UIAUTOMATOR, f'{SETTINGS_LIST}.scrollToEnd(5)')
        except NoSuchElementException:
            pass  # not an element, the scrolling is done anyway
        entry = f"//*[@text='{ENDPOINT}']/.."
        if not d.visible(entry):
            def title_y():
                titles = d.native().find_elements(By.XPATH, "//*[@text='Certificates']")
                return titles[-1].rect["y"] if titles else None

            settings_list = d.by_id(f"{APP_ID}:id/recycler_view").rect
            before = title_y()
            d.native().execute_script("mobile: swipeGesture", dict(
                left=settings_list["x"], top=settings_list["y"] + settings_list["height"] // 3,
                width=settings_list["width"], height=settings_list["height"] // 3, direction="up", percent=0.5))
            time.sleep(1)
            after = title_y()
            moved = f"{before - after} px" if before is not None and after is not None else "? (no entry title)"
            d.log(f"certificates entry cut off at the bottom, extra swipe moved it {moved}")
            if not d.visible(entry):
                d.notes.append("certificates entry cut off at the bottom of the settings list, its summary not reachable")
                entry = "(//*[@text='Certificates'])[last()]/.."
        d.find(entry, what="certificates entry")[0].click()
        try:
            d.text("Trusted certificate authorities", timeout=10)
            return
        except FlowFailed:
            if attempt:
                raise
            d.log("certificates screen did not open, tapping the entry again")
            d.notes.append("certificates entry needed a second tap")


def leave_certificates_screen(d):
    d.native().back()  # certificates screen
    d.native().back()  # settings


def scroll_to_text(d, text):
    return d.native().find_element(By.ANDROID_UIAUTOMATOR, 'new UiScrollable(new UiSelector().scrollable(true))'
                                   f'.scrollIntoView({ui_text(text, prefix=True)})')


# XPath conditions for the entries of the certificates screen: the CAs, the client certificate (both with expiry and
# fingerprint, the client one clickable once the certificate is loaded)
CA_ENTRY = ("contains(@text, 'SHA-256') and not(starts-with(@text, 'Imported')) "
            "and not(starts-with(@text, 'From system store'))")
CLIENT_ENTRY = ("(starts-with(@text, 'Imported') or starts-with(@text, 'From system store')) "
                "and contains(@text, 'SHA-256')")


def client_certificate_text(d):
    """ "No client certificate." or "Imported: CN=…" on the certificates screen (the subject is loaded afterwards)."""
    xpath = "//*[starts-with(@text, 'Imported') or starts-with(@text, 'From system store') or " \
            "@text='No client certificate.']"
    text = d.find(xpath, what="client certificate line")[0].text
    if text.lower().startswith("imported") and "cn=" not in text.lower():
        time.sleep(2)
        text = d.find(xpath, what="client certificate line")[0].text
    return text


def flow_settings(d, ca="imported"):
    """Certificates screen of the server: the CA (subject, expiry, fingerprint) and the client certificate (subject)
    are listed. CA=system (checklists/mobile-system.md): CA and client key are in the system store, no imported CA
    and the client certificate "From system store". Whether the texts read well stays a human check."""
    open_certificates_screen(d)
    cas = [e.text for e in d.native().find_elements(By.XPATH, f"//*[{CA_ENTRY}]")]
    if ca == "system":
        if cas or not d.visible("//*[@text='No imported certificate authorities.']"):
            raise FlowFailed(f"imported CAs listed although the CA is in the system store ({cas})")
        d.log("CA: none imported (system store)")
    elif not cas or "expires" not in cas[0].lower():
        raise FlowFailed(f"no imported CA with expiry and fingerprint listed ({cas})")
    else:
        d.log("CA: " + cas[0].replace("\n", " / "))
    client = client_certificate_text(d)
    if "cn=client-ec" not in client.lower():
        raise FlowFailed(f"client certificate client-ec not listed: {client!r}")
    if "expires" not in client.lower() or "sha-256" not in client.lower():
        raise FlowFailed(f"client certificate without expiry and fingerprint: {client!r}")
    if ca == "system" and not client.lower().startswith("from system store"):
        raise FlowFailed(f"client certificate not from the system store: {client!r}")
    d.log(f"client certificate: {client.replace(chr(10), " / ")}")
    leave_certificates_screen(d)


def wait_toast(d, text, timeout=15):
    """Text of a toast containing text, None when none appears within timeout. UiAutomator2 lists a toast as an
    android.widget.Toast node only while all windows are searched (enableMultiWindows, switched on before the toast),
    read from the page source (see tv_flows.wait_toast)."""
    pattern = re.compile(r'<android\.widget\.Toast [^>]*text="([^"]*)"')
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in pattern.findall(d.native().page_source):
            message = html.unescape(message)
            if text.lower() in message.lower():
                return message
        time.sleep(0.2)
    return None


@contextlib.contextmanager
def toasts(d):
    """UiAutomator2 lists a toast only while all windows are searched (enableMultiWindows), switched on before the toast
    appears: jellyfin-web sends requests as soon as it is shown again, the message can come before the flow asks for
    one (the app shows each problem once)."""
    d.native().update_settings({"enableMultiWindows": True})
    try:
        yield
    finally:
        d.native().update_settings({"enableMultiWindows": False})


def web_request(d):
    """Result of a request of jellyfin-web in the running session: "ok" or "error …" (whatever page is shown)."""
    return d.web().execute_script(
        "return ApiClient.getItems(ApiClient.getCurrentUserId(), {Limit: 1}).then(() => 'ok', e => 'error ' + e)")


def remove_ca(d):
    """On the certificates screen: removes the imported CA."""
    d.find(f"//*[{CA_ENTRY}]", what="imported CA")[0].click()
    d.by_id("android:id/button1").click()  # Remove
    d.text("No imported certificate authorities.")
    d.log("CA removed")


def remove_client_certificate(d):
    """On the certificates screen: removes the client certificate (its entry, Remove in the dialog)."""
    d.find(f"//android.widget.Button[{CLIENT_ENTRY}]", timeout=20, what="client certificate entry")[0].click()
    d.by_id("android:id/button1").click()  # Remove
    d.text("No client certificate.")
    d.log("client certificate removed")


def confirm_ca_import(d, source):
    """On the certificates screen after choosing a CA file: the confirmation shows the certificate (with its fingerprint
    as well), Trust, then the CA is listed as a button."""
    d.find("//*[@resource-id='android:id/alertTitle' and starts-with(@text, 'Import CA certificate')]", timeout=20,
           what="CA import confirmation")
    d.by_id("android:id/button1").click()  # Trust
    d.find(f"//android.widget.Button[{CA_ENTRY}]", timeout=20, what="imported CA")
    d.log(f"CA imported again from {source}")


def check_web_request(d, what):
    result = web_request(d)
    if result != "ok":
        raise FlowFailed(f"jellyfin-web does not reach the server with {what}: {result}")
    d.log(f"request with {what}: ok")


def flow_missing_ca(d):
    """Removes the imported CA on the certificates screen: the next request of jellyfin-web in the running session must
    fail with the "not trusted" toast (the WebView must not keep trusting the server). Then imports ca.pem again with
    the system file picker on the certificates screen (was the step ca-file): requests of jellyfin-web work again
    without restarting the app."""
    open_certificates_screen(d)
    remove_ca(d)
    expected = "is not trusted"
    with toasts(d):
        leave_certificates_screen(d)
        # Decided by the request: it must fail within 15 s (the change reaches the running WebView; on a loaded emulator
        # the first request after Back could still be in flight). The message: each failed request can bring it, one
        # missed toast (shown 2 s, a slow page source read) is no failure, three are
        deadline = time.time() + 15
        while (result := web_request(d)) == "ok" and time.time() < deadline:
            time.sleep(2)
        if result == "ok":
            raise FlowFailed("jellyfin-web still reaches the server 15 s after the CA was removed")
        message = wait_toast(d, expected)
        for attempt in range(2):
            if message:
                break
            d.log(f'no "{expected}" message seen yet, another request')
            web_request(d)
            message = wait_toast(d, expected)
    if not message:
        raise FlowFailed(f'request failed ({result}), but no "{expected}" message in the running session')
    d.log(f"request without the CA: {result}")
    d.log(f"message in the running session: {message}")

    # Imported again with the system file picker ("Import CA certificate…"; the URL import on this screen: p12-ca)
    open_certificates_screen(d)
    scroll_to_text(d, "Import CA certificate").click()
    pick_file(d, "ca.pem")
    confirm_ca_import(d, "the file picker")
    leave_certificates_screen(d)
    check_web_request(d, "the CA imported again")


def flow_missing(d):
    """Removes the client certificate on the certificates screen: the next request of jellyfin-web in the running
    session must bring the "requires a client certificate" toast. Then restarts the app: it falls back to the connect
    screen, Connect must explain it as well (not a generic network error)."""
    open_certificates_screen(d)
    remove_client_certificate(d)
    expected = "requires a client certificate"
    with toasts(d):
        leave_certificates_screen(d)
        web_request(d)
        message = wait_toast(d, expected)
    if not message:
        raise FlowFailed(f'no "{expected}" message in the running session')
    d.log(f"message in the running session: {message}")
    d.close()  # the WebView of the session goes away with the app
    kill_app(d)
    d.native().activate_app(APP_ID)
    connect_without_client_certificate(d)


def flow_reimport(d):
    """On the connect screen (after missing): client certificate from the URL import (Advanced, "Load certificates from
    URL"), connect again, jellyfin-web works without restarting the app."""
    open_url_import(d)
    stage_url_certificate(d, "client-ec.p12")
    d.text("Connect").click()
    # Logged in before: home screen, otherwise the login page
    d.web_wait(f"{MY_MEDIA} || document.querySelector('#txtManualName')", 60, "jellyfin-web after reconnecting")
    if not d.web().execute_script(f"return {MY_MEDIA}"):
        flow_login(d)
    d.log("connected again")


def flow_wrong_password(d):
    """Certificates screen: imports client-rsa.p12 from the URL with a wrong password, the message says so, the
    configured client certificate is unchanged."""
    open_certificates_screen(d)
    before = client_certificate_text(d)
    scroll_to_text(d, "Import from URL").click()
    d.find("//android.widget.EditText", what="URL field")[0].send_keys(f"{IMPORT_URL}/client-rsa.p12")
    d.find("//android.widget.CheckedTextView[starts-with(@text, 'Import client certificate')]",
           what="client certificate target")[0].click()
    d.by_id("android:id/button1").click()
    d.find("//android.widget.EditText", timeout=20, what="password field")[-1].send_keys("wrong")
    d.by_id("android:id/button1").click()
    message = d.find("//*[starts-with(@text, 'Wrong password')]", timeout=20, what="wrong password message")[0].text
    d.log("message: " + " / ".join(line.strip() for line in message.splitlines() if line.strip()))
    d.by_id("android:id/button1").click()
    after = client_certificate_text(d)
    if after != before:
        raise FlowFailed(f"client certificate changed: {before!r} -> {after!r}")
    d.log(f"client certificate unchanged: {after}")
    leave_certificates_screen(d)


def flow_pem_client(d):
    """Certificates screen: picks a PEM file with a private key and a certificate (fixtures/client/pem-key-and-cert.pem)
    as client certificate. Android reads client keys only from PKCS#12: the message must ask for a .p12 / .pfx file,
    the configured client certificate is unchanged."""
    open_certificates_screen(d)
    before = client_certificate_text(d)
    scroll_to_text(d, "Import client certificate").click()
    pick_file(d, "pem-key-and-cert.pem", folder=("fixtures", "client"))
    not_pkcs12 = "//*[contains(@text, '.p12 or .pfx')]"
    password_title = "//*[@text='Password of the certificate file']"
    # The password is asked before the file is read
    d.find(f"{not_pkcs12} | {password_title}", timeout=20, what="password dialog or not PKCS#12 message")
    if d.visible(password_title):
        d.notes.append("password asked before the PEM file was rejected")
        type_into_dialog(d, "jellyfin", "password field")
        d.by_id("android:id/button1").click()
    message = d.find(not_pkcs12, timeout=20, what="not PKCS#12 message")[0].text
    d.log("message: " + message.strip())
    d.by_id("android:id/button1").click()
    after = client_certificate_text(d)
    if after != before:
        raise FlowFailed(f"client certificate changed: {before!r} -> {after!r}")
    d.log(f"client certificate unchanged: {after}")
    leave_certificates_screen(d)


def flow_p12_ca(d):
    """Certificates screen: the client certificate is removed, then client-ec.p12 is imported from the URL as CA
    certificate: its CA (Jellyfin Test Client CA) is listed for trust, then the client certificate in the file is
    offered and used. The client CA is removed again (the proxy's server certificate has its own CA), jellyfin-web works
    with the client certificate from the CA file."""
    open_certificates_screen(d)
    remove_client_certificate(d)
    scroll_to_text(d, "Import from URL").click()
    d.find("//android.widget.EditText", what="URL field")[0].send_keys(f"{IMPORT_URL}/client-ec.p12")
    d.find("//android.widget.CheckedTextView[starts-with(@text, 'Import CA certificate')]", what="CA target")[0].click()
    d.by_id("android:id/button1").click()
    d.find("//android.widget.EditText", timeout=20, what="password field")[-1].send_keys("jellyfin")
    d.by_id("android:id/button1").click()
    d.find("//*[@resource-id='android:id/alertTitle' and starts-with(@text, 'Import CA certificate')]", timeout=20,
           what="CA import confirmation")
    message = d.find("//*[@resource-id='android:id/message']", what="CA details")[0].text
    if CLIENT_CA.lower() not in message.lower():
        raise FlowFailed(f"the CA confirmation does not list {CLIENT_CA}: {message[:200]!r}")
    d.by_id("android:id/button1").click()  # Trust
    d.find("//*[@resource-id='android:id/alertTitle' and @text='Client certificate in the file']", timeout=20,
           what='"Client certificate in the file" offer')
    d.log("offered: " + d.find("//*[@resource-id='android:id/message']")[0].text.splitlines()[0])
    d.by_id("android:id/button1").click()  # Use
    d.find("//*[contains(@text, 'CN=client-ec')]", timeout=20, what="client certificate client-ec")
    d.log(f"client certificate from the CA file: {client_certificate_text(d)}")
    d.find(f"//android.widget.Button[contains(@text, '{CLIENT_CA}')]", what="the client CA")[0].click()
    d.by_id("android:id/button1").click()  # Remove
    d.log(f"{CLIENT_CA} removed again")
    leave_certificates_screen(d)
    check_web_request(d, "the client certificate from the CA file")


# --- System credential store ----------------------------------------------------------------------------------------
# The CA and the client key are installed the way a user does it, never copied with root: only the installer updates
# KeyChain and sends the trust store broadcasts. Android 11+: Settings > Encryption & credentials > Install a
# certificate (the only place a CA can be installed), up to Android 10 the certificate installer intent, which is what
# Settings opens there. Whether the installer insists on a screen lock differs (Android 6: before anything is installed,
# also a CA; Android 16: never), the flows set the PIN only when asked and note it. system-cleanup clears the
# credentials (Settings > Clear credentials) and removes the PIN again, a locked screen would stop later steps.

LOCK_PIN = "1234"
SYSTEM_KEY_ALIAS = "client-ec"  # name the installer proposes for client-ec.p12
SYSTEM_CA_NAME = "jellyfin-test-ca"  # Android 6 - 10 ask for a name of the CA too
# Entries leading from the security settings to "Encryption & credentials", by Android version
CREDENTIALS_PATH = ("More security & privacy", "More security settings", "Advanced settings", "Advanced",
                    "Encryption & credentials")
SECURITY_ADVANCED = "com.android.settings/com.google.android.settings.security.SecurityAdvancedSettingsActivity"
# Android 6 - 7: "You need to set a lock screen PIN …", 9: "… your device need to have a secure lock screen"
LOCK_REQUIRED = ("//*[contains(@text, 'set a lock screen') or contains(@text, 'Set a screen lock') or "
                 "contains(@text, 'set a screen lock') or contains(@text, 'secure lock screen')]")
DONT_REQUIRE_PIN_AT_START = ("//*[@resource-id='com.android.settings:id/encrypt_dont_require_password' or "
                             "@text='No thanks']")
PASSWORD_ENTRY = "//*[@resource-id='com.android.settings:id/password_entry']"
INSTALLER = "//*[@package='com.android.certinstaller']"
INSTALLER_PASSWORD = "//*[@resource-id='com.android.certinstaller:id/credential_password']"
# Android 6 - 10: credential_name (with the credential use), 11+: certificate_name
INSTALLER_NAME = ("//*[@resource-id='com.android.certinstaller:id/credential_name' or "
                  "@resource-id='com.android.certinstaller:id/certificate_name']")
KEYCHAIN_CHOOSER = "//*[@package='com.android.keychain']"
PROXY_CA = Path(__file__).resolve().parent / "tls-test-proxy/public/ca.pem"


def api_level(d):
    return int(d.native().capabilities.get("deviceApiLevel", 99))


def find_scrolling(d, text):
    """Element with exactly this text, scrolled into view, None when the screen has none."""
    driver = d.native()
    found = driver.find_elements(By.XPATH, f'//*[@text="{text}"]')
    if found:
        return found[0]
    try:
        return driver.find_element(By.ANDROID_UIAUTOMATOR, 'new UiScrollable(new UiSelector().scrollable(true))'
                                   f'.scrollIntoView({ui_text(text)})')
    except WebDriverException:
        return None


def open_credential_settings(d):
    """Settings > … > Encryption & credentials (Android 11+)."""
    # The notification shade open over Settings hid every entry (TLS_API_36 system-ca)
    d.shell("cmd statusbar collapse")
    # A fresh task: otherwise Settings resumes on the screen of the previous install ("Install a certificate").
    # "Encryption & credentials" has no activity of its own, "More security settings" above it has on the Google
    # Settings (API 36), otherwise the way through the security settings.
    out = d.shell(f"am start -W --activity-clear-task -n {SECURITY_ADVANCED}")
    time.sleep(1)
    # Android 17 lists the activity but crashes Settings on it, after "Status: ok" (TLS_API_37)
    resumed = d.shell("dumpsys activity activities | grep topResumedActivity")
    if "Error" in out or SECURITY_ADVANCED.split("/")[1] not in resumed:
        d.shell("am start -W --activity-clear-task -a android.settings.SECURITY_SETTINGS")
    time.sleep(2)
    d.shell("cmd statusbar collapse")
    # One lookup for all entries per screen: a search per entry scrolled the whole list for every missing one
    targets = ("Clear credentials",) + tuple(reversed(CREDENTIALS_PATH))  # deepest first
    xpath = "//*[" + " or ".join(f'@text="{text}"' for text in targets) + "]"
    pattern = "(?i)^(" + "|".join(targets) + ")$"  # letters, spaces and &: nothing to escape
    for _ in range(len(CREDENTIALS_PATH) + 1):
        driver = d.native()
        found = driver.find_elements(By.XPATH, xpath)
        if not found:
            try:
                driver.find_element(By.ANDROID_UIAUTOMATOR, 'new UiScrollable(new UiSelector().scrollable(true))'
                                    f'.setMaxSearchSwipes(10).scrollIntoView(new UiSelector().textMatches("{pattern}"))')
            except WebDriverException:
                raise FlowFailed("no way to Encryption & credentials in the security settings") from None
            found = driver.find_elements(By.XPATH, xpath)
        by_text = {element.text.lower(): element for element in found}
        entry = next(text for text in targets if text.lower() in by_text)
        if entry == "Clear credentials":
            return
        by_text[entry.lower()].click()
        time.sleep(2)
    raise FlowFailed("Encryption & credentials not found")


def set_lock_pin(d):
    """Screen lock setup opened by the installer: PIN, no PIN at start-up (would stop the reboot step), entered twice,
    notifications as they are."""
    deadline = time.time() + 60
    while time.time() < deadline:
        done = d.native().find_elements(By.XPATH, "//*[@text='Done']")  # "Done", "DONE" (Android 7)
        if done:
            done[0].click()
            return
        if d.visible("//*[@text='Continue without fingerprint']"):  # Android 8 with a fingerprint sensor
            d.text("Continue without fingerprint").click()
        elif d.visible(DONT_REQUIRE_PIN_AT_START):  # Secure start-up
            d.find(DONT_REQUIRE_PIN_AT_START)[0].click()
            # Android 6 - 8: a radio button and Continue, 9: a "NO" button that goes on by itself
            time.sleep(1)
            for button in d.native().find_elements(By.XPATH, "//*[@text='Continue' or @text='Next']"):
                button.click()
        elif d.visible(PASSWORD_ENTRY):
            d.find(PASSWORD_ENTRY)[0].send_keys(LOCK_PIN)
            d.native().press_keycode(66)  # Enter: Continue, then OK
            time.sleep(1.5)
        elif d.visible("//*[@text='PIN']"):
            d.text("PIN").click()
        time.sleep(1)
    raise FlowFailed("screen lock setup did not finish")


def handle_lock_prompts(d):
    """The installer requires a screen lock: set the PIN. A set PIN to confirm: enter it. True when it acted."""
    if d.visible(LOCK_REQUIRED):
        d.log("the certificate installer requires a screen lock, setting PIN " + LOCK_PIN)
        d.notes.append(f"screen lock required by the certificate installer (API {api_level(d)})")
        d.by_id("android:id/button1").click()
        set_lock_pin(d)
        return True
    if d.visible(PASSWORD_ENTRY):
        d.log("confirming the screen lock PIN")
        d.find(PASSWORD_ENTRY)[0].send_keys(LOCK_PIN)
        d.native().press_keycode(66)
        time.sleep(1.5)
        return True
    return False


def system_ca_installed(d):
    """Read only: whether the proxy CA is in the user CA store (file name: OpenSSL subject_hash_old). A file with
    content: Android 16 leaves an empty file behind after "Clear credentials" (cacerts-added/df2ca97a.0, 0 bytes,
    "Clear credentials" disabled, TLS_API_36 system-ca)."""
    name = subprocess.run(["openssl", "x509", "-subject_hash_old", "-noout", "-in", str(PROXY_CA)],
                          capture_output=True, text=True, check=True).stdout.strip() + ".0"
    return "yes" in d.shell(f"su 0 sh -c 'test -s /data/misc/user/0/cacerts-added/{name} && echo yes' 2>/dev/null")


def system_credentials_installed(d):
    """Read only, Android 6 - 10: keys and certificates installed by the certificate installer, files of the system
    (VPN and apps) or Wi-Fi uid in the credential storage, or the proxy CA in the user CA store. Keys of apps (their
    own uid) do not count, a reset leaves them. Neither does the key of the screen lock (Android 9:
    1000_USRPKEY_synthetic_password_…)."""
    files = d.shell("su 0 ls /data/misc/keystore/user_0 2>/dev/null").split()
    return system_ca_installed(d) or any(re.match(r"(1000|1010)_(USRPKEY|USRCERT|CACERT)_(?!synthetic_password_)", f)
                                         for f in files)


def install_system_certificate(d, name):
    """Installs Download/jellyfin-tls/proxy/<name> into the system store: ca.pem as CA, a .p12 (password jellyfin)
    as client key for VPN and apps."""
    is_key = name.endswith(".p12")
    d.log(f"installing {name} into the system store")
    if api_level(d) >= 30:
        open_credential_settings(d)
        find_scrolling(d, "Install a certificate").click()
        d.text("VPN & app user certificate" if is_key else "CA certificate").click()
        if not is_key:
            d.find("//*[@text='Install anyway']", what='"Install anyway"')[0].click()
    else:
        d.shell("am start -a android.credentials.INSTALL")
    # The document picker, a lock prompt can come first
    deadline = time.time() + 30
    while not d.visible("//*[contains(@package, 'documentsui')]"):
        if not handle_lock_prompts(d) and time.time() > deadline:
            raise FlowFailed("no document picker from the certificate installer")
        time.sleep(1)
    diag(d, f"install {name} picker open")  # TEMP
    pick_file(d, name)
    diag(d, f"install {name} file picked")  # TEMP

    # Password, name and lock prompts until the installer is gone (twice in a row: it switches activities)
    deadline, gone = time.time() + 90, 0
    while gone < 2:
        if time.time() > deadline:
            raise FlowFailed(f"certificate installer did not finish {name}")
        time.sleep(1)
        if handle_lock_prompts(d):
            gone = 0
            continue
        password = d.native().find_elements(By.XPATH, INSTALLER_PASSWORD)
        names = d.native().find_elements(By.XPATH, INSTALLER_NAME)
        if password:
            password[0].send_keys("jellyfin")
            d.by_id("android:id/button1").click()
            gone = 0
        elif names:
            if not names[0].text:
                names[0].send_keys(SYSTEM_CA_NAME)
            d.log(f"name: {names[0].text}")
            # Looked up again: the dialog moves when the keyboard closes
            d.by_id("android:id/button1").click()
            gone = 0
        elif d.visible(INSTALLER):
            # Still working, or an error such as "Install CA certificates in Settings"
            message = d.native().find_elements(By.XPATH, f"{INSTALLER}[@resource-id='android:id/message']")
            if message:
                raise FlowFailed(f"certificate installer: {message[0].text}")
            gone = 0
        else:
            gone += 1
    diag(d, f"install {name} installer gone")  # TEMP
    if not is_key:
        # The file can appear a moment after the installer left the screen (TLS_API_36 system-ca)
        deadline = time.time() + CA_FILE_WAIT
        while not system_ca_installed(d):
            if time.time() > deadline:
                d.log("DIAG CA store: " + d.shell("su 0 sh -c 'ls -la /data/misc/user/0/cacerts-added/' 2>&1").strip())  # TEMP
                raise FlowFailed("the CA is not in the user CA store after the installer finished")
            time.sleep(1)
    d.log(f"{name} installed")


# Files of the user CA store: Android 7+ (API 24+) per user, Android 6 (API 23) one directory for all
USER_CA_DIRS = ("/data/misc/user/0/cacerts-added", "/data/misc/keychain/cacerts-added")
CA_FILE_WAIT = 10  # seconds for the CA file after the certificate installer is gone
STORE_SETTLE = 3  # seconds after the credentials are gone: the trust store broadcast reaches the running apps


def user_cas(d):
    """CA files with content in the user CA store (as root over adb), None without root: not checkable. Android 16 leaves
    an empty file behind after Clear credentials (see system_ca_installed): not counted."""
    found = []
    for directory in USER_CA_DIRS:
        out = d.shell(f"su 0 find {directory} -type f -size +0c 2>&1").strip()
        if "No such file" in out:
            continue
        if out and not out.startswith("/"):  # "su: …", "find: not found", permission denied
            return None
        found += [line.rsplit("/", 1)[-1] for line in out.splitlines() if line.startswith("/")]
    return found


def wait_user_ca_store_empty(d, timeout=20):
    """After Clear credentials: waits (up to timeout s) until the user CA directory is empty over adb, then lets the trust
    store broadcast settle (STORE_SETTLE s). Files the credential UI does not know are removed directly. A CA left
    over made missing-ca fail (TLS_API_36); the UI saying "empty" was no proof."""
    start = time.time()
    left = user_cas(d)
    if left is None:
        d.log("user CA store not checkable over adb (no root)")
        time.sleep(STORE_SETTLE)
        return
    while left and time.time() - start < timeout:
        time.sleep(1)
        left = user_cas(d)
    if left:
        d.log(f"user CA store still has {', '.join(left)} after {timeout} s, removing the files")
        for directory in USER_CA_DIRS:
            d.shell(f"su 0 sh -c 'rm -f {directory}/*'")
        left = user_cas(d)
        if left:
            raise FlowFailed(f"the user CA store still has {', '.join(left)} after clearing")
    time.sleep(STORE_SETTLE)
    d.log(f"user CA store verified empty over adb ({time.time() - start:.0f} s)")


def clear_system_credentials(d):
    """Clear credentials, then the user CA store is checked over adb and given time to settle."""
    clear_system_credentials_ui(d)
    wait_user_ca_store_empty(d)


def clear_system_credentials_ui(d):
    """Removes every user CA and key and checks that the store is empty: "Clear credentials" is disabled then. The
    proxy CA alone was no proof: in system-client only the key is in the store, and on API 28 the confirmation once
    closed by itself (the Appium server restarted) while the key stayed. One more try, then the step fails."""
    for attempt in range(2):
        if clear_system_credentials_once(d) == "empty":
            return
        if api_level(d) < 30:
            # "Clear credentials" stays enabled on Android 7 - 10 after the reset (the storage keeps its master key
            # while a screen lock exists), the second reset then closed without a confirmation: check the files
            if not system_credentials_installed(d):
                d.log("system credentials cleared (no user keys or certificates in the credential storage)")
                return
        else:
            open_credential_settings(d)
            if find_scrolling(d, "Clear credentials").get_attribute("enabled") != "true":
                d.log("system credentials cleared (Clear credentials is disabled now)")
                return
        d.log("the system store still has credentials after clearing" + (", once more" if not attempt else ""))
    raise FlowFailed("the system store still has credentials after clearing twice")


def clear_system_credentials_once(d):
    """Settings > Clear credentials (Android 11+, the reset intent is ignored there), the reset intent of the
    credential storage before. "empty" when there was nothing to clear."""
    if api_level(d) >= 30:
        open_credential_settings(d)
        entry = find_scrolling(d, "Clear credentials")
        if entry.get_attribute("enabled") != "true":
            d.log("system credentials already empty")
            return "empty"
        entry.click()
    elif api_level(d) < 29 and not has_lock_pin(d):
        # Android 6 - 9: the credential storage needs a screen lock, without one it is empty. The reset still asked
        # for a password there after the PIN was removed: Settings crashed on it (Android 7, NullPointerException in
        # checkPassword) or refused every PIN (Android 9)
        d.log("no screen lock, the credential storage is empty")
        return "empty"
    else:
        d.shell("am start -a com.android.credentials.RESET")
    deadline, prompts = time.time() + 20, 0
    while not d.visible("//*[@text='Remove all the contents?']"):
        if d.visible(LOCK_REQUIRED):
            # No screen lock: the credential storage is not set up, nothing to clear (Android 9 asks for a lock first)
            d.by_id("android:id/button2").click()
            d.log("credential storage not set up (no screen lock), nothing to clear")
            return "empty"
        prompts += handle_lock_prompts(d)
        if prompts > 3:  # the PIN is refused, the prompt came back
            raise FlowFailed("screen lock prompt keeps coming back, PIN refused")
        if time.time() > deadline + 60 * prompts:  # setting a PIN takes up to a minute
            raise FlowFailed('no "Remove all the contents?" confirmation')
        time.sleep(1)
    d.by_id("android:id/button1").click()
    # Android 7 - 9 ask for the PIN after the confirmation and only then clear the storage. Waiting for the CA alone
    # was not enough: system-client has only the key in the store, the check ran before the PIN and reset again
    store_filled = system_credentials_installed if api_level(d) < 30 else system_ca_installed
    deadline, prompts = time.time() + 20, 0
    while store_filled(d):
        prompts += handle_lock_prompts(d)
        if prompts > 3:
            raise FlowFailed("screen lock prompt keeps coming back, PIN refused")
        if time.time() > deadline + 60 * prompts:  # setting a PIN takes up to a minute
            raise FlowFailed("the system store still has credentials after the reset was confirmed")
        time.sleep(1)


def has_lock_pin(d):
    """locksettings verifies an empty credential only without a lock. Otherwise (Android 6 - 7 have no locksettings,
    Android 8.0 no verify command): the screen lock settings ask for the PIN first when one is set."""
    if api_level(d) >= 26:
        out = d.shell("locksettings verify")
        if "verified successfully" in out:
            return False
        if "didn't match" in out:
            return True
    d.log("screen lock: asking the screen lock settings")
    d.shell("am start -W -a android.app.action.SET_NEW_PASSWORD")
    # Either the PIN to confirm or, without a lock, the list of lock types (Android 8 with a fingerprint sensor: the
    # fingerprint choices first): 2 s were not always enough (API 24)
    d.find(f"{PASSWORD_ENTRY} | //*[@text='Swipe' or @text='None' or @text='Continue without fingerprint']",
           timeout=15, what="PIN or screen lock types")
    present = d.visible(PASSWORD_ENTRY)
    d.native().back()
    if present and d.visible(PASSWORD_ENTRY):  # the first Back only closed the keyboard
        d.native().back()
    return present


def remove_lock_pin(d):
    """Removes the PIN set by set_lock_pin, nothing when there is none. Android 8+ with locksettings, before through
    the screen lock settings (needs the credentials cleared first, "None" is disabled while there are any)."""
    if api_level(d) >= 26:
        out = d.shell(f"locksettings clear --old {LOCK_PIN}")
        d.log("PIN removed" if "cleared" in out.lower() else "no PIN to remove")
        return
    d.shell("am start -W -a android.app.action.SET_NEW_PASSWORD")
    time.sleep(2)
    if not d.visible(PASSWORD_ENTRY):
        d.native().back()
        d.log("no PIN to remove")
        return
    d.find(PASSWORD_ENTRY)[0].send_keys(LOCK_PIN)
    d.native().press_keycode(66)
    d.text("None", timeout=15).click()
    d.find("//*[starts-with(@text, 'Yes')]", what='"Yes, remove"')[0].click()
    time.sleep(1)
    d.log("PIN removed")


def choose_system_key(d):
    """KeyChain chooser opened by the app on Connect: picks the installed key."""
    d.find(KEYCHAIN_CHOOSER, timeout=30, what="system certificate chooser")
    d.log("system certificate chooser shown")
    d.text(SYSTEM_KEY_ALIAS).click()
    d.find(f"{KEYCHAIN_CHOOSER}[@resource-id='android:id/button1']", what='"Allow" / "Select"')[0].click()
    d.log(f"chose {SYSTEM_KEY_ALIAS}")


def diag(d, label):  # TEMP
    """Temporary diagnosis of the system store steps on API 34: state of the app and the device, in the log and in
    logcat (tag E2E-DIAG), so the lines can be matched with the system log."""
    try:
        top = d.shell("dumpsys activity activities | grep -m1 topResumedActivity").strip()
        top = top.split()[-2] if top else "?"
        webview = d.shell("dumpsys webviewupdate | grep -m1 'Current WebView package'").strip().split("(name, version): ")[-1]
        text = f"DIAG {label}: top={top} webview={webview}"  # no contexts: listing them can take 20 s
    except Exception as e:  # never fails the step
        text = f"DIAG {label}: {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}"
    d.log(text)
    d.shell(f"log -t E2E-DIAG '{label}'")
    try:  # screen at this point, plain adb: the Appium session is not touched
        out = Path("/tmp/diag-34")
        out.mkdir(exist_ok=True)
        name = f"{time.strftime('%H%M%S')}-{label.replace(' ', '_')}.png"
        png = subprocess.run([ADB, "-s", d.serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
        (out / name).write_bytes(png)
    except Exception as e:
        d.log(f"DIAG screenshot skipped: {type(e).__name__}")


def logged_in(d, timeout=10):
    """jellyfin-web of the app has a logged-in session that reaches the server."""
    try:
        return web_request(d) == "ok" if d.web(timeout=timeout).execute_script(
            "return !!(window.ApiClient && ApiClient.getCurrentUserId())") else False
    except (FlowFailed, WebDriverException):
        return False


def flow_ensure_connected(d):
    """Start state of the steps after login: the app connected with CA and client certificate, jellyfin-web logged in
    and reaching the server. A step that failed in the middle can leave the app elsewhere (the certificates screen,
    the connect screen, without a certificate): then the state is restored, app data cleared, CA and client
    certificate from the URL, login, noted in the report. So one failed step does not fail every later one (missing-ca
    took 7 steps with it, TLS_API_28)."""
    driver = d.native()
    with d.no_idle_wait():
        stop_player(d)
    if driver.current_package != APP_ID:
        driver.activate_app(APP_ID)
        time.sleep(2)
    for _ in range(3):  # out of the certificates screen
        if "CertificatesActivity" not in (driver.current_activity or ""):
            break
        driver.back()
        time.sleep(1)
    if logged_in(d):
        return
    d.log("not connected and logged in: restoring (app data cleared, certificates from the URL, login)")
    d.notes.append("start state restored before the step: the step before left the app disconnected")
    d.close()
    kill_app(d)  # pm clear kills the app too, in the foreground it took the emulator with it (kill_app)
    d.shell(f"pm clear {APP_ID}")
    start_app(d)
    enter_address(d)
    open_url_import(d)
    stage_url_certificate(d, "ca.pem")
    stage_url_certificate(d, "client-ec.p12")
    d.text("Connect").click()
    wait_login_page(d, 60)
    flow_login(d)
    d.log("start state restored")


def start_app(d):
    d.close()
    d.native().activate_app(APP_ID)
    d.text("Connect", timeout=60)


def check_client_certificate_from_system(d):
    """Certificates screen: the client certificate is the system key, not an imported copy."""
    open_certificates_screen(d)
    client = client_certificate_text(d)
    if not client.lower().startswith("from system store"):
        raise FlowFailed(f"client certificate not from the system store: {client!r}")
    d.log(f"client certificate: {client}")
    leave_certificates_screen(d)


def flow_system_ca(d):
    """CA only in the system store, installed while the app shows the "not trusted" error, client certificate from a
    file in the app. Connect works again without restarting the app (trust store broadcast)."""
    clear_system_credentials(d)
    start_app(d)
    enter_address(d)
    d.text("Advanced", contains=True).click()
    stage_file_certificate(d, "client-ec.p12")
    d.text("Connect").click()
    message = d.find("//*[contains(@text, 'not trusted')]", timeout=30, what='"not trusted" message')[0].text
    d.log("before: " + message.splitlines()[0])
    install_system_certificate(d, "ca.pem")
    d.native().activate_app(APP_ID)
    d.text("Connect", timeout=20).click()
    wait_login_page(d, 60)
    flow_login(d)


def flow_system_ca_client(d):
    """CA and client key in the system store, nothing imported in the app: Connect opens the chooser by itself."""
    diag(d, "system-ca-client start")  # TEMP
    clear_system_credentials(d)
    install_system_certificate(d, "ca.pem")
    install_system_certificate(d, "client-ec.p12")
    diag(d, "system-ca-client certificates installed")  # TEMP
    start_app(d)
    enter_address(d)
    d.text("Connect").click()
    choose_system_key(d)
    diag(d, "system-ca-client key chosen")  # TEMP
    wait_login_page(d, 60)
    flow_login(d)
    check_client_certificate_from_system(d)
    diag(d, "system-ca-client end")  # TEMP


def flow_system_client(d):
    """Client key only in the system store, CA from a file in the app: Connect opens the chooser by itself."""
    diag(d, "system-client start")  # TEMP
    clear_system_credentials(d)
    install_system_certificate(d, "client-ec.p12")
    if system_ca_installed(d):
        raise FlowFailed("the proxy CA is in the user CA store, the step would not test the imported CA")
    diag(d, "system-client key installed")  # TEMP
    start_app(d)
    enter_address(d)
    d.text("Advanced", contains=True).click()
    stage_file_certificate(d, "ca.pem")
    d.text("Connect").click()
    choose_system_key(d)
    diag(d, "system-client key chosen")  # TEMP
    wait_login_page(d, 60)
    flow_login(d)
    check_client_certificate_from_system(d)
    diag(d, "system-client end")  # TEMP


def flow_system_key_removed(d):
    """After system-client (CA imported, client certificate = the system key client-ec): the key is removed from the
    system store (Clear credentials) while the app still refers to it. After an app restart Connect must explain
    that the client certificate is no longer available, not show a generic error."""
    diag(d, "system-key-removed start")  # TEMP
    clear_system_credentials(d)
    diag(d, "system-key-removed credentials cleared")  # TEMP
    d.close()  # the WebView of the session goes away with the app
    kill_app(d)
    d.native().activate_app(APP_ID)
    d.text("Connect", timeout=60).click()
    diag(d, "system-key-removed Connect clicked")  # TEMP
    message = "//*[contains(@text, 'no longer available')]"
    deadline = time.time() + 30
    last_diag = time.time()  # TEMP
    while time.time() < deadline:
        if time.time() - last_diag >= 8:  # TEMP
            last_diag = time.time()
            diag(d, "system-key-removed waiting")
        if d.visible(KEYCHAIN_CHOOSER):  # Android 10+ may offer the chooser, there is no key to choose
            d.log("system certificate chooser shown, cancelling")
            d.native().back()
        if d.visible(message):
            text = d.find(message)[0].text
            d.log("message: " + next(line.strip() for line in text.splitlines() if "no longer available" in line.lower()))
            return
        time.sleep(1)
    shown = [e.text for e in d.native().find_elements(By.XPATH, "//*[contains(@text, 'certificate')]")]
    raise FlowFailed(f'no "client certificate is no longer available" message ({shown})')


def flow_system_choose(d):
    """After system-key-removed: client-ec back into the system store, then on the connect screen Advanced >
    "Choose from system store…" by hand (not the chooser the app opens after a failed connect), Connect."""
    diag(d, "system-choose start")  # TEMP
    install_system_certificate(d, "client-ec.p12")
    d.close()  # ends the session, d.native() starts a new one
    driver = d.native()
    driver.activate_app(APP_ID)
    diag(d, "system-choose app activated")  # TEMP
    # "Advanced ▼" collapsed, "Advanced ▲" open: the button can be open but below the screen, a click would close it
    advanced = d.text("Advanced", contains=True, timeout=30)
    if "▼" in advanced.text:
        advanced.click()
    hide_keyboard(driver)
    button = "//*[@text='Choose from system store…']"
    time.sleep(1)  # the layout settles after Advanced opened and the keyboard closed
    if not d.visible(button):
        try:
            driver.find_element(By.ANDROID_UIAUTOMATOR, 'new UiScrollable(new UiSelector().scrollable(true))'
                                f'.scrollIntoView({ui_text("Choose from system store…")})')
        except NoSuchElementException:
            pass  # the screen may not scroll (everything fits), the lookup below decides
    d.find(button, what='"Choose from system store…"')[0].click()
    choose_system_key(d)
    d.text("(new)", contains=True, timeout=20)
    d.log("system key staged by hand")
    d.text("Connect").click()
    diag(d, "system-choose Connect clicked")  # TEMP
    # Logged in before: home screen, otherwise the login page
    d.web_wait(f"{MY_MEDIA} || document.querySelector('#txtManualName')", 60, "jellyfin-web after connecting")
    if not d.web().execute_script(f"return {MY_MEDIA}"):
        flow_login(d)
    check_client_certificate_from_system(d)


def device_time(d):
    """Device clock in the format of logcat -T."""
    return d.shell("date '+%m-%d %H:%M:%S.000'").strip()


def connect_without_client_certificate(d):
    """Connect without a client certificate: the "requires a client certificate" message. Android 10+ (keys without a
    screen lock): the app also opens the system certificate chooser, cancelled here. Android shows it only when there
    is a key ("KeyChain: No keys to choose from" otherwise), so the request is checked in the log. Android 9 and older
    without a screen lock: only the message, the chooser would only ask to set a lock screen. Fails on that prompt."""
    since = device_time(d)
    d.text("Connect", timeout=60).click()
    message = "//*[contains(@text, 'requires a client certificate')]"
    # The message comes first, the app opens the chooser right after: watch a while longer once it is there
    deadline, settle, chooser = time.time() + 30, None, False
    while time.time() < deadline:
        if d.visible(LOCK_REQUIRED):
            d.by_id("android:id/button2").click()  # Cancel
            raise FlowFailed('Android asks to "set a lock screen" instead of choosing a certificate')
        if d.visible(KEYCHAIN_CHOOSER):
            d.log("system certificate chooser shown, cancelling")
            chooser = True
            d.native().back()
        elif d.visible(message):
            settle = settle or time.time() + 5
            if time.time() > settle:
                text = d.find(message)[0].text
                d.log("message: " + next(line for line in text.splitlines() if "client certificate" in line.lower()))
                requested = "keychain.CHOOSER" in d.shell(f"logcat -d -T '{since}' | grep -F keychain.CHOOSER")
                d.log(f"system certificate chooser: {'shown' if chooser else 'requested' if requested else 'none'}")
                if not requested and api_level(d) >= 29:
                    raise FlowFailed("the app did not open the system certificate chooser (Android 10+)")
                return
        time.sleep(1)
    raise FlowFailed('neither the chooser nor the "requires a client certificate" message')


def flow_system_no_lock(d):
    """No screen lock, empty system store, CA imported in the app, no client certificate: Connect must show the
    system certificate chooser (cancelled here) or the "requires a client certificate" message, not Android's "set a
    lock screen" prompt (older Android needs a lock for the credential storage, KeyChain asks for one instead of
    choosing)."""
    clear_system_credentials(d)
    remove_lock_pin(d)
    start_app(d)
    enter_address(d)
    d.text("Advanced", contains=True).click()
    stage_file_certificate(d, "ca.pem")
    connect_without_client_certificate(d)


def flow_system_cleanup(d):
    """Clears the system credentials and removes the PIN of the installer steps."""
    clear_system_credentials(d)
    remove_lock_pin(d)


def flow_stop_player(d):
    with d.no_idle_wait():
        stop_player(d)


def wait_home(d, timeout):
    """True when jellyfin-web shows the home screen, False when the app fell back to the connect screen with the
    connection error."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if d.visible(CONNECT_ERROR):
            return False
        try:
            if d.web(timeout=2).execute_script(f"return {MY_MEDIA}"):
                return True
        except (FlowFailed, WebDriverException):
            pass  # no WebView yet or the page is navigating
        time.sleep(1)
    raise FlowFailed(f'"My Media" not shown after {timeout}s')


def flow_home(d):
    """Logged in without importing again (after app restart or reboot). Launched right after a boot, the cold WebView
    plus jellyfin-web can take longer than the app's 10 s initial connection timeout
    (Constants.INITIAL_CONNECTION_TIMEOUT): the app then shows its connect screen with "Connection cannot be
    established". One relaunch, noted in the report."""
    if not wait_home(d, 60):
        d.log("connection error, relaunching the app once")
        d.notes.append("first launch hit the app's 10 s initial connection timeout, connected after a relaunch")
        d.close()  # the WebView of the session goes away with the app
        kill_app(d)
        d.native().activate_app(APP_ID)
        if not wait_home(d, 60):
            raise FlowFailed('"Connection cannot be established" again after relaunching the app')
    d.log("home screen")


FLOWS = {name[5:].replace("_", "-"): function for name, function in globals().items() if name.startswith("flow_")}


def run(d, action, flows=None):
    """Runs "flow [KEY=VALUE …]" of flows (default: FLOWS of this module, tv_flows passes its own). Returns "" or the
    reason it failed."""
    flows = flows or FLOWS
    name, *params = action.split()
    if name not in flows:
        return f"unknown flow {name} (known: {', '.join(flows)})"
    kwargs = {key.lower(): value for key, _, value in (param.partition("=") for param in params)}
    d.reopen_if_lost()
    for attempt in (1, 2):
        try:
            dismiss_got_it(d)
            flows[name](d, **kwargs)
            return ""
        except StaleElementReferenceException as error:
            # The screen was redrawn between finding an element and using it (TLS_API_26 reimport, the checkbox of
            # "Load certificates from URL"): the flow once more, all flows start by looking at the screen
            if attempt == 1:
                d.log(f"{name}: an element changed under the flow, once more")
                time.sleep(1.5)
                continue
            return f"{name}: {type(error).__name__}: {(error.msg or '').splitlines()[0][:200]}"
        except FlowFailed as error:
            return f"{name}: {error}"
        except WebDriverException as error:
            return f"{name}: {type(error).__name__}: {(error.msg or '').splitlines()[0][:200]}"
        except Exception as error:  # a bug in a flow: fail the step, not the whole run (an AVD could keep the PIN)
            d.log(traceback.format_exc().rstrip())
            return f"{name}: {type(error).__name__}: {error}"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    device = Device(sys.argv[1], os.environ.get("APPIUM_URL", "http://127.0.0.1:4724"),
                    log=lambda msg: print(f"  {msg}"))
    try:
        reason = run(device, " ".join(sys.argv[2:]))
    finally:
        device.close()
    print(reason or "passed")
    sys.exit(1 if reason else 0)
