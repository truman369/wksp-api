#!/usr/bin/env python3
# asgi.py

# internal imports
import logging
import sys

# external imports
import requests
import RPi.GPIO as GPIO
import yaml
from adafruit_bme280.basic import Adafruit_BME280_I2C as BME
from adafruit_htu21d import HTU21D as HTU
from adafruit_sht31d import SHT31D as SHT
from board import I2C
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from mpd import MPDClient
from mpd.base import ConnectionError as MPDConnectionError


class Relay:
    """Common relay class"""

    def __init__(self, relay_type: str, attrs: dict):
        self.type = relay_type
        self.attrs = attrs
        # init relay
        if self.type == 'GPIO':
            GPIO.setup(self.attrs['pin'], GPIO.OUT)

    class UnavailableError(Exception):
        """Custom exception"""
        pass

    def set_state(self, state: bool):
        if self.get_state() == state:
            # skip if nothing to change
            return
        if self.type == 'GPIO':
            # 0 == on, 1 == off
            GPIO.output(self.attrs['pin'], int(not state))
        elif self.type == 'WiFi':
            try:
                requests.put(self.attrs['url'], timeout=1,
                             data={'state': int(state)})
            except requests.exceptions.ConnectionError:
                raise self.UnavailableError()

    def get_state(self):
        if self.type == 'GPIO':
            # 0 == on, 1 == off
            res = not bool(GPIO.input(self.attrs['pin']))
        elif self.type == 'WiFi':
            try:
                res = requests.get(self.attrs['url'], timeout=1)
            except requests.exceptions.ConnectionError:
                raise self.UnavailableError()
            else:
                res = res.json()
                res = bool(res['state'])
        return res

    def toggle(self):
        self.set_state(not self.get_state())

    def info(self):
        res = {
            'type': self.type,
            **self.attrs,
            'state': self.get_state()
        }
        return res


class Sensor:
    """Common sensor class"""

    def __init__(self, sensor_type: str,
                 attrs: dict = {}, capabilities: dict = {}):
        sensor_type, driver = sensor_type.split('_')
        if sensor_type == 'I2C' and driver in ['BME', 'HTU', 'SHT']:
            driver = eval(driver)
            self._sensor = driver(I2C(), **attrs)
        else:
            raise Exception(f'Wrong sensor type: {sensor_type}')
        self.capabilities = capabilities

    class UnavailableError(Exception):
        """Custom exception"""
        pass

    def get_values(self):
        """Return dict of values"""
        res = {}
        for cap_key, cap_val in self.capabilities.items():
            res_key = cap_key
            # get value from sensor
            try:
                res_val = eval(f'self._sensor.{cap_key}')
            except Exception as e:
                logging.error(f'Sensor error: {e}')
                raise self.UnavailableError()
            # modify key and value if needed
            if cap_val is not None:
                if 'alias' in cap_val:
                    res_key = cap_val['alias']
                if 'calibration' in cap_val:
                    res_val = eval(cap_val['calibration'].format(res_val))
            # format value
            res_val = '{:0.2f}'.format(res_val)
            res[res_key] = res_val
        return res


# init fastapi
app = FastAPI()

# init gpio
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# read config from file
with open('config.yml', 'r') as file:
    cfg = yaml.safe_load(file)

# init relays
relays = {}
for r in cfg['relays']:
    try:
        relays[r['name']] = Relay(r['type'], r['attrs'])
    except Exception as e:
        logging.error(f"Failed to init relay: {r['name']}")

# init mpd client
mpc = MPDClient()

# init sensors
sensors = {}
for s in cfg['sensors']:
    kwargs = {}
    # add optional kwargs
    [kwargs.update({a: s[a]}) for a in ['attrs', 'capabilities'] if a in s]
    try:
        sensors[s['name']] = Sensor(s['type'], **kwargs)
    except Exception as e:
        logging.error(f"Failed to init {s['name']} sensor: {e}")


@app.exception_handler(Relay.UnavailableError)
async def relay_unavailable_exception_handler(request, exc):
    """Relay unavailable error handler"""
    return JSONResponse(content={"detail": "Relay is not available"},
                        status_code=503)


@app.exception_handler(Sensor.UnavailableError)
async def sensor_unavailable_exception_handler(request, exc):
    """Relay unavailable error handler"""
    return JSONResponse(content={"detail": "Sensor is not available"},
                        status_code=503)


class RelayField(str):
    """Relay name field type"""

    @classmethod
    def __get_validators__(cls):
        yield cls.validate_relay_name

    @classmethod
    def validate_relay_name(cls, v):
        if not v in relays:
            raise HTTPException(status_code=404,
                                detail=f'Relay [{v}] not found')
        return cls(v)


@app.get('/api/relay')
def relay_get_list():
    res = {}
    for n, r in relays.items():
        try:
            res[n] = r.info()
        except Relay.UnavailableError:
            logging.warning(f'Relay [{n}] is not available')
            res[n] = 'N/A'
    return res


@app.get('/api/relay/{name}')
def relay_get_info(name: RelayField):
    res = relays[name].info()
    return res


@app.get('/api/relay/{name}/{action}')
def relay_send_action(name: RelayField,
                      action: str = Path(regex=r'^(on|off|toggle|state)$')):

    if action == 'toggle':
        relays[name].toggle()
    elif action in ['on', 'off']:
        state = True if action == 'on' else False
        relays[name].set_state(state)

    res = relays[name].get_state()
    if action != 'state':
        res = {'state': res}

    return res


@app.get('/api/mpc/{action}')
def mpc_send_command(action: str, args: str = ''):

    # check connection
    try:
        mpc.ping()
    except MPDConnectionError:
        mpc.connect(cfg['mpd']['host'], cfg['mpd']['port'])

    # validate action
    if action not in mpc.commands():
        raise HTTPException(status_code=422, detail=f'Wrong mpc command')

    # send command to mpd
    try:
        res = eval(f'mpc.{action}({args})')
    except Exception as e:
        msg = f'Failed to exec [{action}]: {e}'
        logging.error(msg)
        raise HTTPException(status_code=500, detail=msg)

    return res


@app.get('/api/sensor')
def sensor_get_list():
    res = {}
    for n, s in sensors.items():
        try:
            res[n] = s.get_values()
        except Sensor.UnavailableError:
            logging.warning(f'Sensor [{n}] is not available')
            res[n] = 'N/A'
    return res


@app.get('/api/sensor/{name}')
def sensor_get_values(name: str):
    if name not in sensors:
        raise HTTPException(status_code=404,
                            detail=f'Sensor [{name}] not found')
    res = sensors[name].get_values()

    return res
