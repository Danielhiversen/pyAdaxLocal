"""Local support for Adax wifi-enabled home heaters."""
import asyncio
import logging
import operator
import secrets
import time
import urllib

import async_timeout


ADAX_DEVICE_TYPE_HEATER_BLE = 5
BLE_COMMAND_STATUS_OK = 0
BLE_COMMAND_STATUS_INVALID_WIFI = 1
MAX_BYTES_IN_COMMAND_CHUNK = 17
UUID_ADAX_BLE_SERVICE = "3885cc10-7c18-4ad4-a48d-bf11abf7cb92"
UUID_ADAX_BLE_SERVICE_CHARACTERISTIC_COMMAND = "0000cc11-0000-1000-8000-00805f9b34fb"

_LOGGER = logging.getLogger(__name__)

try:
    import bleak
except FileNotFoundError:
    _LOGGER.error("Import bleak failed", exc_info=True)
    bleak = None


class Adax:
    """Adax data handler."""

    def __init__(self, device_ip, access_token, websession, timeout=15):
        """Init adax data handler."""
        self.device_ip = device_ip
        self._access_token = access_token
        self.websession = websession
        self._url = "https://" + device_ip + "/api"
        self._headers = {"Authorization": "Basic " + self._access_token}
        self._timeout = timeout

    async def set_target_temperature(self, target_temperature):
        """Set target temperature."""
        payload = {
            "command": "set_target",
            "time": int(time.time()),
            "value": int(target_temperature * 100),
        }
        async with async_timeout.timeout(self._timeout):
            async with self.websession.get(
                self._url, params=payload, headers=self._headers
            ) as response:
                _LOGGER.debug("Heater response %s", response.status)
                if response.status != 200:
                    _LOGGER.error(
                        "Failed to set target temperature %s %s",
                        response.status,
                        response.reason,
                    )
                return response.status

    async def get_status(self):
        """Get heater status."""
        payload = {"command": "stat", "time": int(time.time())}
        try:
            async with async_timeout.timeout(self._timeout):
                async with self.websession.get(
                    self._url, params=payload, headers=self._headers
                ) as response:
                    if response.status != 200:
                        _LOGGER.error(
                            "Failed to get status %s %s",
                            response.status,
                            response.reason,
                        )
                        return None, None
                    response_json = await response.json()
        except asyncio.TimeoutError:
            return None, None

        _LOGGER.debug("Heater response %s %s", response.status, response_json)
        data = {}
        data["target_temperature"] = response_json["targTemp"] / 100
        data["current_temperature"] = response_json["currTemp"] / 100
        return data


class AdaxConfig:
    """Adax config handler."""

    def __init__(self, wifi_ssid, wifi_psk):
        self.wifi_ssid = wifi_ssid
        self.wifi_psk = wifi_psk
        self._access_token = secrets.token_hex(10)
        self._device_ip = None
        self._mac_id = None

    @property
    def device_ip(self):
        """Return device ip."""
        return self._device_ip

    @property
    def mac_id(self):
        """Return mac id."""
        return self._mac_id

    @property
    def access_token(self):
        """Return access token."""
        return self._access_token

    def notification_handler(self, _, data):
        if not data:
            _LOGGER.warning("No data")
            return
        byte_list = list(data)
        status = byte_list[0]
        _LOGGER.debug("notification_handler %s", byte_list)
        if status == BLE_COMMAND_STATUS_INVALID_WIFI:
            _LOGGER.debug("Invalid WiFi credentials %s")
            raise InvalidWifiCred

        if status == BLE_COMMAND_STATUS_OK and byte_list and len(byte_list) >= 5:
            self._device_ip = "%d.%d.%d.%d" % (
                byte_list[1],
                byte_list[2],
                byte_list[3],
                byte_list[4],
            )
            _LOGGER.debug("Heater Registered, use with IP %s", self._device_ip)

        _LOGGER.debug("Status %s", byte_list)

    async def configure_device(self):
        if bleak is None:
            _LOGGER.error("Bleak library not loaded")
            return

        _LOGGER.debug(
            "Press and hold OK button on the heater until the blue led starts blinking"
        )
        device, self._mac_id = await scan_for_available_ble_device()
        _LOGGER.debug("device: %s", device)
        if not device:
            return False
        async with bleak.BleakClient(device) as client:

            _LOGGER.debug("start_notify")
            await client.start_notify(
                UUID_ADAX_BLE_SERVICE_CHARACTERISTIC_COMMAND,
                self.notification_handler,
            )
            ssid_encoded = urllib.parse.quote(self.wifi_ssid)
            psk_encoded = urllib.parse.quote(self.wifi_psk)
            access_token_encoded = urllib.parse.quote(self._access_token)
            byte_list = list(
                bytearray(
                    "command=join&ssid="
                    + ssid_encoded
                    + "&psk="
                    + psk_encoded
                    + "&token="
                    + access_token_encoded,
                    "ascii",
                )
            )
            _LOGGER.debug("write_command")
            await write_command(byte_list, client)
            k = 0
            while k < 20 and client.is_connected and self._device_ip is None:
                await asyncio.sleep(1)
                k += 1
            if self._device_ip:
                _LOGGER.debug(
                    "Heater ip is %s and the token is %s",
                    self._device_ip,
                    self._access_token,
                )
                return True
            return False


