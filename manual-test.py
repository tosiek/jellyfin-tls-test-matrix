#!/usr/bin/env python3
"""Assisted manual test session: boots an emulator with a window, installs the app, copies the test certificates to
Download/jellyfin-tls/ and walks through a checklist (checklists/<app>.md). You click, the script collects the
evidence of every step:

  - requests through tls-test-proxy (port 8920, client certificate required) from its request log, by type
    (API, images, streaming, WebSocket, jellyfin-web), errors (status >= 400) and the client certificate CN
  - crashes, TLS errors and playback errors of the app from logcat
  - a screenshot at the end of the step

Every emulator in turn: build once, boot an AVD, run the checklist, shut it down, next AVD. Results go to
manual-results/<time>/<avd>-<app>/REPORT.md, the overview of all AVDs to manual-results/<time>/SUMMARY.md.

Usage:
  ./manual-test.py                      # every TLS_* AVD: phones with the mobile app, TLS_TV_* with the TV app
  ./manual-test.py TLS_API_23 TLS_TV_34 # selected AVDs (one AVD: the emulator keeps running afterwards)
  ./manual-test.py --steps setup,login,play           # only some steps of the checklist on every AVD
  ./manual-test.py TLS_API_23 --skip-build --keep --from reboot   # keep app data, start at a step
  ./manual-test.py --app mobile         # the mobile app also on the TV AVDs

Clicking automation with Appium (steps with "> appium <flow>" in the checklist: appium_flows.py for the mobile app,
tv_flows.py for the TV app), UiAutomator2 driver on every API level, watch it in the emulator window. E2E_USER /
E2E_PASSWORD for the login, playback uses the first video of the first library:
  E2E_USER=jellyfin E2E_PASSWORD=1234 ./manual-test.py TLS_API_36            # flows click, you confirm every step
  E2E_USER=jellyfin E2E_PASSWORD=1234 ./manual-test.py --auto                # only automated steps, every AVD,
                                                                             # unattended, pass/fail by the script
  E2E_USER=jellyfin E2E_PASSWORD=1234 ./manual-test.py --auto --jobs 2       # 2 emulators at a time (up to 4)
  E2E_USER=… ./manual-test.py --auto --jobs 2 --remote-jobs 2              # 2 here, 2 on the remote machine
  E2E_USER=… ./manual-test.py --auto --jobs 0 --remote-jobs 1 TLS_API_34   # only on the remote machine
  E2E_USER=… ./manual-test.py --auto --jobs 2 --remote user@a:2 --remote user@b:1   # 2 here, 2 and 1 on two machines
  E2E_USER=… ./manual-test.py --auto --jobs 2 --here TLS_API_36,TLS_API_23 --remote user@a:1:TLS_TV_34,TLS_API_30
                                # which AVDs run where and in which order, the others on whichever slot is free
Remote machines (--remote HOST[:JOBS], several times; default user@remote-a or MANUAL_REMOTE_HOST, comma
separated): ssh key login, the SDK in ~/Android/Sdk and the TLS_* AVDs in ~/.android/avd, Appium with UiAutomator2
5.0.7 (nvm is loaded), a desktop session logged in (host GPU). Emulators and Appium run there, builds, checks, Jellyfin
and the test Caddy here; one ssh tunnel per machine and run (see start_tunnel).
./manual-test.py --check-remote      # only checks the machines (ssh, KVM, desktop session): exit code 0 ready, 3 not
                                      # ready, 3 as well when a --remote run stops before it starts anything
Needs Appium with the UiAutomator2 driver 5.x (the last with Android 6 support):
  appium driver install uiautomator2@5.0.7
The Appium server is started on MANUAL_APPIUM_PORT (default 4724), log: appium-manual.log. With --jobs every worker
gets its own emulator, Appium and mTLS proxy port (MANUAL_PORT, MANUAL_APPIUM_PORT, MANUAL_SERVER_PORT, see job_ports).

Between two AVDs: Enter starts the next one, s skips it, q ends the run. In a checklist, q ends the current AVD.
MANUAL_PORT=5592 ./manual-test.py …    # other emulator port (default 5590)
MANUAL_VOLUME=5 ./manual-test.py …     # media volume 0 - 15 (default 1, quietest audible)
MANUAL_GPU=swiftshader_indirect …      # emulator GPU mode (default: host)

Needs Jellyfin on 127.0.0.1:8096. The test Caddy (tls-test-proxy) is started when it is not running.
"""
import argparse
import collections
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROXY = HERE / "tls-test-proxy"
# Caddy 2.10 or newer offers X25519MLKEM768 (post-quantum key exchange), the distribution package is older:
# tls-test-proxy/bin/caddy from setup.sh when it is there
CADDY = str(PROXY / "bin" / "caddy") if (PROXY / "bin" / "caddy").exists() else "caddy"
ACCESS_LOG = PROXY / "access.log"
SDK = Path(os.environ.get("ANDROID_HOME", Path.home() / "Android/Sdk"))
ADB = str(SDK / "platform-tools/adb")
EMULATOR = str(SDK / "emulator/emulator")
# Own port, does not collide with run-matrix.sh (5580+). Not $PORT, often set by other tools
PORT = int(os.environ.get("MANUAL_PORT", "5590"))
SERIAL = f"emulator-{PORT}"
# Log files of this instance, the --jobs workers (MANUAL_PORT 5592, 5594, …) get their own
LOG_SUFFIX = "" if PORT == 5590 else f"-{PORT}"
# The emulator log is per AVD (set by boot): per port, the next AVD on the port overwrote the log of one that died
# (TLS_API_28 at reboot on remote-a)
EMULATOR_LOG = HERE / f"emulator-manual{LOG_SUFFIX}.log"
APPIUM_LOG = HERE / f"appium-manual{LOG_SUFFIX}.log"
# GPU mode of the AVD by default. Killing the app during video playback crashed the emulator with the host GPU
# (gfxstream "Failed to create EGL fence sync") and made system_server abort with swiftshader_indirect (software
# decoding overloads it), the checklists leave the player before a force-stop, which sends the app to the background
# first (run_action). MANUAL_GPU to change.
# Default: -gpu host for every AVD that is not in --software-gpu, whatever the AVD's hw.gpu.mode says. "-gpu auto" picked
# SwANGLE (software rendering, laggy) on API 36 although the AVD says host. MANUAL_GPU="" (empty) = no -gpu option, the
# setting of the AVD applies.
GPU = os.environ.get("MANUAL_GPU", "host")
# --software-gpu: software rendering for the AVDs that crash with the host GPU. lavapipe (Mesa's software Vulkan) was the
# best software mode in bench-gpu.sh (TLS_API_34: median frame 31 ms against 38 - 40 ms of swiftshader / software, 486 %
# CPU against 600 - 626 %, 813 % and 65 ms of swangle), MANUAL_SOFTWARE_GPU to change.
SOFTWARE_GPU = os.environ.get("MANUAL_SOFTWARE_GPU", "lavapipe")
emulator_process = None
# Media volume 0 - 15 set after every boot, 1 = quietest audible
VOLUME = int(os.environ.get("MANUAL_VOLUME", "5"))
INSTALL_ATTEMPTS = 3
# Seconds in the background (Home) before force-stop kills the app, see run_action
HOME_WAIT = 5


def proxy_ports(first):
    """Proxy ports of tls-test-proxy/Caddyfile, the address line of the site with the port first: the mTLS site
    (8920, client certificate required) and the CA-only site (8921, no client certificate), one port of each per
    worker slot of --jobs / --remote-jobs (the request log tells the emulators apart only by port). More slots: add
    ports to both lines and restart the test Caddy."""
    for line in (PROXY / "Caddyfile").read_text().splitlines():
        ports = [int(port) for port in re.findall(r"https://:(\d+)", line)]
        if first in ports:
            return ports
    return [first]


MTLS_PORTS = proxy_ports(8920)
CA_ONLY_PORTS = proxy_ports(8921)
MAX_JOBS = min(len(MTLS_PORTS), len(CA_ONLY_PORTS))
# mTLS port of the test Caddy, 8922 for a second instance in parallel (the request log is split by port)
SERVER_PORT = int(os.environ.setdefault("MANUAL_SERVER_PORT", "8920"))
# CA-only port of the test Caddy (no client certificate), the CA-only steps of the checklists (appium_flows.CA_ONLY_SERVER)
CA_ONLY_PORT = int(os.environ.setdefault("MANUAL_CA_ONLY_PORT", "8921"))
# PIN set by the system store steps when Android requires a screen lock (appium_flows.LOCK_PIN)
LOCK_PIN = "1234"
# Seconds system_server must have been running before installing or driving the app
SYSTEM_SERVER_SETTLE = 40

APPS = {
    "mobile": dict(project="jellyfin-android", task=":app:assembleLibreDebug", apk_dir="libre/debug"),
    "tv": dict(project="jellyfin-androidtv", task=":app:assembleDebug", apk_dir="debug"),
}

# Request types by URI, first match wins
REQUEST_TYPES = [
    ("websocket", re.compile(r"^/socket")),
    ("images", re.compile(r"/Images/")),
    ("subtitles", re.compile(r"/Subtitles/", re.I)),
    # Transcoded (HLS playlists and segments) and direct play (one stream with range requests)
    ("streaming-hls", re.compile(r"\.m3u8|/hls1?/", re.I)),
    ("streaming-direct", re.compile(r"^/(Videos|Audio)/[^/]+/(stream|universal)", re.I)),
    # Downloads of the app (FileDownloader): /Items/<id>/Download
    ("download", re.compile(r"/Download", re.I)),
    ("streaming-other", re.compile(r"^/(Videos|Audio)/", re.I)),
    ("web", re.compile(r"^/web/")),
]

# Own Appium server for the clicking automation (appium_flows.py), not the default 4723 of other tools
APPIUM_PORT = int(os.environ.get("MANUAL_APPIUM_PORT", "4724"))
appium_process = None

# Emulators on another machine (--remote-jobs): the worker's emulator and Appium run there over ssh, everything else
# here. adb talks to the adb server there through the parent's tunnel (ANDROID_ADB_SERVER_PORT, also for the flows and
# push-certs.sh), the Appium port is tunneled, the emulator reaches the test Caddy here through reverse tunnels
# (10.0.2.2 is the remote machine's loopback). Same user, SDK in ~/Android/Sdk and AVD names there.
REMOTE = os.environ.get("MANUAL_REMOTE", "")
REMOTE_SDK = "Android/Sdk"  # relative to the remote home
REMOTE_ADB_PORT = 15037  # local end of the tunnel to the adb server of the first remote machine, 15038 of the second, …
# Every ssh gives up connecting after SSH_TIMEOUT seconds, the short commands (probes, pgrep / pkill, listings) are
# limited to as long in total: a remote machine that does not answer must not hang the run (login of
# user@remote-b took 30 s and the run sat at its first ssh)
SSH_TIMEOUT = 5
# Exit code when a remote machine cannot run emulators (no desktop session, no KVM, ssh does not answer): a wrapper can
# tell it from a failed test or a wrong option (1) and nothing was started
EXIT_REMOTE = 3
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", f"ConnectTimeout={SSH_TIMEOUT}"]
# The host GPU needs the display of the desktop session logged in on the remote machine (over plain ssh: "Failed to
# get EGL display"), Ubuntu's GNOME: Wayland with Xwayland on :0
REMOTE_ENV = ("export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 "
              "XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1) "
              f"ANDROID_HOME=$HOME/{REMOTE_SDK} "
              "JAVA_HOME=$(ls -d ~/android-studio/jbr /opt/android-studio/jbr 2>/dev/null | head -1); "
              "[ -s ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; "
              # Settings of one machine, e.g. which GPU the emulator uses on a laptop with two (OpenGL on one GPU
              # and Vulkan on the other killed the emulator when playback started)
              "[ -f ~/.tls-emulator-env ] && . ~/.tls-emulator-env; ")
