"""Clicking automation of checklists/tv.md ("> appium <flow> [KEY=VALUE …]") for jellyfin-androidtv, the counterpart of
appium_flows.py (mobile app). Shares its Device, FlowFailed and run(); see TV-AUTOMATION.md for the design.

The TV app is native (no WebView), everything goes through UiAutomator2. The D-pad is part of what is tested: elements
are focused with key presses (focus()) and activated with DPAD_CENTER, not clicked. The TV images have no document
picker, certificates come from the proxy's file server (IMPORT_URL).

Standalone, against a running emulator and Appium server (manual-test.py starts both):
  python3 tv_flows.py emulator-5590 setup
  python3 tv_flows.py emulator-5590 play CARD=1 QUALITY=low
"""
import contextlib
import html
import os
import re
import sys
import time

from appium.webdriver.common.appiumby import AppiumBy as By
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

import appium_flows
from appium_flows import CA_ONLY_SERVER, IMPORT_URL, SERVER, Device, FlowFailed

APP_ID = "org.jellyfin.androidtv.debug"
ID = f"{APP_ID}:id/"
ENDPOINT = SERVER.removeprefix("https://")
KEYCODES = dict(BACK=4, DPAD_UP=19, DPAD_DOWN=20, DPAD_LEFT=21, DPAD_RIGHT=22, DPAD_CENTER=23, MEDIA_PAUSE=127)
# Focusable containers of other focusable elements, never the element looked for
SCROLL_CONTAINER = ("@scrollable='true' or self::android.widget.ScrollView or self::android.widget.ListView or "
                    "self::android.widget.GridView or self::androidx.recyclerview.widget.RecyclerView")
# Default way to search an element: down the screen, then up again
SWEEP = ("DPAD_DOWN",) * 12 + ("DPAD_UP",) * 24

# Server add screen (ServerAddFragment, CertificateRowsView: the rows have no ids)
LOAD_FROM_URL = "//android.widget.CheckBox[@text='Load certificates from URL (network)']"
CHOOSE = dict(
    ca="@text='Choose…' and preceding::*[@text='CA certificate'] and not(preceding::*[@text='Client certificate'])",
    client="@text='Choose…' and preceding::*[@text='Client certificate']",
)
# Text field of an AlertDialog (URL, password)
DIALOG_FIELD = "//*[@resource-id='android:id/custom']//android.widget.EditText"
DIALOG_MESSAGE = "//*[@resource-id='android:id/message']"
SERVER_SCREEN = f"//*[@resource-id='{ID}users' or @resource-id='{ID}add_user_button']"
HOME = f"//*[@resource-id='{ID}row_header']"
PLAYER = f"//*[@resource-id='{ID}exoPlayerView']"
POSITION = f"//*[@resource-id='{ID}current_time']"
CERTIFICATES_SCREEN = "//*[@text='Trusted certificate authorities']"
CLIENT_LINE = ("//*[starts-with(@text, 'Imported') or starts-with(@text, 'From system store') or "
               "@text='No client certificate.']")


# --- D-pad -------------------------------------------------------------------------------------------------------------

def key(d, name, pause=0.4):
    """One key press, a short pause for the focus change or animation. Through "input keyevent", not Appium's
    press_keycode: after send_keys into a text field its D-pad presses did not move the focus any more (the same
    keys from adb did)."""
    d.shell(f"input keyevent {KEYCODES[name]}")
    time.sleep(pause)


def focused(d):
    """The focused element as text for messages."""
    elements = d.native().find_elements(By.XPATH, "//*[@focused='true']")
    if not elements:
        return "nothing"
    try:
        element = elements[-1]
        label = (element.get_attribute("text") or element.get_attribute("content-desc")
                 or element.get_attribute("resource-id").rpartition("/")[2])
        return f"{element.get_attribute('class').rpartition('.')[2]} {label!r}"
    except StaleElementReferenceException:
        return "? (changed)"


def focus(d, target, keys=SWEEP, what=None):
    """Presses the keys one after the other until the focused element is the one matching the XPath condition target
    (e.g. "@text='Connect'") or contains it: Compose and Leanback put the focus on a container around the text. Not
    a focused list or scroll view around it (the certificates screen starts with the focus on its ScrollView). In a
    focused ListView (the lists of dialogs) the D-pad moves the selection, the selected entry counts."""
    xpath = (f"//*[@focused='true'][not({SCROLL_CONTAINER})][descendant-or-self::*[{target}]] | "
             f"//android.widget.ListView[@focused='true']//*[@selected='true'][descendant-or-self::*[{target}]]")
    for name in keys:
        if d.visible(xpath):
            return
        key(d, name)
    if d.visible(xpath):
        return
    raise FlowFailed(f"{what or target} not reachable with the D-pad (focus on {focused(d)})")


def select(d, target, keys=SWEEP, what=None):
    """Focuses the element with the D-pad and presses DPAD_CENTER."""
    focus(d, target, keys, what)
    key(d, "DPAD_CENTER", pause=1)


def keyboard_shown(d):
    """The on-screen keyboard is up (mIsInputViewShown stays true after it closed)."""
    return "mInputShown=true" in d.shell("dumpsys input_method")


def hide_keyboard(d):
    """Closes the on-screen keyboard when it is shown: with it, the D-pad moves in the keyboard. Back only then, an
    extra Back leaves the screen."""
    for _ in range(2):
        if not keyboard_shown(d):
            return
        key(d, "BACK", pause=1)
    if keyboard_shown(d):
        raise FlowFailed("the on-screen keyboard does not close with Back")


