# Architecture: running the Muse Gadgets SDK on devices it doesn't support

Muse has no public agent API. The only supported way for outside hardware to talk to
your Muse is Meta's [Muse Gadgets SDK](https://github.com/facebookincubator/muse-gadget-sdk),
and it supports exactly two kinds of device: Linux boxes (BlueZ, systemd) and ESP32
boards. Neither an Apple Watch nor a Mac is one of them:

- **The watch** can't run the SDK at all. There's no Python, no Bluetooth LE peripheral
  role for third-party apps, and restricted networking.
- **The Mac** can run the SDK's Python, but its Bluetooth stack and filesystem layout
  aren't the ones the SDK expects.

So the work is split in two:

```
┌────────────┐  HTTP, streamed   ┌──────────────────────────────┐  Noise XX   ┌──────────┐
│ Apple Watch │ ───────────────▶ │ Mac: musebridge               │ ──────────▶ │ your Muse│
│ thin client │ ◀─────────────── │  = Gadgets SDK                │ ◀────────── │ (VM)     │
└────────────┘  PCM16 24 kHz     │  + macOS shims                │  voice call └──────────┘
                                 └──────────────────────────────┘
                                        ▲  BLE, once, for pairing
                                        │
                                  Muse app (iPhone)
```

The **Mac is the gadget**. As far as Muse knows, a community gadget is paired to your
account. The **watch is a remote microphone and speaker** for that gadget.

## 1. What the SDK is made of, and what we did with each part

| SDK module | Job | Here |
|---|---|---|
| `identity` | Device node ID and keys | **Reused** |
| `ble_framing` | Chunked BLE packets (`0xFE`, index, total) | **Reused** |
| `ble_setup`, `pairing` | Encrypted setup protocol (P-256 ECDH, HKDF, AES-GCM), provisioning state machine | **Reused** |
| `pair` | Drives a pairing session | **Reused** |
| `ble_server` | GATT peripheral over **BlueZ/D-Bus** | **Replaced** by `mac_ble.py` (CoreBluetooth) |
| `config` | Paths under `/var/lib`, `/run`, `/etc` | **Redirected** with environment variables |
| `muse_api` | Finds your Muse VM, refreshes device tokens | **Reused** |
| `link_client` | Noise XX session to Muse's link endpoint | **Reused**, without registering the device (below) |
| `voice_call` | Realtime voice call | **Reused**, through a small subclass |
| `service` (`musegadget run`) | Daemon that registers device commands and runs them | **Not used** |

Only the Bluetooth server and the file paths had to change. Everything security-sensitive
(the setup protocol, key exchange, Noise session, token handling) is the SDK's own
code, pinned to a specific commit in `bridge/pyproject.toml`.

## 2. Making the SDK run on macOS

### Filesystem: environment variables, not patches

The SDK reads its locations from the environment before falling back to Linux paths:
`MUSEGADGET_STATE_DIR` (default `/var/lib/musegadget`), `MUSEGADGET_SOCKET` and
`MUSEGADGET_COMMANDS_DIR`. `macos.configure()` points them at
`~/Library/Application Support/MuseBridge` (mode 0700) before anything else from
`musegadget` is imported. Profiles for extra people are just different state
directories (`profiles/<name>`), so each person gets their own identity, pairing and
secret with no other code changes.

### Bluetooth: swapping one module

`musegadget.pair` imports its BLE server lazily, inside the function that needs it,
because D-Bus is only needed on a device that pairs:

```python
# musegadget/pair.py
from musegadget.ble_server import BleServer
```

That lazy import is the seam. Before handing off to the SDK's own `pair` command,
`macos.configure()` registers our module under the SDK's name:

```python
sys.modules["musegadget.ble_server"] = mac_ble
```

When the SDK reaches that import, Python returns the cached module, so the SDK's
pairing code drives our CoreBluetooth peripheral without knowing the difference.
Nothing in the SDK is forked or monkey-patched function by function. `mac_ble.BleServer`
only has to honor the same small contract:

| Contract | CoreBluetooth implementation |
|---|---|
| `BleServer(local_name, on_write, on_disconnect)` | Creates a `CBPeripheralManager`. A PyObjC delegate forwards its callbacks. |
| Same service and characteristics | Service `7fdd3d1c-…`, RX `4d593029-…` (write / write-without-response), TX `d75dc4ca-…` (read / notify) |
| `on_write(bytes)` | Called from `peripheralManager:didReceiveWriteRequests:` |
| `send_packets(list)` | Queues notifications in an outbox, spaced like BlueZ (`CHUNK_STAGGER_S`) |
| Flow control | When `updateValue:` returns NO (transmit queue full), the packet is kept and resent from `peripheralManagerIsReadyToUpdateSubscribers:` |
| `mtu()` | `central.maximumUpdateValueLength + 3` |
| `on_disconnect()` | CoreBluetooth has no disconnect callback for peripherals, so an unsubscribe from TX counts as one |
| `disconnect()` | A no-op: a peripheral can't drop a central, so the phone hangs up on its own |
| `run()` / `stop()` | Pumps the main `NSRunLoop` in 10 ms ticks, which is where CoreBluetooth delivers callbacks, and drains the outbox |

### Advertising: the part that didn't port directly

The ESP32 firmware and the Linux SDK advertise:

- **Primary advertisement:** flags, the 128-bit service UUID, and manufacturer data
  `FF FF 00` (a "paired" flag: company `0xFFFF`, `0` = not set up yet)
- **Scan response:** the name `MuseGadgetXXXXXX`

