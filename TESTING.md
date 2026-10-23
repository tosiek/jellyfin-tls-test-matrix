# Running the TLS tests

Tests of the custom CA / client certificate (mTLS) support in jellyfin-android and jellyfin-androidtv on emulators
Android 6 - 17 and Android TV.

| What | Script | Duration |
|---|---|---|
| Automated test suites (TLS matrix, GUI, end-to-end), every AVD, both apps | `run-matrix.sh` | ~15 min (`JOBS=3`) |
| Checklist with clicking automation, unattended | `manual-test.py --auto [--jobs 2] [--remote-jobs 1]` | ~5 min per AVD |
| Checklist, you click or confirm, with evidence per step | `manual-test.py` | ~15 min per AVD |

## Quick start: test on every AVD

```bash
# Once per session: section 1 (AVD disk, Jellyfin, test Caddy)
cd ~/StudioProjects/tls-test-matrix
export E2E_USER=jellyfin E2E_PASSWORD=1234

# The app code under test is whatever is checked out in ../jellyfin-android and ../jellyfin-androidtv:
# test/custom-ca-mtls-all in both (the mTLS work and every fix branch, see "Branches" below)
git -C ../jellyfin-android branch --show-current
git -C ../jellyfin-androidtv branch --show-current

# 1. Automated suites (TLS matrix, GUI, end-to-end), every TLS_* AVD, both apps, ~15 min
JOBS=3 SUITES=all ./run-matrix.sh > run.log 2>&1
grep -E " [1-9][0-9]* failed|instrumentation exit|crashed|install failed|boot timeout" run.log   # must print nothing

# 2. Clicking automation, every phone AVD, unattended, ~5 min per AVD (~50 min for all 10, about half with --jobs 2)
#    API 29 and 30 separately, their emulators die with the host GPU (see "Emulator window disappears" below)
./manual-test.py --auto --jobs 2 TLS_API_23 TLS_API_24 TLS_API_26 TLS_API_28 TLS_API_33 TLS_API_34 TLS_API_36 TLS_API_37 > manual.log 2>&1
./manual-test.py --auto TLS_API_30 > manual-30.log 2>&1
MANUAL_GPU=swiftshader_indirect ./manual-test.py --auto TLS_API_29 > manual-29.log 2>&1
cat manual-results/$(ls -t manual-results | head -1)/SUMMARY.md   # step × AVD
#    TV app (tv_flows.py, D-pad only), both TV AVDs, unattended, ~10 min with --jobs 2
./manual-test.py --auto --jobs 2 TLS_TV_30 TLS_TV_34 > manual-tv.log 2>&1

# 3. Reading the texts (every step is automated now): the flows click, you confirm every step, every AVD
#    including TV, Enter starts the next AVD, s skips it, q ends the run
./manual-test.py
```

- Only playback on every phone AVD: `./manual-test.py --auto --steps setup,login,browse,play,seek,play-second`.
  Steps that a checklist does not have are left out for its AVDs (TV has no `download`).
- Some AVDs: list them, `./manual-test.py TLS_API_23 TLS_API_36 --auto`.
- `manual-test.py` builds the app first. `--skip-build` reuses the last APK: check which branch it was built from.
- Branches (both apps, local): `feature/custom-ca-mtls-minimal` holds only the mTLS work (the upstream PR), each
  unrelated fix has its own `fix/*` branch from `origin/master` (mobile: `fix/player-service-notification-extra`,
  `fix/pip-feature-check`, `fix/show-fragment-when-started`, `fix/download-request-failure`,
  `fix/connection-error-details`; TV: `fix/connection-error-details`).
  `test/custom-ca-mtls-all` is minimal plus every fix commit at the end, the tests run on it. After a change to
  minimal or a fix branch, rebuild it. `fix/connection-error-details` conflicts with minimal where both add a line
  under an address or to an error text (mobile `ConnectionHelper.kt`, TV `ServerFragment.kt` and the two
  `UserLogin*Fragment.kt`): keep the TLS text first, the mapped cause only without one, the raw cause in both.

  ```bash
  git -C ../jellyfin-android switch -C test/custom-ca-mtls-all feature/custom-ca-mtls-minimal
  for b in fix/player-service-notification-extra fix/pip-feature-check fix/show-fragment-when-started fix/download-request-failure fix/connection-error-details; do
    git -C ../jellyfin-android cherry-pick origin/master..$b
  done
  git -C ../jellyfin-androidtv switch -C test/custom-ca-mtls-all feature/custom-ca-mtls-minimal
  git -C ../jellyfin-androidtv cherry-pick origin/master..fix/connection-error-details
  ```