async def scan_for_available_ble_device(retry=1):
    if bleak is None:
        _LOGGER.error("Bleak library not loaded")
        return
    discovered = await bleak.discover(timeout=60)
    _LOGGER.debug(discovered)
    if not discovered:
        if retry > 0:
            return await scan_for_available_ble_device(retry - 1)
        raise HeaterNotFound

    for discovered_item in discovered:
        metadata = discovered_item.metadata
        uuids = metadata.get("uuids")
        if uuids is None or UUID_ADAX_BLE_SERVICE not in uuids:
            continue
        _LOGGER.info("Found Adax heater %s", discovered_item)
        manufacturer_data = metadata.get("manufacturer_data")
        _LOGGER.debug("manufacturer_data %s", manufacturer_data)
        if not manufacturer_data:
            continue
        first_bytes = next(iter(manufacturer_data))
        _LOGGER.debug("first bytes %s", first_bytes)
        if first_bytes is None:
            continue
        other_bytes = manufacturer_data[first_bytes]
        _LOGGER.debug(other_bytes)
        manufacturer_data_list = [
            first_bytes % 256,
            operator.floordiv(first_bytes, 256),
        ] + list(other_bytes)
        _LOGGER.debug(manufacturer_data_list)
        if not device_available(manufacturer_data_list):
            _LOGGER.warning("Heater not available.")
            raise HeaterNotAvailable
        return discovered_item.address, find_mac_id(manufacturer_data_list)
    if retry > 0:
        return await scan_for_available_ble_device(retry - 1)
    raise HeaterNotFound


def device_available(manufacturer_data):
    _LOGGER.debug("device_available")
    if not manufacturer_data and len(manufacturer_data) < 10:
        return False

    type_id = manufacturer_data[0]
    status_byte = manufacturer_data[1]
    mac_id = find_mac_id(manufacturer_data)
    registered = status_byte & (0x1 << 0)
    managed = status_byte & (0x1 << 1)
    _LOGGER.debug("device_available %s %s %s %s", mac_id, type_id, registered, managed)
    return (
        mac_id
        and type_id == ADAX_DEVICE_TYPE_HEATER_BLE
        and not registered
        and not managed
    )


def find_mac_id(manufacturer_data):
    mac_id = 0
    for byte in manufacturer_data[2:10]:
        mac_id = mac_id * 256 + byte
    return mac_id


async def write_command(command_byte_list, client):
    byte_count = len(command_byte_list)
    chunk_count = operator.floordiv(byte_count, MAX_BYTES_IN_COMMAND_CHUNK)
    if chunk_count * MAX_BYTES_IN_COMMAND_CHUNK < byte_count:
        chunk_count += 1
    sent_byte_count = 0
    chunk_nr = 0
    while chunk_nr < chunk_count:
        is_last = chunk_nr == (chunk_count - 1)
        chunk_data_length = (
            byte_count - sent_byte_count if is_last else MAX_BYTES_IN_COMMAND_CHUNK
        )
        chunk = [chunk_nr, 1 if is_last else 0] + command_byte_list[
            sent_byte_count : (sent_byte_count + chunk_data_length)
        ]
        await client.write_gatt_char(
            UUID_ADAX_BLE_SERVICE_CHARACTERISTIC_COMMAND, bytearray(chunk)
        )
        sent_byte_count += chunk_data_length
        chunk_nr += 1


class InvalidWifiCred(Exception):
    """Invalid wifi credentials exception."""


class HeaterNotAvailable(Exception):
    """Heater not available exception."""


class HeaterNotFound(Exception):
    """Heater not found exception."""
