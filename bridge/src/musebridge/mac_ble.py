"""CoreBluetooth stand-in for ``musegadget.ble_server`` so a Mac can pair.

Exposes the same ``BleServer`` interface the SDK's setup protocol
(``musegadget.ble_setup``) drives: the same service and characteristics, the
same chunked notifications. Only the Bluetooth stack differs.

Differences from BlueZ that CoreBluetooth imposes:

- Apple documents only the local name and service UUID as advertisable. The
  manufacturer-data "paired flag" the Linux SDK and ESP32 firmware send is
  passed along too, but macOS may drop it.
- The GAP device name stays the Mac's own name; the advertised name is
  ``MuseGadgetXXXXXX``.
- A peripheral can't drop a central, so ``disconnect`` is a no-op and the
  phone hangs up on its own.

``run`` drives the main run loop on the calling thread, which must be the main
thread. ``send_packets`` may be called from any thread.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Callable

import CoreBluetooth as CB
import objc
from Foundation import NSData, NSDate, NSDefaultRunLoopMode, NSObject, NSRunLoop

from musegadget.ble_framing import CHUNK_STAGGER_S, MAX_PACKET_BYTES

log = logging.getLogger(__name__)

# Same identifiers as musegadget.ble_server, which can't be imported without D-Bus.
SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"

_ASSUMED_MTU = MAX_PACKET_BYTES + 3
_TICK_S = 0.01
_NO_RESPONSE_WARNING_S = 8
_UNPAIRED_FLAG = bytes([0xFF, 0xFF, 0x00])
# "name" (default) or "both". macOS fits only 31 bytes and drops what doesn't fit:
# with the 128-bit service UUID, the name is lost. "name" leaves the UUID out.
_ADVERTISE = os.environ.get("MUSEBRIDGE_ADVERTISE", "name")

_STATE_PROBLEMS = {
    CB.CBManagerStateUnsupported: "this Mac doesn't support Bluetooth LE peripheral mode",
    CB.CBManagerStateUnauthorized: (
        "Bluetooth access was denied; allow it for your terminal app in "
        "System Settings > Privacy & Security > Bluetooth, then try again"
    ),
    CB.CBManagerStatePoweredOff: "Bluetooth is off; turn it on and try again",
}


class _Delegate(NSObject, protocols=[objc.protocolNamed("CBPeripheralManagerDelegate")]):
    def initWithServer_(self, server):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self.server = server
        return self

    def peripheralManagerDidUpdateState_(self, manager):
        self.server._state_changed(manager)

    def peripheralManager_didAddService_error_(self, manager, service, error):
        self.server._service_added(manager, error)

    def peripheralManagerDidStartAdvertising_error_(self, manager, error):
        if error is not None:
            self.server._fatal(f"advertising failed: {error.localizedDescription()}")
        else:
            log.info("advertising as %s", self.server._local_name)

    def peripheralManager_central_didSubscribeToCharacteristic_(self, manager, central, characteristic):
        self.server._subscribed(central)

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, manager, central, characteristic):
        self.server._unsubscribed(central)

    def peripheralManager_didReceiveReadRequest_(self, manager, request):
        self.server._read(manager, request)

    def peripheralManager_didReceiveWriteRequests_(self, manager, requests):
        self.server._writes(manager, requests)

    def peripheralManagerIsReadyToUpdateSubscribers_(self, manager):
        self.server._drain()


class BleServer:
    """Owns the setup service while pairing is open; implements the setup Transport."""

    def __init__(
        self,
        local_name: str,
        on_write: Callable[[bytes], None],
        on_disconnect: Callable[[], None],
    ) -> None:
        self._local_name = local_name
        self._on_write = on_write
        self._on_disconnect = on_disconnect
        self._lock = threading.Lock()
        self._mtu = _ASSUMED_MTU
        self._outbox: queue.Queue[bytes] = queue.Queue()
        self._pending: bytes | None = None  # a packet that hit a full transmit queue
        self._value = b""
        self._central = None
        self._stopped = threading.Event()
        self._manager = None
        self._delegate = None
        self._tx = None
        self._service_registered = False

    # -- Transport --------------------------------------------------------------

    def mtu(self) -> int:
        with self._lock:
            return self._mtu

    def send_packets(self, packets: list[bytes]) -> None:
        for i, packet in enumerate(packets):
            if i:
                time.sleep(CHUNK_STAGGER_S)
            self._outbox.put(packet)

    def disconnect(self, delay: float) -> None:
        log.debug("CoreBluetooth can't drop a central; waiting for the phone to disconnect")

    # -- Lifecycle --------------------------------------------------------------

    def run(self) -> None:
        """Start advertising and serve until :meth:`stop`. Blocks."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("BleServer.run must be called on the main thread")
        self._delegate = _Delegate.alloc().initWithServer_(self)
        # A nil queue delivers delegate callbacks on the main queue, which the
        # main run loop below services.
        self._manager = CB.CBPeripheralManager.alloc().initWithDelegate_queue_(self._delegate, None)
        loop = NSRunLoop.currentRunLoop()
        warn_at = time.monotonic() + _NO_RESPONSE_WARNING_S
        try:
            while not self._stopped.is_set():
                loop.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(_TICK_S))
                self._drain()
                if warn_at and time.monotonic() > warn_at:
                    warn_at = 0
                    if not self._service_registered:
                        log.warning(
                            "Bluetooth hasn't responded (authorization %d). Look for a macOS "
                            "permission prompt, or allow your terminal app in System Settings > "
                            "Privacy & Security > Bluetooth. Sandboxed shells can't reach Bluetooth.",
                            CB.CBManager.authorization(),
                        )
        finally:
            self._teardown()

    def stop(self) -> None:
        self._stopped.set()

    # -- Internals (main thread) ------------------------------------------------

    def _state_changed(self, manager) -> None:
        state = manager.state()
        if state == CB.CBManagerStatePoweredOn:
            if not self._service_registered:
                self._register(manager)
        elif state in _STATE_PROBLEMS:
            self._fatal(_STATE_PROBLEMS[state])
        else:
            log.info("Bluetooth state %d", state)

    def _register(self, manager) -> None:
        rx = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CB.CBUUID.UUIDWithString_(RX_UUID),
            CB.CBCharacteristicPropertyWrite | CB.CBCharacteristicPropertyWriteWithoutResponse,
            None,
            CB.CBAttributePermissionsWriteable,
        )
        self._tx = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CB.CBUUID.UUIDWithString_(TX_UUID),
            CB.CBCharacteristicPropertyRead | CB.CBCharacteristicPropertyNotify,
            None,
            CB.CBAttributePermissionsReadable,
        )
        service = CB.CBMutableService.alloc().initWithType_primary_(CB.CBUUID.UUIDWithString_(SERVICE_UUID), True)
        service.setCharacteristics_([rx, self._tx])
        manager.addService_(service)
        self._service_registered = True

    def _service_added(self, manager, error) -> None:
        if error is not None:
            self._fatal(f"GATT registration failed: {error.localizedDescription()}")
            return
        log.info("GATT service registered")
        advertisement = {
            CB.CBAdvertisementDataLocalNameKey: self._local_name,
            # The paired flag the apps look for: company 0xFFFF, then 0 = not yet set up.
            # Apple documents only the name and service UUIDs; macOS drops this.
            CB.CBAdvertisementDataManufacturerDataKey: NSData.dataWithBytes_length_(_UNPAIRED_FLAG, 3),
        }
        if _ADVERTISE != "name":
            advertisement[CB.CBAdvertisementDataServiceUUIDsKey] = [CB.CBUUID.UUIDWithString_(SERVICE_UUID)]
        log.info("advertising mode: %s", _ADVERTISE)
        manager.startAdvertising_(advertisement)

    def _note_central(self, central) -> None:
        with self._lock:
            self._central = central.identifier()
            mtu = int(central.maximumUpdateValueLength()) + 3
            if mtu != self._mtu:
                self._mtu = mtu
                log.info("ATT MTU %d", mtu)

    def _subscribed(self, central) -> None:
        log.info("client subscribed to %s", TX_UUID)
        self._note_central(central)

    def _unsubscribed(self, central) -> None:
        # CoreBluetooth reports a central's disconnect as an unsubscribe.
        with self._lock:
            ours = self._central in (None, central.identifier())
            if ours:
                self._central = None
                self._mtu = _ASSUMED_MTU
        log.info("client unsubscribed")
        if ours:
            self._on_disconnect()

    def _read(self, manager, request) -> None:
        offset = int(request.offset())
        if offset > len(self._value):
            manager.respondToRequest_withResult_(request, CB.CBATTErrorInvalidOffset)
            return
        chunk = self._value[offset:]
        request.setValue_(NSData.dataWithBytes_length_(chunk, len(chunk)))
        manager.respondToRequest_withResult_(request, CB.CBATTErrorSuccess)

    def _writes(self, manager, requests) -> None:
        for request in requests:
            self._note_central(request.central())
            value = request.value()
            self._on_write(bytes(value) if value is not None else b"")
        if len(requests):
            manager.respondToRequest_withResult_(requests[0], CB.CBATTErrorSuccess)

    def _drain(self) -> None:
        while True:
            if self._pending is not None:
                packet, self._pending = self._pending, None
            else:
                try:
                    packet = self._outbox.get_nowait()
                except queue.Empty:
                    return
            self._value = packet
            with self._lock:
                subscribed = self._central is not None
            if self._tx is None or not subscribed:
                continue  # like BlueZ: kept for reads, not notified
            data = NSData.dataWithBytes_length_(packet, len(packet))
            if not self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(data, self._tx, None):
                # Transmit queue full; peripheralManagerIsReadyToUpdateSubscribers_ resumes.
                self._pending = packet
                return

    def _fatal(self, message: str) -> None:
        log.error("%s", message)
        self.stop()

    def _teardown(self) -> None:
        if self._manager is not None:
            self._manager.stopAdvertising()
            self._manager.removeAllServices()