- `--jobs N` (needs `--auto`): N emulators at a time, each with its own emulator port (5590, 5592, …), Appium
  port (4724, 4725, …) and mTLS proxy port (8920, 8922, 8923, …, the request evidence is split by port). `--jobs` +
  `--remote-jobs` is limited by the mTLS ports on the 8920 line of `tls-test-proxy/Caddyfile` (8 now); for more, add
  ports there and restart the test Caddy. Output
  prefixed with the AVD, per AVD in `manual-results/<time>/<avd>-<app>.log`, one `SUMMARY.md`. Two jobs need about
  6 GB of free RAM.
- `--remote-jobs N` (needs `--auto`): N more workers on the remote machine (default user@remote-a or
  `MANUAL_REMOTE_HOST`, section 6), all jobs together at most the mTLS ports (one proxy port each).
  `--jobs 0 --remote-jobs 1`: only there.
- `--remote HOST[:JOBS]`, several times: workers on several machines, each with its own ssh tunnel, e.g.
  `--jobs 2 --remote user@a:2 --remote user@b:1`. A machine without `:JOBS` gets `--remote-jobs` (1 without it).
  `MANUAL_REMOTE_HOST` may list several, comma separated. Every machine needs all AVDs of the run (checked at the
  start). With several machines the output prefix names the host. `kill-run.sh` stops the emulators on all of them.
- Which AVD runs where, and in which order: `--here AVD,AVD…` for this machine and a third part of `--remote`,
  `--remote HOST:JOBS:AVD,AVD…`. The slots of a machine take its own AVDs first, in the given order; AVDs given for
  no machine go to whichever slot is free and has none of its own left. Example: `--jobs 2 --here
  TLS_API_36,TLS_API_34 --remote user@remote-a:2:TLS_API_28,TLS_API_29 --remote
  user@remote-b:2:TLS_TV_34,TLS_API_30`. An AVD can be given for one machine only and must be part of the run.
- `./watch-run.sh [manual-results/<time>]` shows the state of every AVD of a run and warns when one writes nothing for
  10 minutes; `./kill-run.sh` stops a run (Ctrl+C to the parent, the summary is written), `./kill-run.sh TLS_API_30`
  one AVD of it. For a run that outlives the terminal: `setsid nohup ./manual-test.py --auto … > run.log 2>&1 &`.
- A run with several AVDs shuts each emulator down before the next. A single-AVD run leaves it running for the next
  run (`adb -s emulator-5590 emu kill` to stop it).

## 0. Setup from scratch (once)

`./setup.sh` checks Appium (UiAutomator2 driver 5.0.7, the last one that runs on Android 6 - 7, and the Python
client; Appium itself is installed with `sudo npm install -g appium@3.8.0`, the script only says so), installs the
SDK packages and system images, creates the missing TLS_* AVDs (existing ones are not
changed), generates the test PKI (`tls-test-proxy/generate.sh`), checks the WebView APKs in `webview-apks/` and
installs them on the AVDs (`SUITES=webview ./run-matrix.sh`), downloads the file manager of `checklists/tv-picker.md`
(Material Files from F-Droid, SHA-256 checked) to `tv-picker/`, and checks that the test Caddy runs. Every step is
skipped when its result exists, the script lists what is left to do. The WebView APKs are downloaded by hand (which
file for which API level: the script prints it, sources in WEBVIEW-APKS.md). Jellyfin is not part of it.