EMULATOR_ON_HOST = f"{REMOTE_SDK}/emulator/emulator" if REMOTE else EMULATOR
ADB_ON_HOST = f"{REMOTE_SDK}/platform-tools/adb" if REMOTE else ADB


def on_host(command, remote=REMOTE, tty=False):
    """command (a list) on the machine of the emulator: here, or over ssh on remote. tty: the remote process ends with
    the ssh connection (SIGHUP), for the emulator and Appium."""
    if not remote:
        return command
    return [*SSH, *(["-tt"] if tty else []), remote, REMOTE_ENV + "exec " + shlex.join(command)]

# Interesting logcat lines: crashes, TLS errors, playback and WebView errors
# Expected: the WebView does not know the imported CA, JellyfinWebViewClient validates and proceeds
# The Appium UiAutomator2 server (instrumentation) crashes on Android 6 when a session ends ("Error while
# disconnecting UiAutomation"), not the app
LOGCAT_EXPECTED = re.compile(r"cr_X509Util|net_error -202|JellyfinWebViewClient: (Received SSL error|Issued| on URL)|"
                             r"FATAL EXCEPTION: Instr: androidx\.test\.runner")
LOGCAT_PATTERNS = re.compile(
    r"FATAL EXCEPTION|ANR in|SSL\w*Exception|CertPathValidator|CertificateException|"
    r"TlsManager|TlsDiagnostics|PlaybackException|ExoPlaybackException|ERR_SSL|ERR_CERT|ERR_BAD_SSL|"
    r"NetworkOnMainThread",
    re.I,
)

GREEN, RED, YELLOW, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def log(msg):
    print(f"{DIM}[{datetime.now():%H:%M:%S}]{RESET} {msg}", flush=True)