def type_into(d, xpath, text, what):
    """Sets the text of the field (UiAutomator2 sets it directly, no on-screen keyboard typing), closes the keyboard.
    The last match: the field of the dialog on top."""
    for attempt in range(3):
        try:
            d.find(xpath, timeout=15, what=what)[-1].send_keys(text)
            break
        except StaleElementReferenceException:  # the dialog was redrawn
            if attempt == 2:
                raise
            time.sleep(1)
    time.sleep(0.5)
    hide_keyboard(d)


def confirm_dialog(d, what="OK"):
    """The positive button of the AlertDialog on top, by D-pad: below the field or list, at the right."""
    select(d, "@resource-id='android:id/button1'", ("DPAD_DOWN", "DPAD_RIGHT") * 5, f'"{what}" of the dialog')


def dialog_message(d, timeout=20):
    return d.find(DIALOG_MESSAGE, timeout=timeout, what="message dialog")[0].text


def back_until(d, xpath, what, presses=6):
    """Back until the screen shows the element. Waits for it after every press: one Back too many leaves the app."""
    for _ in range(presses):
        if d.visible(xpath):
            return
        key(d, "BACK", pause=0.5)
        deadline = time.time() + 3
        while time.time() < deadline:
            if d.visible(xpath):
                return
            time.sleep(0.5)
    d.find(xpath, timeout=5, what=what)


def go_home(d):
    """Back to the home screen rows (from the player, an item, the library or the settings)."""
    back_until(d, HOME, "home screen", presses=8)


# --- Connect and log in ------------------------------------------------------------------------------------------------

def stage_from_url(d, kind, name, password="jellyfin"):
    """Server add screen with Advanced open: "Choose…" of the CA (kind ca) or client certificate row, the URL of the
    proxy's file server, the password for a .p12. The row then says "<name> (new)"."""
    row = "CA certificate" if kind == "ca" else "Client certificate"
    select(d, CHOOSE[kind], what=f'"Choose…" of "{row}"')
    type_into(d, DIALOG_FIELD, f"{IMPORT_URL}/{name}", "URL field")
    confirm_dialog(d)
    finish_staging(d, kind, name, password, IMPORT_URL)


MATERIAL_FILES = "me.zhanghai.android.files"


def pick_file(d, name):
    """Picks Download/jellyfin-tls/proxy/<name> in the file picker the app opened: Material Files on the TV AVDs
    (tv-picker.md, it opens at the storage root or in the last folder), the system document picker on a phone."""
    d.find(f"//*[@package='{MATERIAL_FILES}' or @package='com.google.android.documentsui' or "
           "@package='com.android.documentsui']", timeout=20, what="file picker")
    if not d.visible(f"//*[@package='{MATERIAL_FILES}']"):
        appium_flows.pick_file(d, name)
        return
    entry = f"//*[@resource-id='{MATERIAL_FILES}:id/nameText' and @text='{{}}']"
    for part in ("Download", "jellyfin-tls", "proxy"):
        if d.visible(entry.format(name)):
            break
        if d.visible(entry.format(part)):
            d.find(entry.format(part), what=part)[0].click()
            time.sleep(1)
    d.find(entry.format(name), what=f"{name} in Material Files")[0].click()


def stage_from_file(d, kind, name, password="jellyfin"):
    """As stage_from_url on a device with a document picker (tv-phone.md): "Choose…" opens the system file picker,
    Download/jellyfin-tls/proxy/<name> is picked by touch (the picker is not part of the app)."""
    row = "CA certificate" if kind == "ca" else "Client certificate"
    select(d, CHOOSE[kind], what=f'"Choose…" of "{row}"')
    pick_file(d, name)
    finish_staging(d, kind, name, password, "the file picker")


def decline_contained_ca(d, name):
    """The app offers to trust the client CA contained in client-ec.p12, it did not issue the server certificate:
    "Don't trust" with the D-pad (left of "Trust")."""
    appium_flows.contained_ca_offer(d, name)
    select(d, "@resource-id='android:id/button2'", ("DPAD_DOWN", "DPAD_LEFT") * 3, "\"Don't trust\" of the offer")


def finish_staging(d, kind, name, password, source):
    """The password for a .p12, then the row says "<name> (new)"."""
    row = "CA certificate" if kind == "ca" else "Client certificate"
    if kind == "client":
        d.find(f"//*[@text='Enter the password for {name}']", timeout=20, what="password dialog")
        type_into(d, DIALOG_FIELD, password, "password field")
        confirm_dialog(d)
        decline_contained_ca(d, name)
    staged = d.find(f"//*[@text='{name} (new)'] | {DIALOG_MESSAGE}", timeout=20, what=f'"{name} (new)"')[0]
    if staged.get_attribute("resource-id") == "android:id/message":
        raise FlowFailed(f"{name}: {staged.text}")
    d.log(f"{row}: {name} from {source}")