```bash
./setup.sh                          # everything
./setup.sh TLS_API_23 TLS_TV_30     # only these AVDs
SKIP_WEBVIEW=1 ./setup.sh           # without the WebView update (builds the mobile app, boots every AVD)
AVD_CORES=2 ./setup.sh              # 2 virtual CPUs per AVD (default 4, used on every machine)
./webview-api23.sh                  # webview-apks/webview-api23.apk: LineageOS WebView 88 re-signed with the AOSP test key
./webview-api23.sh other.apk        # or another com.android.webview APK (x86_64, 80+)
./webview-resign.sh -o out.apk in.apk   # any com.android.webview APK, any output: the signing itself (key inside the script)
```

The AOSP images (`default`) only accept `com.android.webview` signed with the key of the system image (the public AOSP
test key, inside `webview-resign.sh`) as WebView provider, Google's WebView and Chrome are never used there. The newest
that runs per Android version (LineageOS `android_external_chromium-webview_prebuilt_x86_64`, branch main, `webview.apk`
in Git LFS, one commit per version): Android 6 only 88 (the old `..._backup-20210212` repo, the newer builds have
minSdk 24), Android 7 up to 119, Android 8 and 9 up to 138 (minSdk 26), but 135 on API 28 (chromedriver 137 and 138 ask `pm path --user cur`, which Android 9 does not understand: "org.jellyfin.mobile.debug is not installed", the session cannot be created; 133, 135, 146 and 155 use plain `pm path`), Android 10 and newer up to the latest (155,
minSdk 29). Install with `adb install --no-streaming -r -d` (a streamed install of a big APK hung on Android 7). The AOSP
Android 8 image (`TLS_API_26`) accepts no `com.android.webview` at all (its provider list names only Chrome and Google's
WebView): Chrome (`provider-api26.apk`, 126) is the provider there. Chrome is not in the repository and no script downloads it
(it may not be redistributed): download it by hand, see [WEBVIEW-APKS.md](WEBVIEW-APKS.md). TLS_API_23 has a 2 GB data partition: with 6 GB the first
boot of Android 6 fails to encrypt /data and stays in the minimal framework (every install "Aborted").

## 1. Before every session

```bash
# 1. The AVDs (in ~/.android/avd, or ANDROID_AVD_HOME)
ls ~/.android/avd/ | grep TLS_          # TLS_API_23 … TLS_API_37, TLS_TV_30, TLS_TV_34

# 2. Jellyfin (docker, user jellyfin / password 1234)
docker compose -f jellyfin-compose.yaml up -d
curl -s http://127.0.0.1:8096/health    # Healthy

# 3. Test proxy (Caddy): 8920 mTLS, 8921 CA only, 8930 certificate download
cd ~/StudioProjects/tls-test-matrix/tls-test-proxy && nohup ./bin/caddy run --config Caddyfile --adapter caddyfile >> caddy.log 2>&1 &
cd ~/StudioProjects/tls-test-matrix
```

`manual-test.py` starts the test Caddy itself when 8920 is not open.

Do not touch the system Caddy (`/etc/caddy/Caddyfile`), it is unrelated.

## 2. Automated test suites (`run-matrix.sh`)

```bash
JOBS=3 SUITES=all E2E_USER=jellyfin E2E_PASSWORD=1234 ./run-matrix.sh > run.log 2>&1
grep -E " [1-9][0-9]* failed|instrumentation exit|crashed|install failed|boot timeout" run.log   # must print nothing
```

AVDs whose run was cut short (emulator crash, boot or install failure, instrumentation timeout) run once more at the
end, alone and on this machine (`run cut short on …, once more, alone` in the log). The grep above also finds the
lines of the first attempt: check the lines after that message. Failed tests are not retried. `RETRY=0` turns it off.

- Builds both apps once, boots every `TLS_*` AVD headless, runs the suites `device` (TLS matrix), `gui` (certificate
  screens) and `e2e` (real Jellyfin through the proxy).
