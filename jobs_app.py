#!/usr/bin/env python

import os
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


@app.route("/wait", methods=["POST"])
def wait():
    data = request.get_json()
    jobid = data["jobid"]
    return json.dumps(dict(jobid=jobid, result=d.wait(jobid)))


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


@app.route("/stop")
def stop():
    d.stop()
    sys.exit(0)


"""
@app.route('/login', methods=['POST', 'GET'])
def login():
    error = None
    if request.method == 'POST':
        if valid_login(request.form['username'],
                       request.form['password']):
            return log_the_user_in(request.form['username'])
        else:
            error = 'Invalid username/password'
    # the code below is executed if the request method
    # was GET or the credentials were invalid
    return render_template('login.html', error=error)
"""



app.run(debug=True)
