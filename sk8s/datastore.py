
## Ultra-lightweight key/value store.

import sk8s
import subprocess
from flask import Flask
from flask import request
import requests
import json
import jinja2
import re


class Datastore:
    def __init__(self):
        self.app = Flask(__name__)
        self.data = dict()
        self.app.add_url_rule('/put/<key>', 'put', self.put, methods=["POST"])
        self.app.add_url_rule('/get/<key>', 'get', self.get, methods=["GET"])
    
    def put(self, key):
        data = request.data
        self.data[key] = json.loads(data.decode("utf-8"))
        print(self.data)
        return "OK"

    def get(self, key):
        return json.dumps(self.data[key])

    def run(self):
        return self.app.run(host="0.0.0.0")


def put(url, key, val):
    r = requests.post(f"{url}/put/{key}", json=val)
    return r.content.decode("utf-8")


def get(url, key):
    r = requests.get(f"{url}/get/{key}")
    return json.loads(r.content.decode("utf-8"))