- With the proxy running, `device` also does a post-quantum mTLS handshake (only X25519MLKEM768 offered) with Caddy
  on 8920 on Android 17+, once with `client-ec.p12` and once with `client-rsa.p12` ("Post-quantum mTLS handshake"
  rows of the matrix). Skipped before Android 17 and without the proxy. Needs Caddy ≥ 2.10 (`tls-test-proxy/bin/caddy`
  from `setup.sh`).
- Results: `results/MATRIX.md`, `results-gui/MATRIX.md`, `results-e2e/MATRIX.md`.
- Variants: `./run-matrix.sh TLS_API_23 TLS_API_36` (selected AVDs), `APPS=mobile`, `SUITES=device`, `SHOW=1` (window),
  `SKIP_BUILD=1`.
- `JOBS=2 REMOTE_JOBS=2`: two workers here, two on the remote machine (section 6); `JOBS=0 REMOTE_JOBS=1` only there.
  2 + 2 on 11 AVDs, all suites: about 8 minutes.
- Run it from a shell without `PORT` set, or with `JOBS` ≥ 2 (a `PORT=0` from other tools breaks `JOBS=1`).

## 3. Checklist with Appium (`manual-test.py`)

Boots one AVD at a time **with a window**, installs the app fresh, copies the certificates to Download/jellyfin-tls/,
walks through `checklists/mobile.md` (or `tv.md`), shuts the emulator down, next AVD.

```bash
export E2E_USER=jellyfin E2E_PASSWORD=1234

./manual-test.py --auto                       # all phone AVDs, automated steps only, no questions
./manual-test.py TLS_API_23 TLS_API_36 --auto # selected AVDs
./manual-test.py TLS_API_36                   # flows click, you confirm every step (p/f/s, n=note, q=next AVD)
./manual-test.py TLS_TV_34                    # TV app: tv_flows.py drives the D-pad, you confirm every step
./manual-test.py --steps setup,login,play     # only some steps
./manual-test.py TLS_API_36 --keep --skip-build --from reboot   # keep the logged-in app, start at a step
```

The flows are in `appium_flows.py` (Appium, UiAutomator2 driver for the native screens, the WebView context with
chromedriver for jellyfin-web), the same on every API level. The TV app's flows are in `tv_flows.py` (native only,
every element focused with the D-pad, certificates from the URL import; design in TV-AUTOMATION.md). Maestro is not used: it cannot drive Android 6 (its adb
client needs shell protocol v2, Android 7+).

One-time setup:

```bash
npm install -g appium
appium driver install uiautomator2@5.0.7   # 5.x: the last driver whose server runs on API 21+, 6.x+ needs API 26
pip install --user Appium-Python-Client
```

`manual-test.py` starts its own Appium server on port 4724 (`MANUAL_APPIUM_PORT`, log `appium-manual.log`) and
downloads the chromedriver matching the WebView of the AVD to `~/.cache/appium-chromedriver/`. A flow on its own,
against a running emulator and Appium: `APPIUM_URL=http://127.0.0.1:4724 python3 appium_flows.py emulator-5590 setup`.

Steps (mobile):

