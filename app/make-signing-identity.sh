#!/bin/bash
# 创建一张本地自签名代码签名证书，让辅助功能授权在重新编译后依然有效。
# 只用一次；之后的 ./build.sh 会自动用它签名。
set -euo pipefail
DIR="$HOME/.config/alma/voice-type-signing"
NAME="VoiceType Local Signing"
mkdir -p "$DIR" && cd "$DIR"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$NAME"; then
  echo "签名身份已存在：$NAME"; exit 0
fi
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes \
  -subj "/CN=$NAME/O=Goodfun Studio" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" >/dev/null 2>&1
# macOS 读不了 OpenSSL 3 默认的 PKCS12 加密，必须用老算法
openssl pkcs12 -export -out identity.p12 -inkey key.pem -in cert.pem -passout pass:vtlocal \
  -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg SHA1 >/dev/null 2>&1
security import identity.p12 -k "$HOME/Library/Keychains/login.keychain-db" -P vtlocal -A -T /usr/bin/codesign
security add-trusted-cert -r trustRoot -p codeSign -k "$HOME/Library/Keychains/login.keychain-db" cert.pem
echo "已创建签名身份：$NAME"
