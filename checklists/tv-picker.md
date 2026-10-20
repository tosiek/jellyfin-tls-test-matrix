# Checklist of manual-test.py for jellyfin-androidtv on a TV AVD with a file manager as file picker
# (./manual-test.py --checklist tv-picker TLS_TV_30 TLS_TV_34). The TV images have no file picker (only a stub that
# finishes without a result), TV devices with a file manager have one: Material Files (tv-picker/, F-Droid) handles
# OPEN_DOCUMENT and GET_CONTENT. The app picks the files there, its D-pad screens are used as in tv.md, Material Files
# by touch. Format: see tv.md. uninstall-picker at the end leaves the AVD as tv.md expects it.

## setup: Add the server with certificates from the file manager
> install-picker
> appium setup SOURCE=file
? api
Enter server address: https://10.0.2.2:8920, open Advanced, "Load certificates from URL (network)" is not checked (a
file picker exists). CA: Choose…, Material Files opens (no app chooser with the TV stub),
Download/jellyfin-tls/proxy/ca.pem. Client certificate: Choose…, client-ec.p12, password: jellyfin. The app offers to
trust Jellyfin Test Client CA from the file: Don't trust (the proxy has its own CA). Connect.
Check: the picked files come back to the connect screen (it is not closed while the file manager is open), the user
selection / login appears.

## login: Log in
> appium login
? api
Add account > Use a password, log in as jellyfin (password 1234).
Check: home screen, no certificate error.

## browse: Home screen and library
> appium browse
? images
Scroll the home rows, open a library and an item detail page.
Check: posters, backdrops and the user avatar load, the evidence shows image requests.

## restart: App restart keeps the certificates
> force-stop
> launch
> appium home
? api
Check: the app connects without importing again, home screen loads with images.

## import-file: Import from the file manager on the certificates screen
> appium import-file
? api
Certificates screen: remove the CA, Import CA certificate… (opens Material Files, not the URL dialog), ca.pem, Trust.
Import client certificate…, client-ec.p12, password jellyfin, Use. The app offers to trust Jellyfin Test Client CA from
the file: Don't trust (the proxy has its own CA). Restart the app, sign in.
Check: both buttons open the file manager, the CA and the client certificate are listed, signing in works.

## uninstall-picker: Remove the file manager
> uninstall-picker
> appium settings
Remove Material Files, open the certificates screen again.
Check: the certificates are still listed, the app works without the file manager.