def flow_setup(d, source="url", ca_only="0", client="file"):
    """Welcome screen > Enter server address, address, Advanced, CA and client certificate from the URL (the TV
    images have no file picker), Connect, all with the D-pad. The user selection of the server appears. SOURCE=file
    (a device with a file picker: tv-phone.md, tv-picker.md): "Load certificates from URL" is unchecked, the
    certificates come from the file picker. CA_ONLY=1: the worker's CA-only port (no client certificate required),
    only the CA: a server with its own CA without mutual TLS. CLIENT=system (tv-phone.md, the key installed by
    system-client): the client certificate from "Choose from system store…" below the client row."""
    select(d, f"@resource-id='{ID}enter_server_address'", what='"Enter server address"')
    focus(d, f"@resource-id='{ID}address'", what="address field")
    type_into(d, f"//*[@resource-id='{ID}address']", CA_ONLY_SERVER if ca_only == "1" else SERVER, "address field")
    select(d, f"@resource-id='{ID}advanced'", ("DPAD_DOWN", "DPAD_RIGHT") * 2, '"Advanced"')
    # The app checks for a document picker, there is none on the TV images: loading from a URL is the default
    from_url = d.find(LOAD_FROM_URL, what='"Load certificates from URL"')[0].get_attribute("checked") == "true"
    if source == "file" and from_url:
        raise FlowFailed('"Load certificates from URL" checked although the device has a document picker')
    if source != "file" and not from_url:
        raise FlowFailed('"Load certificates from URL" not checked although the device has no document picker')
    stage = stage_from_file if source == "file" else stage_from_url
    stage(d, "ca", "ca.pem")
    if ca_only != "1" and client == "system":
        select(d, "@text='Choose from system store…'", ("DPAD_DOWN",) * 6, '"Choose from system store…"')
        appium_flows.choose_system_key(d)
        name = appium_flows.SYSTEM_KEY_ALIAS
        d.find(f"//*[@text='{name} (new)']", timeout=20, what=f'"{name} (new)"')
        d.log(f"Client certificate: {name} from the system store")
    elif ca_only != "1":
        stage(d, "client", "client-ec.p12")
    # Back from a file picker the focus can be on the address field above "Connect" (TLS_TV_30)
    above = ("DPAD_DOWN",) if d.visible(f"//*[@resource-id='{ID}address'][@focused='true']") else ()
    select(d, f"@resource-id='{ID}confirm'", above + ("DPAD_UP", "DPAD_LEFT") * 6, '"Connect"')
    d.find(f"{SERVER_SCREEN} | //*[@resource-id='{ID}error' and string-length(@text) > 0]", timeout=30,
           what="server screen")
    if not d.visible(SERVER_SCREEN):
        error = d.by_id(f"{ID}error").text
        raise FlowFailed(f"not connected: {error}")
    d.log("connected, server screen")


def flow_login(d):
    """Add account > Use a password, credentials from E2E_USER / E2E_PASSWORD, Sign in: the home screen appears."""
    user, password = os.environ.get("E2E_USER"), os.environ.get("E2E_PASSWORD")
    if not (user and password):
        raise FlowFailed("E2E_USER / E2E_PASSWORD not set")
    select(d, f"@resource-id='{ID}add_user_button'", what='"Add account"')
    # Quick Connect first, nothing focused yet
    select(d, f"@resource-id='{ID}use_credentials'", what='"Use a password"')
    focus(d, f"@resource-id='{ID}username'", what="user name field")
    type_into(d, f"//*[@resource-id='{ID}username']", user, "user name field")
    focus(d, f"@resource-id='{ID}password'", what="password field")
    type_into(d, f"//*[@resource-id='{ID}password']", password, "password field")
    select(d, f"@resource-id='{ID}confirm'", what='"Sign in"')
    d.find(f"{HOME} | //*[@resource-id='{ID}error' and string-length(@text) > 0]", timeout=30, what="home screen")
    if not d.visible(HOME):
        raise FlowFailed(f"not logged in: {d.by_id(ID + 'error').text}")
    d.log(f"logged in as {user}")


LOGIN_SCREEN = f"//*[@resource-id='{ID}password']"
LOGIN_ERROR = f"//*[@resource-id='{ID}error']"


@contextlib.contextmanager
def toasts(d):
    """Around the action that shows a toast and the wait for it: UiAutomator2 lists a toast as an android.widget.Toast
    node only while all windows are searched (enableMultiWindows), and only one shown after that was switched on (a
    toast shown while the flow was still pressing Back was missed, TLS_TV_34)."""
    d.native().update_settings({"enableMultiWindows": True})
    try:
        yield
    finally:
        if d.driver:
            d.driver.update_settings({"enableMultiWindows": False})


def wait_toast(d, text, timeout=15):
    """Text of a toast containing text (inside toasts()), None when none appears within timeout. From the page source:
    it listed the toast node right away when an XPath lookup did not."""
    pattern = re.compile(r'<android\.widget\.Toast [^>]*text="([^"]*)"')
    deadline = time.time() + timeout
    while time.time() < deadline:
        for message in pattern.findall(d.native().page_source):
            message = html.unescape(message)
            if text.lower() in message.lower():
                return message
        time.sleep(0.2)
    return None


def expect_toast(d, text, where, timeout=15):
    message = wait_toast(d, text, timeout)
    if not message:
        raise FlowFailed(f'no "{text}" message {where}')
    d.log(f"message {where}: {message}")


def restart_app(d):
    appium_flows.kill_app(d, APP_ID)
    d.native().activate_app(APP_ID)


def sign_in(d, expected_error=None):
    """User selection of the server ("Who's watching?") > the user > password > Sign in. Without expected_error the
    home screen must appear (directly when the stored session still works), otherwise the login screen must show an
    error containing it (returned)."""
    d.find(SERVER_SCREEN, timeout=60, what="user selection")
    # The focus starts on the user or in the toolbar
    select(d, f"ancestor::*[@resource-id='{ID}users']", ("DPAD_DOWN", "DPAD_UP", "DPAD_LEFT") * 3, "the user")
    d.find(f"{LOGIN_SCREEN} | {HOME}", timeout=30, what="login screen")
    if d.visible(HOME) and not expected_error:
        d.log("signed in with the stored session")
        return None
    if d.visible(HOME):
        raise FlowFailed(f'home screen instead of the "{expected_error}" error')
    focus(d, f"@resource-id='{ID}password'", what="password field")
    type_into(d, LOGIN_SCREEN, os.environ.get("E2E_PASSWORD", ""), "password field")
    select(d, f"@resource-id='{ID}confirm'", what='"Sign in"')
    deadline, error = time.time() + 30, ""
    while time.time() < deadline:
        if not expected_error and d.visible(HOME):
            d.log("signed in")
            return None
        errors = d.native().find_elements(By.XPATH, LOGIN_ERROR)
        error = errors[0].text if errors else ""
        if expected_error and expected_error.lower() in error.lower():
            d.log(f"message: {error}")
            return error
        time.sleep(1)
    raise FlowFailed(f'expected "{expected_error}", the login screen shows: {error!r}' if expected_error
                     else f"not signed in, the login screen shows: {error!r}")


