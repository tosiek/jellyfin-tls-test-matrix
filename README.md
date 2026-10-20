# Automated TLS tests for the Jellyfin Android apps

Automated tests of the custom CA / client certificate (mTLS) support in jellyfin-android and jellyfin-androidtv,
on emulators from Android 6 to 17 and Android TV, against a Jellyfin server behind a test Caddy that requires a client
certificate:

- **Instrumented test suites** (`run-matrix.sh`): the TLS layer, the certificate screens and end-to-end connections
  on every emulator, with a TLS compatibility matrix per Android version as the result.
- **Checklists with clicking automation** (`manual-test.py --auto`, Appium): the whole app as a user would use it
  (connect, login, browse, playback, restart, reboot, download, error messages, the Android system certificate
  store), every step checked against the requests that went through the mTLS proxy and the app's logcat.

Results: [tosiek/jellyfin-tls-results](https://github.com/tosiek/jellyfin-tls-results), the matrices and the
recommended TLS / client certificate setup per Android version.

## Layout

The scripts expect the app repositories next to this one, on the branch under test, and this repository in a folder
named `tls-test-matrix`:

```bash
cd ~/StudioProjects
git clone https://github.com/tosiek/jellyfin-tls-test-matrix.git tls-test-matrix
```

```
~/StudioProjects/
  jellyfin-android/      # branch test/custom-ca-mtls-all
  jellyfin-androidtv/    # branch test/custom-ca-mtls-all
  tls-test-matrix/       # this repository (the remote machines use this path)
```

The instrumented tests themselves (`TlsDeviceTests`, the `tls.ui` GUI and end-to-end tests) are part of the app
branches; this repository builds the apps, runs the tests on every emulator and collects the results.

## Quick start

Needs: Linux with KVM, the Android SDK command line tools, Docker, openssl, Python 3, Node with
`npm install -g appium@3.8.0`, `appium driver install uiautomator2@5.0.7`, `pip install --user Appium-Python-Client`.

```bash
# 1. Emulators, SDK packages, test certificates, WebViews, Caddy (run again until it reports nothing missing)
./setup.sh
#    Chrome 126 for the Android 8 emulator has to be downloaded by hand, see WEBVIEW-APKS.md

# 2. Jellyfin: two videos in ~/media (the first with an English .srt next to it), then the setup wizard on
#    http://127.0.0.1:8096: user jellyfin, password 1234, a library on /media
docker compose -f jellyfin-compose.yaml up -d

# 3. The test Caddy in front of Jellyfin (its own terminal)
(cd tls-test-proxy && ./bin/caddy run --config Caddyfile --adapter caddyfile)

# 4. Automated suites on every emulator, both apps (~15 min)
SUITES=all E2E_USER=jellyfin E2E_PASSWORD=1234 JOBS=3 ./run-matrix.sh
#    → results/MATRIX.md, results-gui/MATRIX.md, results-e2e/MATRIX.md

# 5. Checklists with clicking automation, every emulator (~5 min per emulator, two at a time)
E2E_USER=jellyfin E2E_PASSWORD=1234 ./manual-test.py --auto --jobs 2
#    → manual-results/<time>/SUMMARY.md
```

More:

- [TESTING.md](TESTING.md): single emulators, single steps, other machines, known problems.
- [WEBVIEW-APKS.md](WEBVIEW-APKS.md): the WebView updates of the old emulators (Android 6 - 10 ship a WebView older
  than 80, the minimum of the mobile app) and where the APKs come from, including Chrome for Android 8, which has to
  be downloaded by hand.

## Automated tests

Two kinds of tests, both run on every emulator (Android 6 - 17 phones, Android TV 11 and 14) and both apps.

### Instrumented suites (`run-matrix.sh`)

JUnit tests inside the apps (`androidTest` of the app branches), installed and run with `adb shell am instrument`.
Each suite writes a matrix: test results per emulator plus the measured TLS capabilities.

| Suite | Tests (mobile / TV) | What it tests | Needs |
|---|---|---|---|
| `device` (`TlsDeviceTests`) | 20 / 20 | The TLS layer itself against local test servers on the device: importing CA files (PEM, DER, PKCS#7, bundles, broken files) and PKCS#12 files in every key type and encryption (the import matrix), mTLS handshakes with RSA and EC keys over TLS 1.2 and 1.3, a separate configuration per server, configuration changes closing pooled connections, the error for a missing client certificate, an untrusted, expired or wrong-host server certificate; the ClientHello the app sends (TLS versions, key exchange groups, ciphers, signature algorithms) and the post-quantum key exchange (Android 17) | nothing; the post-quantum handshake also uses the test Caddy |
| `gui` (`tls.ui`) | 15 / 14 | The screens, driven like a user: the certificates screen (import from URL and from the file picker, wrong password, unsupported key type, removal, D-pad navigation) and the Advanced section of the connect / server add screen against a fake Jellyfin server (HTTP address explained, staging certificates, a failed connection restores the old ones and explains why) | nothing |
| `e2e` | 7 / 6 | Against the real Jellyfin behind the test Caddy: public info, login and authenticated requests, WebSocket, the HttpsURLConnection path, the message for a missing client certificate, connecting from the UI with certificates from the URL import, and on mobile the login in the WebView | Jellyfin, the test Caddy, `E2E_USER` / `E2E_PASSWORD` |
| `webview` | – | Updates the WebView of old images and records the version in use | the WebView APKs |

### Checklists with clicking automation (`manual-test.py --auto`)

The whole app as a user would use it, driven by Appium (`appium_flows.py` for mobile, `tv_flows.py` for TV with the
D-pad only) through the real test Caddy and Jellyfin. Every step is checked three ways: the flow passed, the expected
requests went through the mTLS proxy with the client certificate (from Caddy's request log), and no crash or TLS error
in logcat. A screenshot per step goes into the report.

| Checklist | Steps | What it covers |
|---|---|---|
| `mobile` | 31 | Connect with the system file picker, login in the WebView, browse, direct play, seek with subtitles, transcoded play (HLS), the media notification, app restart, a full emulator reboot, download, the certificates screen, removed CA and missing client certificate messages, import from URL, wrong password, PEM and `.p12` mix-ups, the whole course with a CA only, the Android system certificate store (CA, client key, chooser, key removed, no screen lock) |
| `mobile-system` | 11 | The same course with the CA and the client key only in the Android system store |
| `tv` | 21 | The TV course with the D-pad: certificates from the URL import, browse, play, seek, transcoding, restart, reboot, the certificates screen, missing / untrusted / unavailable certificate messages, CA only |
| `tv-phone` | 10 | The TV app on a phone emulator: the system file picker and the system store, which the TV images lack |
| `tv-picker` | 6 | The TV app with a file manager as file picker |

Without `--auto` the same flows click and you confirm every step (to read the texts).

## Files

### Documentation

| File | What it is |
|---|---|
| `README.md` | This overview |
| `TESTING.md` | How to set up and run everything, the step table of the checklists, known problems and their causes |
| `TV-AUTOMATION.md` | Design of the TV clicking automation (`tv_flows.py`): D-pad navigation, what the TV images lack, decisions |
| `WEBVIEW-APKS.md` | Where the WebView APKs of the old Android versions come from; Chrome for the Android 8 AVD has to be downloaded by hand |

### Running the tests

| File | What it is |
|---|---|
| `run-matrix.sh` | Builds both apps and their test APKs once, then runs the instrumented suites on every AVD (device / TLS matrix, GUI, end-to-end, all on one boot), several emulators at a time, also on other machines; writes the results and the matrices |
| `run-gui.sh` | Only the GUI suite (`tls.ui`), a shortcut for `run-matrix.sh` |
| `matrix.py` | Builds the compatibility matrix (Markdown) from the results `run-matrix.sh` collected |
| `instrument2junit.py` | Converts the raw output of `adb shell am instrument -r -w` into JUnit XML |
| `manual-test.py` | Checklist runs: boots each AVD with a window, installs the app, copies the test certificates, walks through a checklist and collects evidence per step (requests through the mTLS proxy, logcat crashes and TLS errors, a screenshot); with `--auto` the flows click and decide pass / fail; `--jobs`, `--remote` for parallel runs |
| `appium_flows.py` | The clicking automation of the mobile checklists (Appium, UiAutomator2 5.x, native screens and jellyfin-web in the WebView) |
| `tv_flows.py` | The clicking automation of the TV checklists (D-pad, UiAutomator2) |
| `watch-run.sh` | Watches a running `manual-test.py` run: state and duration per AVD, hang warnings, failures, desktop notifications; read-only |
| `kill-run.sh` | Stops a `manual-test.py` run or one AVD of it and cleans up its emulators, Appium servers, ssh tunnels and adb servers, here and on the remote machines |

### Checklists (`checklists/`)

Steps of `manual-test.py`: `## id: title`, the instructions, `> action` lines (flows, restart, reboot …) and the
request types that must go through the proxy.

| File | What it is |
|---|---|
| `mobile.md` | jellyfin-android: connect, login, browse, play, seek, transcoding, media notification, restart, reboot, download, certificates screen, error messages, CA-only course, system store steps |
| `mobile-system.md` | jellyfin-android, the whole course with the CA and client key from the Android system store (KeyChain) instead of imported files |
| `tv.md` | jellyfin-androidtv on the TV AVDs: the same course with the D-pad, certificates from the URL import |
| `tv-phone.md` | jellyfin-androidtv on a phone AVD: the paths the TV images lack (document picker, system store with the KeyChain chooser) |
| `tv-picker.md` | jellyfin-androidtv on a TV AVD with a file manager (Material Files) as file picker |

### Test server

| File | What it is |
|---|---|
| `jellyfin-compose.yaml` | Jellyfin in Docker for the tests (user `jellyfin`, password `1234`, media in `~/media`) |
| `tls-test-proxy/Caddyfile` | The test Caddy in front of Jellyfin: 8920 (and 8922 - 8928 for parallel workers) CA + required client certificate, 8921 (8931 - 8937) CA only, 8930 file server for the certificate downloads; request log for the evidence |
| `tls-test-proxy/generate.sh` | Creates the test PKI: server CA, client CA, server certificate (`localhost`, `127.0.0.1`, `10.0.2.2`), client certificates with an EC P-256 and an RSA 2048 key, each as `.p12` with 3DES and with AES, and as PEM; password `jellyfin`. The other key types and encryptions of the matrix are test fixtures of the app branches |
| `push-certs.sh` | Copies the test certificates to `Download/jellyfin-tls/` of a running emulator, for the file picker |

### Emulator setup and maintenance

| File | What it is |
|---|---|
| `setup.sh` | Sets up everything from scratch, skips what exists: checks (KVM, SDK, Appium), SDK packages and system images, the `TLS_*` AVDs, the test PKI, the WebView APKs, the TV file picker, the Caddy release |
| `check-avds.py` | Compares every setting of the `TLS_*` AVDs here with the other machines |
| `trust-adb-key.sh` | Enters this machine's adb key into every AVD, so adb stays trusted |
| `clean-lock.sh` | Removes a leftover screen lock (PIN `1234`) and system credentials from AVDs after an interrupted system store step |

### WebView APKs (see `WEBVIEW-APKS.md`)

| File | What it is |
|---|---|
| `webview-fetch.sh` | Downloads a LineageOS Chromium WebView build of a given version |
| `webview-api23.sh` | Builds `webview-apks/webview-api23.apk` (WebView 88, the newest that runs on Android 6) |
| `webview-resign.sh` | Re-signs a WebView APK with the public AOSP test key, the only key the emulator images accept |
| `webview-install.sh` | Installs the WebView APKs into the old AVDs (Android 6 - 10), once per AVD |

### Other machines (emulators over ssh)

| File | What it is |
|---|---|
| `sync-remotes.sh` | Brings the other machines in line with this one: WebView APKs, setup and WebView scripts, checked with checksums |
| `check-remote-net.sh` | Measures the connection to the other machines (ping, ssh round trip, throughput, their load), a slow link makes their AVDs slow |

`remote-a`, `remote-b` and `remote-c` in the docs and defaults are placeholders for other Linux machines with KVM
that run emulators over ssh (`--remote HOST[:JOBS]`, `MANUAL_REMOTE_HOST`). Name your machines so in
`~/.ssh/config` or pass the hosts explicitly.

### Diagnostics

| File | What it is |
|---|---|
| `bench-gpu.sh` | Benchmarks the emulator's `-gpu` modes without the app (boot, frame times, a soak test), to choose one for AVDs that crash on the host GPU |
| `bench-steps.sh` | Runs a few checklist steps on one AVD once per `-gpu` mode and compares time, results and emulator crashes |

### Not in git (created by the scripts)

| Folder / file | What it is |
|---|---|
| `tls-test-proxy/certs/`, `tls-test-proxy/public/` | The test PKI (`generate.sh`), `public/` is what the file server offers |
| `tls-test-proxy/bin/` | The Caddy release (`setup.sh`), the distribution packages lack the post-quantum key exchange |
| `webview-apks/` | The WebView APKs (`WEBVIEW-APKS.md`) |
| `tv-picker/` | Material Files from F-Droid for `tv-picker.md` (`setup.sh`) |
| `results*/` | Results and matrices of `run-matrix.sh` |
| `manual-results/` | Reports, screenshots and logs of `manual-test.py` |
| `timings.txt` | Durations of earlier runs, `run-matrix.sh` derives its timeouts from them |
