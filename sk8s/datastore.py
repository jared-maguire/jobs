
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
        self.app.add_url_rule('/getall', 'getall', self.getall, methods=["GET"])
        self.app.add_url_rule('/check', 'check', self.check, methods=["GET"])
    
    def put(self, key):
        data = request.data
        self.data[key] = json.loads(data.decode("utf-8"))
        print(self.data)
        return "OK"

    def get(self, key):
        return json.dumps(self.data[key])

    def getall(self):
        return json.dumps(self.data)
    
    def check(self):
        return json.dumps("ok")

    def run(self):
        return self.app.run(host="0.0.0.0")


def put(url, key, val):
    r = requests.post(f"{url}/put/{key}", json=val)
    return r.content.decode("utf-8")


def get(url, key=None):
    if key is not None:
        r = requests.get(f"{url}/get/{key}")
        return json.loads(r.content.decode("utf-8"))
    else:
        r = requests.get(f"{url}/getall")
        return json.loads(r.content.decode("utf-8"))


def check(url):
    try:
        r = requests.get(f"{url}/check")
    except:
        return False
    r = json.loads(r.content.decode("utf-8"))
    return r == "ok"


def launch(name="ds-{s}"):
    def datastore_service():
        import sk8s.datastore as ds
        datastore = ds.Datastore()
        datastore.run()

    s = sk8s.util.random_string(5)
    name = name.format(s=s)

    results_url = sk8s.services.service(datastore_service, name=name)
    return results_url