def leave_login_screen(d):
    """Cancel on the login screen: back to the user selection."""
    if d.visible(LOGIN_SCREEN):
        select(d, f"@resource-id='{ID}cancel'", what='"Cancel" of the login screen')
    d.find(SERVER_SCREEN, timeout=10, what="user selection")


# --- Library and player ------------------------------------------------------------------------------------------------

def open_library(d):
    """Home screen > the first library of "My media"."""
    go_home(d)
    # The focus is on the toolbar after the settings, on a row otherwise
    select(d, "ancestor::*[@content-desc='My media']", ("DPAD_DOWN",) * 3 + ("DPAD_UP",) * 6 + ("DPAD_LEFT",) * 6,
           'a "My media" library')
    status = d.find(f"//*[@resource-id='{ID}statusText']", timeout=20, what="library grid")[0].text
    d.log(status)


def open_item(d, index):
    """Library grid > item index (from 0, the grid's order by name), its details page."""
    open_library(d)
    counter = f"//*[@resource-id='{ID}counter']"
    for _ in range(index + 6):
        text = d.find(counter, what="item counter")[0].text  # "2 | 5"
        if text.startswith(f"{index + 1} |"):
            break
        key(d, "DPAD_DOWN" if int(text.split("|")[0]) <= index else "DPAD_UP")
    else:
        raise FlowFailed(f"item {index + 1} of the library not reachable with the D-pad")
    key(d, "DPAD_CENTER", pause=1)
    title = d.find(f"//*[@resource-id='{ID}fdTitle']", timeout=20, what="details page")[0].text
    d.log(f"item: {title}")


def player_visible(d):
    return d.visible(PLAYER)


def stop_player(d):
    """Leaves the player: the first Back can only hide the controls. Killing the app during playback crashes the
    emulator with the host GPU."""
    if player_visible(d):
        d.log("pausing playback")
        key(d, "MEDIA_PAUSE", pause=1)
    for attempt in range(3):
        if not player_visible(d):
            return
        if not attempt:
            d.log("leaving the player")
        key(d, "BACK", pause=2)
    if player_visible(d):
        raise FlowFailed("the player did not close with Back")


def position(d):
    """Playback position of the player controls ("1:05"). A key press shows hidden controls, Down only moves the
    focus between the control rows."""
    for _ in range(3):
        for element in d.native().find_elements(By.XPATH, POSITION):
            try:
                return element.text
            except WebDriverException:
                pass  # hidden in between
        key(d, "DPAD_DOWN", pause=1)
    raise FlowFailed("player position not readable (controls did not show)")


def player_menu(d, button, row, what):
    """Opens a menu of the player controls by D-pad, returns the titles of its entries. From the progress bar: the
    button is in the row above it (DPAD_UP, subtitles) or below it (DPAD_DOWN, quality). Not Right on the progress
    bar itself, that seeks: it is pressed only after the focus left the bar (five presses on the bar seeked the 2:00
    clip to its end, `current_time` = `total_time`, TLS_TV_34 play-second in three runs)."""
    progress = f"@resource-id='{ID}playback_progress'"
    bar_focused = f"//*[@focused='true'][{progress}]"
    for attempt in range(2):
        position(d)  # controls shown
        focus(d, progress, ("DPAD_DOWN", "DPAD_UP") * 3, "progress bar")
        try:
            # Up / Down only until the focus is off the bar: on hidden controls a key press just shows them, in scrub
            # mode Up / Down do nothing
            for _ in range(3):
                key(d, row)
                if not d.visible(bar_focused):
                    break
            else:
                raise FlowFailed(f'"{button}" not reachable with the D-pad (focus on {focused(d)})')
            # The focus lands under / above the bar, not necessarily left of the button: right, then left
            focus(d, f"@content-desc='{button}'", ("DPAD_RIGHT",) * 3 + ("DPAD_LEFT",) * 6, f'"{button}"')
            break
        except FlowFailed as error:
            # The focus stayed on the progress bar once (the controls hid in between, TLS_TV_34)
            if attempt:
                raise
            d.log(f"{error}, once more")
    key(d, "DPAD_CENTER", pause=1.5)
    entries = d.find("//android.widget.ListView//*[@resource-id='android:id/title' or "
                     f"@resource-id='{ID}title']", timeout=10, what=what)
    return [entry.text for entry in entries]


def choose_lowest_quality(d):
    """Quality menu of the player ("200 Mbit/s" … "420 Kbit/s"): the last entry, the lowest bitrate. The menu is
    longer than the screen, Down to its end."""
    player_menu(d, "Quality profile", "DPAD_DOWN", "quality menu")
    for _ in range(40):
        key(d, "DPAD_DOWN", pause=0.1)
    time.sleep(0.5)
    label = d.find("//android.widget.ListView//*[contains(@text, 'bit/s')]", what="quality entries")[-1].text
    key(d, "DPAD_CENTER", pause=3)
    d.log(f"quality: {label}")
    d.find(PLAYER, timeout=30, what="player after switching the quality")


def seconds(text):
    return appium_flows.seconds(text)


