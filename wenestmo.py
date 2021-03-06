import configparser
import datetime
import json
import time
import traceback
from collections import Counter, deque
from random import random
import requests

import httplib2
from googleapiclient.discovery import build
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import run_flow

import pywemo

STORAGE = Storage("credentials.storage")

config = configparser.ConfigParser()
config.read("config.ini")

# Console output will show the temperature in F or C.
FAHRENHEIT = config.getboolean("DEFAULT", "Fahrenheit")
POLLING_PERIOD_S = config.getint("DEFAULT", "PollingPeriodS")
aux_heat_thresh = config.getint("DEFAULT", "AuxHeatThreshold")
AUX_HEAT_THRESHOLD_C = aux_heat_thresh * 5 / 9.0 if FAHRENHEIT else aux_heat_thresh
HUMIDITY_PERCENT_TARGET = config.getint("DEFAULT", "HumidityPercentTarget")
HUMIDITY_PERCENT_THRESHOLD = config.getint("DEFAULT", "HumidityPercentThreshold")

GOOGLE_ENTERPRISE = config["google"]["Enterprise"]
GOOGLE_CLIENT_SECRET = config["google"]["ClientSecretFile"]
GOOGLE_SCOPE = "https://www.googleapis.com/auth/sdm.service"

WEMO_HEATING_DEVICE_NAMES = set(json.loads(config.get("wemo", "HeatingDeviceNames")))
WEMO_COOLING_DEVICE_NAMES = set(json.loads(config.get("wemo", "CoolingDeviceNames")))
WEMO_AUXILLIARY_HEATING_DEVICE_NAMES = set(
    json.loads(config.get("wemo", "AuxiliaryHeatingDeviceNames"))
)
WEMO_HUMIDIFIER_DEVICE_NAMES = set(json.loads(config.get("wemo", "HumidifierNames")))

BOND_IP = config.get("bond", "HubIp")
BOND_TOKEN = config.get("bond", "Token")
BOND_FAN_IDS = set(json.loads(config.get("bond", "FanIds")))


def authorize_credentials():
    """Start the OAuth flow to retrieve credentials.
    This may require launching a browser, one time."""
    # Fetch credentials from storage
    credentials = STORAGE.get()
    # If the credentials doesn't exist in the storage location then run the flow.
    if credentials is None or credentials.invalid:
        flow = flow_from_clientsecrets(GOOGLE_CLIENT_SECRET, scope=GOOGLE_SCOPE)
        http = httplib2.Http()
        credentials = run_flow(flow, STORAGE, http=http)
    return credentials


service = None


def nest_client():
    global service
    if service is None:
        credentials = authorize_credentials()
        http = credentials.authorize(httplib2.Http())
        service = build(
            serviceName="smartdevicemanagement.googleapis.com",
            version="v1",
            http=http,
            discoveryServiceUrl="https://{api}/$discovery/rest?version={apiVersion}",
        )
    return service


def get_nest_devices():
    devices = (
        nest_client()
        .enterprises()
        .devices()
        .list(parent="enterprises/" + GOOGLE_ENTERPRISE)
        .execute()
    )
    return devices["devices"]


def get_thermostats():
    devices = get_nest_devices()
    return [x for x in devices if x["type"] == "sdm.devices.types.THERMOSTAT"]


def get_nest_device(name):
    return nest_client().enterprises().devices().get(name=name).execute()


thermostat_name = None


def get_first_thermostat():
    global thermostat_name
    if thermostat_name is not None:
        try:
            return get_nest_device(thermostat_name)
        except Exception as e:
            # Thermostat has changed?
            print("Unable to read thermostat {}: {}".format(thermostat_name, e))
            thermostat_name = None
    # First time, or the old thermostat is offline
    if thermostat_name is None:
        thermostat_name = get_thermostats()[0]["name"]
        print("New thermostat discovered: {}".format(thermostat_name))
    return get_nest_device(thermostat_name)


# This tells how far back to remember a wemo device that isn't showing
# up in discovery any more.
WEMO_DISCOVERY_HISTORY_LEN = 10
wemo_discovery_history = deque(maxlen=WEMO_DISCOVERY_HISTORY_LEN)
# Once the discovery history is full, it refreshes much less frequently
# (controlled by the refresh probability) and relies on the cached results.
WEMO_REFRESH_PROB = 0.05


