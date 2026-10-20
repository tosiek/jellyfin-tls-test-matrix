# Checklist of manual-test.py for jellyfin-androidtv
# "## id: title", instructions, "> action" lines run by the script before the step
# (force-stop, launch, reboot, clear-data, push-certs, install-picker, uninstall-picker,
# appium <flow of tv_flows.py> [KEY=VALUE …]),
# "> appium ensure-connected" restores the connected, logged-in app when the step before left it otherwise (noted
# in the report), "? kinds" request types that must go through the proxy during the step
# (api, images, streaming, subtitles, …),
# "streaming" matches HLS (transcoded) and direct streams. Caddy logs a request when it ends: a direct-play stream
# stays open while the video plays and shows up in the step that ends it (seek), play checks the position instead.
# Use only the D-pad: arrow keys, Enter and Esc (back) in the emulator window, or the emulator's remote control.
# The TV images have no file picker: certificates come from the proxy's file server http://10.0.2.2:8930/.
# setup removes Material Files (tv-picker.md) in case an earlier run left it installed.

## setup: Add the server with certificates from the URL
> uninstall-picker
> appium setup
? api
Enter server address: https://10.0.2.2:8920, open Advanced, "Load certificates from URL (network)" is checked.
CA: Choose…, http://10.0.2.2:8930/ca.pem. Client certificate: Choose…, http://10.0.2.2:8930/client-ec.p12, password:
jellyfin. The app offers to trust Jellyfin Test Client CA from the file: Don't trust (the proxy has its own CA).
Connect.
Check: every field and button reachable with the D-pad, the user selection / login appears.

## login: Log in
> appium login
? api
Add account > Use a password, log in as jellyfin (password 1234).
Check: home screen, no certificate error.

## browse: Home screen and library
> appium ensure-connected
> appium browse
? images
Scroll the home rows, open a library and an item detail page.
Check: posters, backdrops and the user avatar load (no empty tiles), the evidence shows image requests.

## play: Play the video
> appium ensure-connected
> appium play
Play the first video of the library for at least 10 seconds.
Check: starts within a few seconds, no error, the position advances (the stream request of direct play is logged when
it ends, transcoded files show HLS playlists and segments right away).

## seek: Seek and switch tracks
> appium seek
? streaming subtitles
Seek with right on the progress bar (Enter ends the seeking), switch the subtitles on (test clip: external English
.srt).
Check: resumes after each seek, no error, the subtitle lines appear (the .srt is loaded through the proxy).

## play-second: Play the second video (transcoded)
> appium play CARD=1 QUALITY=low
? streaming-hls
Back to the library, play the second video and choose the lowest bitrate in the player's quality menu, play it for at
least 10 seconds. Both test videos are H264 (the emulator plays them directly), the low bitrate makes Jellyfin
transcode.
Check: playback restarts within a few seconds after the quality change, no error, the evidence shows HLS playlists and
segments through the proxy.

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

## settings: Certificates screen
> appium ensure-connected
> appium settings
Settings > Login > the server > Certificates: the CA and the client certificate are listed with details.
Check: reachable with the D-pad, names, expiry dates and key type make sense, texts are readable on a TV.

## missing: Missing client certificate is explained
> appium ensure-connected
> appium missing
Remove the client certificate on the certificates screen, back to the home screen; restart the app,
choose the user, sign in.
Check: in the running session, on the user selection after the restart (failed automatic sign-in) and on the login
screen the message explains that the server requires a client certificate (not a generic network error).

## saved-server: Untrusted saved server is explained
> appium saved-server
Remove the CA on the certificates screen, back to the user selection, the server button > the saved server, the user,
sign in.
Check: the error explains that the server certificate is not trusted and names its issuer (not "Unable to connect").

## reimport: Import from URL
> appium reimport
? api
On the certificates screen Import from URL…: http://10.0.2.2:8930/ca.pem as CA certificate (Trust), then
http://10.0.2.2:8930/client-ec.p12 as client certificate, password jellyfin (Use). The app offers to trust Jellyfin Test
Client CA from the file: Don't trust (the proxy has its own CA). Back, sign in.
Check: the app works again without restarting, text entry with the on-screen keyboard is usable.

## unavailable: Client key no longer available
> appium ensure-connected
> appium unavailable
The app's key of the client certificate is gone while the configuration still refers to it (after a restore from a
backup). The flow simulates it: the stored key alias is replaced by one that does not exist (run-as on the debug app).
Restart the app, choose the user, sign in.
Check: the user selection (failed automatic sign-in) and the login screen say the client certificate is no longer
available, the certificates screen labels it "(no longer available)". The flow imports
client-ec.p12 again and signs in.

## wrong-password: Wrong password message
> appium ensure-connected
> appium wrong-password
Import http://10.0.2.2:8930/client-rsa.p12 as client certificate with the password "wrong".
Check: the message clearly says the password is wrong, the existing certificate is unchanged.

## pem-client: PEM file as client certificate
> appium ensure-connected
> appium pem-client
Import http://10.0.2.2:8930/pem-key-and-cert.pem (private key and certificate in PEM) as client certificate.
Check: the message says client certificates must be a .p12 / .pfx file with the certificate and its key, before any
password is asked, the existing certificate is unchanged.

## p12-ca: Client .p12 picked as CA certificate
> appium ensure-connected
> appium p12-ca
? api
Remove the client certificate. Import from URL… http://10.0.2.2:8930/client-ec.p12 as CA certificate, password jellyfin.
Check: the CA confirmation lists Jellyfin Test Client CA (Trust), then the app offers the client certificate in the file
(Use): the client line shows CN=client-ec. Remove Jellyfin Test Client CA again (the proxy has its own CA), the app
works with the client certificate from the CA file.

## ca-only-setup: Custom CA without a client certificate
> clear-data
> launch
> appium setup CA_ONLY=1
? api
The most common setup: a server with its own CA, no client certificate. Enter server address: the worker's CA-only
address (https://10.0.2.2:8921 for the first worker), Advanced, CA: Choose…, http://10.0.2.2:8930/ca.pem, no client
certificate. Connect.
Check: the user selection / login appears, the requests carry no client certificate.

## ca-only-login: Log in, CA only
> appium login
? api
Check: home screen.

## ca-only-browse: Home screen and library, CA only
> appium browse
? images
Check: posters and backdrops load through the CA-only port.

## ca-only-play: Play the video, CA only
> appium play
Check: the position advances.

## ca-only-play-second: Transcoded video, CA only
> appium play CARD=1 QUALITY=low
? streaming-hls
Check: HLS playlists and segments through the CA-only port.
