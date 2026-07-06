"""X32 OSC address constants.

CONFIRMED via a real console scene (``.scn``) file dump, uploaded and
inspected 2026-07-06 -- scene files are literal OSC address/value dumps of
console state, so this is empirical confirmation, not a guess. See
CLAUDE.md's "Open items to verify" for the history: an earlier version of
this module guessed per-channel addresses (``/config/userrout/in/NN``,
``/config/routing/IN/1-8``-style blocks) based on CLAUDE.md's own
(incorrect) assumption. The real scene dump shows a different, simpler
shape:

    /config/userrout/out 0 0 0 ... (48 values)
    /config/userrout/in  0 0 0 ... (32 values)
    /config/routing REC
    /config/routing/IN     AN1-8 AN9-16 AN17-24 AN25-32 AUX1-4
    /config/routing/AES50A OUT1-8 OUT9-16 OUT1-8 OUT9-16 P161-8 P169-16
    /config/routing/AES50B OUT1-8 OUT9-16 OUT1-8 OUT9-16 P161-8 P169-16
    /config/routing/CARD   AN1-8 AN9-16 AN17-24 AN25-32
    /config/routing/OUT    OUT1-4 OUT5-8 OUT9-12 OUT13-16
    /config/routing/PLAY   CARD1-8 CARD9-16 CARD17-24 CARD25-32 AUX1-4

i.e. each of these is a *single* OSC address whose reply carries an array
of values (per-channel-index for userrout, per-8-channel-block source
tokens for the routing nodes) -- there is no addressable
``/config/userrout/in/01`` sub-node. A per-channel bypass/restore (per
CLAUDE.md's routing-automation section) therefore means: read the full
array, mutate the one index for the target channel, and write the whole
array back as a single message -- still "a single message," just not a
single-value one.

The *meaning* of individual values (which integer maps to which physical
source for userrout, what "AN1-8"/"P161-8" mean precisely for the routing
nodes) is still not decoded here -- per CLAUDE.md's rule to always store
raw data, not an interpreted summary. Only the address shapes are
confirmed.
"""
from __future__ import annotations

XINFO = "/xinfo"
XREMOTE = "/xremote"

NUM_USERROUT_IN = 32
NUM_USERROUT_OUT = 48

USERROUT_IN = "/config/userrout/in"
USERROUT_OUT = "/config/userrout/out"

# CONFIRMED -- see module docstring. Queried/replied as a single address
# each; args are raw block-source tokens, not decoded.
ROUTING_REC = "/config/routing"
ROUTING_IN = "/config/routing/IN"
ROUTING_AES50A = "/config/routing/AES50A"
ROUTING_AES50B = "/config/routing/AES50B"
ROUTING_CARD = "/config/routing/CARD"
ROUTING_OUT = "/config/routing/OUT"
ROUTING_PLAY = "/config/routing/PLAY"

ROUTING_ADDRESSES_VERIFIED = True

ROUTING_BLOCK_ADDRESSES = {
    "rec": ROUTING_REC,
    "in": ROUTING_IN,
    "aes50a": ROUTING_AES50A,
    "aes50b": ROUTING_AES50B,
    "card": ROUTING_CARD,
    "out": ROUTING_OUT,
    "play": ROUTING_PLAY,
}