def get_wemo_devices():
    # Merges the last few discovery attempts, in case some wemos
    # intermittently fail to appear.
    if (
        len(wemo_discovery_history) < WEMO_DISCOVERY_HISTORY_LEN
        or random() < WEMO_REFRESH_PROB
    ):
        try:
            wemo_discovery_history.appendleft(pywemo.discover_devices())
        except:
            print("Wemo discovery exception:")
            traceback.print_exc()
    # Merge and filter out duplicates by MAC address
    devices = set()
    macs = set()
    for discovery in wemo_discovery_history:
        for device in discovery:
            if device.mac not in macs:
                macs.add(device.mac)
                devices.add(device)
    return devices


device_error_count = Counter()
MAX_RETRIES = config.getint("wemo", "MaxPowerOffRetries")


def reset_wemo_devices(device_set, skipping=None):
    """Turns as set of wemos off, and removes them from the set if successful.
    Devices specified by 'skipping' are not turned off, although they are
    still removed from the set."""

    if skipping:
        device_set.difference_update(skipping)
    toggled_successfully = set()
    for device in device_set:
        try:
            print("Turning {} off.".format(device.name))
            device.off()
            toggled_successfully.add(device)
        except:
            print("Unable to toggle {}".format(device.name))
            traceback.print_exc()
            device_error_count[device.mac] += 1
            if device_error_count[device.mac] > MAX_RETRIES:
                print(
                    "Giving up on {} after {} retries.".format(device.name, MAX_RETRIES)
                )
                toggled_successfully.add(device)
    for device in toggled_successfully:
        device_set.discard(device)
        del device_error_count[device.mac]


activated_heating_devices = set()
activated_cooling_devices = set()
activated_humidifier_devices = set()


def power_off_unneeded_wemos(hvac_status):
    # Turns off wemos that aren't needed in the current state.
    # Should not mess with devices that were manually toggled, since it acts only
    # on devices that this script turned on.
    if hvac_status == "COOLING":
        reset_wemo_devices(
            activated_heating_devices, skipping=activated_humidifier_devices
        )
    elif hvac_status == "HEATING":
        reset_wemo_devices(
            activated_cooling_devices, skipping=activated_humidifier_devices
        )
    else:
        reset_wemo_devices(
            activated_heating_devices, skipping=activated_humidifier_devices
        )
        reset_wemo_devices(
            activated_cooling_devices, skipping=activated_humidifier_devices
        )


def power_on_needed_wemo(device, hvac_status):
    # powers on a wemo and adds it to an active set so we can remember
    # to turn if off later when HVAC status changes.
    print("Turning {} on for {}.".format(device.name, hvac_status))
    try:
        device.on()
        if hvac_status == "COOLING":
            activated_cooling_devices.add(device)
            activated_heating_devices.discard(device)
        elif hvac_status == "HEATING":
            activated_heating_devices.add(device)
            activated_cooling_devices.discard(device)
        elif hvac_status == "HUMIDIFYING":
            activated_humidifier_devices.add(device)
        else:
            print("Unexpected hvac status to enable a wemo: {}".format(hvac_status))
    except:
        print("Wemo powering exception:")
        traceback.print_exc()


def aux_heat_is_needed(thermostat):
    # Actual room temperature.
    temperature_c = thermostat["traits"]["sdm.devices.traits.Temperature"][
        "ambientTemperatureCelsius"
    ]
    # The temperature that the heater is "set" to.
    heat_temperature_c = thermostat["traits"][
        "sdm.devices.traits.ThermostatTemperatureSetpoint"
    ]["heatCelsius"]
    hvac_status = thermostat["traits"]["sdm.devices.traits.ThermostatHvac"]["status"]
    return (
        hvac_status == "HEATING"
        and heat_temperature_c - temperature_c > AUX_HEAT_THRESHOLD_C
    )


def forget_user_controlled_wemos(all_wemos):
    # If code turned a switch on but the user manually turned it off,
    # then forget about turning it off by code later. The user has taken
    # responsibility.
    global activated_heating_devices
    global activated_cooling_devices
    global activated_humidifier_devices
    activated_wemos = (
        activated_heating_devices
        | activated_cooling_devices
        | activated_humidifier_devices
    )
    user_toggled = set()

    for device in activated_wemos:
        if device.is_off():
            user_toggled.add(device)

    # As a second pass, also check for user-toggled devices by mac address.
    # This might be important if a wemo device's name is changed.
    activated_mac_to_wemo = {x.mac: x for x in activated_wemos}
    for device in all_wemos:
        if device.mac in activated_mac_to_wemo and device.is_off():
            user_toggled.add(activated_mac_to_wemo[device.mac])

    activated_heating_devices -= user_toggled
    activated_cooling_devices -= user_toggled
    activated_humidifier_devices -= user_toggled


