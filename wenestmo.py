import configparser
from collections import Counter, deque
import httplib2
import json
from random import random
import time
import traceback 

import pywemo

from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import run_flow
from googleapiclient.discovery import build

STORAGE = Storage('credentials.storage')

config = configparser.ConfigParser()
config.read('config.ini')

# Console output will show the temperature in F or C.
FAHRENHEIT = config.getboolean('DEFAULT', 'Fahrenheit')
POLLING_PERIOD_S = config.getint('DEFAULT', 'PollingPeriodS')

GOOGLE_ENTERPRISE = config['google']['Enterprise']
GOOGLE_CLIENT_SECRET = config['google']['ClientSecretFile']
GOOGLE_SCOPE = 'https://www.googleapis.com/auth/sdm.service'

WEMO_HEATING_DEVICE_NAMES = set(json.loads(config.get('wemo', 'HeatingDeviceNames')))
WEMO_COOLING_DEVICE_NAMES = set(json.loads(config.get('wemo', 'CoolingDeviceNames')))

# Start the OAuth flow to retrieve credentials.
# This may require launching a browser, one time.
def authorize_credentials():
  # Fetch credentials from storage
  credentials = STORAGE.get()
  # If the credentials doesn't exist in the storage location then run the flow.
  if credentials is None or credentials.invalid:
    flow = flow_from_clientsecrets(GOOGLE_CLIENT_SECRET, scope=GOOGLE_SCOPE)
    http = httplib2.Http()
    credentials = run_flow(flow, STORAGE, http=http)
  return credentials
credentials = authorize_credentials()

service = None
def nest_client():
  global service
  if service is None:
    credentials = authorize_credentials()
    http = credentials.authorize(httplib2.Http())
    service = build(serviceName='smartdevicemanagement.googleapis.com', version='v1', http=http, discoveryServiceUrl='https://{api}/$discovery/rest?version={apiVersion}')
  return service
 
def get_nest_devices():
  devices = nest_client().enterprises().devices().list(parent='enterprises/' + GOOGLE_ENTERPRISE).execute()
  return devices['devices']
    
def get_thermostats():
  devices = get_nest_devices()
  return [x for x in devices if x['type'] == 'sdm.devices.types.THERMOSTAT']
  
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
    thermostat_name = get_thermostats()[0]['name']
    print('New thermostat discovered: {}'.format(thermostat_name))
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
  if len(wemo_discovery_history) < WEMO_DISCOVERY_HISTORY_LEN or random() < WEMO_REFRESH_PROB:
    try:
      wemo_discovery_history.appendleft(pywemo.discover_devices())
    except:
      print('Wemo discovery exception:')
      traceback.print_exc()
  # Merge and filter out duplicates by MAC address
  wemos = set()
  macs = set()
  for discovery in wemo_discovery_history:
    for wemo in discovery:
      if wemo.mac not in macs:
        macs.add(wemo.mac)
        wemos.add(wemo)
  return wemos  

device_error_count = Counter()
MAX_RETRIES = config.getint('wemo', 'MaxPowerOffRetries')
def reset_wemo_devices(device_set):
  # turns as set of wemos off, and removes them from the
  # set if successful.
  toggled_successfully = set()
  for wemo in device_set:
    try:
      print('Turning {} off.'.format(wemo.name))
      wemo.off()
      toggled_successfully.add(wemo)
    except:
      print('Unable to toggle {}'.format(wemo.name))
      traceback.print_exc()
      device_error_count[wemo.mac] += 1
      if device_error_count[wemo.mac] > MAX_RETRIES:
        print('Giving up on {} after {} retries.'.format(wemo.name, MAX_RETRIES))
        toggled_successfully.add(wemo)
  for wemo in toggled_successfully:
    device_set.discard(wemo)
    del device_error_count[wemo.mac]

activated_heating_devices = set()
activated_cooling_devices = set()

def power_off_unneeded_wemos(hvac_status):
  # Turns off wemos that aren't needed in the current state.
  if hvac_status == 'COOLING':
    reset_wemo_devices(activated_heating_devices)
  elif hvac_status == 'HEATING':
    reset_wemo_devices(activated_cooling_devices)
  else:
    reset_wemo_devices(activated_heating_devices)
    reset_wemo_devices(activated_cooling_devices)
    
def power_on_needed_wemo(wemo, hvac_status):
  # powers on a wemo and adds it to an active set so we can remember
  # to turn if off later when HVAC status changes.
  print('Turning {} on for {}.'.format(wemo.name, hvac_status))
  try:
    wemo.on()
    if hvac_status == 'COOLING':
      activated_cooling_devices.add(wemo)
      activated_heating_devices.discard(wemo)
    elif hvac_status == 'HEATING':
      activated_heating_devices.add(wemo)
      activated_cooling_devices.discard(wemo)
    else:
      print('Unexpected hvac status to enable a wemo: {}'.format(hvac_status))
  except:
    print('Wemo powering exception:')
    traceback.print_exc()

prev_hvac_status = None
while(True):
  # Detect when the HVAC status changes to heating, cooling, or neither.
  # Toggle Wemo switches accordingly.
  # Remember that some switches may be for both heating and cooling.
  try:
    wemos = get_wemo_devices()
    thermostat = get_first_thermostat()
    temperature_c = thermostat['traits']['sdm.devices.traits.Temperature']['ambientTemperatureCelsius']
    if FAHRENHEIT:
      temperature_f = (temperature_c * 9/5) + 32
      print('Temperature: {:.1f} degrees F'.format(temperature_f))
    else:
      print('Temperature: {:.1f} degrees C'.format(temperature_c))
    hvac_status = thermostat['traits']['sdm.devices.traits.ThermostatHvac']['status']
    if hvac_status == prev_hvac_status:
      # no change in hvac status, just do monitoring.
      # If code turned a switch on but the user manually turned it off,
      # then forget about turning it off by code later. The user has taken
      # responsibility.
      user_toggled = set()
      for wemo in wemos:
        if wemo.is_off() and wemo in activated_heating_devices:
          user_toggled.add(wemo)
        if wemo.is_off() and wemo in activated_cooling_devices:
          user_toggled.add(wemo) 
        activated_heating_devices -= user_toggled
        activated_cooling_devices -= user_toggled
    else:
      # hvac status has changed. flick some switches.
      for wemo in wemos:
        if hvac_status == 'COOLING' and wemo.name in WEMO_COOLING_DEVICE_NAMES and wemo.is_off():
          power_on_needed_wemo(wemo, hvac_status)
        elif hvac_status == 'HEATING' and wemo.name in WEMO_HEATING_DEVICE_NAMES and wemo.is_off():
          power_on_needed_wemo(wemo, hvac_status)
    power_off_unneeded_wemos(hvac_status)
    prev_hvac_status = hvac_status
  except:
    print('Top-level exception:')
    traceback.print_exc()
  time.sleep(POLLING_PERIOD_S)