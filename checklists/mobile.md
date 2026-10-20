# Checklist of manual-test.py for jellyfin-android
# "## id: title", instructions, "> action" lines run by the script before the step
# (force-stop, launch, reboot, clear-data, push-certs, appium <flow of appium_flows.py> [KEY=VALUE …]),
# "> appium ensure-connected" restores the connected, logged-in app when the step before left it otherwise (noted
# in the report), "? kinds" request types that must go through the proxy during the step
# (api, images, streaming, websocket, …),
# "streaming" matches HLS (transcoded) and direct streams. Caddy logs a request when it ends: a direct-play stream
# stays open while the video plays and shows up in the step that ends it (seek), play checks the position instead

## setup: Connect with certificates from the file picker
> appium setup
? api
Server address: https://10.0.2.2:8920, open Advanced.
CA: pick Download/jellyfin-tls/proxy/ca.pem in the system file picker (not the URL import).
Client certificate: client-ec.p12 from the same folder, password: jellyfin. The app offers to trust Jellyfin Test Client
CA from the file: Don't trust (the proxy has its own CA). Connect.
Check: no "untrusted server" or "client certificate required" message, the login page appears.

## login: Log in in the WebView
> appium login
? api
Log in as jellyfin with the jellyfin-web login form.
Check: home screen of jellyfin-web, no certificate error page.

## browse: Home screen and library
> appium ensure-connected
> appium browse
? images
Scroll the home screen, open the first library (Movies).
Check: posters, backdrops and the user avatar load (no empty tiles), the evidence shows image requests.

## play: Play the first movie
> appium ensure-connected
> appium play
Play the first movie of the library for at least 10 seconds.
Check: starts within a few seconds, no error, the position advances (the stream request of direct play is
logged when it ends, transcoded files show HLS playlists and segments right away).

## seek: Seek and switch tracks
> appium seek
? streaming
Seek forward and back a few times, switch the subtitles on (test clip: external English .srt).
Check: resumes after each seek, no error, the subtitle lines appear (the .srt is loaded through the proxy).

## play-second: Play the second movie (transcoded)
> appium play CARD=1 QUALITY=low
? streaming-hls
Go back to the library, play the second movie and choose the lowest quality in the player's quality menu, play it
for at least 10 seconds. Both test videos are H264 (the emulator plays them directly), the low bitrate makes Jellyfin
transcode.
Check: playback restarts within a few seconds after the quality change, no error, the evidence shows HLS playlists
and segments through the proxy.

