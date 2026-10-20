# Automating the TV checklist

The clicking automation for jellyfin-androidtv, the counterpart of `appium_flows.py` for the mobile app:
`manual-test.py --auto` runs `checklists/tv.md` on TLS_TV_30 and TLS_TV_34 unattended, with the same evidence as
for mobile (requests through the mTLS proxy, logcat, a screenshot per step). Written as a plan, then built
(`tv_flows.py`); re-check the facts below when an image or the app changes.

## 1. Where things stand

| Part | State |
| --- | --- |
| Instrumented suites (`run-matrix.sh`: device, gui, e2e) | run on both TV AVDs, 14 / 14 / 6 tests pass |
| `checklists/tv.md` | 15 steps, every one with a `> appium …` flow, the setup step uses the URL import |
| `manual-test.py` | builds, installs and launches the TV app (`--app tv` is the default for `TLS_TV_*`), runs the flows of `tv_flows.py` for the TV app (`flow_module`), no chromedriver for it |
| `tv_flows.py` | the TV flows, D-pad helpers; reuses `Device`, `FlowFailed` and `run()` of `appium_flows.py` (`run` takes the flow table) |
| App code | the `tls` package is identical in both apps (apart from the package name and indentation); both offer "Choose from system store…" on the connect / server add screen, only mobile opens the chooser by itself when the server asks for a client certificate (on TV the store is mostly empty) |

## 2. The TV images (measured)

| | TLS_TV_30 | TLS_TV_34 |
| --- | --- | --- |
| Image | `android-30;android-tv;x86` | `android-34;google-tv;x86` |
| Document picker (`ACTION_OPEN_DOCUMENT`, openable, `*/*`) | none | none |
| KeyChain chooser (`com.android.keychain.CHOOSER`) | yes | yes |
| Certificate installer (`android.credentials.INSTALL`) | yes | yes |
| Security settings | `com.android.tv.settings/.system.SecurityActivity` | same |
| Keyboard | LatinIME | LatinIME |
| Screen lock | none | none |
| WebView | not used by the TV app (native UI) | |

