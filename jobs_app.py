#!/usr/bin/env python

import os
import signal
import sys
import dill as pickle
import tempfile
import inspect
import traceback
from queue import Queue
import threading
import time
import multiprocessing
from multiprocessing.pool import ThreadPool
import base64

import string
import random

from flask import Flask, request

import json


import jobs



app = Flask("dispatcher")

d = jobs.Dispatcher()
d.start()


def random_string(length):
    letters = string.ascii_lowercase
    print (''.join(random.choice(letters) for i in range(length)))


@app.route("/enqueue", methods=["POST"])
def enqueue():
    data = request.get_json()
    func = pickle.loads(base64.b64decode(data["code"]))
    jobid = d.enqueue(func, deps=data["deps"])
    return json.dumps(dict(jobid=jobid))


@app.route("/map", methods=["POST"])
def map():
    data = request.get_json()
    funcs = [pickle.loads(base64.b64decode(c)) for c in data["code"]]
    jobids = [d.enqueue(func, deps=data["deps"]) for func in funcs]
    return json.dumps(dict(jobids=jobids))


@app.route("/wait", methods=["POST"])
def wait():
    data = request.get_json()
    jobid = data["jobid"]
    result = json.dumps(dict(jobid=jobid, result=d.wait(jobid)))
    print("API: wait:", data, result)
    return result


@app.route("/check", methods=["POST"])
def check():
    data = request.get_json()
    jobid = data["jobid"]
    return json.dumps(dict(jobid=jobid, result=d.check(jobid)))


@app.route("/enqueue", methods=["POST"])
def requeue():
    data = request.get_json()
    func = pickle.loads(base64.b64decode(data["code"]))
    jobid = d.requeue(data["jobid"], func, deps=data["deps"])
    return json.dumps(dict(jobid=jobid))


@app.route('/shutdown', methods=["GET"])
def shutdown():
    d.stop()
    os.kill(os.getpid(), signal.SIGINT)


@app.route("/dump", methods=["GET"])
def dump():
    file = StringIO()
    d.dump(file)
    data = file.read()
    return data


if __name__ == "__main__":
    app.run()
