# Checklist of manual-test.py for jellyfin-android with the CA and the client key from the Android system store
# (./manual-test.py --checklist mobile-system). The same course as mobile.md, but nothing imported in the app: the
# WebView, the player, the media notification, downloads, an app restart and a reboot all have to work with the
# KeyChain key. Format: see mobile.md. Android 6 - 9 require a screen lock for the system store, the installer steps
# set PIN 1234, "> unlock" enters it after the reboot; system-cleanup removes everything again.

## system-setup: CA and client certificate only in the system store
> clear-data
> appium system-ca-client
? api
Clear credentials, install ca.pem (CA certificate) and client-ec.p12 (VPN & app user certificate, password jellyfin) in
the system store, nothing in the app. Connect, choose client-ec in the system certificate chooser, log in as jellyfin.
Check: the chooser opens by itself, login works, the certificates screen shows the client certificate "From system
store". A screen lock asked for by the installer: PIN 1234.

## browse: Home screen and library
> appium browse
? images
Scroll the home screen, open the first library (Movies).
Check: posters, backdrops and the user avatar load (no empty tiles), the evidence shows image requests.

## play: Play the first movie
> appium play
Play the first movie of the library for at least 10 seconds.
Check: starts within a few seconds, no error, the position advances (the player's requests use the system key).

## seek: Seek and switch tracks
> appium seek
? streaming
Seek forward and back a few times, switch the subtitles on.
Check: resumes after each seek, no error, the subtitle lines appear.

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
Check: controls work, playback continues where it was (the player service uses the system key).

## restart: App restart keeps the system certificates
> appium stop-player
> force-stop
> launch
> appium home
? api
Check: the app connects without the chooser and without importing anything, home screen loads with images.

## reboot: Switching the device off and on keeps the system certificates
> reboot
> unlock
> launch
> appium home
? api
Unlock with PIN 1234 if the lock screen is shown (Android 6 - 9).
Check: same as restart, the KeyChain key and the system CA are usable after the reboot and the first unlock.

## download: Download for offline use
> appium download
? download
Download the video (item menu > Download), wait until it is finished.
Check: download completes in the background worker with the system key, the evidence shows download requests.

## settings: Certificates screen
> appium settings CA=system
Settings > certificates of the server.
Check: no imported CA ("No imported certificate authorities." and the hint about the system store), the client
certificate "From system store: CN=client-ec", texts are understandable.

## system-cleanup: Clear the system certificate store
> appium system-cleanup
Settings > Clear credentials, remove the PIN 1234 if the installer asked for one.
Check: no user CA or key left, no screen lock (later runs need an unlocked device).
