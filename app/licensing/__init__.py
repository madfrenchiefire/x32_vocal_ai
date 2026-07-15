"""Offline license-key system.

Ed25519-signed license tokens, verified locally against an embedded public
key -- no license server, works with no internet (the venue PC may have
none). The vendor holds the private key and mints tokens with
``python -m app.tools.license_gen`` (never shipped in the build); the app
carries only the public half.

Security is proportionate, not absolute: a determined person can patch any
local application, so this deters casual sharing ("my buddy gave me his
key") rather than defeating a skilled cracker. See app.licensing.keys for
the token format and app.licensing.manager for how startup evaluates it.
"""