CoreBluetooth lets an app advertise only a local name and service UUIDs. It silently
drops manufacturer data, and it gets just 31 bytes. Flags (3) + a 128-bit UUID (18)
leave 10 bytes, too few for the 18-byte name, so macOS sent the UUID and **dropped the
name**. A scan with nRF Connect showed exactly that, and the Muse app listed "No
devices found".

The Muse app turns out to find gadgets **by name**. So on macOS the bridge advertises
**only the name**. The app connects, finds the service through ordinary GATT discovery,
and the SDK's setup protocol runs unchanged. (`MUSEBRIDGE_ADVERTISE=both` restores the
UUID, for experimenting.) The missing paired flag hasn't mattered.

### Permissions

macOS only lets an app that the user has allowed under **Privacy & Security >
Bluetooth** use Bluetooth. Sandboxed shells, such as the ones editors and AI coding
tools run commands in, never get the prompt: their manager stays in "not determined"
and nothing happens. `mac_ble` logs a warning after 8 seconds of silence. Pairing has
to run in Terminal.app.

### After pairing, Bluetooth is done

Pairing provisions a device access token and refresh token over BLE. From then on
everything is HTTPS and Noise over the internet, so `musebridge serve` never touches
Bluetooth.

## 3. Talking to Muse without `musegadget run`

The SDK's daemon (`musegadget run`) registers the device with a list of **commands**
that Muse can call, including running shell commands on the device. That's the point
of a gadget, but it isn't something a watch bridge should hand out. So the bridge
assembles the runtime pieces itself:

1. **Credentials** (`credentials.py`): load the pairing, refresh the device token when
   it gets old, and on HTTP 401 force a refresh and retry once (as the SDK's service
   does), then pick your Muse VM with `muse_api`.
2. **Link** (`voice.py`): open the SDK's `LinkSession` (Noise XX) with
   `register_device=False`, describing the device with **no commands**. Muse can't
   run anything on the Mac.
3. **Voice call:** the SDK's `VoiceCall` runs the realtime protocol: PCM16LE mono
   24 kHz in 40 ms frames, provider-side voice activity detection, half duplex. The
   call introduces itself as device type `mcu-wearable`. The bridge feeds it a
   `Microphone` (frames from the watch, and silence otherwise, which the remote VAD
   needs to detect the end of speech) and a `Speaker` (reply audio, routed to whichever
   watch request is current).

### Turn logic on top of a continuous call

A voice call is continuous, but a watch turn is one press. `MuseVoice.turn()` turns
one into the other:

- **Opening:** the first press opens the call (about 1–2 s), and later presses reuse
  it. After 5 idle minutes the call is hung up.
- **Interrupting:** each turn starts with `barge_in()`, which cuts off anything Muse
  was still saying.
- **When a reply is over:**
  - Muse often says a short lead-in ("let me check…"), finishes that response, runs a
    tool, and then answers in a *separate, delayed* response.
  - So the bridge doesn't end the reply when the first response settles. It ends it
    only when no response is in progress (`busy` is false), playback is idle, and 10 s
    have passed in silence.
- **Newest press wins:**
  - The watch's connection goes through the iPhone, so a hang-up can take a minute to
    reach the Mac.
  - A new turn therefore cancels whatever turn is in flight instead of queuing behind it.
- **Silent turns:**
  - The SDK drops microphone audio while Muse "holds" input, or while its own audio
    is still playing (half duplex).
  - The bridge logs both. If Muse doesn't answer within 60 s, it hangs up that call,
    and the next press starts a fresh one.

## 4. The watch side

watchOS restricts WebSockets and low-level networking for ordinary apps, so each turn
is **one plain HTTP request** with streaming in both directions:

- **Upload:** `URLSession.uploadTask(withStreamedRequest:)` reads from a bound stream
  pair. A writer thread feeds it microphone audio while you talk and closes it when
  you release.
- **Response:** the bridge streams Muse's reply back as it arrives
  (`audio/L16; rate=24000`).
- **Audio:**
  - `AVAudioEngine` taps the microphone in whatever format the hardware chose and
    converts it to 24 kHz Int16 with `AVAudioConverter`.
  - Playback goes through an `AVAudioPlayerNode` with a 250 ms pre-buffer to ride out
    network jitter.
  - One pitfall: touch `engine.inputNode` *before* starting the engine. Adding the
    input to a running engine aborts on watchOS.
- **Auth:** each bridge has a random 256-bit secret. The watch sends it as a bearer
  token, and the bridge compares it in constant time. The bridge address and secret
  are compiled into the app from `Secrets.xcconfig`, and can be changed in the app.
- **Reachability:** the watch's traffic goes through the paired iPhone, including the
  iPhone's Tailscale VPN. So a tailnet address works away from home, with the bridge
  never exposed to the internet.

## 5. Adapting this to other devices

The same seams work for any device the SDK doesn't support:

- **Another OS:**
  - Point the `MUSEGADGET_*` environment variables somewhere writable.
  - Put a module with the `BleServer` contract into
    `sys.modules["musegadget.ble_server"]` before pairing.
  - Advertise the gadget's name.
- **A device that can't run Python** (a watch, a car, a smart speaker): make it a thin
  client of a bridge like this one. The bridge owns the pairing and the voice call,
  and the device only moves audio.
- **No Bluetooth peripheral at all:** in principle you could pair once on a machine
  where pairing works and copy the state directory over, since nothing after pairing
  needs Bluetooth. This hasn't been tried.