Consequences:
- **No file picker**, but a stub: TLS_TV_34 has `com.android.tv.frameworkpackagestubs/.Stubs$DocumentsStub` for
  `OPEN_DOCUMENT` (finishes without a result). `hasDocumentPicker()` took it for a picker: "Load from URL" was
  unchecked and "Choose…" did nothing. Fixed in both apps (see 8), certificates come from a URL
  (`http://10.0.2.2:8930/…`, the proxy's file server).
- **No WebView**: no chromedriver, no WebView context switching. Everything is native and reachable through
  UiAutomator2, which is simpler than on mobile.
- **x86 (32-bit) images**: KVM works, boots take ~15 s.

## 3. Prerequisites (same as mobile)

- Appium 3.8.0 with the UiAutomator2 driver **5.0.7** and the Appium Python client (`./setup.sh` checks them). 5.x is
  only needed for Android 6 - 7, it works on the TV images too, keep one driver for both apps.
- The test Caddy (`tls-test-proxy/`), Jellyfin with the user `jellyfin` / `1234` and the two test videos (both
  H264: "transcoding" needs the lowest quality, see the play-second step of `mobile.md`).
- The TV app built: `cd ../jellyfin-androidtv && ./gradlew :app:assembleDebug` (or let `manual-test.py` build it).

## 4. Design

### Code layout
- New module **`tv_flows.py`** next to `appium_flows.py`. Import the shared parts from `appium_flows`
  (`Device`, `FlowFailed`, `run`'s error handling, `install_system_certificate` …) instead of copying them.
  `Device.open()` sets the capabilities; for TV only the chromedriver capabilities are unused, they do no harm.
  Make `APP_ID` a parameter of `Device` (or of the module) instead of the mobile constant.
- `FLOWS` of the TV module: built the same way (`flow_<name>` functions → `name`).
- `manual-test.py`: pick the flow module by app. Today it imports `appium_flows` and checks for mobile flows
  only (search for "no Appium flows for"); make `run_flow` call `tv_flows.run` for `app == "tv"`. Nothing else in
  the evidence collection is app specific.
- `checklists/tv.md`: add `> appium <flow>` and `? <request kind>` lines to every step, like `mobile.md`.

### Interaction rules for a TV UI
1. **The D-pad is part of what is tested.** The checklist asks "every field and button reachable with the D-pad".
   Write a helper `focus(d, locator, keys=("DPAD_DOWN",), max_presses=15)` that presses keys until the target
   element reports `focused="true"`, and fails with "not reachable with the D-pad" otherwise. Then press
   `DPAD_CENTER` (keycode 23) instead of `element.click()`. Use `click()` only where reachability is not the subject
   (dialog buttons, already checked screens), and say so in a comment.
2. **Text entry**: `element.send_keys(…)` (UiAutomator2 sets the text directly). The on-screen keyboard may open:
   close it only when it is shown (`dumpsys input_method | grep mInputShown=true`), an extra Back leaves the screen
   (that closed the app on mobile).
3. **Dialogs** (URL prompt, password, warnings) are AlertDialogs: the field has the hint
   `https://example.lan/certificate.p12` (URL) or the string `tls_password`, the buttons are
   `android:id/button1` (OK) and `android:id/button2` (Cancel).
4. **Waits**: wait for an element, never for a fixed time, except after a key press that starts an animation
   (≤ 1 s). Leanback rows load asynchronously, poll for the row title.
5. **Evidence first, UI second**: every step must also show its requests in the proxy log (`? api`, `? images`,
   `? streaming`, `? streaming-hls`), that is the actual mTLS proof; the UI checks only make sure the step did what
   it says.

### Locators (measured on TLS_TV_34)

Resource ids carry the package: `org.jellyfin.androidtv.debug:id/<id>`.

| Screen | Elements |
| --- | --- |
| Welcome (`SelectServerFragment`) | `enter_server_address` (focused at start), `stored_servers`, `discovery_servers`, `welcome_title` |
| Server add (`ServerAddFragment`) | `address` (EditText, focused), `confirm` ("Connect"), `advanced` ("Advanced"), `advanced_section`, `error` |
| Advanced section (`CertificateRowsView`) | CheckBox "Load certificates from URL (network)"; rows "CA certificate" / "Client certificate", each: value text ("None", "ca.pem (new)", "… (no longer available)"), Button "Choose…", Button "Remove". **No ids**: locate by the row title and the following buttons |
| Server (`ServerFragment`) | `users`, `add_user_button`, `server_button`, `notification` |
| Login (`UserLoginCredentialsFragment`) | `username`, `password`, `confirm`, `error` |
| User selection (`ServerFragment`) | `users` (grid, the user card is a ViewGroup inside it), `add_user_button`, `server_button` (focus on an inner View); toolbar Views with content-desc `Help`, `Preferences` |
| Quick Connect / login | Quick Connect first (nothing focused), `use_credentials` ("Use a password"), then `username`, `password`, `confirm`, `error`, `cancel`; a known user has `username` filled and disabled |
| Home | toolbar Views (content-desc `Switch account`, `Preferences`, texts Home / Search), `row_header` "My media", `row_content` (content-desc "My media"), library cards: ComposeView without text, the library name in a TextView inside |
| Library grid | `title` (focused item), `counter` ("1 \| 2"), `statusText`, cards: ComposeView without ids, the first is focused |
| Item details | `fdTitle`, `fdButtonRow`: focused FrameLayout around an ImageView with content-desc `Play` / `Resume` |
| Player | `exoPlayerView`; controls after any key: `playback_progress` (SeekBar, focused), row above: content-desc `Pause`, `Rewind`, `Fast Forward`, `Select subtitle track`; row below: `Playback speed`, `Quality profile`, `Zoom`; `current_time`, `total_time`. Menus are ListViews (`title` entries) |
| Settings (Compose) | items are focusable Views with the texts inside: Login > the server (its address) > Certificates; from the user selection the Preferences open the Login settings directly |
| Certificates screen | ScrollView (focused at start, not a button), Buttons by text; the URL dialog has a single choice list (CA / client) above the field |

Not done (the flows locate by text, which works): give the certificate row views ids in `CertificateRowsView`
(e.g. `tls_ca_value`, `tls_ca_choose`, `tls_ca_remove`, `tls_client_…`, `tls_load_from_url`) with `View.generateViewId`
replaced by ids from `res/values/ids.xml`. Make the same change in jellyfin-android: the `tls` package must stay
identical in both apps.

## 5. Work plan

### 5.1 Explore the screens (done)
Boot TLS_TV_34 with a window, install the app, walk through the checklist by hand and dump every screen:
```bash
~/Android/Sdk/emulator/emulator -avd TLS_TV_34 -port 5620 -read-only -no-snapshot &
adb -s emulator-5620 shell uiautomator dump /sdcard/u.xml && adb -s emulator-5620 shell cat /sdcard/u.xml
```
Record for home rows, the library grid, the item details page, the player (position text, seek, audio /
subtitle / quality menus), Settings > Authentication > server > certificates: element class, id, text,
content-desc, which element has focus after each key press. Add the results to the locator table above.

Result: "Load from URL" was unchecked because of the TV framework stubs (see section 2), fixed in both apps; "Choose…"
and the certificates screen's import buttons now go to the URL dialog when there is no real picker.

### 5.2 Infrastructure (done)
- `tv_flows.py` with `Device` reuse, `APP_ID = "org.jellyfin.androidtv.debug"`, the D-pad helpers (`key`,
  `focus`, `center`), `type_into_dialog`, `hide_keyboard_if_shown`.
- `manual-test.py`: flow module per app, remove the "no Appium flows for tv" skip for TV once `tv.md` has flows.
- One trivial flow (`welcome`: the welcome screen with focus on "Enter server address") and run it:
  `./manual-test.py --auto --steps welcome TLS_TV_34`.

### 5.3 Flows for `tv.md` (done, as planned except where noted in 8)

| Step | Flow | What it does, what it checks | Evidence |
| --- | --- | --- | --- |
| setup | `setup` | Enter server address, `https://10.0.2.2:<proxy port>`, Advanced, CA from URL `…:8930/ca.pem`, client from URL `…:8930/client-ec.p12` password `jellyfin`, Connect, all by D-pad; the server screen appears | `? api` |
| login | `login` | Add user / login with `jellyfin` / `1234` (E2E_USER / E2E_PASSWORD), home appears | `? api` |
| browse | `browse` | Scroll the home rows, open a library and an item; poster / backdrop requests | `? images` |
| play | `play` | Play the first video ≥ 10 s, position advances | `? streaming` |
| play-second | `play CARD=1 QUALITY=low` (new step, like mobile) | Second video at the lowest quality, the server transcodes | `? streaming-hls` |
| seek | `seek` | `DPAD_RIGHT` / `MEDIA_FAST_FORWARD` past ~55 s (the open direct stream ends and is logged), switch subtitles | `? streaming` |
| restart | `stop-player`, `force-stop` (Home and 5 s first), `launch`, `home` | Home screen without importing again. Killed in the foreground the app crashed the host GPU (`error null ctx`) | `? api` |
| reboot | `reboot` (power off, new emulator process), `launch`, `home` | Same after switching off and on, the client key survived (Android Keystore) | `? api` |
| settings | `settings` | Settings > Authentication > server > certificates: CA and client listed with subject, expiry, key type | |
| missing | `missing` | Remove the client certificate, reconnect: "requires a client certificate" (not a generic error) | |
| saved-server (new) | `saved-server` | Remove the CA on the certificates screen, back to the server list, select the server: the toast explains "not trusted (issued by …)" (fix c39e17c65) | |
| reimport | `reimport` | Client certificate from URL again, works without restart | `? api` |
| wrong-password | `wrong-password` | `client-rsa.p12` with "wrong": wrong password message, certificate unchanged | |
| pem-client (new) | `pem-client` | A PEM file (URL `…:8930/` has none: add `pem-key-and-cert.pem` to `tls-test-proxy/public/` in `generate.sh`) is rejected **before** a password dialog (fix 069175a81) | |
| unavailable (new) | part of `missing` or own step | A removed key is labeled "… (no longer available)" (fix 3453a2e8a) | |

Toasts (saved-server, missing) are short-lived: read them with the UiAutomator2 toast support
(`//android.widget.Toast`) right after the key press, or check logcat for the message.

### 5.4 System store on TV (decided: not used on the TV emulators)
Checked on TLS_TV_34: the TV Settings app has no credential screens ("Security & Restrictions" only has unknown
sources), so a CA cannot be installed into the system store on Android 11+ TV and keys cannot be cleared. The
certificate installer exists and opens a .p12 (`VIEW`, `application/x-pkcs12`), but cannot read shared storage
(EACCES): keys only come from an app (`KeyChain.createInstallIntent`) or device management, and would stay in the AVD.
The KeyChain chooser exists on both images. The TV certificates screen and the server add screen
offer "Choose from system store…", tested with the TV app on a phone AVD (tv-phone.md system-client, system-setup),
not on the TV images.

### 5.5 Parallel runs and the full suite (done, see 8)
- `--jobs 2` with both TV AVDs: every worker has its own proxy port (8920, 8922); the flows must take the
  address from the environment (`MANUAL_SERVER_PORT`), like the mobile flows.
- Run `./manual-test.py --auto --jobs 2 TLS_TV_30 TLS_TV_34` three times in a row: all steps must pass every time
  before the automation counts as done. Then add the TV run to TESTING.md section 2.

## 6. Lessons from the mobile automation (apply them from the start)
- Appium session: `newCommandTimeout` 0 and `Device.reopen_if_lost()` are already in `Device`, keep using it.
- A video stream is one long request, Caddy logs it when it ends: `manual-test.py` counts it for the step it started
  in (late evidence), a seek past the buffer or leaving the player ends it.
- Never press Back "to be sure": check the keyboard state first.
- Killing the app during playback or in the foreground can crash the emulator with the host GPU: leave the player
  before `force-stop`, which presses Home and waits 5 s itself (`appium_flows.kill_app` in the flows).
- Emulators are powered off cleanly (`reboot -p`), the next boot waits until the old process is gone; a single-AVD
  run leaves the emulator running.
- Every flow logs what it saw (`d.log`) and raises `FlowFailed` with a reason a person understands; the report
  shows both next to the screenshot.

## 7. Done when
- [x] `checklists/tv.md` has a flow for every step, the setup step uses the URL import.
- [x] `./manual-test.py --auto --jobs 2 TLS_TV_30 TLS_TV_34` passes three times in a row (see 8).
- [x] TESTING.md lists the TV run.
- [x] The decision on the system store for TV: not used on the TV emulators (5.4).

## 8. Result

Three runs of `./manual-test.py --auto --jobs 2 TLS_TV_30 TLS_TV_34`, 15 / 15 steps on both AVDs every time, ~10 min per
run. Every run has one note: TLS_TV_30 `reboot`, right after the
boot auto sign-in fell back to the user selection without a message, the relaunch signed in (the same cold-start
connection timeout as the mobile app's `home` flow, which also relaunches once).

Steps beyond the plan: `unavailable` (the key is simulated as lost with `run-as`, the TV images are user builds without
root), `saved-server`, `pem-client` (`tls-test-proxy/generate.sh` now writes `public/pem-key-and-cert.pem`).
Differences to the plan: `missing` also restarts the app and signs in, `reimport` imports the CA as well
(saved-server removed it), the messages of `saved-server` are read on the login screen (choosing a saved server or the
user leads to it, its error line explains the TLS problem), the toasts added afterwards in `missing` and `unavailable`.

App fixes (both apps, the `tls` package stays identical):
- `hasDocumentPicker()` ignores the TV framework stubs (`com.android.tv.frameworkpackagestubs` on TLS_TV_34,
  `com.google.android.tv.frameworkpackagestubs` on TLS_TV_30): "Load from URL" was unchecked and "Choose…" opened the
  stub, which did nothing. "Choose…" on the connect screen and the import buttons of the certificates screen go to the
  URL dialog without a picker.
- `CertificatesActivity.finish()` starts the app again when it is the last activity of its task: opened from the user
  selection (Preferences > Login > server > Certificates), Back left the app, `StartupActivity` is `noHistory`.

Fixed afterwards (three more runs passed 15 / 15 on both AVDs with them):
- In a running session, removing the client certificate (or the CA) left the home rows and libraries empty without
  any message. `TlsManager` reports failed requests with a TLS problem to listeners, the TV `MainActivity` shows the
  reason as a toast, once per problem while it is shown. `missing` checks it.
- A failed auto sign-in (missing certificate, cold start; upstream `SessionRepository`) showed the user selection
  without a message: the error is kept, the user selection shows its reason as a toast. `missing` and `unavailable`
  check it; the TLS_TV_30 `reboot` relaunch note stays (cold start, "Unable to connect").
- The CA removal dialog had the focus on "REMOVE" (one Enter removed the CA), now on "Cancel" (both apps;
  `saved-server` still reaches "REMOVE" with Right).

Accepted: after a list re-render (remove, import) the certificates screen puts the focus back on the ScrollView at the
top.

Checklist order changed with the in-session check: settings, missing, saved-server, reimport, unavailable (imports the
key again at its end), wrong-password, pem-client.

Automation lessons (in `tv_flows.py`):
- Keys through `adb shell input keyevent`: Appium's `press_keycode` stopped moving the focus after `send_keys` into a
  field.
- The keyboard is up only with `mInputShown=true` (`mIsInputViewShown` stays true after it closed). Back when it is
  not up leaves the screen or the app: `back_until()` waits for the expected screen after every press.
- `focus()` does not count a focused ScrollView / ListView around the target; in a dialog list the selected entry
  counts.
- After Right on the player's progress bar it stays in scrub mode (Up does nothing, Right seeks on) until Center.
- `enforceXPath1`: the XPath 2 engine of UiAutomator2 failed on a union ("ArrayList$ListItr cannot be cast to
  NodeType", TLS_TV_34).
- The clip is 2:00 long, each Right on the progress bar seeks 30 s: `seek` presses it twice.
- Toasts: UiAutomator2 lists them as `android.widget.Toast` nodes only with `enableMultiWindows` on, and only those
  shown after it was switched on; read from the page source (`toasts()`, `wait_toast()`). The in-session toast comes
  with the first Back from the certificates screen (the main activity starts again under the settings).