| Step | Automated | Checks |
|---|---|---|
| setup | ✅ | address, Advanced, CA + client certificate from the **real file picker**, connect |
| login | ✅ | jellyfin-web login in the WebView |
| browse | ✅ | first library opens, posters load (image requests) |
| play | ✅ | first movie plays, position advances (direct play) |
| seek | ✅ | seeking, subtitles, the direct stream shows up in the evidence |
| play-second | ✅ | second movie plays transcoded (HLS segments through the proxy) |
| background | ✅ | Home during playback, pause and resume in the media notification (MediaSession state), back in the app the position advances, HLS segments through the proxy during the step (`? streaming-hls`). Below Android 10 the notification carries the poster (the `TransactionTooLargeException` crash on Android 6) |
| restart | ✅ | playback stopped, Home, 5 s, then force-stop and launch: connected without importing again. The app is only killed from the background (host GPU crash when it was killed in the foreground) |
| reboot | ✅ | the emulator is powered off cleanly and started again (a new emulator process, not `adb reboot`), the app starts connected: the client key survives (Android Keystore) |
| download | ✅ | download network set to "Mobile Data" (WiFi only waits forever on the Android 6 - 7 images), "Download" on the details page, download location `Download/jellyfin-tls/downloads` in the **real folder picker**, the file arrives with the server's size, a `download` request through the proxy (the app's OkHttp with the client certificate) |
| settings | ✅ | certificates screen (app settings): the CA with expiry and fingerprint, the client certificate `CN=client-ec` |
| missing-ca | ✅ | CA removed on the certificates screen in a running session: jellyfin-web no longer reaches the server within 15 s and a message says the server certificate is not trusted; the CA is imported again |
| missing | ✅ | client certificate removed, app restarted: connect screen, Connect shows "requires a client certificate" |
| reimport | ✅ | connect screen: Advanced, Load certificates from URL, `client-ec.p12` from `http://10.0.2.2:8930`, Connect: jellyfin-web works again |
| wrong-password | ✅ | certificates screen: `client-rsa.p12` from the URL with "wrong": "Wrong password…" message, configured certificate unchanged |
| pem-client | ✅ | certificates screen: `fixtures/client/pem-key-and-cert.pem` (key + certificate) in the file picker: ".p12 or .pfx" message (Android reads client keys only from PKCS#12), configured certificate unchanged |
| p12-ca | ✅ | the client `.p12` picked as CA: the CA confirmation lists Jellyfin Test Client CA (Trust), then the app offers the client certificate of the same file |
| ca-only-setup | ✅ | app data cleared, connect with the CA only on the CA-only port (8921): login page, the requests carry no client certificate |
| ca-only-login, ca-only-browse, ca-only-play, ca-only-play-second, ca-only-download | ✅ | the course again with the CA only: login, images, direct play, HLS, a download through the CA-only port |
| system-ca | ✅ | CA installed in the system certificate store (Settings), not in the app: login works, the trust store change reaches the running app. Android 6 - 9 ask for a screen lock: PIN 1234 |
| system-ca-client | ✅ | CA and client certificate in the system store: the KeyChain chooser opens by itself, after choosing `client-ec` the login works |
| system-client | ✅ | only the client certificate from the system store: the chooser opens by itself, the certificates screen says "From system store" |
| system-key-removed | ✅ | the system key removed while the app uses it: the message says the client certificate is no longer available |
| system-choose | ✅ | "Choose from system store…" by hand on the certificates screen |
| system-no-lock | ✅ | no screen lock and no client certificate: the chooser or the "requires a client certificate" message, not Android's "set a screen lock" |
| system-cleanup | ✅ | user CA, keys and the screen lock removed again (later runs and the reboot step need an unlocked device) |

Steps (TV, `checklists/tv.md`, D-pad only, certificates from the URL import of the proxy's file server): the same
course without `background`, `download`, `missing-ca` and the `system-*` steps, plus

| Step | Automated | Checks |
|---|---|---|
| saved-server | ✅ | the CA removed, a saved server chosen: the error says the server certificate is not trusted and names its issuer |
| unavailable | ✅ | the client key lost (simulated): the user selection and the login screen say the client certificate is no longer available |

Other checklists (`--checklist NAME`, all automated):

| Checklist | Run with | Steps |
|---|---|---|
| `mobile-system` | `./manual-test.py --auto --checklist mobile-system` (every phone AVD) | the course from `browse` to `settings` with the CA and the client key only in the system store (`system-setup`), then `system-cleanup` |
| `tv-phone` | `./manual-test.py --auto --app tv --checklist tv-phone TLS_API_36` | the TV app on a phone: `setup` with the system file picker, `login`, `browse`, `play`, `restart`, `settings`, `import-file`, `system-client`, `system-setup`, `system-cleanup` |
| `tv-picker` | `./manual-test.py --auto --checklist tv-picker TLS_TV_30 TLS_TV_34` | the TV app with a file manager as file picker: `setup`, `login`, `browse`, `restart`, `import-file`, `uninstall-picker` |

Evidence per step (shown live, in the report): requests through the mTLS proxy by type (api, images, streaming-hls,
streaming-direct, subtitles, websocket, web), errors (status ≥ 400), client certificate name, crashes and TLS errors
from logcat, a screenshot.

Results: `manual-results/<time>/SUMMARY.md` (step × AVD), `manual-results/<time>/<avd>-<app>/REPORT.md` with
screenshots and logcat, `<step>-screen.xml` (screen hierarchy) when a flow failed.

Options: `MANUAL_VOLUME=5` (media volume 0 - 15, default 1), `MANUAL_GPU=swiftshader_indirect` (default: `-gpu host` for every AVD, not the setting of the AVD),
`MANUAL_PORT=5592` (default 5590). The emulator of a single-AVD run keeps running, the next run reuses it.

## 4. Test media

The play steps use the first two cards of the first library, any title. Current files in `~/media`:

| Card | File | Path through the proxy |
|---|---|---|
| 1 | `TLS Test Clip H264.mp4` + `TLS Test Clip H264.en.srt` | direct play, external subtitles |
| 2 | `TLS Test Transcode.mkv` (HEVC 10-bit, embedded subtitles) | transcoded by Jellyfin (VAAPI), HLS |

After adding or removing files: Jellyfin Dashboard → Libraries → Scan, or
`curl -X POST -H 'Authorization: MediaBrowser Token="…"' http://127.0.0.1:8096/Library/Refresh`.

## 5. Known problems

| Symptom | Cause / fix |
|---|---|
| `AVD … not found` | AVDs not created (`setup.sh`) or ANDROID_AVD_HOME not set |
| File picker shows an empty folder | `push-certs.sh` rescans the MediaStore, run it again: `ANDROID_SERIAL=emulator-5590 ./push-certs.sh` |
| `RECOVERY n/2` in the log, "recovered Nx" in `watch-run.sh`, "Emulators that died and were booted again" in SUMMARY.md | the emulator died during a step (host GPU: `null ctx` / `bad color buffer`, cause in the line): the harness boots the same AVD again (data kept, `emulator-died-n.log` in the AVD folder keeps the log of the dead one) and runs the step again, at most 2 times per AVD, `--auto` only. Flows that need the state of an earlier step start it themselves (`play` opens the library, `seek` and `background` play the video first) |
| Emulator window disappears | app killed during video playback (host GPU crash, `Failed to create EGL fence sync` / `bad color buffer` / `error null ctx` in `emulator-manual-<AVD>.log`), the flows leave the player first, and every kill and data clear sends the app Home and waits 5 s first. Still on two images: TLS_API_29 at every `restart` step, also alone (passes with `MANUAL_GPU=swiftshader_indirect`); TLS_API_30 only next to a second emulator (passes with `--jobs 1`) |
| Connect screen keeps spinning after choosing a client certificate (Android 11 emulator, `reimport`) | the Android keystore does not answer the key import (`binder: got transaction to invalid handle` of the keystore process). Fixed in the apps: 10 s limit, then the encrypted file storage (`ClientKeyStore.withKeyStoreTimeout`) |
| "Connection cannot be established" right after a reboot | app's 10 s initial WebView timeout on a cold emulator: the `home` flow relaunches the app once and notes it in the report |
| `play` fails reading the position | controls hide quickly under load, the flows retry 3 times; check CPU load (transcoding) |
| No sound | host output device / volume; `MANUAL_VOLUME` raises the emulator volume |
| `appium not found` / UiAutomator2 driver missing | one-time setup in section 3 |
| Every step fails on TLS_API_23 - 25 with the UiAutomator2 driver 6.x+ | its server needs API 26: `appium driver uninstall uiautomator2; appium driver install uiautomator2@5.0.7` |
| play: "Jellyfin has stopped" (Android < 10, seen on API 23) | app bug unrelated to mTLS, `TransactionTooLargeException` in `PlayerService.start`: the notification with the poster as large icon went into the service intent. Fixed on `fix/player-service-notification-extra`, part of `test/custom-ca-mtls-all` |
| API 30: install fails with `Failure calling service package: Broken pipe`, or the reboot step fails in Appium | the API 30 image's system_server dies (`SecurityException … binding PermissionControllerService`) when a package is removed and ~30 s after a boot. `manual-test.py` waits until system_server has run 40 s and retries the install 3 times |
| App window freezes after returning from Home during playback (`dequeueBuffer failed, error = -110`, 4 s frames) | host GPU of the emulator (`bad color buffer handle` in `emulator-manual-<AVD>.log`), not seen with `MANUAL_GPU=swiftshader_indirect`, intermittent with the host GPU |
| background on API 24 - 25: "Jellyfin has stopped" after Home during playback | app bug unrelated to mTLS, `IllegalStateException: enterPictureInPictureMode: Device doesn't support picture-in-picture mode` from `PlayerFragment.onUserLeaveHint`: it checks only the Android version (7+), not `FEATURE_PICTURE_IN_PICTURE` (phones have picture-in-picture from Android 8). Not fixed yet |
| Remote: "device unauthorized", boot timeout (TLS_TV_34) | the remote adb server uses another key than the one the AVD trusts: copy `~/.android/adbkey*` there, `adb kill-server` there |
| Remote: "Failed to get EGL display", the emulator never reaches adb | no desktop session logged in on the remote machine (host GPU), log in or `MANUAL_GPU` / `MATRIX_GPU=swiftshader_indirect` |
| `--jobs`: a step shows another AVD's requests | every worker has its own proxy port, check that the test Caddy runs with the current Caddyfile (8922 - 8928) |

## 6. Emulators on the remote machine

Emulators and Appium can run on other Linux machines with KVM (placeholders `remote-a`, `remote-b`, …), driven from here: builds, checks, reports, Jellyfin and the test Caddy stay here.

- One ssh tunnel per run: the adb server there to local port 15037 (`ANDROID_ADB_SERVER_PORT` of the remote workers,
  so installs, `push`, logcat, screenshots and the flows' adb calls go there), the Appium ports of the remote slots,
  and back to the remote loopback the proxy ports of those slots and 8930 (`ssh -R`; the emulator's 10.0.2.2 is that
  loopback). The emulator, `adb emu …` and Appium are started over ssh (`-tt`: they end with the connection).
- Requirements there: login with this machine's ssh key (`~/.ssh/id_ed25519`), the SDK in `~/Android/Sdk`, the
  TLS_* AVDs in `~/.android/avd` (copies of these, `hw.cpu.ncore = 4` as here), Appium 3.8.0 with UiAutomator2 5.0.7
  (in `~/.nvm`, loaded by the scripts), this machine's adb key in `~/.android/adbkey` (TLS_TV_34 only trusts that
  one, see below), and **a desktop session logged in**: the host GPU needs its display (`DISPLAY=:0`, Xwayland), over
  plain ssh the emulator fails with "Failed to get EGL display".
- Capacity: 2 emulators there for the instrumented suites; for the checklists with video playback better 1.
- Setting up another machine: the SDK, system images and AVDs as `setup.sh` makes them (or copied), Node and npm,
  `sudo npm install -g appium@3.8.0`, `appium driver install uiautomator2@5.0.7`, this machine's adb key,
  `hw.cpu.ncore = 4`. Two machines: `--remote user@remote-a:1 --remote user@remote-b:1`.
- Copying AVDs there: `rsync -aS` (sparse, without `snapshots/`, `*.lock`, `hardware-qemu.ini`); over Wi-Fi this is
  slow (10 - 50 MB/s), a USB disk is faster. System images copied without `-S`: `fallocate --dig-holes` there.

`trust-adb-key.sh` (`REMOTE=user@remote-a ./trust-adb-key.sh` for the copies there) enters
`~/.android/adbkey.pub` into `/data/misc/adb/adb_keys` of every AVD with adb root (all phone AVDs). The TV images are
production builds without root, their trust comes from "Always allow", kept in their user data.