def flow_play(d, card="0", quality=""):
    """Plays item CARD (from 0) of the first library for 10 seconds, the position must advance. QUALITY=low picks the
    lowest bitrate in the player's quality menu: the test videos are H264 the emulator plays directly, the low bitrate
    makes the server transcode (HLS)."""
    with d.no_idle_wait():
        stop_player(d)
        open_item(d, int(card))
        select(d, "@content-desc='Resume' or @content-desc='Play'", ("DPAD_LEFT",) * 4 + ("DPAD_UP",) * 4,
               '"Play" of the details page')
        d.find(PLAYER, timeout=30, what="player")
        if quality == "low":
            choose_lowest_quality(d)
        start = position(d)
        time.sleep(10)
        end = position(d)
    d.log(f"position {start} -> {end}")
    if start == end:
        raise FlowFailed(f"position did not advance ({start})")


def flow_seek(d):
    """Seeks forward with Right on the progress bar (+30 s per press) past ExoPlayer's buffer (~50 s): only then the
    open direct stream ends and shows up in the proxy log. Then switches the subtitles on (external English .srt)."""
    with d.no_idle_wait():
        d.find(PLAYER, what="player")
        before = position(d)
        focus(d, f"@resource-id='{ID}playback_progress'", ("DPAD_DOWN", "DPAD_UP") * 3, "progress bar")
        # Right (+30 s) until far enough (appium_flows.seek_far_enough): on TLS_TV_34 one of two presses was not
        # counted (+31 s, twice in a row). At most 3 presses
        for _ in range(3):
            key(d, "DPAD_RIGHT", pause=1)
            # Only a shown position: position() presses Down for hidden controls, that would leave the progress bar
            shown = [e.text for e in d.native().find_elements(By.XPATH, POSITION)]
            if shown and appium_flows.seek_far_enough(before, shown[0]):
                break
        # The progress bar stays in scrub mode (Up does nothing, Right seeks on) until Center
        key(d, "DPAD_CENTER", pause=2)
        after = position(d)
        d.log(f"position {before} -> {after}")
        appium_flows.check_seek(before, after)
        titles = player_menu(d, "Select subtitle track", "DPAD_UP", "subtitle menu")
        track = next((index for index, title in enumerate(titles) if title.lower() != "none"), None)
        if track is None:
            raise FlowFailed(f"no subtitle track in the menu ({titles})")
        for _ in range(track):
            key(d, "DPAD_DOWN", pause=0.3)
        key(d, "DPAD_CENTER", pause=3)
        d.log(f"subtitles: {titles[track]}")
        start = position(d)
        time.sleep(3)
        end = position(d)
        if start == end:
            raise FlowFailed(f"position did not advance after switching the subtitles ({start})")


def flow_stop_player(d):
    with d.no_idle_wait():
        stop_player(d)


def flow_home(d):
    """Logged in without importing again (after app restart or reboot): the home screen loads. A cold start right
    after a boot can take longer than the first connection attempt: one relaunch, noted in the report."""
    try:
        d.find(HOME, timeout=60, what="home screen")
    except FlowFailed:
        d.log(f"no home screen (shown: {shown_texts(d)}), relaunching the app once")
        d.notes.append("home screen did not load on the first launch, loaded after a relaunch")
        restart_app(d)
        d.find(HOME, timeout=60, what="home screen after relaunching")
    d.log("home screen")


def flow_ensure_connected(d):
    """Start state of the steps after login: the home screen of the logged-in app. A step that failed in the middle can
    leave the app elsewhere (the certificates screen, the user selection, without a certificate): Back to the home
    screen, else the state is restored, app data cleared, CA and client certificate from the URL, login, noted in the
    report. So one failed step does not fail every later one."""
    driver = d.native()
    with d.no_idle_wait():
        stop_player(d)
    if driver.current_package != APP_ID:
        driver.activate_app(APP_ID)
        time.sleep(3)
    for _ in range(4):
        if d.visible(HOME):
            return
        key(d, "BACK", pause=2)
    if d.visible(HOME):
        return
    d.log(f"not on the home screen (shown: {shown_texts(d)}): restoring (app data cleared, certificates from the URL, "
          "login)")
    d.notes.append("start state restored before the step: the step before left the app off the home screen")
    appium_flows.kill_app(d, APP_ID)  # pm clear kills the app too, not in the foreground (kill_app)
    d.shell(f"pm clear {APP_ID}")
    driver.activate_app(APP_ID)
    flow_setup(d)
    flow_login(d)
    d.log("start state restored")


def shown_texts(d, limit=6):
    """Texts on the screen, for messages."""
    texts = [e.text for e in d.native().find_elements(By.XPATH, "//*[string-length(@text) > 0]")]
    return " / ".join(texts[:limit]) or "nothing"


def flow_browse(d):
    """Home screen > first library > first item: posters and backdrops (checked by the request evidence)."""
    open_item(d, 0)


# --- Settings and certificates screen ----------------------------------------------------------------------------------

def open_certificates_screen(d):
    """Home or user selection > Preferences (toolbar) > Login > the server > Certificates, all with the D-pad. The
    Preferences of the user selection open the login settings directly."""
    if not d.visible(SERVER_SCREEN):
        go_home(d)
    select(d, "@content-desc='Preferences'", ("DPAD_UP",) * 3 + ("DPAD_RIGHT",) * 8, '"Preferences"')
    d.find("//*[@text='Login' or @text='Automatic sign in']", timeout=10, what="settings")
    if not d.visible("//*[@text='Automatic sign in']"):
        select(d, "@text='Login'", what='"Login" in the settings')
    select(d, f"@text='{SERVER}'", what=f"server {SERVER} in the login settings")
    select(d, "@text='Certificates'", what='"Certificates" of the server')
    d.find(CERTIFICATES_SCREEN, timeout=10, what="certificates screen")