def print_temp(thermostat):
    # Actual room temperature.
    temperature_c = thermostat["traits"]["sdm.devices.traits.Temperature"][
        "ambientTemperatureCelsius"
    ]
    if FAHRENHEIT:
        temperature_f = (temperature_c * 9 / 5.0) + 32
        print(
            "{} temperature: {:.1f} degrees F".format(
                start.strftime("%Y-%m-%d %H:%M"), temperature_f
            )
        )
    else:
        print(
            "{} temperature: {:.1f} degrees C".format(
                start.strftime("%Y-%m-%d %H:%M"), temperature_c
            )
        )


# Normally we don't take control of already-running devices since we don't want to override user
# intent. But on first launch, we do. This prevents devices from getting orphaned on if the script
# is restarted.
first_iteration = True
prev_hvac_status = None
aux_heat_engaged = False
humidifiers_engaged = False
while True:
    # Detect when the HVAC status changes to heating, cooling, or neither.
    # Toggle Wemo switches accordingly.
    # Remember that some switches may be for both heating and cooling.
    start = datetime.datetime.now()
    try:
        wemos = get_wemo_devices()
        thermostat = get_first_thermostat()
        print_temp(thermostat)
        hvac_status = thermostat["traits"]["sdm.devices.traits.ThermostatHvac"][
            "status"
        ]

        forget_user_controlled_wemos(wemos)

        if hvac_status != prev_hvac_status:
            # hvac status has changed. flick some switches.
            aux_heat_engaged = False
            for wemo in wemos:
                if hvac_status == "COOLING" and wemo.name in WEMO_COOLING_DEVICE_NAMES:
                    power_on_needed_wemo(wemo, hvac_status)
                elif (
                    hvac_status == "HEATING" and wemo.name in WEMO_HEATING_DEVICE_NAMES
                ):
                    power_on_needed_wemo(wemo, hvac_status)
            for fan in BOND_FAN_IDS:
                address_template = "http://{}/v2/devices/{}/actions/{}"
                if hvac_status == "COOLING":
                    response = requests.put(
                        address_template.format(BOND_IP, fan, "SetSpeed"),
                        data='{"argument": 1}',
                        headers={"BOND-Token": BOND_TOKEN},
                    )
                    print(
                        "Turned on fan {}. Response: {}".format(fan, response.content)
                    )
                else:
                    response = requests.put(
                        address_template.format(BOND_IP, fan, "TurnOff"),
                        data="{}",
                        headers={"BOND-Token": BOND_TOKEN},
                    )
                    print(
                        "Turned off fan {}. Response: {}".format(fan, response.content)
                    )

        # Humidifiers can kick on or off independent of the hvac
        humidity = thermostat["traits"]["sdm.devices.traits.Humidity"][
            "ambientHumidityPercent"
        ]
        if (
            not humidifiers_engaged
            and humidity < HUMIDITY_PERCENT_TARGET - HUMIDITY_PERCENT_THRESHOLD
        ):
            humidifiers_engaged = True
            for wemo in wemos:
                if wemo.name in WEMO_HUMIDIFIER_DEVICE_NAMES and (
                    wemo.is_off() or first_iteration
                ):
                    # dummy hvac status, but our method understands it anyway.
                    power_on_needed_wemo(wemo, "HUMIDIFYING")
        elif humidity > HUMIDITY_PERCENT_TARGET + HUMIDITY_PERCENT_THRESHOLD:
            humidifiers_engaged = False
            reset_wemo_devices(
                activated_humidifier_devices,
                skipping=activated_cooling_devices | activated_heating_devices,
            )

        # Auxiliary heat can kick on in the middle of a cycle, but only once per cycle.
        if aux_heat_is_needed(thermostat) and not aux_heat_engaged:
            aux_heat_engaged = True
            # aux heat includes stuff like little space heaters. If you turned one on
            # manually, I want to leave it out of automatic control so you can have
            # your room as toasty as you like. Hence the "is_off()" check before
            # starting automatic control here.
            for wemo in wemos:
                if wemo.name in WEMO_AUXILLIARY_HEATING_DEVICE_NAMES and (
                    wemo.is_off() or first_iteration
                ):
                    power_on_needed_wemo(wemo, hvac_status)
        power_off_unneeded_wemos(hvac_status)
        prev_hvac_status = hvac_status
    except:
        print("Top-level exception:")
        traceback.print_exc()
        first_iteration = False
    first_iteration = False
    iteration_s = (datetime.datetime.now() - start).total_seconds()
    time.sleep(max(POLLING_PERIOD_S - iteration_s, 5))