def adb(*args, timeout=120, check=False):
    # No stdin, adb would consume the keys typed for the checklist
    result = subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        sys.exit(f"adb {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.replace("\r", "")


def shell(cmd, timeout=120):
    return adb("shell", cmd, timeout=timeout).strip()


GOT_IT_BUTTON = re.compile(r'text="got it"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', re.I)


def dismiss_got_it(tries=3):
    """Clicks "GOT IT" of the first-run tips of the launcher after a boot ("Welcome - Wallpapers, widgets, & settings" of
    the AOSP launcher on Android 6 - 7, TLS_API_23). It is found in a uiautomator dump, no Appium needed
    (the session of a flow does not exist yet). Several tips can follow each other."""
    for _ in range(tries):
        xml = shell("uiautomator dump /sdcard/window.xml > /dev/null 2>&1; cat /sdcard/window.xml", timeout=60)
        button = GOT_IT_BUTTON.search(xml)
        if not button:
            return
        left, top, right, bottom = map(int, button.groups())
        shell(f"input tap {(left + right) // 2} {(top + bottom) // 2}")
        log('clicked "GOT IT" of a first-run tip')
        time.sleep(1.5)


def shell_booting(cmd, timeout=120):
    """shell() while the emulator boots: an adb command that does not answer counts as "not ready yet" (the boot
    deadline decides), it must not end the worker (TLS_API_23 on remote-c: getprop hung 120 s)."""
    try:
        return shell(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""


def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


last_register = 0.0


def register_emulator():
    """Tells the adb server about the emulator of PORT, as the emulator does once at its start. A restarted adb server
    (something ran kill-server) scans only 5555 - 5585 and lost every emulator of the --jobs ports for
    good: "device not found" while they ran. Harmless when nothing listens there yet."""
    message = f"host:emulator:{PORT + 1}".encode()
    try:
        with socket.create_connection(("127.0.0.1", int(os.environ.get("ANDROID_ADB_SERVER_PORT", "5037"))), 5) as s:
            s.sendall(f"{len(message):04x}".encode() + message)
    except OSError:
        pass


def device_online():
    global last_register
    for attempt in range(2):
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        if any(line.startswith(SERIAL) and line.endswith("device") for line in out.splitlines()):
            return True
        if attempt or SERIAL in out or time.time() - last_register < 10:
            return False
        last_register = time.time()
        register_emulator()
        time.sleep(1)
    return False


# --- Preparation -----------------------------------------------------------------------------------------------------

def check_servers():
    if not port_open(8096):
        sys.exit("Jellyfin is not running on 127.0.0.1:8096, start it first")
    if not port_open(SERVER_PORT):
        log("starting the test Caddy (tls-test-proxy)")
        with open(PROXY / "caddy.log", "a") as out:
            subprocess.Popen([CADDY, "run", "--config", "Caddyfile", "--adapter", "caddyfile"], cwd=PROXY,
                             stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            if port_open(SERVER_PORT):
                break
            time.sleep(0.5)
        else:
            sys.exit("the test Caddy did not start, see tls-test-proxy/caddy.log")
    if not ACCESS_LOG.exists():
        print(f"{YELLOW}No {ACCESS_LOG}, the Caddy running on {SERVER_PORT} has no request log (restart it with the current "
              f"Caddyfile). Request evidence is missing.{RESET}")


def build(app):
    cfg = APPS[app]
    project = ROOT / cfg["project"]
    log(f"build {app}")
    env = dict(os.environ, JAVA_HOME=os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-21-openjdk-amd64"))
    with open(HERE / f"build-{app}.log", "w") as out:
        if subprocess.run(["./gradlew", cfg["task"], "-q"], cwd=project, stdout=out, stderr=out, env=env,
                          stdin=subprocess.DEVNULL).returncode:
            sys.exit(f"build failed, see build-{app}.log")


def apk_info(app):
    apk_dir = ROOT / APPS[app]["project"] / "app/build/outputs/apk" / APPS[app]["apk_dir"]
    apks = sorted(apk_dir.glob("*.apk"))
    if not apks:
        sys.exit(f"no APK in {apk_dir}, build without --skip-build")
    app_id = json.loads((apk_dir / "output-metadata.json").read_text())["applicationId"]
    return apks[0], app_id


def list_avds(remote=REMOTE):
    emulator = f"{REMOTE_SDK}/emulator/emulator" if remote else EMULATOR
    try:
        out = subprocess.run(on_host([emulator, "-list-avds"], remote), capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT if remote else None).stdout
    except subprocess.TimeoutExpired:
        sys.exit(f"{remote} did not answer within {SSH_TIMEOUT} s (ssh to list its AVDs)")
    return out.split()


def emu(*args, timeout=30):
    """adb emu: adb connects to the emulator console itself, on the emulator's machine (not through the adb server)."""
    return subprocess.run(on_host([ADB_ON_HOST, "-s", SERIAL, "emu", *args]), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL).stdout.replace("\r", "")


def emulator_processes(action="pgrep"):
    """pgrep / pkill the emulator process of PORT on its machine, True when pgrep found one."""
    try:
        return subprocess.run(on_host([action, "-f", f"qemu-system.* -port {PORT}( |$)"]), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT if REMOTE else None).returncode == 0
    except subprocess.TimeoutExpired:
        return True  # unknown: counts as still running, the callers kill it after their own wait


def running_avd():
    if not device_online():
        return None
    # Empty while an emulator is shutting down
    return next(iter(emu("avd", "name", timeout=10).splitlines()), "").strip() or None


def wait_for_emulator_exit(seconds=60):
    """adb drops the emulator before its process ends: the next emulator on the port (or of the AVD) failed to start
    (TLS_API_36 boot timeout)."""
    for _ in range(seconds):
        if not emulator_processes():
            return
        time.sleep(1)
    log(f"{YELLOW}the emulator process on port {PORT} is still running after {seconds} s, killing it{RESET}")
    emulator_processes("pkill")
    time.sleep(2)


def kill_emulator():
    if not device_online():
        return
    log(f"shutting down {SERIAL}")
    # "emu kill" is pulling the plug: data written just before (the lock settings after system-cleanup removed the
    # PIN) could be lost, Android 9 then crashed in every boot (SyntheticPasswordManager, TLS_API_28,
    # even with sync + 3 s before the kill). A clean power off lets system_server finish its writes, the
    # emulator exits by itself then; "emu kill" only when it hangs.
    try:
        shell("sync", timeout=30)
        subprocess.run([ADB, "-s", SERIAL, "shell", "reboot", "-p"], capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        for _ in range(60):
            out = subprocess.run([ADB, "devices"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
            if SERIAL not in out:
                wait_for_emulator_exit()
                return
            time.sleep(1)
        log(f"{YELLOW}the emulator did not power off within 60 s, killing it{RESET}")
    except subprocess.TimeoutExpired:
        log(f"{YELLOW}sync or power off timed out (the emulator does not respond), killing it anyway{RESET}")
    try:
        emu("kill", timeout=30)
    except subprocess.TimeoutExpired:
        log(f"{YELLOW}the emulator console does not respond, killing its process{RESET}")
        emulator_processes("pkill")
    for _ in range(60):
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        if SERIAL not in out:
            wait_for_emulator_exit()
            return
        time.sleep(1)


def boot(avd, keep_log=False):
    """Boots avd on PORT unless it runs already. keep_log: the emulator log is continued (the reboot step)."""
    current = running_avd()
    if current == avd:
        log(f"using the running emulator {SERIAL} ({avd})")
        return
    if current:
        kill_emulator()
    elif emulator_processes():
        # Not in adb any more (frozen, "the emulator is gone") but its process holds the ports: the next emulator on
        # the port booted without adb (TLS_API_29 / 30 after a frozen TLS_API_23 / 24)
        log(f"{YELLOW}an emulator process on port {PORT} without adb, killing it{RESET}")
        emulator_processes("pkill")
        time.sleep(5)
        if emulator_processes():
            try:
                subprocess.run(on_host(["pkill", "-9", "-f", f"qemu-system.* -port {PORT}( |$)"]), capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT if REMOTE else None)
            except subprocess.TimeoutExpired:
                log(f"{YELLOW}pkill on {REMOTE} did not answer within {SSH_TIMEOUT} s{RESET}")
        wait_for_emulator_exit()
    global emulator_process, EMULATOR_LOG
    EMULATOR_LOG = HERE / f"emulator-manual-{avd}.log"
    log(f"booting {avd} ({f'on {REMOTE}, window there' if REMOTE else 'window'}, GPU {GPU or 'of the AVD'})")
    command = [EMULATOR_ON_HOST, "-avd", avd, "-port", str(PORT), *(["-gpu", GPU] if GPU else []), "-no-boot-anim",
               "-no-snapshot"]  # with REMOTE the window is on the desktop session there (REMOTE_ENV)
    emulator_process = subprocess.Popen(on_host(command, tty=True), stdout=open(EMULATOR_LOG, "a" if keep_log else "w"),
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    wait_for_boot()


def media_volume():
    if shell("getprop ro.build.version.sdk") == "30":  # not "cmd media_session" there, see set_volume
        match = re.search(r"- STREAM_MUSIC:.*?streamVolume:(\d+)", shell("dumpsys audio"), re.S)
    else:
        match = re.search(r"volume is (\d+)", shell("cmd media_session volume --stream 3 --get 2>&1"))
    return int(match.group(1)) if match else None


def set_volume(level=VOLUME):
    """Media volume (0 - 15) after every boot. "cmd media_session --set" is ignored on newer images and "media"
    exists only on old ones, so the result is checked and volume keys are the last resort (they control the media
    stream on Android 9+). Not "cmd media_session" on Android 11: it calls ActivityThread.systemMain() inside
    system_server (MediaShellCommand, fixed in 12), ActivityManager loses system_server's own process record and the
    next package change crashes system_server ("Unable to find app for caller" binding RoleControllerService)."""
    if shell("getprop ro.build.version.sdk") != "30":
        shell(f"cmd media_session volume --stream 3 --set {level} 2>&1")
        if media_volume() == level:
            return
    shell(f"media volume --stream 3 --set {level} 2>&1")
    if media_volume() == level:
        return
    for _ in range(16):
        shell("input keyevent KEYCODE_VOLUME_DOWN")
    for _ in range(level):
        shell("input keyevent KEYCODE_VOLUME_UP")
    if media_volume() not in (None, level):
        log(f"{YELLOW}media volume is {media_volume()}, could not set {level}{RESET}")


class EmulatorLost(Exception):
    """The emulator exited or disappeared from adb."""


WEBVIEW_MINIMUM = 80  # Constants.MINIMUM_WEB_VIEW_VERSION of jellyfin-android, the same in run-matrix.sh


class WebViewTooOld(EmulatorLost):
    """The WebView of the AVD is older than the app needs (an update of it was lost, e.g. killed before the package list
    was written): the AVD fails with this message instead of a chromedriver download error that ends the worker
    (TLS_API_28 on remote-a). Handled like EmulatorLost: the AVD gets one failed step."""


def emulator_alive():
    return emulator_process is None or emulator_process.poll() is None


def system_server_age():
    """pid and seconds since system_server started, (None, 0) while it is not running."""
    pid = shell("pidof system_server")
    if not pid.isdigit():
        # Android 6: pidof ignores its argument and lists every process, ps lists all (8+ needs -A, not this path)
        pid = next((line.split()[1] for line in shell("ps").splitlines() if line.endswith(" system_server")), "")
    if not pid.isdigit():
        return None, 0
    fields = shell(f"cat /proc/uptime /proc/{pid}/stat").split()
    try:
        # stat field 22 is the start time in clock ticks (USER_HZ 100), after the 2 uptime values
        return pid, float(fields[0]) - int(fields[2 + 21]) / 100
    except (IndexError, ValueError):
        return None, 0


def boot_timeout():
    """Stops the emulator started by boot(): one that never reached adb kept running, held the AVD and the port, and
    the next AVD on the port or a later run of the same AVD failed (TLS_API_36)."""
    if emulator_process is not None and emulator_process.poll() is None:
        log(f"{YELLOW}boot timeout, stopping the emulator process{RESET}")
        os.killpg(emulator_process.pid, signal.SIGTERM)
        try:
            emulator_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(emulator_process.pid, signal.SIGKILL)
    if REMOTE:  # ends with the ssh connection, unless it hangs
        emulator_processes("pkill")
    return EmulatorLost("boot timeout")


def wait_for_boot(timeout=300):
    deadline = time.time() + timeout
    while not device_online():
        if not emulator_alive():
            raise EmulatorLost(f"the emulator exited, see {EMULATOR_LOG}")
        if time.time() > deadline:
            raise boot_timeout()
        time.sleep(2)

    log("waiting for OS boot completion...")
    while not (shell_booting("getprop sys.boot_completed") == "1"
               and "package:" in shell_booting("pm path android")
               and shell_booting("getprop init.svc.bootanim") != "running"):
        if not emulator_alive():
            raise EmulatorLost(f"the emulator exited, see {EMULATOR_LOG}")
        if time.time() > deadline:
            raise boot_timeout()
        time.sleep(2)

    # Check Launcher screen
    log("waiting for launcher/home screen...")
    while time.time() < deadline:
        resumed = shell_booting("dumpsys activity activities")
        if "launcher" in resumed.lower() or "home" in resumed.lower():
            break
        time.sleep(2)

    # FIX: Ensure Package Manager service is responsive to incoming APK installation requests
    log("waiting for package manager service to settle...")
    while time.time() < deadline:
        # 'cmd package' or 'pm list packages' fails or hangs if system_server isn't ready
        pm_status = shell("pm list packages android 2>&1")
        if "package:android" in pm_status:
            break
        time.sleep(2)

    # system_server restarts right after boot made an install or Appium command fail. On API 30 they came from
    # set_volume's "cmd media_session" (see there), the wait stays for safety
    log("waiting for system_server to stay up...")
    first_pid = pid = None
    while time.time() < deadline:
        pid, age = system_server_age()
        if pid and first_pid and pid != first_pid:
            log(f"system_server restarted (pid {first_pid} -> {pid})")
        first_pid = pid or first_pid
        if pid and age >= SYSTEM_SERVER_SETTLE:
            break
        time.sleep(2)

    shell("svc power stayon true")
    shell("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
    # No "… has stopped" dialogs: one of Google Play Movies after a reboot covered the app for 9 steps (TLS_API_26).
    # Crashes stay in logcat, where the steps look for them.
    shell("settings put global hide_error_dialogs 1")
    time.sleep(3)  # the tip of the launcher comes a moment after the home screen
    try:
        dismiss_got_it()
    except subprocess.TimeoutExpired:
        log("no uiautomator dump for the first-run tips, skipped")
    set_volume()
    log(f"booted, API {shell('getprop ro.build.version.sdk')}")

def install(apk, app_id, fresh):
    if fresh:
        adb("uninstall", app_id)
    log(f"installing {apk.name}" + (" (fresh, no app data)" if fresh else " (app data kept)"))
    no_streaming = ["--no-streaming"] if int(shell("getprop ro.build.version.sdk") or 99) <= 25 else []
    # system_server restarts after a boot made an install fail (API 30: set_volume's "cmd media_session", see there)
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        result = subprocess.run([ADB, "-s", SERIAL, "install", *no_streaming, "-r", "-t", str(apk)],
                                capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if result.returncode == 0 and shell(f"pm path {app_id}").startswith("package:"):
            break
        error = (result.stderr.strip() or result.stdout.strip() or "app not installed").splitlines()[-1]
        if attempt == INSTALL_ATTEMPTS:
            sys.exit(f"adb install failed {attempt} times: {error}")
        log(f"install failed ({error}), waiting for the system and retrying ({attempt}/{INSTALL_ATTEMPTS})")
        time.sleep(10)
        wait_for_boot()
    # Notifications for the media controls (Android 13+), fails harmlessly below
    shell(f"pm grant {app_id} android.permission.POST_NOTIFICATIONS")
    # No battery optimization: the app asks to disable it with a snackbar that stays (LENGTH_INDEFINITE) over the bottom of
    # the screen while it is not exempt: over the player's control row (the subtitles button of the seek step, the clip
    # ended before the attempts were over: TLS_API_34 / 36) and the settings list. A swipe does not always
    # remove it. appium_flows.dismiss_battery_snackbar stays as the second line.
    shell(f"dumpsys deviceidle whitelist +{app_id}")


# A file manager as file picker for the TV AVDs (checklists/tv-picker.md): they have none, only a stub. Material Files
# (F-Droid, me.zhanghai.android.files_40.apk, downloaded by setup.sh) handles OPEN_DOCUMENT and GET_CONTENT.
PICKER_APP_ID = "me.zhanghai.android.files"
PICKER_APK = HERE / "tv-picker" / "me.zhanghai.android.files_40.apk"


def install_picker():
    """Installs Material Files with access to the shared storage: "All files access" (Android 11+) or the storage
    permission, it would ask on its first start otherwise."""
    if not shell(f"pm path {PICKER_APP_ID}").startswith("package:"):
        if not PICKER_APK.exists():
            sys.exit(f"{PICKER_APK} missing, ./setup.sh downloads it")
        log(f"installing {PICKER_APK.name}")
        adb("install", "-r", str(PICKER_APK), timeout=300, check=True)
    shell(f"appops set {PICKER_APP_ID} MANAGE_EXTERNAL_STORAGE allow")
    shell(f"pm grant {PICKER_APP_ID} android.permission.READ_EXTERNAL_STORAGE")


# Google TV launcher (TLS_TV_34): it keeps showing "Set up Google TV" (sign in to a Google account), its Cancel switches
# the TV to basic mode without internet. Disabled: Home shows the empty fallback home of TV Settings, the tests start the
# app directly. Undo with: adb shell pm enable com.google.android.apps.tv.launcherx
GOOGLE_TV_LAUNCHER = "com.google.android.apps.tv.launcherx"


def disable_google_tv_launcher():
    if not shell(f"pm list packages -e {GOOGLE_TV_LAUNCHER}").strip():
        return  # not installed (phones, Android TV 11) or already disabled
    log("disabling the Google TV launcher (its setup prompt)")
    shell(f"pm disable-user --user 0 {GOOGLE_TV_LAUNCHER}")


# Google Messages (phone images with Google APIs): its debug socket
# @stetho_com.google.android.apps.messaging_devtools_remote looks like a WebView to Appium. Every WebView lookup
# forwards a port to it and waits 2 s for an answer that never comes (adbd: "timeout expired while flushing socket"),
# and on TLS_API_36 adb stopped answering right after such a lookup.
# Undo with: adb shell pm enable com.google.android.apps.messaging
GOOGLE_MESSAGES = "com.google.android.apps.messaging"


def disable_google_messages():
    if not shell(f"pm list packages -e {GOOGLE_MESSAGES}").strip():
        return  # not installed or already disabled
    log("disabling Google Messages (its devtools socket slows every WebView lookup of Appium)")
    shell(f"pm disable-user --user 0 {GOOGLE_MESSAGES}")


def adb_answers():
    try:
        return shell("echo answers", timeout=15) == "answers"
    except subprocess.TimeoutExpired:
        return False


def push_certs():
    env = dict(os.environ, ANDROID_SERIAL=SERIAL)
    result = subprocess.run([str(HERE / "push-certs.sh")], env=env, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:  # e.g. the files not in the MediaStore: the file picker steps fail then
        log(f"{YELLOW}push-certs.sh failed: {(result.stderr or result.stdout).strip()[-300:]}{RESET}")


def launch(app_id):
    # Leanback launcher on TV, normal launcher on phones
    for category in ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER"):
        out = shell(f"monkey -p {app_id} -c {category} 1")
        if "No activities found" not in out:
            return
    log(f"{RED}could not launch {app_id}{RESET}")


# --- Evidence --------------------------------------------------------------------------------------------------------

class Evidence:
    """Collects the request log of the proxy and logcat between start() and stop()."""

    def __init__(self, session_dir, app_id):
        self.app_id = app_id
        self.logcat_file = session_dir / "logcat.txt"
        self.logcat = None
        self.access_offset = 0
        self.logcat_offset = 0

    def start_logcat(self):
        if self.logcat:
            self.logcat.terminate()
        # Appends, a reboot ends the logcat process, it is restarted afterwards
        self.logcat = subprocess.Popen([ADB, "-s", SERIAL, "logcat", "-v", "threadtime"],
                                       stdout=open(self.logcat_file, "a"), stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL)

    def stop_logcat(self):
        if self.logcat:
            self.logcat.terminate()
            self.logcat = None

    def mark(self):
        self.access_offset = ACCESS_LOG.stat().st_size if ACCESS_LOG.exists() else 0
        self.logcat_offset = self.logcat_file.stat().st_size if self.logcat_file.exists() else 0

    @staticmethod
    def _read_from(path, offset):
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace").splitlines()

    def collect(self):
        requests = []
        for line in self._read_from(ACCESS_LOG, self.access_offset):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            req = entry.get("request", {})
            # Only this worker's ports: its mTLS port and its CA-only port (the CA-only steps)
            if not req.get("host", "").endswith((f":{SERVER_PORT}", f":{CA_ONLY_PORT}")):
                continue
            uri = req.get("uri", "")
            kind = next((name for name, pattern in REQUEST_TYPES if pattern.search(uri)), "api")
            # Caddy logs a request when it ends: start time for long requests (a video stream) logged steps later
            start = entry.get("ts", 0) - entry.get("duration", 0)
            requests.append(dict(status=entry.get("status", 0), method=req.get("method"), uri=uri, kind=kind, start=start,
                                 cn=req.get("tls", {}).get("client_common_name", ""), proto=req.get("proto", ""),
                                 size=entry.get("size", 0)))
        lines = self._read_from(self.logcat_file, self.logcat_offset)
        logcat = []
        for number, line in enumerate(lines):
            if not LOGCAT_PATTERNS.search(line) or LOGCAT_EXPECTED.search(line):
                continue
            if "FATAL EXCEPTION" in line:
                # "Process: <package>, PID: n" on the next line: a crash of another app (Google Play Movies after a
                # reboot) is no failure of the app under test
                process = re.search(r"Process: ([\w.]+)", lines[number + 1]) if number + 1 < len(lines) else None
                if process and not process.group(1).startswith(self.app_id):
                    line = line.replace("FATAL EXCEPTION", f"other app crashed ({process.group(1)})")
            logcat.append(line)
        return requests, logcat


def matches_kind(request, kind):
    """"streaming" matches streaming-hls, streaming-direct, …"""
    return request["kind"] == kind or request["kind"].startswith(kind + "-")


def confirm_late_requests(pending, requests, results):
    """Passes earlier steps that failed only because a request type was missing when a request of that type, started
    during the step, is logged now: Caddy logs a request when it ends, a direct video stream (one request, API 33+
    with the host GPU decodes the "transcode" movie itself) ends only when the player stops in a later step."""
    for item in list(pending):
        found = {kind for kind in item["kinds"]
                 if any(matches_kind(r, kind) and item["start"] <= r["start"] <= item["end"] for r in requests)}
        item["kinds"] = [kind for kind in item["kinds"] if kind not in found]
        result = item["result"]
        for kind in found:
            note = f"no {kind} requests through the proxy"
            result["notes"] = [n for n in result["notes"] if n != note]
            result["evidence"] = [line for line in result["evidence"] if line != f"Problem: {note}"]
            result["evidence"].append(f"{kind} request started in this step, logged later when it ended")
        if not item["kinds"]:
            # By eye the answer stays the user's, the evidence is completed only
            if result["result"] == "fail" and not result.get("by_eye"):
                result["result"] = "pass"
            pending.remove(item)
            log(f"{GREEN}{result['id']}: {'' if result.get('by_eye') else 'passed, '}its {', '.join(sorted(found))} "
                f"request was logged after the step{RESET}")


def summarize(requests, logcat):
    """Evidence of a step as markdown lines, also printed."""
    lines = []
    if requests:
        by_kind = collections.Counter(r["kind"] for r in requests)
        kinds = ", ".join(f"{kind} {count}" for kind, count in by_kind.most_common())
        cns = ", ".join(sorted({r["cn"] or "-" for r in requests}))
        mb = sum(r["size"] for r in requests) / 1e6
        lines.append(f"Requests via mTLS proxy: {len(requests)} ({kinds}), {mb:.1f} MB, client certificate: {cns}")
        errors = [r for r in requests if r["status"] >= 400]
        if errors:
            lines.append(f"Errors: {len(errors)}")
            by_uri = collections.Counter((r["status"], r["uri"].split("?")[0]) for r in errors)
            for (status, uri), count in by_uri.most_common(8):
                lines.append(f"  {status} {uri}" + (f" (×{count})" if count > 1 else ""))
    else:
        lines.append("Requests via mTLS proxy: none")
    if logcat:
        unique = list(dict.fromkeys(re.sub(r"^\S+ \S+\s+\d+\s+\d+ ", "", line) for line in logcat))
        lines.append(f"Logcat: {len(logcat)} notable lines")
        lines.extend(f"  {line[:180]}" for line in unique[:10])
    return lines


def print_evidence(lines):
    for line in lines:
        color = RED if line.startswith(("Errors", "  4", "  5", "Problem")) or "FATAL" in line or "Exception" in line else ""
        if line.startswith("Requests via mTLS proxy: none"):
            color = YELLOW
        print(f"  {color}{line}{RESET}")


# --- Checklist -------------------------------------------------------------------------------------------------------

# --checklist: another checklist (checklists/<name>.md) of the app its name starts with ("tv…": the TV app, otherwise
# the mobile app), e.g. mobile-system, tv-phone
CHECKLISTS = {"mobile": "mobile", "tv": "tv"}


def checklist_app(name):
    return "tv" if name.startswith("tv") else "mobile"


def checklist_name(app):
    return CHECKLISTS[app]


def load_checklist(app):
    """checklists/<app>.md (or --checklist): "## id: title", instruction lines, "> action" lines run by the script
    first, "? kinds" request types that must go through the proxy during the step (images, streaming, …)."""
    steps = []
    for line in (HERE / "checklists" / f"{checklist_name(app)}.md").read_text().splitlines():
        if line.startswith("## "):
            step_id, _, title = line[3:].partition(":")
            steps.append(dict(id=step_id.strip(), title=title.strip(), text=[], actions=[], expect=[]))
        elif steps and line.startswith("> "):
            steps[-1]["actions"].append(line[2:].strip())
        elif steps and line.startswith("? "):
            steps[-1]["expect"] += line[2:].split()
        elif steps and line.strip():
            steps[-1]["text"].append(line.rstrip())
    return steps


def appium_ready():
    """Appium answers on APPIUM_PORT. Not port_open: the local end of a tunnel accepts without Appium behind it."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{APPIUM_PORT}/status", timeout=3) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def start_appium():
    """Appium server for the clicking automation, reused when one already answers on APPIUM_PORT. With REMOTE on the
    remote machine (UiAutomator2 server and chromedriver are reached through adb forwards there)."""
    global appium_process
    if appium_ready():
        return
    log(f"starting Appium on port {APPIUM_PORT}{f' on {REMOTE}' if REMOTE else ''} (log: {APPIUM_LOG.name})")
    appium_process = subprocess.Popen(on_host(["appium", "--port", str(APPIUM_PORT)], tty=True),
                                      env=dict(os.environ, ANDROID_HOME=str(SDK)),
                                      stdout=open(APPIUM_LOG, "w"), stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if appium_ready():
            return
        time.sleep(1)
    sys.exit(f"Appium did not start, see {APPIUM_LOG}")


def stop_appium():
    if appium_process and appium_process.poll() is None:
        appium_process.terminate()


def remote_problem(remote):
    """What keeps a remote machine from running emulators right now, "" when nothing: KVM usable by the user and a
    desktop session logged in (the host GPU needs its display). After a reboot without a login both were missing on
    user@remote-b: every emulator exited at once and its slot used up six AVDs of the run in seconds.
    The login screen only (a logout, a GDM restart) is the same."""
    test = ('[ -r /dev/kvm ] && [ -w /dev/kvm ] || echo "KVM: /dev/kvm not accessible for $USER '
            '(sudo usermod -aG kvm $USER; until then only while logged in on the desktop)"; '
            '[ -n "$XAUTHORITY" ] && [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] '
            '|| echo "no desktop session of $USER (log in on the machine: the host GPU needs its display)"')
    try:
        out = subprocess.run([*SSH, remote, REMOTE_ENV + test], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"{remote} did not answer within {SSH_TIMEOUT} s (ssh login or its desktop session check)"
    problems = out.stdout.strip() or (out.stderr.strip() if out.returncode else "")
    return f"{remote} cannot run emulators:\n  " + problems.replace("\n", "\n  ") if problems else ""


def check_remotes(remotes, quiet=False):
    """Every remote machine can run emulators, else all the problems are printed (not only the first machine's) and the
    exit code is EXIT_REMOTE. Without quiet the machines that are ready are listed as well (--check-remote)."""
    problems = []
    for remote in remotes:
        problem = remote_problem(remote)
        if problem:
            problems.append(problem)
        elif not quiet:
            print(f"{remote}: ready (ssh, KVM, desktop session)")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        sys.exit(EXIT_REMOTE)


def check_appium(remote=REMOTE):
    """appium on PATH with the UiAutomator2 driver 5.x: 6.x+ needs API 26 (server minSdk). On remote as well."""
    where = f" on {remote}" if remote else ""
    try:
        out = subprocess.run(on_host(["appium", "driver", "list", "--installed", "--json"], remote),
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120).stdout
    except FileNotFoundError:
        sys.exit("appium not found, install it: npm install -g appium && appium driver install uiautomator2@5.0.7")
    try:
        version = json.loads(out or "{}").get("uiautomator2", {}).get("version")
    except ValueError:
        version = None
    if not version:
        sys.exit(f"Appium or its UiAutomator2 driver missing{where}: npm install -g appium && "
                 "appium driver install uiautomator2@5.0.7")
    if not version.startswith("5."):
        print(f"{YELLOW}Appium UiAutomator2 driver {version}: 6.x+ does not run on API < 26, "
              f"5.0.7 runs on all (appium driver uninstall uiautomator2; appium driver install uiautomator2@5.0.7)"
              f"{RESET}")


def flow_module(app):
    """appium_flows (mobile app) or tv_flows (TV app)."""
    if app == "tv":
        import tv_flows
        return tv_flows
    import appium_flows
    return appium_flows


def run_flow(device, action, session_dir, name, app):
    """Runs "flow [KEY=VALUE …]" of the app's flow module, progress lines shown live. Returns (passed, reason)."""
    log(f"appium {action}")
    reason = flow_module(app).run(device, action)
    if reason:
        print(f"  {RED}{reason}{RESET}")
        try:
            (session_dir / f"{name}-screen.xml").write_text(device.native().page_source)
        except Exception:  # the session or the app is gone
            pass
    return not reason, reason


def run_action(action, app_id, evidence, device=None, avd=None):
    log(f"action: {action}")
    if device and action in ("force-stop", "launch", "reboot", "clear-data"):
        device.close()  # the WebView connection of the Appium session does not survive it
    if action in ("force-stop", "clear-data", "reboot"):
        # The app in the background first: Android releases its windows and surfaces (player, WebView). Killed in the
        # foreground, the emulator's host GPU lost its contexts ("error null ctx") and the emulator exited (restart,
        # TLS_API_30 / TLS_TV_34; "pm clear" kills the app as well: 4 emulators of one run).
        # The checklists stop playback before ("> appium stop-player")
        shell("input keyevent KEYCODE_HOME")
        time.sleep(HOME_WAIT / 2)
        # In the background the app is asked to give up its graphics memory (WebView and renderer contexts): even
        # after Home and 5 s the kill took the emulator with it at times (TLS_API_23 16 ms after the kill, TLS_API_34
        # and 36 later with "invalid color buffer"). Not on every image, the answer is ignored
        shell(f"am send-trim-memory {app_id} COMPLETE 2>&1")
        time.sleep(HOME_WAIT / 2)
        shell(f"am force-stop {app_id}")
    if action == "clear-data":
        shell(f"pm clear {app_id}")
        # pm clear revokes runtime permissions: the notification prompt (Android 13+) would block the download
        shell(f"pm grant {app_id} android.permission.POST_NOTIFICATIONS")
    elif action == "launch":
        launch(app_id)
    elif action == "reboot":
        # The device is switched off and on: a clean power off, then a new emulator process (like a phone, and with
        # fresh graphics state of the emulator), not "adb reboot" inside the running one (as before)
        evidence.stop_logcat()
        kill_emulator()
        boot(avd, keep_log=True)
        disable_google_tv_launcher()
        disable_google_messages()
        evidence.start_logcat()
        # No settle time: the home flow relaunches the app once if the cold start hits its connection timeout
    elif action == "unlock":
        unlock_screen()
    elif action == "push-certs":
        push_certs()
    elif action == "install-picker":
        install_picker()
    elif action == "uninstall-picker":
        adb("uninstall", PICKER_APP_ID)
    elif action != "force-stop":
        log(f"{RED}unknown action {action}{RESET}")


def keyguard_showing():
    """The lock screen is shown and not occluded. Android 9: "isStatusBarKeyguard=true" in the window policy, the
    focus on the StatusBar window (NotificationShade on Android 11+)."""
    policy = shell("dumpsys window policy")
    if re.search(r"(isStatusBarKeyguard|mShowingLockscreen|isKeyguardShowing|mKeyguardShowing)\s*=\s*true", policy) \
            and "mKeyguardOccluded=true" not in policy:
        return True
    focus = next((line for line in shell("dumpsys window").splitlines() if "mCurrentFocus" in line), "")
    return bool(re.search(r"(StatusBar|NotificationShade|Keyguard)", focus)) and "mDreamingLockscreen=true" in policy


def unlock_screen():
    """Enters the PIN of the system store steps (appium_flows.LOCK_PIN) on the lock screen, after a reboot of
    checklists/mobile-system.md: Android 6 - 9 require a screen lock for the system store. Without a lock the
    keyguard is gone after "wm dismiss-keyguard" already."""
    for attempt in range(3):
        shell("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        time.sleep(2)
        if not keyguard_showing():
            log("screen unlocked" if attempt else "no lock screen")
            return
        # Swipe up to the PIN pad (KEYCODE_MENU does it on older versions), then the PIN
        shell("input keyevent KEYCODE_MENU; input swipe 540 1800 540 300 300")
        time.sleep(1)
        shell(f"input text {LOCK_PIN}; input keyevent KEYCODE_ENTER")
        time.sleep(3)
    if keyguard_showing():
        log(f"{RED}the lock screen is still shown after entering the PIN{RESET}")
    else:
        log("screen unlocked")


def lock_pin_left():
    """True when the AVD has a screen lock PIN: the system store steps set one (Android 6 - 9 require it) and an
    interrupted run leaves it, the next run starts at the PIN form. Android 7+: the keyguard reports "secure=true"
    (Android 9 keeps the PIN behind a synthetic password, no gatekeeper.password.key: TLS_API_28); Android 6
    has no such line, its PIN file is empty without a lock (root, images without su: no lock). "locksettings verify"
    first (Android 8.1+): the keyguard keeps "secure=true" after the PIN is removed until the screen turns off."""
    verify = shell("locksettings verify 2>&1")
    if "verified successfully" in verify:
        return False
    if "didn't match" in verify or "can't be null" in verify:
        return True
    if re.search(r"^\s*secure=true", shell("dumpsys window policy"), re.M):
        return True
    out = shell("su 0 stat -c %s /data/system/gatekeeper.password.key /data/system/password.key 2>/dev/null")
    return any(size.isdigit() and int(size) > 0 for size in out.split())


def remove_left_lock(device, avd):
    """A PIN left by an earlier run: unlocked, then system-cleanup (credentials and PIN, like clean-lock.sh)."""
    if not lock_pin_left():
        return
    log(f"{YELLOW}screen lock PIN left by an earlier run, removing it{RESET}")
    unlock_screen()
    if not device:
        log(f"{YELLOW}no Appium session, the PIN stays (./clean-lock.sh {avd}){RESET}")
        return
    import appium_flows
    reason = appium_flows.run(device, "system-cleanup")
    device.close()
    if reason or lock_pin_left():
        log(f"{RED}the PIN is still set ({reason or 'system-cleanup passed'}){RESET}")
    else:
        log("PIN and system credentials removed")


USER_CA_DIRS = ("/data/misc/user/0/cacerts-added", "/data/misc/keychain/cacerts-added")  # Android 7+, Android 6


def system_ca_left():
    """True when the user certificate store may hold a CA of an earlier run (needs root: "su 0", not available: unknown,
    counts as left). An interrupted run leaves it (the emulator froze in system-ca, the teardown timed out): the app
    trusts the proxy CA from then on and missing-ca fails with "still reaches the server" (TLS_API_36, three
    runs until system-cleanup). Files with content only: Android 16 leaves an empty one after Clear credentials."""
    for directory in USER_CA_DIRS:
        out = shell(f"su 0 find {directory} -type f -size +0c 2>&1").strip()
        if "No such file" in out:
            continue
        if out:  # a file, or an error (no su, no find): left or unknown
            return True
    return False


def remove_left_system_ca(device, avd):
    """A CA left in the user certificate store by an earlier run: system-cleanup (Clear credentials) first."""
    log(f"{YELLOW}CA or credentials left in the user certificate store by an earlier run, running system-cleanup{RESET}")
    unlock_screen()
    import appium_flows
    reason = appium_flows.run(device, "system-cleanup")
    device.close()
    if system_ca_left():
        # Files Clear credentials does not know (not in the keystore): removed directly, the trust store reads the
        # directory on every lookup
        for directory in USER_CA_DIRS:
            shell(f"su 0 sh -c 'rm -f {directory}/*' 2>&1")
    if reason or system_ca_left():
        log(f"{RED}the user certificate store is not clean ({reason or 'system-cleanup passed'}){RESET}")
    else:
        log("user certificate store cleared")


def cleanup_system_store(device, results):
    """Teardown: system-cleanup (credentials and PIN) when a system store step ran and system-cleanup did not pass in
    this session, or a PIN is left. As the last step only, it was skipped whenever the session ended before it (an
    emulator lost mid-run), and the next run started at the PIN form (TLS_API_24 / 28)."""
    try:
        if not device_online():
            return
        system_steps = any(r["id"].startswith("system-") for r in results)
        cleaned = any(r["id"] == "system-cleanup" and r["result"] == "pass" for r in results)
        if not (system_steps and not cleaned) and not lock_pin_left():
            return
        log(f"{YELLOW}teardown: system store or screen lock left by this session, running system-cleanup{RESET}")
        unlock_screen()
        import appium_flows
        reason = appium_flows.run(device, "system-cleanup")
        log(f"{RED}teardown: {reason}{RESET}" if reason else "teardown: system store and PIN cleared")
    except Exception as error:  # the teardown must not hide the session's own result
        log(f"{RED}teardown failed: {type(error).__name__}: {error}{RESET}")


STUCK_THREADS = """pid=$(pgrep -f '[q]emu-system.* -avd %s -port %d ' | head -1)
echo "emulator pid: ${pid:-not found}, console: $(timeout 10 %s -s %s emu avd status 2>&1 | tr '\\n' ' ')"
[ -n "$pid" ] || exit 0
for pass in 1 2; do
    echo "threads $(date +%%T): tid state utime stime name"
    for task in /proc/$pid/task/*; do
        echo "${task##*/} $(sed 's/.*) //' $task/stat | cut -d' ' -f1,12,13) $(cat $task/comm)"
    done
    [ $pass = 1 ] && sleep 10
done
echo "adb devices: $(timeout 10 %s devices | tr '\\n' ' ')"
echo "adb reconnect: $(timeout 10 %s -s %s reconnect 2>&1)"
sleep 3
echo "adb shell after the reconnect: $(timeout 15 %s -s %s shell echo answers 2>&1 || echo no answer)"
"""


def record_stuck_emulator(session_dir, avd):
    """emulator-stuck.txt: whether the emulator console answers and state (R running, S sleeping, D uninterruptible)
    and CPU ticks of every thread of the emulator process, twice 10 s apart. The threads whose ticks grow are the ones
    that spin, a guest that only waits has idle "CPU n/KVM" threads. Then whether adb answers again after "adb
    reconnect": on TLS_API_36 the guest went on (the WebView kept sending requests through the proxy for minutes)
    while every adb command timed out, starting when Appium listed the WebViews (adb forward to the
    devtools sockets). gdb may not attach (ptrace_scope 1: only an ancestor of the emulator)."""
    script = STUCK_THREADS % (avd, PORT, ADB_ON_HOST, SERIAL, ADB_ON_HOST, ADB_ON_HOST, SERIAL, ADB_ON_HOST, SERIAL)
    try:
        output = subprocess.run(on_host(["sh", "-c", script]), capture_output=True, text=True, timeout=120,
                                stdin=subprocess.DEVNULL)
        (session_dir / "emulator-stuck.txt").write_text(output.stdout + output.stderr)
        print(f"  {DIM}emulator process and adb state: emulator-stuck.txt{RESET}")
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"  {YELLOW}threads of the emulator process not recorded: {error}{RESET}")


def screenshot(session_dir, name):
    """File name of the screenshot, None when screencap hangs: adb does not answer any more while the guest goes on
    (TLS_API_36, see record_stuck_emulator), or the emulator's graphics are stuck (seen on API 30 with the host GPU)."""
    path = session_dir / f"{name}.png"
    try:
        with open(path, "wb") as f:
            subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"], stdout=f, timeout=30,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        print(f"  {YELLOW}screenshot timed out{RESET}")
        return None
    return path.name


def write_report(session_dir, meta, results):
    icons = {"pass": "✅", "fail": "❌", "skip": "⏭️"}
    out = [f"# Manual test {meta['app']} on {meta['avd']}", "",
           f"{meta['started']}, API {meta['api']}, {meta['apk']}, WebView: {meta['webview']}", "",
           "| Step | Result | Note |", "|---|---|---|"]
    for r in results:
        out.append(f"| {r['id']}: {r['title']} | {icons.get(r['result'], r['result'])} | {' / '.join(r['notes'])} |")
    for r in results:
        out += ["", f"## {r['id']}: {r['title']} {icons.get(r['result'], '')}", ""]
        out += [f"Note: {note}" for note in r["notes"]]
        out += ["```", *r["evidence"], "```"]
        out += [f"![{shot}]({shot})" for shot in r["screenshots"]]
    (session_dir / "REPORT.md").write_text("\n".join(out) + "\n")


class EndSession(Exception):
    """q in the checklist: ends the checklist of the current AVD, the run continues."""


ICONS = {"pass": "✅", "fail": "❌", "skip": "⏭️"}


MAX_RECOVERIES = 2  # per AVD: an emulator that dies again and again is no flaky host GPU, the AVD fails


def emulator_death_cause():
    """What the log of the dead emulator says (the host GPU lost its contexts: gfxstream "null ctx")."""
    try:
        text = EMULATOR_LOG.read_text(errors="replace")
    except OSError:
        return "no emulator log"
    if "null ctx" in text:
        return "gfxstream null ctx (host GPU)"
    if "bad color buffer" in text:
        return "gfxstream bad color buffer (host GPU)"
    return "unknown, see the emulator log"


def recover_emulator(avd, app_id, session_dir, evidence, device, count, number, step_id):
    """The emulator died during a step (or between two): the same AVD is booted again, its data kept (the app, the
    certificates, the logins), the app in front. The caller runs the step again. Logged as "RECOVERY n/m" (watch-run.sh
    lists these lines), the log of the dead emulator is kept as emulator-died-n.log. Returns the cause."""
    cause = emulator_death_cause()
    log(f"{RED}RECOVERY {count}/{MAX_RECOVERIES}: the emulator of {avd} died in step {number} ({step_id}): {cause}; "
        f"booting it again{RESET}")
    if EMULATOR_LOG.exists():
        (session_dir / f"emulator-died-{count}.log").write_bytes(EMULATOR_LOG.read_bytes())
    evidence.stop_logcat()
    if device:
        device.close()  # the Appium session is gone with the emulator
    boot(avd)  # an emulator process without adb is killed there
    disable_google_tv_launcher()
    disable_google_messages()
    evidence.start_logcat()
    launch(app_id)
    log(f"{GREEN}RECOVERY {count}/{MAX_RECOVERIES} done: {avd} is up again{RESET}")
    return cause


def run_session(avd, app, apk, app_id, steps, session_dir, keep, auto=False):
    """Checklist on one AVD, returns the step results."""
    boot(avd)
    disable_google_tv_launcher()
    disable_google_messages()
    install(apk, app_id, fresh=not keep)
    push_certs()
    session_dir.mkdir(parents=True)
    webview = shell("dumpsys webviewupdate | grep -m1 'Current WebView package'").partition(":")[2].strip()
    if not webview:  # Android 7: the dump is empty, the provider is a setting
        provider = shell("settings get global webview_provider")
        version = shell(f"dumpsys package {provider} | grep -m1 versionName").partition("=")[2]
        webview = f"({provider}, {version})" if version else "?"
    meta = dict(app=app, avd=avd, apk=apk.name, api=shell("getprop ro.build.version.sdk"), webview=webview,
                started=f"{datetime.now():%Y-%m-%d %H:%M}")
    old_version = re.search(r"(\d+)\.\d+\.\d+\.\d+", webview)  # no version: unknown (Android 6), not an error
    if app == "mobile" and old_version and int(old_version[1]) < WEBVIEW_MINIMUM:
        raise WebViewTooOld(f"WebView {old_version[0]} of {avd} is older than {WEBVIEW_MINIMUM}, the minimum of the mobile "
                            f"app: ./webview-install.sh [-r HOST] {meta['api']} (webview-apks/)")
    evidence = Evidence(session_dir, app_id)
    evidence.start_logcat()

    print(f"\n{BOLD}{avd} ({app}), server: https://10.0.2.2:{SERVER_PORT}{RESET}  certificates: Download/jellyfin-tls/proxy/ "
          f"(.p12 password: jellyfin), import URL: http://10.0.2.2:8930/")
    print(f"Report: {session_dir / 'REPORT.md'}\n")
    launch(app_id)

    device = None
    if any(action.startswith("appium ") for step in steps for action in step["actions"]):
        import appium_flows
        version = re.search(r"\d+\.\d+\.\d+\.\d+", webview)
        if version and app == "mobile":  # the TV app has no WebView
            appium_flows.ensure_chromedriver(version.group(0), log=log)
            if REMOTE:  # Appium runs there, same path in the same home
                subprocess.run(["rsync", "-a", f"{appium_flows.CHROMEDRIVER_DIR}/",
                                f"{REMOTE}:{appium_flows.CHROMEDRIVER_DIR.relative_to(Path.home())}/"],
                               stdin=subprocess.DEVNULL, check=True)
        device = appium_flows.Device(SERIAL, f"http://127.0.0.1:{APPIUM_PORT}",
                                     log=lambda msg: print(f"  {DIM}{msg}{RESET}"))
    if lock_pin_left():
        remove_left_lock(device, avd)
        # The removal ends in Android's Settings: the app in front again, the first step expects its connect screen
        # (TLS_API_26: setup, login and browse failed after a PIN left by a stopped run)
        launch(app_id)
    elif app == "mobile" and device and system_ca_left():
        remove_left_system_ca(device, avd)
        launch(app_id)

    results = []
    pending_kinds = []  # steps that failed only for missing request types, see confirm_late_requests
    recoveries = 0  # emulators of this AVD booted again after they died (auto only), at most MAX_RECOVERIES
    try:
        for number, step in enumerate(steps, 1):
            result = dict(id=step["id"], title=step["title"], result="skip", notes=[], evidence=[], screenshots=[],
                          by_eye=not auto)
            if not device_online() or not emulator_alive():
                if not auto or recoveries >= MAX_RECOVERIES:
                    raise EmulatorLost(f"the emulator is gone (exited or disconnected), see {EMULATOR_LOG}")
                # It died after the last step had ended (or while this one was about to start)
                recoveries += 1
                cause = recover_emulator(avd, app_id, session_dir, evidence, device, recoveries, number, step["id"])
                result["notes"].append(f"RECOVERY {recoveries}: the emulator died before this step ({cause}), booted again")
                result["recoveries"] = recoveries
            while True:
                print(f"{BOLD}[{avd} {number}/{len(steps)}] {step['id']}: {step['title']}{RESET}")
                for line in step["text"]:
                    print(f"  {line}")
                evidence.mark()
                step_start = time.time()
                flows = [action.split(None, 1)[1] for action in step["actions"] if action.startswith("appium ")]
                if auto and not flows:
                    print(f"  {DIM}manual step, skipped with --auto{RESET}")
                    result["notes"].append("manual step")
                    answer = "s"
                    break
                problems = []
                for action in step["actions"]:
                    if action.startswith("appium "):
                        passed, reason = run_flow(device, action.split(None, 1)[1], session_dir,
                                                  f"{number:02d}-{step['id']}", app)
                        result["notes"] += device.notes
                        device.notes.clear()
                        if not passed:
                            problems.append(reason)
                            break
                    else:
                        run_action(action, app_id, evidence, device, avd)
                if not auto:
                    input(f"{DIM}  Enter when done{RESET} ")
                time.sleep(1)  # last requests and log lines
                if auto and (not device_online() or not emulator_alive()):
                    # The emulator died during this step: boot it again and run the step again (not a failure of the app)
                    if recoveries >= MAX_RECOVERIES:
                        result["notes"].append(f"the emulator died again ({MAX_RECOVERIES} recoveries used)")
                        raise EmulatorLost(f"the emulator died in step {number} ({step['id']}) after {recoveries} "
                                           f"recoveries, see {EMULATOR_LOG}")
                    recoveries += 1
                    cause = recover_emulator(avd, app_id, session_dir, evidence, device, recoveries, number, step["id"])
                    result["notes"].append(f"RECOVERY {recoveries}: the emulator died in this step ({cause}), booted "
                                           f"again, the step run again")
                    result["recoveries"] = recoveries
                    result["screenshots"].clear()
                    pending_kinds[:] = [p for p in pending_kinds if p["result"] is not result]
                    continue
                requests, logcat = evidence.collect()
                result["evidence"] = summarize(requests, logcat)
                confirm_late_requests(pending_kinds, requests, results)
                # "streaming" matches streaming-hls, streaming-direct, …
                missing = [kind for kind in step["expect"] if not any(matches_kind(r, kind) for r in requests)]
                flow_failed = bool(problems)
                problems += [f"no {kind} requests through the proxy" for kind in missing]
                problems += [f"crash: {line.strip()[:120]}" for line in logcat if "FATAL EXCEPTION" in line][:1]
                if missing and not flow_failed and len(problems) == len(missing):
                    # A video stream that is still playing is logged only when it ends, maybe in a later step
                    pending_kinds.append(dict(result=result, kinds=missing, start=step_start, end=time.time()))
                result["evidence"] += [f"Problem: {problem}" for problem in problems]
                print_evidence(result["evidence"])
                shot = screenshot(session_dir, f"{number:02d}-{step['id']}")
                if shot:
                    result["screenshots"].append(shot)
                else:
                    stuck = ("the emulator's graphics are stuck (adb answers)" if adb_answers()
                             else "adb does not answer")
                    problems.append(f"screenshot timed out, {stuck}")
                    result["evidence"].append(f"Problem: {problems[-1]}")
                    if auto and not screenshot(session_dir, f"{number:02d}-{step['id']}"):
                        # Stuck for good: the next steps only wait for their timeouts (TLS_API_36 hung for 15
                        # minutes). The session ends here
                        result["notes"] += problems
                        record_stuck_emulator(session_dir, avd)
                        raise EmulatorLost(f"{stuck} (two screenshots timed out)")
                if auto:
                    answer = "f" if problems else "p"
                    result["notes"] += problems
                    print(f"  {RED + 'FAIL' if problems else GREEN + 'PASS'}{RESET}")
                    break
                if flows and not problems:
                    print(f"  {GREEN}automated checks passed{RESET}")
                answer = ""
                while answer not in ("p", "f", "s", "r"):
                    answer = input(f"  {GREEN}p{RESET}ass / {RED}f{RESET}ail / s=skip, n=note, c=screenshot, "
                                   f"r=redo, q=end this AVD: ").strip().lower()
                    if answer == "n":
                        result["notes"].append(input("  note: ").strip())
                    elif answer == "c":
                        shot = screenshot(session_dir, f"{number:02d}-{step['id']}-{len(result['screenshots'])}")
                        if shot:
                            result["screenshots"].append(shot)
                            print("  screenshot saved")
                    elif answer == "q":
                        raise EndSession
                if answer != "r":
                    break
                result["screenshots"].clear()
            result["result"] = {"p": "pass", "f": "fail", "s": "skip"}[answer]
            if answer == "f" and not result["notes"] and not auto:
                result["notes"].append(input("  what went wrong? ").strip())
            results.append(result)
            write_report(session_dir, meta, results)
            print()
    except EndSession:
        print(f"  checklist on {avd} ended")
    except EmulatorLost as error:
        print(f"  {RED}{error}{RESET}")
        if "result" in locals() and result not in results:
            result["result"] = "fail"
            result["notes"].append(str(error))
            results.append(result)
    finally:
        if pending_kinds:
            time.sleep(2)
            confirm_late_requests(pending_kinds, evidence.collect()[0], results)
        if device:
            cleanup_system_store(device, results)
            device.close()
        evidence.stop_logcat()
        # A copy with the results of the session
        if EMULATOR_LOG.exists():
            (session_dir / "emulator.log").write_bytes(EMULATOR_LOG.read_bytes())
        write_report(session_dir, meta, results)
        # Read by the --jobs parent for the common SUMMARY.md
        (session_dir / "results.json").write_text(json.dumps(results, default=str))

    counts = collections.Counter(r["result"] for r in results)
    print(f"{BOLD}{avd}: {counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped{RESET}\n")
    return results


def write_summary(run_dir, runs, checklists):
    """Matrix step × AVD per app, notes of failed steps below."""
    out = [f"# Manual tests {run_dir.name}", ""]
    for app, steps in checklists.items():
        app_runs = [(avd, results) for avd, run_app, results in runs if run_app == app]
        if not app_runs:
            continue
        out += [f"## {app}", "", "| Step | " + " | ".join(avd for avd, _ in app_runs) + " |",
                "|---|" + "---|" * len(app_runs)]
        for step in steps:
            cells = []
            for _, results in app_runs:
                result = next((r for r in results if r["id"] == step["id"]), None)
                cells.append(ICONS.get(result["result"], "") if result else "")
            out.append(f"| {step['id']} | " + " | ".join(cells) + " |")
        out.append("")
        for avd, results in app_runs:
            for r in results:
                if r["result"] == "fail" or r["notes"]:
                    out.append(f"- {ICONS.get(r['result'], '')} {avd} {r['id']}: {' / '.join(r['notes'])} "
                               f"([report]({avd}-{app}/REPORT.md))")
        out.append("")
    recovered = [(avd, r) for avd, _, results in runs for r in results if r.get("recoveries")]
    if recovered:
        out += ["## Emulators that died and were booted again", ""]
        out += [f"- {avd} {r['id']}: " + " / ".join(n for n in r["notes"] if n.startswith("RECOVERY")) for avd, r in recovered]
        out.append("")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "SUMMARY.md").write_text("\n".join(out))


def job_ports(slot):
    """Emulator, Appium, mTLS and CA-only proxy port of worker slot 0, 1, …: 5590/4724/8920/8921, 5592/4725/8922/8931,
    5594/4726/8923/8932, … (the proxy ports from the Caddyfile)."""
    return 5590 + 2 * slot, 4724 + slot, MTLS_PORTS[slot], CA_ONLY_PORTS[slot]


def start_tunnel(remote, slots, adb_port):
    """ssh tunnel for the worker slots on remote: the adb server there to adb_port here, their Appium ports here,
    their mTLS proxy ports and the file server (8930) of this machine to the remote loopback, the emulator's 10.0.2.2.
    Returns the ssh process."""
    subprocess.run(on_host([f"{REMOTE_SDK}/platform-tools/adb", "start-server"], remote), capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=60)
    # An adb run here while the tunnel was down started its own server on adb_port (adb -L tcp:15037 fork-server,
    # network interrupted): the tunnel got only [::1], the workers reached the empty local server
    if port_open(adb_port):
        log(f"{YELLOW}port {adb_port} in use, stopping the adb server there{RESET}")
        subprocess.run(["pkill", "-f", f"[a]db -L tcp:{adb_port} "], stdin=subprocess.DEVNULL)
        for _ in range(10):
            if not port_open(adb_port):
                break
            time.sleep(0.5)
    # Local ends bound to 127.0.0.1: a port in use there ends ssh (ExitOnForwardFailure), with "localhost" ssh went on
    # with [::1] alone
    forwards = ["-L", f"127.0.0.1:{adb_port}:127.0.0.1:5037", "-R", "8930:127.0.0.1:8930"]
    for slot in slots:
        _, appium_port, server_port, ca_only_port = job_ports(slot)
        forwards += ["-L", f"127.0.0.1:{appium_port}:127.0.0.1:{appium_port}",
                     "-R", f"{server_port}:127.0.0.1:{server_port}",
                     "-R", f"{ca_only_port}:127.0.0.1:{ca_only_port}"]
    proxy = ", ".join(f"{job_ports(s)[2]}/{job_ports(s)[3]}" for s in slots)
    log(f"tunnel to {remote} (adb, Appium, proxy ports {proxy}, 8930)")
    tunnel = subprocess.Popen([*SSH, "-N", "-o", "ExitOnForwardFailure=yes", *forwards, remote],
                              stdin=subprocess.DEVNULL, start_new_session=True)
    # The local ends listen at once, the remote ones (-R) are confirmed later: wait until they listen there (an emulator
    # booted before got "Failed to connect to /10.0.2.2:8920" once)
    reverse = [8930, *(port for slot in slots for port in job_ports(slot)[2:])]
    for _ in range(30):
        if tunnel.poll() is not None:
            sys.exit(f"the ssh tunnel to {remote} failed (ports in use here or there? an older tunnel still open?)")
        try:
            listening = subprocess.run(on_host(["ss", "-ltnH"], remote), capture_output=True, text=True,
                                       stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT).stdout
        except subprocess.TimeoutExpired:
            listening = ""  # not answered: counts as not listening yet, the loop tries again
        if port_open(adb_port) and all(f"127.0.0.1:{port} " in listening for port in reverse):
            return tunnel
        time.sleep(1)
    tunnel.terminate()
    sys.exit(f"the ssh tunnel to {remote} did not come up (proxy ports {reverse} not listening there)")


def run_parallel(plan, args, checklists, run_dir):
    """--jobs / --remote: every slot takes the next AVD of the plan and runs this script on it as a worker (one AVD,
    its own ports), output prefixed with the AVD and saved to <run_dir>/<avd>-<app>.log. SUMMARY.md over all AVDs.
    Slots 0 … jobs-1 here, the following ones on the remote machines in their order (MANUAL_REMOTE, adb through the
    tunnel of the machine)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    # (slot, ssh target or None, local port of its adb server)
    # MANUAL_SLOT_BASE: first slot number, to run several invocations at the same time without sharing ports
    base = int(os.environ.get("MANUAL_SLOT_BASE", "0"))
    slots = [(slot, None, None) for slot in range(base, base + args.jobs)]
    for index, (remote, jobs) in enumerate(args.remotes):
        slots += [(slot, remote, REMOTE_ADB_PORT + index + base)
                  for slot in range(base + len(slots), base + len(slots) + jobs)]
    # AVDs given for a machine (--here, --remote HOST:JOBS:AVD,…) wait for its slots in that order, the others are
    # taken in the order of the plan by whichever slot is free and has none of its own left
    apps = dict(plan)
    own = {machine: [(avd, apps[avd]) for avd in avds if avd in apps] for machine, avds in args.own.items()}
    given = {avd for avds in own.values() for avd, _ in avds}
    if not given:
        slots = slots[:len(plan)]
    remote_slots = {}  # ssh target: (adb port, its slots), only machines that got a slot
    for slot, remote, adb_port in slots:
        if remote:
            remote_slots.setdefault(remote, (adb_port, []))[1].append(slot)
    here = sum(1 for _, remote, _ in slots if not remote)
    print(", ".join([f"{here} jobs here" if remote_slots else f"{here} jobs",
                     *(f"{len(on)} on {remote}" for remote, (_, on) in remote_slots.items())]),
          f"summary: {run_dir / 'SUMMARY.md'}", sep=", ")
    tunnels = []
    queue = [(avd, app) for avd, app in plan if avd not in given]
    lock = threading.Lock()
    finished = {}
    children = set()

    def worker_args(avd, app):
        command = [sys.executable, str(Path(__file__).resolve()), "--auto", "--skip-build", "--app", app,
                   "--checklist", args.checklist, avd]
        if args.keep:
            command.append("--keep")
        if args.start:
            command += ["--from", args.start]
        if args.steps:
            command += ["--steps", ",".join(args.steps)]
        if avd in args.software_gpu:
            command += ["--gpu", SOFTWARE_GPU]
        return command

    # --solo: per machine (None = here) how many emulators run, and whether a solo AVD waits for / owns the machine
    cond = threading.Condition(lock)
    running, solo_pending, solo_running, stopped = collections.Counter(), collections.Counter(), set(), []

    def enter_machine(remote, avd):
        """Waits until the AVD may start on its machine: a solo AVD when nothing else runs there, any other AVD when no
        solo AVD runs or waits there (so a solo AVD is not starved by the ones behind it). False when stopped."""
        with cond:
            solo = avd in args.solo
            if solo:
                solo_pending[remote] += 1
                if running[remote]:
                    log(f"{avd} waits for the emulators on {remote or 'this machine'} (--solo)")
            while not stopped and ((running[remote] or remote in solo_running) if solo
                                   else (solo_pending[remote] or remote in solo_running)):
                cond.wait()
            if solo:
                solo_pending[remote] -= 1
                solo_running.add(remote)
            running[remote] += 1
            return not stopped

    def leave_machine(remote):
        with cond:
            running[remote] -= 1
            solo_running.discard(remote)
            cond.notify_all()

    def slot_loop(slot, remote, adb_port):
        emulator_port, appium_port, server_port, ca_only_port = job_ports(slot)
        env = dict(os.environ, MANUAL_PORT=str(emulator_port), MANUAL_APPIUM_PORT=str(appium_port),
                   MANUAL_SERVER_PORT=str(server_port), MANUAL_CA_ONLY_PORT=str(ca_only_port),
                   MANUAL_RUN_DIR=str(run_dir))
        if remote:
            env.update(MANUAL_REMOTE=remote, ANDROID_ADB_SERVER_PORT=str(adb_port))
        while True:
            with lock:
                mine = own.get(remote, [])
                if not mine and not queue:
                    break
                avd, app = (mine or queue).pop(0)
            if not enter_machine(remote, avd):
                break
            # One remote machine: "remote", several: its host name
            where = "" if not remote else " remote" if len(remote_slots) == 1 else f" {remote.split('@')[-1]}"
            prefix = f"[{avd}{where}] "
            with open(run_dir / f"{avd}-{app}.log", "w") as output:
                child = subprocess.Popen(worker_args(avd, app), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
                with lock:
                    children.add(child)
                for line in child.stdout:
                    output.write(line)
                    output.flush()  # live, watch-run.sh and hang checks look at it
                    print(prefix + line, end="", flush=True)
                code = child.wait()
                with lock:
                    children.discard(child)
            leave_machine(remote)
            results_file = run_dir / f"{avd}-{app}" / "results.json"
            if results_file.exists():
                results = json.loads(results_file.read_text())
            else:
                step = checklists[app][0]
                results = [dict(id=step["id"], title=step["title"], result="fail",
                                notes=[f"worker exited with code {code} before the report, see {avd}-{app}.log"])]
            with lock:
                finished[(avd, app)] = results
                write_summary(run_dir, [(a, p, finished[(a, p)]) for a, p in plan if (a, p) in finished],
                              checklists)
        adb_on_host = f"{REMOTE_SDK}/platform-tools/adb" if remote else ADB
        try:
            subprocess.run(on_host([adb_on_host, "-s", f"emulator-{emulator_port}", "emu", "kill"], remote),
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT * 2 if remote else 30)
        except subprocess.TimeoutExpired:
            log(f"{YELLOW}emu kill of emulator-{emulator_port} did not answer in time, left running{RESET}")

    threads = [threading.Thread(target=slot_loop, args=slot, daemon=True) for slot in slots]
    try:
        for remote, (adb_port, on) in remote_slots.items():
            tunnels.append(start_tunnel(remote, on, adb_port))
        for thread in threads:
            thread.start()
            time.sleep(5)  # staggered boots, the first one fetches chromedrivers alone
        for thread in threads:
            while thread.is_alive():
                thread.join(1)
    except KeyboardInterrupt:
        print("\nstopped")
        with cond:
            stopped.append(True)
            cond.notify_all()
            queue.clear()
            for mine in own.values():
                mine.clear()
            for child in children:
                child.terminate()
        for thread in threads:
            thread.join(30)
    finally:
        for tunnel in tunnels:
            tunnel.terminate()
    print(f"Summary: {run_dir / 'SUMMARY.md'}")


def select_steps(app, start, only, all_ids):
    """Steps of checklists/<app>.md from --from, only --steps. An id unknown in every checklist is an error, one
    missing only in this app's checklist (download in tv.md) is left out for its AVDs."""
    steps = load_checklist(app)
    ids = [s["id"] for s in steps]
    for step_id in ([start] if start else []) + (only or []):
        if step_id not in all_ids:
            sys.exit(f"unknown step {step_id}, steps: {', '.join(all_ids)}")
        if step_id not in ids:
            print(f"{YELLOW}no step {step_id} in checklists/{checklist_name(app)}.md, left out there{RESET}")
    if start in ids:
        steps = steps[ids.index(start):]
    if only:
        steps = [s for s in steps if s["id"] in only]
    return steps


def remote_jobs(targets, default_jobs):
    """--remote HOST[:JOBS][:AVD,AVD…] … and --remote-jobs as [(ssh target, jobs)], machines without jobs left out,
    and the AVDs given for a machine {ssh target: [AVD, …]}. A target without JOBS gets --remote-jobs; without that
    option 1 when --remote was given, 0 for the default machines."""
    if default_jobs is None:
        default_jobs = 1 if targets else 0
    if not targets:
        targets = [t for t in os.environ.get("MANUAL_REMOTE_HOST", "user@remote-a").replace(",", " ").split()]
    remotes, own = {}, {}
    for target in targets:
        host, _, rest = target.partition(":")
        jobs, _, avds = rest.partition(":")
        if not host or (jobs and not jobs.isdigit()):
            sys.exit(f"--remote {target}: expected HOST, HOST:JOBS or HOST:JOBS:AVD,AVD…")
        if host in remotes:
            sys.exit(f"--remote {host} given twice")
        remotes[host] = int(jobs) if jobs else default_jobs
        if avds:
            own[host] = [avd for avd in avds.split(",") if avd]
    return [(host, jobs) for host, jobs in remotes.items() if jobs], own


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__[__doc__.index("Usage:"):])
    parser.add_argument("avds", nargs="*", help="AVDs (default: every TLS_* AVD)")
    parser.add_argument("--app", choices=APPS, help="default: tv for TLS_TV_*, otherwise mobile")
    parser.add_argument("--keep", action="store_true", help="keep the installed app and its data")
    parser.add_argument("--skip-build", action="store_true", help="use the APKs of the previous build")
    parser.add_argument("--software-gpu", metavar="AVD,AVD…", type=lambda v: v.split(","), default=[],
                        help="these AVDs run with software rendering (-gpu lavapipe, MANUAL_SOFTWARE_GPU) instead of the host "
                             "GPU, for the ones that crash in gfxstream (\"null ctx\"), here and on the remote "
                             "machines; the others keep the AVD setting")
    parser.add_argument("--check-remote", action="store_true",
                        help="only check that the remote machines can run emulators (ssh, KVM, desktop session): "
                             "exit code 0 when all are ready, 3 when not (the same check runs before every --remote "
                             "run)")
    parser.add_argument("--solo", metavar="AVD,AVD…", type=lambda v: v.split(","), default=[],
                        help="these AVDs run alone on their machine: no other emulator there while they run (they "
                             "wait for the running ones, nothing new starts there meanwhile); for the ones that freeze "
                             "under load (TLS_API_36). Where an AVD runs: --here / --remote HOST:JOBS:AVD,…, its "
                             "rendering: --software-gpu")
    parser.add_argument("--gpu", metavar="MODE", help="emulator -gpu mode (default: host)")
    parser.add_argument("--from", dest="start", help="start the checklist at this step id")
    parser.add_argument("--steps", type=lambda v: v.split(","), help="only these step ids (comma separated)")
    parser.add_argument("--checklist", default="mobile",
                        help="checklist in checklists/, for the TV app when the name starts with tv, otherwise for "
                             "the mobile app (default: mobile and tv; mobile-system: the whole course with CA and "
                             "client key from the system store; tv-phone: the TV app on a phone AVD, --app tv)")
    parser.add_argument("--auto", action="store_true",
                        help="only steps with an Appium flow, pass/fail by the script, no questions")
    parser.add_argument("--jobs", type=int, default=1, choices=range(0, MAX_JOBS + 1), metavar=f"0-{MAX_JOBS}",
                        help="AVDs in parallel here, each with its own emulator, Appium and proxy port (needs --auto)")
    parser.add_argument("--here", type=lambda v: [avd for avd in v.split(",") if avd], default=[], metavar="AVD,AVD…",
                        help="AVDs that run here, in this order (with --jobs / --remote); AVDs given for no machine "
                             "go to whichever slot is free")
    parser.add_argument("--remote-jobs", type=int, choices=range(0, MAX_JOBS + 1), metavar=f"0-{MAX_JOBS}",
                        help="AVDs in parallel on every --remote without its own :JOBS (needs --auto); all jobs "
                             f"together at most {MAX_JOBS} (proxy ports). Default: 0, or 1 when --remote is given")
    parser.add_argument("--remote", action="append", metavar="HOST[:JOBS[:AVD,AVD…]]",
                        help="ssh target of remote workers, with its number of jobs or --remote-jobs, and the AVDs "
                             "that run there in this order; several times for several machines (default: "
                             "MANUAL_REMOTE_HOST, comma separated, or user@remote-a)")
    args = parser.parse_args()
    global GPU
    if args.gpu:  # also for the workers (--jobs / --remote) started from here: they inherit the environment
        GPU = os.environ["MANUAL_GPU"] = args.gpu
    default_gpu = GPU
    CHECKLISTS[checklist_app(args.checklist)] = args.checklist
    if not (HERE / "checklists" / f"{args.checklist}.md").exists():
        sys.exit(f"no checklists/{args.checklist}.md")
    if args.check_remote:  # only the machines, nothing is started
        check_remotes([host for host, _ in remote_jobs(args.remote, 1)[0]])
        sys.exit(0)
    args.remotes, args.own = remote_jobs(args.remote, args.remote_jobs)
    if args.here:
        args.own[None] = args.here
    args.remote_jobs = sum(jobs for _, jobs in args.remotes)
    if (args.jobs > 1 or args.remote_jobs) and not args.auto:
        sys.exit("--jobs / --remote-jobs need --auto: the manual steps wait for you in one terminal")
    if args.jobs + args.remote_jobs > MAX_JOBS:
        sys.exit(f"--jobs + remote jobs: at most {MAX_JOBS}, one mTLS proxy port each (tls-test-proxy/Caddyfile: "
                 f"{', '.join(map(str, MTLS_PORTS))}; add ports there and restart the test Caddy)")
    if args.jobs + args.remote_jobs == 0:
        sys.exit("--jobs 0 needs --remote-jobs or --remote HOST:JOBS")

    available = list_avds()
    avds = args.avds or [avd for avd in available if avd.startswith("TLS_")]
    if not avds:
        sys.exit("No TLS_* AVDs found (is the disk with ~/.android/avd mounted?)")
    for avd in avds:
        if avd not in available:
            sys.exit(f"AVD {avd} not found (is the disk with ~/.android/avd mounted?)")
    for option, names in (("--software-gpu", args.software_gpu), ("--solo", args.solo)):
        unknown = [name for name in names if name not in avds]
        if unknown:
            sys.exit(f"{option}: {', '.join(unknown)} not in the AVDs of this run ({', '.join(avds)})")
    check_remotes([remote for remote, _ in args.remotes], quiet=True)
    for remote, _ in args.remotes:  # every slot takes the next AVD, whichever it is
        missing = sorted(set(avds) - set(list_avds(remote)))
        if missing:
            sys.exit(f"AVD {', '.join(missing)} not found on {remote} (ssh key login? AVDs in ~/.android/avd there?)")
    seen = {}
    for machine, names in args.own.items():
        where = machine or "--here"
        if (args.jobs if machine is None else dict(args.remotes).get(machine, 0)) == 0:
            sys.exit(f"{where}: AVDs given, but no jobs there")
        for avd in names:
            if avd not in avds:
                sys.exit(f"{where}: {avd} is not an AVD of this run ({', '.join(avds)})")
            if avd in seen:
                sys.exit(f"{avd} given for two machines ({seen[avd]}, {where})")
            seen[avd] = where
    plan = [(avd, args.app or ("tv" if avd.startswith("TLS_TV") else "mobile")) for avd in avds]
    apps = list(dict.fromkeys(app for _, app in plan))
    all_ids = list(dict.fromkeys(s["id"] for app in APPS for s in load_checklist(app)))
    checklists = {app: select_steps(app, args.start, args.steps, all_ids) for app in apps}
    for app in [app for app in apps if not checklists[app]]:
        print(f"{YELLOW}none of the steps in checklists/{checklist_name(app)}.md, its AVDs are skipped{RESET}")
    plan = [(avd, app) for avd, app in plan if checklists[app]]
    if not plan:
        sys.exit("no AVD has any of the selected steps")

    uses_appium = any(a.startswith("appium ") for steps in checklists.values() for s in steps for a in s["actions"])
    if uses_appium:
        if not os.environ.get("MANUAL_RUN_DIR"):  # the parent checked already for its workers
            if args.jobs:
                check_appium(None)
            for remote, _ in args.remotes:
                check_appium(remote)
        if not (os.environ.get("E2E_USER") and os.environ.get("E2E_PASSWORD")):
            print(f"{YELLOW}E2E_USER / E2E_PASSWORD not set, the login flow will fail{RESET}")
    if args.auto:
        for app in apps:
            if not any(a.startswith("appium ") for s in checklists[app] for a in s["actions"]):
                print(f"{YELLOW}no Appium flows for {app}, its AVDs are skipped with --auto{RESET}")
        plan = [(avd, app) for avd, app in plan
                if any(a.startswith("appium ") for s in checklists[app] for a in s["actions"])]
        if not plan:
            sys.exit("nothing to run with --auto")

    check_servers()
    parallel = args.jobs > 1 or args.remote_jobs > 0
    if not parallel and args.here:  # one AVD at a time here: the given ones first, in their order
        plan = sorted(plan, key=lambda item: args.here.index(item[0]) if item[0] in args.here else len(args.here))
    # Every slot's proxy port: a Caddy started with an older Caddyfile lacks the new ones, the workers there would
    # connect nowhere
    slots_used = args.jobs + args.remote_jobs
    closed = [port for port in MTLS_PORTS[:slots_used] + CA_ONLY_PORTS[:slots_used] if not port_open(port)]
    if parallel and closed:
        sys.exit(f"mTLS proxy port {', '.join(map(str, closed))} not listening: restart the test Caddy with the current "
                 f"tls-test-proxy/Caddyfile")
    if uses_appium and not parallel:
        start_appium()  # --jobs: every worker starts its own
    apks = {}
    for app in apps:
        if not args.skip_build:
            build(app)
        apks[app] = apk_info(app)

    # A --jobs worker writes into the run directory of its parent, which writes SUMMARY.md
    worker = bool(os.environ.get("MANUAL_RUN_DIR"))
    run_dir = Path(os.environ["MANUAL_RUN_DIR"]) if worker else HERE / "manual-results" / f"{datetime.now():%Y%m%d-%H%M%S}"
    print(f"{BOLD}{len(plan)} AVDs:{RESET} " + ", ".join(f"{avd} ({app})" for avd, app in plan))
    if parallel:
        run_parallel(plan, args, checklists, run_dir)
        return
    if not worker:
        print(f"Summary: {run_dir / 'SUMMARY.md'}")
    runs = []
    try:
        for index, (avd, app) in enumerate(plan):
            if len(plan) > 1 and not args.auto:
                answer = input(f"\n{BOLD}Next: {avd} ({app}, {index + 1}/{len(plan)}){RESET} "
                               f"Enter=start, s=skip, q=quit: ").strip().lower()
                if answer == "q":
                    break
                if answer == "s":
                    continue
            apk, app_id = apks[app]
            GPU = SOFTWARE_GPU if avd in args.software_gpu else default_gpu
            try:
                results = run_session(avd, app, apk, app_id, checklists[app], run_dir / f"{avd}-{app}", args.keep,
                                      auto=args.auto)
            except EmulatorLost as error:
                print(f"{RED}{avd}: {error}{RESET}")
                results = [dict(id=checklists[app][0]["id"], title=checklists[app][0]["title"], result="fail",
                                notes=[str(error)], evidence=[], screenshots=[])]
            runs.append((avd, app, results))
            if not worker:
                write_summary(run_dir, runs, checklists)
            # The next AVD boots on the same port
            if len(plan) > 1:
                kill_emulator()
    except (KeyboardInterrupt, EOFError):
        print("\nstopped")
        if len(plan) > 1:
            kill_emulator()
    finally:
        stop_appium()

    if runs and not worker:
        write_summary(run_dir, runs, checklists)
        print(f"Summary: {run_dir / 'SUMMARY.md'}")
    if len(plan) == 1 and device_online() and not worker:
        print(f"The emulator keeps running ({SERIAL}), stop it with: {ADB} -s {SERIAL} emu kill")


if __name__ == "__main__":
    main()
