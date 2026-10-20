#!/usr/bin/env bash
# Re-signs a com.android.webview APK with the public AOSP test key, the key of the WebView on the emulator images
# (tags test-keys). The system WebView accepts an update only when it is signed with the same key, and only
# com.android.webview is accepted as provider: Google's WebView (com.google.android.webview), Chrome and forks with
# another package name are never used there, and Google's builds can not be re-signed into com.android.webview.
# The key (platform/build/target/product/security/testkey.pk8 / .x509.pem, public and widely known) is inside this
# script, nothing is downloaded. No network needed.
#
# Usage:
#   ./webview-resign.sh INPUT.apk                      # -> webview-apks/webview-<version>.apk
#   ./webview-resign.sh -o OUT.apk INPUT.apk           # another output file (an existing one is kept as OUT.apk.previous)
#   ./webview-resign.sh -m 100 -a x86 INPUT.apk        # minimum version (default 80, the minimum of the mobile app),
#                                                      # ABI of the AVD (default x86_64)
#   ./webview-resign.sh -p org.some.webview INPUT.apk  # another package name, when the image accepts it
# Install: adb install --no-streaming -r -d OUT.apk  (or SUITES=webview ./run-matrix.sh TLS_API_23). Without
# --no-streaming a streamed install of a 260 MB APK hung on Android 7 (the file arrived, the install never started);
# --no-streaming pushes the file and takes seconds. An update can be removed again: adb shell pm uninstall
# com.android.webview (back to the system version).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
TOOLS=$(ls -d "$SDK"/build-tools/*/ 2>/dev/null | sort -V | tail -1)
[ -n "$TOOLS" ] || { echo "no build-tools in $SDK (./setup.sh installs them)"; exit 1; }
# SHA-256 of the AOSP test key certificate
TESTKEY_SHA256="a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc"

OUT=""; MIN_VERSION=80; ABI=x86_64; PACKAGE=com.android.webview
while getopts "o:m:a:p:h" opt; do
	case $opt in
		o) OUT=$OPTARG ;; m) MIN_VERSION=$OPTARG ;; a) ABI=$OPTARG ;; p) PACKAGE=$OPTARG ;;
		*) sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \?//'; exit 1 ;;
	esac
done
shift $((OPTIND - 1))
[ $# -eq 1 ] && [ -f "$1" ] || { echo "usage: $0 [-o OUT.apk] [-m MIN_VERSION] [-a ABI] [-p PACKAGE] INPUT.apk (-h for details)"; exit 1; }
INPUT=$1

badging=$("$TOOLS/aapt2" dump badging "$INPUT" 2>/dev/null) || { echo "$INPUT is not an APK"; exit 1; }
package=$(sed -n "s/^package: name='\([^']*\)'.*/\1/p" <<< "$badging")
version=$(sed -n "s/^package: .*versionName='\([^']*\)'.*/\1/p" <<< "$badging")
minsdk=$(sed -n "s/^minSdkVersion:'\([0-9]*\)'.*/\1/p" <<< "$badging")
[ "$package" = "$PACKAGE" ] || { echo "the APK is $package, the image only loads $PACKAGE (-p to change)"; exit 1; }
[ "${version%%.*}" -ge "$MIN_VERSION" ] || { echo "WebView $version is older than $MIN_VERSION (-m to change)"; exit 1; }
grep -q "native-code:.*$ABI" <<< "$badging" || { echo "the APK has no $ABI libraries (-a to change)"; exit 1; }
echo "$package $version, minSdk ${minsdk:-?}, $ABI"
OUT=${OUT:-$HERE/webview-apks/webview-$version.apk}

work=$(mktemp -d /tmp/webview-resign.XXXX)
trap 'rm -r "$work"' EXIT

# The AOSP test key: PKCS#8 (DER, base64) and its certificate
base64 -d > "$work/testkey.pk8" <<'KEY'
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDWkxkE3sYLJLHtx2Lg2dglPj7N
bOsd4v8GjKjovKjNa9N4bqcKp2zmDrsPmTVZ/9k+d6lD5+g9S2S45P6i0+ZW8eJnqBu/sjC1eMIE
Q75Mchi4RvUhFYbwOKFOicK+OH+Ovs+PysPaHuMwyeqT0KfD3ErzUCINUAgHMuCAlxfuagUzWeam
lOwss/KEoKRmyHqU2DsxCTpnNy4vZBLAbm1C8VgY3/4DgcwM1ETabN3DuCRYGUgBsyVkE0+/3pjJ
KHdI2/VnalQNgVTIu8oHueJHVTMRxGua92/e7MyOaefIotCOeCYglD+Zcn08BP5ymR2Z35uuOKCy
F3+jHVtq/ukfAgEDAoIBAQCPDLtYlIQHbcvz2kHrO+VuKX8znfIT7KoEXcXwfcXeR+JQScSxxPNE
CdIKZiORVTt++nDX7/Ao3O3QmKnB4pmPS+xFGr0qdssjpdatgn7doWXQL04WDln1exY0W9cpev+0
fzUKhy08FJd12/G34G/X6DH3isFeNVqvd0BVug/0RXWihnmONcUztAJ25E5YNqHadWSt+vU4pJOp
vxDyE6ZXrBIpHBvlaZf8atJ7maf8iXfSZUzrqnx1O5zaTGRnGo7o/UdrfuLDfpVXnXBEHm+rk6QT
q2ZKyZj6JZQ/K1LB+cXqZO9KG8oBSecXohQBeJYIDEikB9xHdsvelr1MoYR7AoGBAOrAmRccm5Un
jAe/npdFGIVXkXaep7Ur9rqT4NaoSMSnDRim6Kii2lNoZ2szvvKYuxRNmvi1u60iRvQsLM10duqy
G+FKdx+S5632ALWTKvdH97l3VYcRCrDYAyMYdotYavF8bcT9QKgYHoWHb18KLL27A4afIXmrVXCn
WXp1e2GbAoGBAOn+9xk0qK83mecSq5edXgJ1lq2NaRVmSZYc5KKtCC8YYiQ0TSuIiRSpzJ3tR28w
LtxO5lvqd72R8vBMPzS6CbY5RCj7tOBVW8bPTuwOYUN+AAN87csZvlmPsUsXMmBNQTYycvo0Keh/
ZR0RIoFmN37SyagZC1ybj90t4cUCkUDNAoGBAJyAZg9oZ7jFCAUqabouEFjlC6RpxSNypHxileRw
MIMaCLsZ8HBskYzwRPIif0xl0g2JEfsj0nNsL01yyIj4T0chZ+uG+hUMmnP5Vc5iHKTapSZPjloL
XHXlV2y6+bI68fZS89io1cVlaa5aSj9cHdPSAlm/a6ZyOPXE5lGjp5ZnAoGBAJv/T2YjGx96ZpoM
cmUTlAGjuckI8Lju27lomGxzWsoQQW14M3JbBg3GiGlI2kogHz2J7ufxpSkL90rdf3h8Bnl7gsX9
I0A459nfifK0QNepVVeonodmfuZfy4dkzEAzgM7MTKbNcUWqQ2i2FwDuz6nh28VmB5MSX+jJQS4B
tiszAoGAYyqt2RrdpGLZlaZyYlsFzalGIfTpWXPuj5ot63Ghwawb0xoN1qKJdYcbanvrblVhtKEs
YKOkae96d1grNcf4Vbm3bMrPwHdIRf6pRS+x46mMBfuap1JoGcXESY4NwdsbpYo71PuBgykeNHaO
2nq0BYcm/RyNFHuJZd+PFfOevDc=
KEY
cat > "$work/testkey.x509.pem" <<'CERT'
-----BEGIN CERTIFICATE-----
MIIEqDCCA5CgAwIBAgIJAJNurL4H8gHfMA0GCSqGSIb3DQEBBQUAMIGUMQswCQYD
VQQGEwJVUzETMBEGA1UECBMKQ2FsaWZvcm5pYTEWMBQGA1UEBxMNTW91bnRhaW4g
VmlldzEQMA4GA1UEChMHQW5kcm9pZDEQMA4GA1UECxMHQW5kcm9pZDEQMA4GA1UE
AxMHQW5kcm9pZDEiMCAGCSqGSIb3DQEJARYTYW5kcm9pZEBhbmRyb2lkLmNvbTAe
Fw0wODAyMjkwMTMzNDZaFw0zNTA3MTcwMTMzNDZaMIGUMQswCQYDVQQGEwJVUzET
MBEGA1UECBMKQ2FsaWZvcm5pYTEWMBQGA1UEBxMNTW91bnRhaW4gVmlldzEQMA4G
A1UEChMHQW5kcm9pZDEQMA4GA1UECxMHQW5kcm9pZDEQMA4GA1UEAxMHQW5kcm9p
ZDEiMCAGCSqGSIb3DQEJARYTYW5kcm9pZEBhbmRyb2lkLmNvbTCCASAwDQYJKoZI
hvcNAQEBBQADggENADCCAQgCggEBANaTGQTexgskse3HYuDZ2CU+Ps1s6x3i/waM
qOi8qM1r03hupwqnbOYOuw+ZNVn/2T53qUPn6D1LZLjk/qLT5lbx4meoG7+yMLV4
wgRDvkxyGLhG9SEVhvA4oU6Jwr44f46+z4/Kw9oe4zDJ6pPQp8PcSvNQIg1QCAcy
4ICXF+5qBTNZ5qaU7Cyz8oSgpGbIepTYOzEJOmc3Li9kEsBubULxWBjf/gOBzAzU
RNps3cO4JFgZSAGzJWQTT7/emMkod0jb9WdqVA2BVMi7yge54kdVMxHEa5r3b97s
zI5p58ii0I54JiCUP5lyfTwE/nKZHZnfm644oLIXf6MdW2r+6R8CAQOjgfwwgfkw
HQYDVR0OBBYEFEhZAFY9JyxGrhGGBaR0GawJyowRMIHJBgNVHSMEgcEwgb6AFEhZ
AFY9JyxGrhGGBaR0GawJyowRoYGapIGXMIGUMQswCQYDVQQGEwJVUzETMBEGA1UE
CBMKQ2FsaWZvcm5pYTEWMBQGA1UEBxMNTW91bnRhaW4gVmlldzEQMA4GA1UEChMH
QW5kcm9pZDEQMA4GA1UECxMHQW5kcm9pZDEQMA4GA1UEAxMHQW5kcm9pZDEiMCAG
CSqGSIb3DQEJARYTYW5kcm9pZEBhbmRyb2lkLmNvbYIJAJNurL4H8gHfMAwGA1Ud
EwQFMAMBAf8wDQYJKoZIhvcNAQEFBQADggEBAHqvlozrUMRBBVEY0NqrrwFbinZa
J6cVosK0TyIUFf/azgMJWr+kLfcHCHJsIGnlw27drgQAvilFLAhLwn62oX6snb4Y
LCBOsVMR9FXYJLZW2+TcIkCRLXWG/oiVHQGo/rWuWkJgU134NDEFJCJGjDbiLCpe
+ZTWHdcwauTJ9pUbo8EvHRkU3cYfGmLaLfgn9gP+pWA7LFQNvXwBnDa6sppCccEX
31I828XzgXpJ4O+mDL1/dBd+ek8ZPUP0IgdyZm5MTYPhvVqGCHzzTy3sIeJFymwr
sBbmg2OAUNLEMO6nwmocSdN2ClirfxqCzJOLSDE4QyS9BAH6EhY6UFcOaE0=
-----END CERTIFICATE-----
CERT
fingerprint=$(openssl x509 -in "$work/testkey.x509.pem" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')
[ "$fingerprint" = "$TESTKEY_SHA256" ] || { echo "unexpected test key certificate ($fingerprint)"; exit 1; }

# Re-sign: aligned first, then v1 (JAR, needed by Android 6) + v2/v3 signatures
"$TOOLS/zipalign" -f -p 4 "$INPUT" "$work/aligned.apk"
"$TOOLS/apksigner" sign --key "$work/testkey.pk8" --cert "$work/testkey.x509.pem" --out "$work/signed.apk" "$work/aligned.apk"
verify=$("$TOOLS/apksigner" verify -v --print-certs --min-sdk-version "${minsdk:-23}" "$work/signed.apk")
# Android 6 (API 23) only reads v1 signatures: an APK that installs there must have one
if [ "${minsdk:-23}" -lt 24 ]; then
	grep -q "v1 scheme (JAR signing): true" <<< "$verify" || { echo "no v1 signature, Android 6 would reject it"; exit 1; }
fi
grep -qi "SHA-256 digest: $TESTKEY_SHA256" <<< "$verify" || { echo "not signed with the test key"; exit 1; }

mkdir -p "$(dirname "$OUT")"
if [ -f "$OUT" ]; then
	mv "$OUT" "$OUT.previous"
	echo "previous APK kept as $(basename "$OUT").previous"
fi
cp "$work/signed.apk" "$OUT"
echo "$OUT: signed with the AOSP test key"
