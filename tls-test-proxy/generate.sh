#!/usr/bin/env bash
# Test PKI for the Jellyfin mTLS proxy (Caddy).
# Server certificate is valid for localhost, 127.0.0.1 and 10.0.2.2 (host address seen from the Android emulator).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs public
cd certs

PASS="jellyfin"
DAYS=825

# Server CA and server certificate
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key -out ca.pem -days 3650 \
	-subj "/CN=Jellyfin Test Proxy CA" -addext "basicConstraints=critical,CA:true" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout server.key -out server.csr -subj "/CN=jellyfin-test-proxy" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem -days $DAYS \
	-extfile <(printf "basicConstraints=CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1,IP:10.0.2.2") 2>/dev/null

# Client CA and client certificates
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout client-ca.key -out client-ca.pem -days 3650 \
	-subj "/CN=Jellyfin Test Client CA" -addext "basicConstraints=critical,CA:true" 2>/dev/null

client() { # name, keygen args...
	local name=$1; shift
	openssl genpkey "$@" -out "$name.key" 2>/dev/null
	openssl req -new -key "$name.key" -out "$name.csr" -subj "/CN=$name" 2>/dev/null
	openssl x509 -req -in "$name.csr" -CA client-ca.pem -CAkey client-ca.key -CAcreateserial -out "$name.pem" -days $DAYS \
		-extfile <(printf "basicConstraints=CA:false\nextendedKeyUsage=clientAuth") 2>/dev/null
	# 3DES/SHA-1 is readable on every Android version, AES is the OpenSSL 3 default
	openssl pkcs12 -export -inkey "$name.key" -in "$name.pem" -certfile client-ca.pem -out "../public/$name.p12" \
		-passout "pass:$PASS" -name "$name" -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1
	openssl pkcs12 -export -inkey "$name.key" -in "$name.pem" -certfile client-ca.pem -out "../public/$name-aes.p12" \
		-passout "pass:$PASS" -name "$name"
}
client client-ec -algorithm EC -pkeyopt ec_paramgen_curve:P-256
client client-rsa -algorithm RSA -pkeyopt rsa_keygen_bits:2048

cp ca.pem ../public/ca.pem
# Private key and certificate in PEM: the apps reject it as client certificate (PKCS#12 only), the TV checklist
# imports it from the URL (no file picker on the TV images)
cat client-ec.key client-ec.pem > ../public/pem-key-and-cert.pem
echo "Generated. Password of the .p12 files: $PASS"
