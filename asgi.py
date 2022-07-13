#!/usr/bin/env python3
# asgi.py

# internal imports
import logging
import sys

# external imports
import requests
import RPi.GPIO as GPIO
import yaml
from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse


class Relay:
    """Common relay class"""

    def __init__(self, relay_type: str, attrs: dict, state: bool = False):
        self.type = relay_type
        self.attrs = attrs
        # init relay
        if self.type == 'GPIO':
            GPIO.setup(self.attrs['pin'], GPIO.OUT)
        # set inital state
        self.set_state(state)

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
                requests.put(self.attrs['url'], data={'state': int(state)})
            except requests.exceptions.ConnectionError:
                raise self.UnavailableError()

    def get_state(self):
        if self.type == 'GPIO':
            # 0 == on, 1 == off
            res = not bool(GPIO.input(self.attrs['pin']))
        elif self.type == 'WiFi':
            try:
                res = requests.get(self.attrs['url'], timeout=3)
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


@app.exception_handler(Relay.UnavailableError)
async def relay_unavailable_exception_handler(request, exc):
    """Relay unavailable error handler"""
    return JSONResponse(content={"detail": "Relay is not available"},
                        status_code=503)


class RelayField(str):
    """Relay name field type"""

    @classmethod
    def __get_validators__(cls):
        yield cls.validate_relay_name

    @classmethod
    def validate_relay_name(cls, v):
        if not v in relays:
            raise HTTPException(status_code=404, detail=f'Relay {v} not found')
        return cls(v)


@app.get('/api/relay')
def get_relay_list():
    res = {}
    for n, r in relays.items():
        try:
            res[n] = r.info()
        except Relay.UnavailableError:
            logging.warning(f'Relay {n} is not available')
            res[n] = 'Unavailable'
    return res


@app.get('/api/relay/{name}')
def get_relay_info(name: RelayField):
    res = relays[name].info()
    return res


@app.get('/api/relay/{name}/{action}')
def send_relay_action(name: RelayField,
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