def client_certificate_text(d):
    """ "No client certificate." or "Imported: CN=…" (the subject is loaded afterwards)."""
    text = d.find(CLIENT_LINE, what="client certificate line")[0].text
    if text.lower().startswith("imported") and "cn=" not in text.lower():
        time.sleep(2)
        text = d.find(CLIENT_LINE, what="client certificate line")[0].text
    return text


def check_buttons_reachable(d):
    """Every button of the certificates screen gets the focus when going down the screen."""
    buttons = {e.text for e in d.native().find_elements(By.XPATH, "//android.widget.Button")}
    reached = set()
    for _ in range(len(buttons) + 4):
        key(d, "DPAD_DOWN", pause=0.3)
        reached.update(e.text for e in d.native().find_elements(By.XPATH, "//android.widget.Button[@focused='true']"))
        # New buttons scrolled into view
        buttons.update(e.text for e in d.native().find_elements(By.XPATH, "//android.widget.Button"))
    missing = buttons - reached
    if missing:
        raise FlowFailed(f"not reachable with the D-pad: {', '.join(sorted(missing))}")
    d.log(f"{len(buttons)} buttons reachable with the D-pad")


def flow_settings(d):
    """Certificates screen of the server: the CA (subject, expiry, fingerprint) and the client certificate (subject)
    are listed, every button is reachable with the D-pad."""
    open_certificates_screen(d)
    cas = [e.text for e in d.native().find_elements(By.XPATH, f"//*[{appium_flows.CA_ENTRY}]")]
    if not cas or "expires" not in cas[0].lower():
        raise FlowFailed(f"no imported CA with expiry and fingerprint listed ({cas})")
    d.log("CA: " + cas[0].replace("\n", " / "))
    client = client_certificate_text(d)
    if "cn=client-ec" not in client.lower():
        raise FlowFailed(f"client certificate client-ec not listed: {client!r}")
    if "expires" not in client.lower() or "sha-256" not in client.lower():
        raise FlowFailed(f"client certificate without expiry and fingerprint: {client!r}")
    d.log(f"client certificate: {client.replace(chr(10), " / ")}")
    check_buttons_reachable(d)
    go_home(d)


def leave_certificates_screen(d):
    """Back to the home screen or the user selection, whichever the settings were opened from."""
    back_until(d, f"{HOME} | {SERVER_SCREEN}", "home screen or user selection")


def import_from_url(d, name, client):
    """Certificates screen > Import from URL…: the URL of the proxy's file server, the target (CA or client
    certificate) in the dialog's list, OK."""
    select(d, "@text='Import from URL…'", ("DPAD_DOWN",) * 14, '"Import from URL…"')
    type_into(d, DIALOG_FIELD, f"{IMPORT_URL}/{name}", "URL field")
    target = "Import client certificate (.p12 / .pfx)…" if client else "Import CA certificate…"
    select(d, f"self::android.widget.CheckedTextView[@text='{target}']", ("DPAD_UP",) * 3 + ("DPAD_DOWN",) * 3,
           f'"{target}" in the dialog')
    if d.find(f"//android.widget.CheckedTextView[@text='{target}']")[0].get_attribute("checked") != "true":
        raise FlowFailed(f'"{target}" not chosen in the dialog')
    confirm_dialog(d)
    d.log(f"import from {IMPORT_URL}/{name}")


def dialog_title(d):
    titles = d.native().find_elements(By.XPATH, "//*[@resource-id='android:id/alertTitle']")
    return titles[0].text if titles else ""


def wait_dialog(d, what, timeout=20):
    """Waits for the next dialog, returns (title, message). A dialog without a title is a message of the app."""
    d.find(f"//*[@resource-id='android:id/button1']", timeout=timeout, what=what)
    messages = d.native().find_elements(By.XPATH, DIALOG_MESSAGE)
    return dialog_title(d), messages[0].text if messages else ""


def import_client_from_url(d, name, password="jellyfin"):
    """Certificates screen: client certificate from the URL, password, "Use". The client line shows its subject."""
    import_from_url(d, name, client=True)
    confirm_client_import(d, name, password)


def confirm_client_import(d, name, password="jellyfin"):
    """Certificates screen after choosing a client certificate file: password, "Use". The client line shows its
    subject."""
    title, message = wait_dialog(d, "password dialog")
    if title.lower() != "password of the certificate file":
        raise FlowFailed(f"{name}: {message or title}")
    type_into(d, DIALOG_FIELD, password, "password field")
    confirm_dialog(d)
    title, message = wait_dialog(d, "client certificate details")
    if not title.lower().startswith("import client certificate"):
        raise FlowFailed(f"{name}: {message or title}")
    confirm_dialog(d, "Use")
    decline_contained_ca(d, name)
    d.find("//*[starts-with(@text, 'Imported: CN=')]", timeout=20, what="imported client certificate")
    d.log(f"client certificate: {client_certificate_text(d)}")


def flow_unavailable(d):
    """The key of the imported client certificate is gone while the app still refers to it (after a restore from a
    backup, or the Keystore lost it). Simulated: the stored configuration of the server gets a key alias that does
    not exist (run-as, debug app). The user selection (failed automatic sign-in) and signing in must say the client
    certificate is no longer available, the certificates screen labels it "(no longer available)". Imports
    client-ec.p12 again at the end."""
    appium_flows.kill_app(d, APP_ID)
    prefs = "shared_prefs/tls_servers.xml"
    d.shell(f"run-as {APP_ID} sed -i 's/jellyfin_tls_client_[0-9a-f-]*/jellyfin_tls_client_gone/' {prefs}")
    if "jellyfin_tls_client_gone" not in d.shell(f"run-as {APP_ID} cat {prefs}"):
        raise FlowFailed(f"no imported client certificate in the app's configuration ({prefs})")
    d.log("client key reference replaced by one that does not exist")
    with toasts(d):
        d.native().activate_app(APP_ID)
        expect_toast(d, "no longer available", "on the user selection (failed automatic sign-in)", timeout=30)
    sign_in(d, "no longer available")
    leave_login_screen(d)
    open_certificates_screen(d)
    client = client_certificate_text(d)
    if "(no longer available)" not in client.lower():
        raise FlowFailed(f'client certificate not labeled "(no longer available)": {client!r}')
    d.log(f"certificates screen: {client}")
    # Usable again for the next steps
    import_client_from_url(d, "client-ec.p12")
    leave_certificates_screen(d)
    sign_in(d)


