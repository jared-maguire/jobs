#!/usr/bin/env python

import os
import dill as pickle
import tempfile
import inspect
import traceback
from queue import Queue
import threading
import time
import multiprocessing
from multiprocessing.pool import ThreadPool

from flask import Flask, request


import jobs




app = Flask(__name__)


d = Dispatcher()
d.start()


@app.route("/enqueue", methods=["POST"])
def enqueue(data):
    return "enqueue."


@app.route("/requeue")
def requeue():
    pass


@app.route("/wait")
def wait():
    pass


@app.route("/check")
def check():
    pass