## background: Media notification
> appium background
? streaming-hls
Press Home during playback, pause and resume with the notification controls, open the app again.
Check: controls work, playback continues where it was, the HLS segments keep coming through the proxy while the app is
in the background (the player's TLS without the app in front).

## restart: App restart keeps the certificates
> appium ensure-connected
> appium stop-player
> force-stop
> launch
> appium home
? api
Check: the app connects without importing again, home screen loads with images. Playback is stopped and the app
goes to the background (Home, 5 s) before force-stop kills it: killed in the foreground it crashed the emulator's
host GPU.

## reboot: Switching the device off and on keeps the certificates
> appium ensure-connected
> reboot
> launch
> appium home
? api
Check: same as restart, the client key survived (Android Keystore). The emulator is powered off and started again
(a new emulator process), not rebooted with adb.

## download: Download for offline use
> appium ensure-connected
> appium download
? download
Download the video (item menu > Download), wait until it is finished.
Check: download completes, the evidence shows download requests.

## settings: Certificates screen
> appium ensure-connected
> appium settings
Settings > certificates of the server: the CA and the client certificate are listed with details.
Check: names, expiry dates and key type make sense, texts are understandable.

## missing-ca: Removed CA in a running session
> appium ensure-connected
> appium missing-ca
Remove the CA on the certificates screen, go back (jellyfin-web makes a request). Then Import CA certificate…, pick
Download/jellyfin-tls/proxy/ca.pem in the system file picker, Trust, go back.
Check: without the CA jellyfin-web no longer reaches the server (within 15 s) and a message explains that the server
certificate is not trusted. The confirmation shows the certificate, the CA is listed again, jellyfin-web works without
restarting the app.

## missing: Missing client certificate is explained
> appium ensure-connected
> appium missing
Remove the client certificate on the certificates screen, go back (jellyfin-web makes a request), then reload /
reconnect.
Check: in the running session a message explains that the server requires a client certificate. Connect opens the
system certificate chooser, cancel it. The error explains that the server requires a client certificate (not a generic
network error).

## reimport: Import from URL
> appium reimport
? api
After missing, on the connect screen: Advanced, Load certificates from URL, client certificate Choose…,
http://10.0.2.2:8930/client-ec.p12, password jellyfin. The app offers to trust Jellyfin Test Client CA from the file:
Don't trust (the proxy has its own CA). Then Connect.
Check: the app works again without restarting.

## wrong-password: Wrong password message
> appium ensure-connected
> appium wrong-password
Import http://10.0.2.2:8930/client-rsa.p12 with the password "wrong", then cancel.
Check: the message clearly says the password is wrong, the existing certificate is unchanged.

## pem-client: PEM file as client certificate
> appium ensure-connected
> appium pem-client
Certificates screen: Import client certificate, pick Download/jellyfin-tls/fixtures/client/pem-key-and-cert.pem (private
key and certificate in PEM). If a password is asked, jellyfin.
Check: the message says client certificates must be a .p12 / .pfx file with the certificate and its key (Android reads
client keys only from PKCS#12), the existing certificate is unchanged.

## p12-ca: Client .p12 picked as CA certificate
> appium ensure-connected
> appium p12-ca
Remove the client certificate. Import from URL… http://10.0.2.2:8930/client-ec.p12 as CA certificate, password jellyfin.
Check: the CA confirmation lists Jellyfin Test Client CA (Trust), then the app offers the client certificate in the file
(Use): the client line shows CN=client-ec. Remove Jellyfin Test Client CA again (the proxy has its own CA), the app
works with the client certificate from the CA file.

## ca-only-setup: Custom CA without a client certificate
> clear-data
> launch
> appium setup CA_ONLY=1
? api
The most common setup: a server with its own CA, no client certificate. Connect screen: the worker's CA-only address
(https://10.0.2.2:8921 for the first worker), Advanced, Load certificates from URL, CA certificate Choose…,
http://10.0.2.2:8930/ca.pem, no client certificate. Connect.
Check: no "untrusted server" message, the login page appears, the requests carry no client certificate.

## ca-only-login: Log in, CA only
> appium login
? api
Log in as jellyfin with the jellyfin-web login form.
Check: home screen of jellyfin-web.

## ca-only-browse: Home screen and library, CA only
> appium browse
? images
Check: posters and backdrops load through the CA-only port.

## ca-only-play: Play the first movie, CA only
> appium play
Check: the position advances (direct play with the app's CA).

## ca-only-play-second: Transcoded movie, CA only
> appium play CARD=1 QUALITY=low
? streaming-hls
Check: HLS playlists and segments through the CA-only port.

## ca-only-download: Download, CA only
> appium download
? download
Check: the download completes (the app's OkHttp with the CA, no client certificate).

## system-ca: CA from the system certificate store
> clear-data
> appium system-ca
? api
Client certificate client-ec.p12 from the file picker, Connect: "not trusted". Install ca.pem in Settings > Encryption &
credentials > Install a certificate > CA certificate (Android 10 and older: the certificate installer, name it), back
to the app, Connect again without restarting it.
Check: login works, the trust store change reached the running app. A screen lock asked for by the installer: PIN 1234.

## system-ca-client: CA and client certificate from the system certificate store
> clear-data
> appium system-ca-client
? api
Clear credentials, install ca.pem and client-ec.p12 (VPN & app user certificate, password jellyfin) in the system store,
nothing in the app. Connect.
Check: the system certificate chooser opens by itself, after choosing client-ec the login works, the certificates screen
shows the client certificate "From system store".

## system-client: Client certificate from the system certificate store
> clear-data
> appium system-client
? api
Clear credentials, install only client-ec.p12 in the system store. Connect screen: Advanced, CA ca.pem from the file
picker, Connect.
Check: the chooser opens by itself, login works, the client certificate is "From system store".

## system-key-removed: System key removed while the app uses it
> appium system-key-removed
After system-client: Settings > Encryption & credentials > Clear credentials (the app still refers to the key), restart
the app, Connect.
Check: the message says the client certificate is no longer available, not a generic error.

## system-choose: Choose the system key by hand
> appium system-choose
? api
Install client-ec.p12 in the system store again. Connect screen: Advanced, client certificate "Choose from system
store…", pick client-ec, Connect.
Check: login works, the certificates screen shows the client certificate "From system store".

## system-no-lock: No screen lock, no client certificate
> clear-data
> appium system-no-lock
Clear credentials, no screen lock. Connect screen: Advanced, CA ca.pem from the file picker, Connect.
Check: the system certificate chooser (cancel it) or the "requires a client certificate" message, not Android's "set a
lock screen PIN" prompt.

## system-cleanup: Clear the system certificate store
> appium system-cleanup
Settings > Clear credentials, remove the PIN 1234 if the installer asked for one.
Check: no user CA or key left, no screen lock (later runs and the reboot step need an unlocked device).
