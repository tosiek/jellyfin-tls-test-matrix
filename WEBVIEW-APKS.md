# WebView APKs

WebView APK origin (`webview-apks/`, about 0.8 GB, not in git), all for the AOSP `default` images:
- `webview-api23.apk`: `com.android.webview` 88 (the newest that runs on Android 6), LineageOS
  `android_external_chromium-webview_backup-20210212`, re-signed with the public AOSP test key (`./webview-api23.sh`)
- `webview-api24.apk` (119, Android 7), `webview-api28.apk` (135, Android 9: chromedriver 137 and 138 fail there, `pm path --user cur`), `webview-api29.apk` (155, minSdk 29):
  LineageOS `android_external_chromium-webview_prebuilt_x86_64` (branch main, `webview.apk` in Git LFS at
  review.lineageos.org, one commit per version), re-signed with `./webview-resign.sh`
- `provider-api26.apk`: Chrome 126 (APKMirror), the only provider the AOSP Android 8 image accepts
Install with `adb install --no-streaming` (a streamed install of a 260 MB APK hung on Android 7).

## Chrome for Android 8: download it yourself

`webview-apks/provider-api26.apk` is **required** for the Android 8 AVD (`TLS_API_26`) and is **not** in this
repository and not downloaded by any script: Google Chrome may not be redistributed. The AOSP Android 8 image accepts
no `com.android.webview`, only Chrome or Google's WebView, so without it the WebView of `TLS_API_26` stays too old for
the tests. Download Chrome (`com.android.chrome`) 126, x86_64, as a single APK (not a split bundle), e.g. from
APKMirror, and save it as `webview-apks/provider-api26.apk`. `setup.sh` reports it while it is missing.

The other WebView APKs are open source builds of LineageOS, `webview-fetch.sh` / `webview-api23.sh` download them and
`webview-resign.sh` signs them for the emulator images.