def flow_missing(d):
    """Removes the client certificate on the certificates screen. The running session must explain the failing
    requests (back on the home screen), after an app restart the user selection (failed automatic sign-in)
    and the login screen must say that the server requires a client certificate (not a generic network error)."""
    expected = "requires a client certificate"
    open_certificates_screen(d)
    remove_client_certificate(d)
    with toasts(d):
        # Back in the main activity (the settings on top) the home rows reload
        key(d, "BACK", pause=0)
        message = wait_toast(d, expected, timeout=10)
    if not message:
        raise FlowFailed(f'no "{expected}" message in the running session')
    d.log(f"message in the running session: {message}")
    leave_certificates_screen(d)
    with toasts(d):
        restart_app(d)
        expect_toast(d, expected, "on the user selection (failed automatic sign-in)", timeout=30)
    sign_in(d, expected)


def flow_saved_server(d):
    """After missing: removes the CA on the certificates screen, then the saved server in the server list > the user
    > Sign in: the message explains the untrusted server certificate and its issuer (not "Unable to connect")."""
    leave_login_screen(d)
    open_certificates_screen(d)
    remove_ca(d)
    back_until(d, SERVER_SCREEN, "user selection")
    select(d, f"ancestor-or-self::*[@resource-id='{ID}server_button']", ("DPAD_DOWN",) * 3 + ("DPAD_RIGHT",) * 3,
           "the server button")
    select(d, f"ancestor::*[@resource-id='{ID}stored_servers'] and descendant::*[@text='{SERVER}']",
           what=f"saved server {SERVER}")
    message = sign_in(d, "not trusted")
    if "issued by cn=" not in message.lower():
        raise FlowFailed(f"the message does not name the issuer: {message!r}")


def remove_ca(d):
    """On the certificates screen: removes the imported CA (the focus starts on Cancel in the dialog)."""
    select(d, appium_flows.CA_ENTRY, ("DPAD_DOWN",) * 6, "the imported CA")
    confirm_dialog(d, "Remove")
    d.find("//*[@text='No imported certificate authorities.']", what='"No imported certificate authorities."')
    d.log("CA removed")


def remove_client_certificate(d):
    """On the certificates screen: removes the client certificate, its entry is clickable once the certificate is
    loaded (the focus starts on Cancel in the dialog)."""
    d.find(f"//*[{appium_flows.CLIENT_ENTRY}]", timeout=20, what="client certificate entry")
    select(d, appium_flows.CLIENT_ENTRY, ("DPAD_DOWN",) * 10, "the client certificate")
    confirm_dialog(d, "Remove")
    d.find("//*[@text='No client certificate.']", what='"No client certificate."')
    d.log("client certificate removed")


def confirm_ca_import(d, name):
    """Certificates screen after choosing a CA file: the details, "Trust", the CA is listed."""
    title, message = wait_dialog(d, "CA details")
    if title.lower() != "import ca certificate…":
        raise FlowFailed(f"{name}: {message or title}")
    confirm_dialog(d, "Trust")
    d.find(f"//*[{appium_flows.CA_ENTRY}]", timeout=10, what="imported CA")
    d.log("CA imported")


def flow_reimport(d):
    """After saved-server (no CA, no client certificate): on the certificates screen the CA and the client certificate
    from the URL import, then signing in works without restarting the app."""
    leave_login_screen(d)
    open_certificates_screen(d)
    import_from_url(d, "ca.pem", client=False)
    confirm_ca_import(d, "ca.pem")
    import_client_from_url(d, "client-ec.p12")
    leave_certificates_screen(d)
    sign_in(d)


def flow_wrong_password(d):
    """Certificates screen: client-rsa.p12 from the URL with the password "wrong": the message says the password is
    wrong, the configured client certificate is unchanged."""
    open_certificates_screen(d)
    before = client_certificate_text(d)
    import_from_url(d, "client-rsa.p12", client=True)
    title, _ = wait_dialog(d, "password dialog")
    if title.lower() != "password of the certificate file":
        raise FlowFailed(f"no password dialog ({title!r})")
    type_into(d, DIALOG_FIELD, "wrong", "password field")
    confirm_dialog(d)
    _, message = wait_dialog(d, "wrong password message")
    d.log("message: " + message.replace("\n", " / "))
    if not message.lower().startswith("wrong password"):
        raise FlowFailed(f"not a wrong password message: {message!r}")
    confirm_dialog(d)
    after = client_certificate_text(d)
    if after != before:
        raise FlowFailed(f"client certificate changed: {before!r} -> {after!r}")
    d.log(f"client certificate unchanged: {after}")
    leave_certificates_screen(d)


def flow_pem_client(d):
    """Certificates screen: a PEM file with a private key and a certificate from the URL as client certificate. Android
    reads client keys only from PKCS#12: the message must ask for a .p12 / .pfx file before any password is asked, the
    configured client certificate is unchanged."""
    open_certificates_screen(d)
    before = client_certificate_text(d)
    import_from_url(d, "pem-key-and-cert.pem", client=True)
    title, message = wait_dialog(d, "not PKCS#12 message")
    if title.lower() == "password of the certificate file":
        raise FlowFailed("password asked before the PEM file was rejected")
    d.log("message: " + message)
    if ".p12 or .pfx" not in message.lower():
        raise FlowFailed(f"not the PKCS#12 message: {message!r}")
    confirm_dialog(d)
    after = client_certificate_text(d)
    if after != before:
        raise FlowFailed(f"client certificate changed: {before!r} -> {after!r}")
    d.log(f"client certificate unchanged: {after}")
    leave_certificates_screen(d)


