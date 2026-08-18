UniFi Protect Air Quality Test Add-on This is a local Home Assistant add-on for testing the Ubiquiti UP-AirQuality / Vape Detection & Air Quality Sensor before native UniFi Protect support exposes all fields.

It logs into UniFi Protect with a local user, reads /proxy/protect/api/bootstrap, finds sensors with type: UP-AirQuality, publishes every key found under airQuality through MQTT Discovery, and then follows live changes from /proxy/protect/ws/updates.

Known mapped readings:

CO2 AQI / Air Quality Index Vape Index VOC Index TVOC Index PM1.0 PM2.5 PM4.0 PM10 Temperature Humidity NOx, if a future firmware exposes it Unknown future airQuality keys are still published as generic MQTT sensor entities, so the add-on can reveal new fields in the logs and in Home Assistant.

Install from the Home Assistant UI This add-on is now shaped as a Home Assistant add-on repository. If you cannot SSH into Home Assistant, put this project in a GitHub repository, then install it through the UI.

Repository layout should be:

repository.yaml up-airquality-addon/ config.yaml Dockerfile requirements.txt run.py README.md Then in Home Assistant:

Go to Settings > Add-ons > Add-on Store. Open the three-dot menu. Choose Repositories. Paste your GitHub repository URL. Click Add. Find UniFi Protect Air Quality Test under the new repository. Install it. After install, use this add-on configuration:

protect_host: 192.168.1.1 protect_user: your_local_unifi_user protect_pass: your_password mqtt_host: core-mosquitto mqtt_port: 1883 mqtt_user: your_mqtt_user mqtt_pass: your_mqtt_password discovery_prefix: homeassistant publish_mqtt: true log_raw_airquality: true verify_tls: false protect_host should be the UniFi console IP or hostname without https://.

If you use the official Mosquitto broker add-on, core-mosquitto is usually the right mqtt_host.

Install locally Copy the up-airquality-addon folder into your Home Assistant addons directory. In Home Assistant, go to Settings > Add-ons > Add-on Store. Open the three-dot menu and choose Check for updates. Install UniFi Protect Air Quality Test. Fill in the add-on options. Use a dedicated local UniFi Protect user. UI Cloud-only credentials will not work for local Protect login.

Options protect_host: UniFi console hostname or IP, without https://. protect_user: Local UniFi OS / Protect username. protect_pass: Local password. mqtt_host: MQTT broker hostname or IP. Leave blank to only log readings. mqtt_port: MQTT broker port, usually 1883. mqtt_user / mqtt_pass: MQTT credentials if your broker requires them. discovery_prefix: Home Assistant MQTT discovery prefix, usually homeassistant. publish_mqtt: Disable this to run as a log-only probe. log_raw_airquality: Keep enabled while validating field names. verify_tls: Usually false for UniFi consoles using self-signed certificates. Sources The public HA discussion reports the missing UP-AirQuality entities and notes that values are available on the Protect WebSocket. The community UPAQ-MQTT bridge validates the bootstrap/WebSocket approach and the expected metric set. Ubiquiti's product specs list CO2, PM1, PM2.5, PM4, PM10, VOC, AQI, and Vape, plus temperature and humidity.
