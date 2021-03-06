WeNestMo
================================================================
Trigger Wemo switches based on Nest thermostat status. (Now with some Bond hub support too).

Easily sync a fan with your AC, a space heater with your furnace, or trigger a portable humidifier when the air gets dry.

Google recently deprecated the Works With Nest program, and IFTTT is adding a paywal anyway. So if you want to sync Wemo switches to Nest status, this is probably the cheapest solution at the moment (2020).

Dependencies
------------
Install depedencies with: "pip install -r requirements.txt"

WeNestMo is built on top of pyWeMo.

Google charges a one-time $5 payment to enable API access. See the directions below for more detail.

How to use
----------
Get your Google credentials, update the config file, and leave wenestmo.py running on a device in your house.


#.  If you have an old-school Nest account, you must `migrate to the newer Google system <https://support.google.com/googlenest/answer/9297676?p=migration-account-faq>`_. Note this breaks IFTTT compatibility and is not reversable.
#.  Create a Google Device Access developer account. They charge $5 (one time cost) and you should be comfortable using the linux Curl tool to complete the process. Follow the steps carefully at https://developers.google.com/nest/device-access/get-started
    Make sure to save the oauth credential file to the wenestmo folder and record the "Project ID" in the last step of Getting Started. It looks like a UUID.
#.  On the `GCP credentials page <https://console.developers.google.com/apis/credentials>`_, edit your OAuth client "URIs" to include http://localhost:8080/
#.  Open up config.ini and fill in your details. Mainly, the device names you want controlled, and your Google credentials. There is a section for Bond account details too for optionally controlling a Bond hub.
    ..
        | [wemo]
        | # Devices to turn on when the heater is running (register boosters, heaters, etc)
        | HeatingDeviceNames = ["Vent booster", "Space heater"]
        | # Auxiliary heating devices to turn on when the heat is very low
        | AuxiliaryHeatingDeviceNames = ["My space heater"]
        | # Devices to turn on when the cooler is running (fans etc)
        | CoolingDeviceNames = ["Vent booster", "Ceiling fan"]
        | # Small humidifiers to trigger when the air is dry
        | HumidifierNames = []
        |
        | [google]
        | # Secret file that must be saved during the "Set up Google Cloud Platform" setup step
        | ClientSecretFile = goog_credentials.json
        | # Project ID aka Enterprise which is generated as the last step of "Create a Device Access project" setup step
        | Enterprise = 9aba7f9c-13a8-4b3d-bf04-2d5adad3da55
#.  Run "python wenestmo.py" and follow the directions to do one-time authentication in a browser window.
#.  Now just leave "python wenestmo.py" running on some device on your network. (PC, raspberrypi, toaster, whatever). It needs to have internet access (for Nest integration) and be on the same subnet as your Wemos. For example, running in a docker image did not work for me; it could not find the local Wemo devices. First time authentication is tricky on a headless machine. I found the easiest way was to run wenestmo once on a normal machine and then copy "credentials.storage" to the working directory on the headless device.

License
-------
The code in pywemo/ouimeaux_device is written and copyright by Ian McCracken and released under the BSD license. The rest is released under the MIT license.
