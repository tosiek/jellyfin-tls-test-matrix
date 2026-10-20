# Checklist of manual-test.py for jellyfin-androidtv on a phone AVD
# (./manual-test.py --app tv --checklist tv-phone TLS_API_36). A phone has what the TV images lack: a document picker
# and the system credential store with the KeyChain chooser, the paths of the TV app that never run on the TV AVDs.
# The app is still used with the D-pad, the system screens (file picker, Settings, chooser) by touch.
# Format: see tv.md. system-client sets PIN 1234 when the installer asks for a screen lock, system-cleanup removes it.

## setup: Add the server with certificates from the file picker
> appium setup SOURCE=file
? api
Enter server address: https://10.0.2.2:8920, open Advanced, "Load certificates from URL (network)" is not checked (the
phone has a file picker). CA: Choose…, pick Download/jellyfin-tls/proxy/ca.pem. Client certificate: Choose…,
client-ec.p12, password: jellyfin. The app offers to trust Jellyfin Test Client CA from the file: Don't trust (the proxy
has its own CA). Connect.
Check: Choose… opens the system file picker, the rows show the picked files, the user selection / login appears.

## login: Log in
> appium login
? api
Add account > Use a password, log in as jellyfin (password 1234).
Check: home screen, no certificate error.

## browse: Home screen and library
> appium browse
? images
Scroll the home rows, open a library and an item detail page.
Check: posters, backdrops and the user avatar load (no empty tiles), the evidence shows image requests.

## play: Play the video
> appium play
Play the first video of the library for at least 10 seconds.
Check: starts within a few seconds, no error, the position advances.

## restart: App restart keeps the certificates
> appium stop-player
> force-stop
> launch
> appium home
? api
Check: the app connects without importing again, home screen loads with images.

## settings: Certificates screen
> appium settings
Settings > Login > the server > Certificates: the CA and the client certificate are listed with details.
Check: "Choose from system store…" is offered (the phone has the KeyChain chooser), every button reachable with the
D-pad.

## import-file: Import from the file picker on the certificates screen
> appium import-file
? api
Remove the CA, Import CA certificate… (opens the file picker, not the URL dialog), ca.pem, Trust. Import client
certificate…, client-ec.p12, password jellyfin, Use. The app offers to trust Jellyfin Test Client CA from the file:
Don't trust (the proxy has its own CA). Restart the app, sign in.
Check: both buttons open the system file picker, the CA and the client certificate are listed, signing in works.

## system-client: Client certificate from the system store
> appium system-client
? api
Clear credentials, install client-ec.p12 in Settings (VPN & app user certificate, password jellyfin). Certificates
screen of the server: Choose from system store…, client-ec in the chooser. Restart the app, sign in.
Check: the client line says "From system store", signing in works with the system key.

## system-setup: Add the server with the client certificate from the system store
> clear-data
> launch
> appium setup SOURCE=file CLIENT=system
> appium login
? api
App data cleared. Enter server address, the address, Advanced: CA ca.pem from the file picker, client certificate with
Choose from system store… (below the client row), client-ec in the chooser. Connect, log in.
Check: the client row says "client-ec (new)", the user selection appears, signing in works with the system key.

## system-cleanup: Clear the system certificate store
> appium system-cleanup
Clear credentials, remove the screen lock PIN.
Check: nothing of the test is left in the system store.