def flow_p12_ca(d):
    """Certificates screen: the client certificate is removed, then client-ec.p12 is imported from the URL as CA
    certificate: its CA (Jellyfin Test Client CA) is listed for trust, then the client certificate in the file is
    offered and used. The client CA is removed again (the proxy has its own CA), the app signs in after a restart."""
    open_certificates_screen(d)
    remove_client_certificate(d)
    import_from_url(d, "client-ec.p12", client=False)
    title, _ = wait_dialog(d, "password dialog")
    if title.lower() != "password of the certificate file":
        raise FlowFailed(f"no password dialog ({title!r})")
    type_into(d, DIALOG_FIELD, "jellyfin", "password field")
    confirm_dialog(d)
    title, message = wait_dialog(d, "CA details")
    if title.lower() != "import ca certificate…" or appium_flows.CLIENT_CA.lower() not in message.lower():
        raise FlowFailed(f"no CA confirmation listing {appium_flows.CLIENT_CA}: {title!r} {message[:200]!r}")
    confirm_dialog(d, "Trust")
    title, message = wait_dialog(d, "client certificate offer")
    if title.lower() != "client certificate in the file":
        raise FlowFailed(f'no "Client certificate in the file" offer ({title!r})')
    d.log("offered: " + message.splitlines()[0])
    confirm_dialog(d, "Use")
    d.find("//*[starts-with(@text, 'Imported: CN=client-ec')]", timeout=20, what="client certificate client-ec")
    d.log(f"client certificate from the CA file: {client_certificate_text(d)}")
    select(d, f"contains(@text, '{appium_flows.CLIENT_CA}')", ("DPAD_DOWN",) * 6 + ("DPAD_UP",) * 12, "the client CA")
    confirm_dialog(d, "Remove")
    d.log(f"{appium_flows.CLIENT_CA} removed again")
    leave_certificates_screen(d)
    restart_and_sign_in(d)


# --- The TV app on a phone (checklists/tv-phone.md) ---------------------------------------------------------------------
# A phone has what the TV images lack: a document picker and the system credential store with the KeyChain chooser.
# The app is still driven with the D-pad, the system screens (picker, Settings, chooser) by touch with the helpers of
# appium_flows.

def restart_and_sign_in(d):
    """Restarts the app: the home screen with the stored session, otherwise the user selection > sign in."""
    restart_app(d)
    d.find(f"{HOME} | {SERVER_SCREEN}", timeout=60, what="home screen or user selection")
    if d.visible(HOME):
        d.log("home screen with the stored session")
    else:
        sign_in(d)


def flow_import_file(d):
    """Certificates screen: removes the CA, "Import CA certificate…" and "Import client certificate…" open the system
    file picker (ca.pem, client-ec.p12), then the app signs in again after a restart."""
    open_certificates_screen(d)
    remove_ca(d)
    select(d, "@text='Import CA certificate…'", ("DPAD_DOWN",) * 14, '"Import CA certificate…"')
    pick_file(d, "ca.pem")
    confirm_ca_import(d, "ca.pem")
    select(d, "@text='Import client certificate (.p12 / .pfx)…'", ("DPAD_DOWN",) * 14, '"Import client certificate…"')
    pick_file(d, "client-ec.p12")
    confirm_client_import(d, "client-ec.p12")
    leave_certificates_screen(d)
    restart_and_sign_in(d)


def flow_system_client(d):
    """Client key client-ec in the system store (Settings > Install a certificate), on the certificates screen
    "Choose from system store…" > the KeyChain chooser: the client line says "From system store", the app signs in
    again after a restart with the system key."""
    appium_flows.clear_system_credentials(d)
    appium_flows.install_system_certificate(d, "client-ec.p12")
    d.native().activate_app(APP_ID)
    # The app is still starting: without the wait go_home presses Back on the starting home screen and closes the app
    d.find(f"//*[@resource-id='{ID}row_header' or @resource-id='{ID}users' or @resource-id='{ID}add_user_button']",
           timeout=60, what="home screen or user selection")
    open_certificates_screen(d)
    select(d, "@text='Choose from system store…'", ("DPAD_DOWN",) * 14, '"Choose from system store…"')
    appium_flows.choose_system_key(d)
    d.find("//*[starts-with(@text, 'From system store')]", timeout=20, what='"From system store" client line')
    d.log(f"client certificate: {client_certificate_text(d)}")
    leave_certificates_screen(d)
    restart_and_sign_in(d)


def flow_system_cleanup(d):
    """Clears the system credentials and removes the PIN of system-client."""
    appium_flows.flow_system_cleanup(d)


FLOWS = {name[5:].replace("_", "-"): function for name, function in globals().items() if name.startswith("flow_")}


def run(d, action):
    """Runs "flow [KEY=VALUE …]". Returns "" or the reason it failed. XPath 1 for every session: the XPath 2 engine of
    UiAutomator2 failed on a union in focus() ("ArrayList$ListItr cannot be cast to NodeType", TLS_TV_34), the
    expressions here are all XPath 1."""
    d.reopen_if_lost()
    try:
        d.native().update_settings({"enforceXPath1": True})
    except WebDriverException as error:
        return f"{action.split()[0]}: no Appium session: {(error.msg or '').splitlines()[0][:200]}"
    return appium_flows.run(d, action, FLOWS)


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
